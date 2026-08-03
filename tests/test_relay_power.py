"""
Tests for relay power control (src/services/relay_service.py and the settings,
queue and URL-validation changes that go with it).

Everything here runs offline. No socket is opened, no printer is contacted and
no HTTP request leaves the process: the reachability probe, the pending-work
probe and the webhook sender are all injected, and the one test that exercises
the real ``urllib`` sender patches ``urlopen`` itself. Every service instance is
given a temporary state file via ``tmp_path``, so the real ``/app/data`` is never
touched.
"""

import json
import os
import threading
import time
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.api import printer_controller
from src.config.default_settings import (
    AUTO_POWER_OFF_MISMATCH_WARNING,
    DEFAULT_SETTINGS,
    PRINTER_AUTO_POWER_OFF_CHOICES,
)
from src.services.queue_service import PrintQueueService
from src.services.relay_service import (
    ACTION_TURN_OFF,
    ACTION_TURN_ON,
    AUTHORIZATION_ENV_VAR,
    ORIGIN_SOURCE_IDLE,
    ORIGIN_SOURCE_PRINT,
    ORIGIN_SOURCE_STARTUP,
    STEP_KEEP_ALIVE_END,
    STEP_PRINTER_POWER_OFF,
    STEP_TURN_OFF,
    RelayPowerService,
)
from src.services.settings_service import SettingsService
from src.utils.exceptions import PrinterError, RelayWebhookError, ValidationError
from src.utils.uri_validation import validate_printer_uri, validate_webhook_url

HOUR = 3600


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    """A settings dict with the relay feature configured but overridable."""
    base = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "keep_alive_enabled": True,
        "keep_alive_interval": 60,
        "keep_alive_mode": "timed",
        "keep_alive_duration_seconds": 4 * HOUR,
        "relay_webhook_enabled": True,
        "relay_webhook_turn_on_url": "http://192.168.1.42/relay/0?turn=on",
        "relay_webhook_turn_off_url": "",
        "relay_webhook_turn_off_enabled": True,
        "relay_webhook_turn_off_delay_minutes": 5,
        "printer_auto_power_off_minutes": 10,
    }
    base.update(overrides)
    return base


class _Settings:
    """Minimal stand-in for the settings service."""

    def __init__(self, settings, settings_file=""):
        self.settings = dict(settings)
        self.settings_file = settings_file

    def get_settings(self):
        return dict(self.settings)


class _Sender:
    """Records webhook sends; optionally fails.

    ``status`` is what a real sender returns: the HTTP status the relay answered
    with. It stays None by default so the sequencing tests are unaffected by it.
    """

    def __init__(self, error=None, status=None):
        self.calls = []
        self.error = error
        self.status = status

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.status

    @property
    def actions(self):
        return [call["payload"]["action"] for call in self.calls]


def _forbidden_sender(*_args, **_kwargs):
    """A sender that fails the test if it is ever called."""
    raise AssertionError("a webhook was sent when none should have been")


def _forbidden_probe(*_args, **_kwargs):
    """A reachability probe that fails the test if it is ever called."""
    raise AssertionError("the printer was probed when it should not have been")


def _make_service(tmp_path, settings=None, reachable=False, pending=False,
                  sender=None, name="relay_power.json", origin=None):
    """Build a fully injected RelayPowerService with a temporary state file.

    ``origin`` is the ``(timestamp, printed)`` pair the timing chain is measured
    from, or a callable returning one. Left out, the real printer service
    supplies it, which is what the shipped app does.
    """
    extra = {}
    if origin is not None:
        extra["origin_provider"] = origin if callable(origin) else (lambda: origin)
    service = RelayPowerService(
        state_file=str(tmp_path / name),
        settings_provider=_Settings(settings if settings is not None else _settings()),
        reachability_probe=(reachable if callable(reachable) else (lambda _s: reachable)),
        pending_work_probe=(pending if callable(pending) else (lambda: pending)),
        sender=sender if sender is not None else _Sender(),
        **extra,
    )
    # Keep the waits short so the retry sequence runs in milliseconds. The values
    # under test are the *sequence and its outcome*, not the constants, which are
    # asserted separately in test_turn_on_wait_constants_are_a_bounded_sequence.
    service.turn_on_waits = (0.05, 0.02)
    service.poll_interval = 0.01
    return service


# --------------------------------------------------------------------------- #
# The timing arithmetic
# --------------------------------------------------------------------------- #

def test_hardware_interval_is_subtracted_from_the_keep_alive_window():
    """4 h configured, 10 min on the device -> keep-alive stops at 3:50."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10)
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 4 * HOUR - 600
    # And the printer therefore switches itself off at exactly the 4 h asked for.
    assert (RelayPowerService.effective_keep_alive_seconds(settings)
            + RelayPowerService.hardware_power_off_seconds(settings)) == 4 * HOUR


def test_turn_off_is_the_configured_window_plus_the_margin():
    """turn_off goes out at 4:05, measured from the same origin."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         relay_webhook_turn_off_delay_minutes=5)
    assert RelayPowerService.turn_off_offset_seconds(settings) == 4 * HOUR + 300


def test_the_whole_chain_lines_up():
    """The four moments of the documented chain, from one origin."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10,
                         relay_webhook_turn_off_delay_minutes=5)
    keep_alive_stops = RelayPowerService.effective_keep_alive_seconds(settings)
    printer_sleeps = keep_alive_stops + RelayPowerService.hardware_power_off_seconds(settings)
    relay_opens = RelayPowerService.turn_off_offset_seconds(settings)

    assert keep_alive_stops == 3 * HOUR + 50 * 60      # 3:50
    assert printer_sleeps == 4 * HOUR                  # 4:00
    assert relay_opens == 4 * HOUR + 5 * 60            # 4:05
    # The relay must always open after the printer has slept, never before.
    assert relay_opens > printer_sleeps


def test_edge_case_duration_equal_to_hardware_is_valid_and_silences_keep_alive():
    """duration == hardware is intended: the hardware carries the whole window."""
    settings = _settings(keep_alive_duration_seconds=600,
                         printer_auto_power_off_minutes=10)
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 0
    # Still a real window for the turn-off clock.
    assert RelayPowerService.turn_off_offset_seconds(settings) == 600 + 300


def test_edge_case_duration_equal_to_hardware_passes_validation(tmp_path):
    service, _ = _settings_service(tmp_path)
    assert service.save_settings(_persistable(
        keep_alive_duration_seconds=600, printer_auto_power_off_minutes=10)) is True


def test_edge_case_duration_shorter_than_hardware_is_reported_not_clamped(tmp_path):
    """A window shorter than the hardware interval cannot be expressed."""
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(_persistable(
            keep_alive_duration_seconds=300, printer_auto_power_off_minutes=10))
    message = str(exc.value)
    assert "keep_alive_duration_seconds" in message
    assert "auto-power-off" in message
    # Named numbers, so the user can act on it rather than guess.
    assert "300" in message and "600" in message
    # And it is refused, not quietly turned into something else.
    assert service.save_settings(_persistable(
        keep_alive_duration_seconds=300, printer_auto_power_off_minutes=10)) is False


def test_no_timed_window_means_no_shortening_and_no_schedule():
    """"forever" mode and a zero duration both mean "there is no window"."""
    for settings in (
        _settings(keep_alive_mode="forever", relay_webhook_turn_off_enabled=False),
        _settings(keep_alive_duration_seconds=0, relay_webhook_turn_off_enabled=False),
    ):
        assert RelayPowerService.effective_keep_alive_seconds(settings) is None
        assert RelayPowerService.turn_off_offset_seconds(settings) is None


def test_keep_alive_disabled_schedules_nothing_but_leaves_the_worker_rule_alone():
    """The two windows differ by one condition, on purpose.

    A turn-off moment is refused when keep-alive is not running. There is no
    window being held open to measure from. The keep-alive worker's own rule is
    left exactly as it always was, so a flag out of step with the running thread
    can never flip it from "pause outside the window" to "ping forever".
    """
    settings = _settings(keep_alive_enabled=False,
                         relay_webhook_turn_off_enabled=False)
    assert RelayPowerService.configured_window_seconds(settings) is None
    assert RelayPowerService.turn_off_offset_seconds(settings) is None
    assert RelayPowerService.timed_window_seconds(settings) == 4 * HOUR
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 4 * HOUR - 600


def test_subtraction_only_applies_while_the_feature_is_on():
    """With relay control off, the keep-alive window is untouched."""
    settings = _settings(relay_webhook_enabled=False,
                         relay_webhook_turn_off_enabled=False,
                         keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=60)
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 4 * HOUR


def test_turn_on_wait_constants_are_a_bounded_sequence():
    """The shipped waits: decreasing, bounded, and polled finely."""
    service = RelayPowerService(state_file="/dev/null",
                                settings_provider=_Settings(_settings()))
    first, second = service.turn_on_waits
    assert first > second, "the second wait is the shorter one"
    assert first + second <= 120, "the total wait stays a bounded, explainable number"
    assert service.poll_interval < second, "a quick printer must not wait out the window"


# --------------------------------------------------------------------------- #
# Settings validation
# --------------------------------------------------------------------------- #

def _persistable(**overrides):
    """A complete, savable settings dict (the validator needs `printers`)."""
    base = _settings()
    base["printers"] = [{
        "id": "default", "name": "Default Printer",
        "printer_uri": "tcp://192.168.1.100", "printer_model": "QL-800",
        "label_size": "62",
    }]
    base.update(overrides)
    return base


def _settings_service(tmp_path, settings=None):
    path = tmp_path / "settings.json"
    if settings is not None:
        path.write_text(json.dumps(settings), encoding="utf-8")
    return SettingsService(settings_file=str(path)), path


@pytest.mark.parametrize("overrides", [
    {"keep_alive_mode": "forever"},
    {"keep_alive_enabled": False},
    {"keep_alive_duration_seconds": 0},
])
def test_turn_off_is_refused_without_a_timed_keep_alive_window(tmp_path, overrides):
    """No expiry means no origin to measure the turn-off moment from."""
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(_persistable(**overrides))
    message = str(exc.value)
    assert "relay_webhook_turn_off_enabled" in message
    assert "timed" in message
    assert "origin" in message, "the message must say why, not just that"


def test_turn_off_is_allowed_with_a_timed_window(tmp_path):
    service, _ = _settings_service(tmp_path)
    assert service.save_settings(_persistable()) is True


def test_relay_without_turn_off_does_not_need_a_timed_window(tmp_path):
    """Only the turn_off half needs an expiry; turn_on alone is fine."""
    service, _ = _settings_service(tmp_path)
    assert service.save_settings(_persistable(
        relay_webhook_turn_off_enabled=False, keep_alive_mode="forever")) is True


def test_enabling_the_relay_requires_a_turn_on_url(tmp_path):
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(_persistable(relay_webhook_turn_on_url=""))
    assert "relay_webhook_turn_on_url is required" in str(exc.value)


@pytest.mark.parametrize("minutes", [0, 5, 15, 45, 70, 90, "10", True])
def test_auto_power_off_minutes_is_restricted_to_what_the_device_offers(tmp_path, minutes):
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(_persistable(printer_auto_power_off_minutes=minutes))
    assert "printer_auto_power_off_minutes" in str(exc.value)


@pytest.mark.parametrize("minutes", list(PRINTER_AUTO_POWER_OFF_CHOICES))
def test_every_interval_the_device_offers_is_accepted(tmp_path, minutes):
    service, _ = _settings_service(tmp_path)
    # The window has to stay at least as long as the hardware interval.
    assert service.save_settings(_persistable(
        printer_auto_power_off_minutes=minutes,
        keep_alive_duration_seconds=4 * HOUR)) is True


@pytest.mark.parametrize("delay", [-1, 61, 600])
def test_turn_off_delay_is_bounded(tmp_path, delay):
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(
            _persistable(relay_webhook_turn_off_delay_minutes=delay))
    assert "relay_webhook_turn_off_delay_minutes" in str(exc.value)


@pytest.mark.parametrize("url", [
    "ftp://192.168.1.42/on",
    "http://169.254.169.254/switch",
    "not a url",
])
def test_bad_webhook_urls_are_refused_at_settings_time(tmp_path, url):
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError) as exc:
        service._validate_settings(_persistable(relay_webhook_turn_on_url=url))
    assert "relay_webhook_turn_on_url" in str(exc.value)


def test_a_bad_url_is_refused_even_while_the_feature_is_off(tmp_path):
    """A typo is caught where it is made, not when the relay first fires."""
    service, _ = _settings_service(tmp_path)
    with pytest.raises(ValueError):
        service._validate_settings(_persistable(
            relay_webhook_enabled=False,
            relay_webhook_turn_off_enabled=False,
            relay_webhook_turn_on_url="http://169.254.169.254/switch"))


def test_defaults_merge_into_an_old_settings_file(tmp_path):
    """A settings file written before this feature gains the keys, off."""
    legacy = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
    }
    service, _ = _settings_service(tmp_path, legacy)
    loaded = service.get_settings()
    assert loaded["relay_webhook_enabled"] is False
    assert loaded["relay_webhook_turn_on_url"] == ""
    assert loaded["relay_webhook_turn_off_url"] == ""
    assert loaded["relay_webhook_turn_off_enabled"] is False
    assert loaded["relay_webhook_turn_off_delay_minutes"] == 5
    assert loaded["printer_auto_power_off_minutes"] == 10


def test_relay_settings_are_not_inherited_by_a_print_request(tmp_path):
    """They are not render options, so they must not reach the print path."""
    service, _ = _settings_service(tmp_path, _persistable())
    resolved = service.resolve_print_settings(None)
    for key in ("relay_webhook_enabled", "relay_webhook_turn_on_url",
                "relay_webhook_turn_off_url", "relay_webhook_turn_off_enabled",
                "relay_webhook_turn_off_delay_minutes",
                "printer_auto_power_off_minutes"):
        assert key not in resolved


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://192.168.1.42/relay/0?turn=on",     # the ordinary case: a LAN relay
    "http://10.50.60.30:1880/endpoint/printer",  # Node-RED on another RFC1918 net
    "http://172.16.4.4/on",                    # the third RFC1918 block
    "https://nodered.lan/printer/power",       # hostname, no DNS performed
    "http://shelly-relay.local/relay/0",       # mDNS name
    "http://[fd12:3456:789a::1]/on",           # IPv6 unique-local
    "https://relay.example.com/hook",          # a hosted bridge is legitimate too
])
def test_webhook_urls_on_the_lan_are_accepted(url):
    assert validate_webhook_url(url) is None


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # AWS/GCP/Azure/DO metadata
    "http://169.254.169.254",                     # bare
    "http://[fe80::1]/on",                        # IPv6 link-local
    "http://[fd00:ec2::254]/latest/meta-data/",   # AWS IMDS over IPv6 (inside ULA)
    "http://100.100.100.200/latest/meta-data/",   # Alibaba (inside CGNAT)
    "http://192.0.0.192/opc/v1/instance/",        # Oracle Cloud
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata/computeMetadata/v1/",
])
def test_link_local_and_metadata_endpoints_are_refused(url):
    with pytest.raises(ValueError) as exc:
        validate_webhook_url(url)
    assert "Invalid webhook URL" in str(exc.value)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1880/on",     # loopback: a relay is never inside this container
    "http://[::1]:1880/on",
    "http://0.0.0.0/on",            # unspecified
    "file:///etc/passwd",           # not an HTTP scheme
    "gopher://relay/on",
    "tcp://192.168.1.42",           # a printer URI is not a webhook URL
    "ftp://192.168.1.42/on",
    "//192.168.1.42/on",            # scheme-relative
    "192.168.1.42/on",              # no scheme at all
    "http://",                      # no host
    "",
    "   ",
])
def test_other_dangerous_or_malformed_webhook_urls_are_refused(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_none_webhook_url_is_refused():
    with pytest.raises(ValueError):
        validate_webhook_url(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("url", [
    "http://192.168.1.42:notaport/on",
    "http://192.168.1.42:0/on",
    "http://192.168.1.42:99999/on",
])
def test_a_bad_port_is_refused_here_rather_than_inside_urllib(url):
    with pytest.raises(ValueError) as exc:
        validate_webhook_url(url)
    assert "port" in str(exc.value)


def test_a_good_port_is_accepted():
    assert validate_webhook_url("http://192.168.1.42:1880/on") is None
    assert validate_webhook_url("http://192.168.1.42:65535/on") is None


def test_whitespace_around_a_webhook_url_is_tolerated():
    assert validate_webhook_url("  http://192.168.1.42/on  ") is None


def test_the_printer_validator_was_not_widened():
    """The relay allowance must not have leaked into the printer URI rules."""
    for url in ("http://192.168.1.42/relay/0", "https://relay.example.com/hook"):
        with pytest.raises(ValueError) as exc:
            validate_printer_uri(url)
        assert "Invalid printer URI" in str(exc.value)
    # And the printer validator still accepts exactly what it always did.
    assert validate_printer_uri("tcp://192.168.1.100") is None
    assert validate_printer_uri("usb://0x04f9:0x209c") is None


# --------------------------------------------------------------------------- #
# turn_on
# --------------------------------------------------------------------------- #

def test_turn_on_is_not_sent_when_the_printer_already_answers(tmp_path):
    sender = _Sender()
    service = _make_service(tmp_path, reachable=True, sender=sender)
    service.ensure_printer_powered()
    assert sender.calls == []


def test_turn_on_is_sent_when_the_printer_does_not_answer(tmp_path):
    """Unreachable at first, answering by the first poll -> one webhook."""
    answers = iter([False, True, True, True])
    sender = _Sender()
    service = _make_service(tmp_path, reachable=lambda _s: next(answers, True),
                            sender=sender)
    service.ensure_printer_powered()
    assert sender.actions == [ACTION_TURN_ON]
    assert sender.calls[0]["url"] == "http://192.168.1.42/relay/0?turn=on"


def test_turn_on_payload_shape(tmp_path):
    sender = _Sender()
    answers = iter([False, True])
    service = _make_service(tmp_path, reachable=lambda _s: next(answers, True),
                            sender=sender)
    service.ensure_printer_powered()
    payload = sender.calls[0]["payload"]
    assert payload["action"] == ACTION_TURN_ON
    assert payload["source"] == "brother_ql_app"
    assert payload["printer_uri"] == "tcp://192.168.1.100"
    assert payload["printer_model"] == "QL-800"
    assert isinstance(payload["timestamp"], str) and payload["timestamp"]


def test_turn_on_retries_once_and_then_fails_naming_what_happened(tmp_path):
    """Send, wait, send again, wait less, then fail."""
    sender = _Sender()
    service = _make_service(tmp_path, reachable=False, sender=sender)

    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()

    assert sender.actions == [ACTION_TURN_ON, ACTION_TURN_ON], "exactly two attempts"
    message = str(exc.value)
    assert "did not answer" in message
    assert "http://192.168.1.42/relay/0?turn=on" in message, "names the endpoint called"
    assert "2 turn_on webhook(s) delivered" in message, "names what was attempted"
    # The failure is recorded for the UI, not only raised.
    assert service.status()["last_error"] == message


def test_turn_on_succeeding_on_the_second_attempt_does_not_fail(tmp_path):
    """The printer comes up only after the second webhook, and that is enough.

    Keyed off the number of webhooks delivered rather than off the number of
    polls, so the outcome depends on the retry *sequence* and not on how many
    times a wait window happened to loop.
    """
    sender = _Sender()
    service = _make_service(tmp_path,
                            reachable=lambda _s: len(sender.calls) >= 2,
                            sender=sender)
    service.ensure_printer_powered()
    assert sender.actions == [ACTION_TURN_ON, ACTION_TURN_ON]
    assert service.status()["last_error"] is None
    # It got as far as scheduling the turn-off, so the job really did proceed.
    assert service.scheduled_turn_off_at() is not None


def test_a_webhook_that_errors_is_reported_not_swallowed(tmp_path):
    sender = _Sender(error=RelayWebhookError("Relay webhook returned HTTP 500"))
    service = _make_service(tmp_path, reachable=False, sender=sender)

    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()

    assert "HTTP 500" in str(exc.value)
    assert len(sender.calls) == 1, "a URL that refuses is reported, not hammered"
    assert "HTTP 500" in service.status()["last_error"]


def test_a_webhook_that_times_out_is_reported(tmp_path):
    sender = _Sender(error=TimeoutError("timed out"))
    service = _make_service(tmp_path, reachable=False, sender=sender)

    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()

    assert "timed out" in str(exc.value)
    assert service.status()["last_error"] is not None


def test_turn_on_arms_the_turn_off_clock(tmp_path):
    """A job that had to wake the printer still gets a scheduled turn-off.

    Arming here rather than only after a successful print is what stops the
    relay being left on when the job that woke the printer then fails.
    """
    answers = iter([False, True])
    service = _make_service(tmp_path, reachable=lambda _s: next(answers, True))
    before = time.time()
    service.ensure_printer_powered()
    scheduled = service.scheduled_turn_off_at()
    assert scheduled is not None
    assert scheduled >= before + 4 * HOUR + 300 - 1


def test_turn_on_is_skipped_when_no_url_is_configured(tmp_path):
    """Enabled but unconfigured must not probe or send; it warns and returns."""
    service = _make_service(
        tmp_path,
        settings=_settings(relay_webhook_turn_on_url=""),
        reachable=_forbidden_probe,
        sender=_forbidden_sender,
    )
    service.ensure_printer_powered()  # must not raise


# --------------------------------------------------------------------------- #
# The real urllib sender (patched; nothing leaves the process)
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_default_sender_posts_json_and_honours_the_authorization_env_var(tmp_path):
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["timeout"] = timeout
        return _FakeResponse(200)

    with patch.dict(os.environ, {AUTHORIZATION_ENV_VAR: "Bearer s3cret"}), \
            patch("src.services.relay_service.urllib.request.urlopen", fake_urlopen):
        service.send(ACTION_TURN_ON)

    assert captured["method"] == "POST"
    assert captured["url"] == "http://192.168.1.42/relay/0?turn=on"
    assert captured["body"]["action"] == ACTION_TURN_ON
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["authorization"] == "Bearer s3cret"
    assert captured["timeout"] == service.webhook_timeout


def test_default_sender_omits_authorization_when_the_env_var_is_unset(tmp_path):
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _FakeResponse(200)

    env = {k: v for k, v in os.environ.items() if k != AUTHORIZATION_ENV_VAR}
    with patch.dict(os.environ, env, clear=True), \
            patch("src.services.relay_service.urllib.request.urlopen", fake_urlopen):
        service.send(ACTION_TURN_ON)

    assert "authorization" not in captured["headers"]


@pytest.mark.parametrize("error, expected", [
    (urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None),
     "HTTP 503"),
    (urllib.error.URLError("Connection refused"), "could not be reached"),
    (TimeoutError("timed out"), "failed"),
])
def test_default_sender_turns_transport_failures_into_relay_errors(tmp_path, error, expected):
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))

    def fake_urlopen(_request, timeout=None):
        raise error

    with patch("src.services.relay_service.urllib.request.urlopen", fake_urlopen):
        with pytest.raises(RelayWebhookError) as exc:
            service.send(ACTION_TURN_ON)
    assert expected in str(exc.value)


def test_default_sender_rejects_a_non_2xx_response(tmp_path):
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))

    with patch("src.services.relay_service.urllib.request.urlopen",
               lambda *_a, **_k: _FakeResponse(302)):
        with pytest.raises(RelayWebhookError) as exc:
            service.send(ACTION_TURN_ON)
    assert "HTTP 302" in str(exc.value)


def test_send_refuses_a_url_that_would_not_validate(tmp_path):
    """Defence in depth: the URL is re-checked immediately before the request."""
    service = RelayPowerService(
        state_file=str(tmp_path / "s.json"),
        settings_provider=_Settings(
            _settings(relay_webhook_turn_on_url="http://169.254.169.254/on")),
        sender=_forbidden_sender,
    )
    with pytest.raises(RelayWebhookError) as exc:
        service.send(ACTION_TURN_ON)
    assert "Refusing to call the relay webhook" in str(exc.value)


# --------------------------------------------------------------------------- #
# turn_off: scheduling, pending work, restart
# --------------------------------------------------------------------------- #

def test_turn_off_fires_once_the_moment_has_passed(tmp_path):
    sender = _Sender()
    service = _make_service(tmp_path, sender=sender)
    origin = time.time() - (4 * HOUR + 400)   # window + margin already elapsed
    service.note_print_activity(origin)

    assert service.tick() == ACTION_TURN_OFF
    assert sender.actions == [ACTION_TURN_OFF]
    # And the schedule is cleared, so it does not fire again on the next tick.
    assert service.scheduled_turn_off_at() is None
    assert service.tick() is None
    assert sender.actions == [ACTION_TURN_OFF]


def test_turn_off_does_not_fire_before_the_moment(tmp_path):
    sender = _Sender()
    service = _make_service(tmp_path, sender=sender)
    service.note_print_activity()
    assert service.tick() is None
    assert sender.calls == []


def test_turn_off_uses_the_separate_url_when_one_is_configured(tmp_path):
    sender = _Sender()
    service = _make_service(
        tmp_path,
        settings=_settings(relay_webhook_turn_off_url="http://192.168.1.42/relay/0?turn=off"),
        sender=sender)
    service.note_print_activity(time.time() - (4 * HOUR + 400))
    service.tick()
    assert sender.calls[0]["url"] == "http://192.168.1.42/relay/0?turn=off"


def test_turn_off_falls_back_to_the_turn_on_url(tmp_path):
    """One URL, two actions: the body says which."""
    sender = _Sender()
    service = _make_service(tmp_path, sender=sender)
    service.note_print_activity(time.time() - (4 * HOUR + 400))
    service.tick()
    assert sender.calls[0]["url"] == "http://192.168.1.42/relay/0?turn=on"
    assert sender.calls[0]["payload"]["action"] == ACTION_TURN_OFF


def test_a_queued_job_resets_the_turn_off_clock(tmp_path):
    """SAFETY: never switch off while anything is pending."""
    sender = _Sender()
    service = _make_service(tmp_path, pending=True, sender=sender)
    # A moment that is already overdue by hours.
    service.note_print_activity(time.time() - (8 * HOUR))
    overdue = service.scheduled_turn_off_at()
    assert overdue < time.time()

    now = time.time()
    assert service.tick(now) == "deferred"

    assert sender.calls == [], "nothing was switched off while work was pending"
    # The clock was pushed out to a full window from now.
    rescheduled = service.scheduled_turn_off_at()
    assert rescheduled >= now + 4 * HOUR + 300 - 1


def test_the_clock_keeps_resetting_while_work_stays_pending(tmp_path):
    sender = _Sender()
    pending = {"busy": True}
    service = _make_service(tmp_path, pending=lambda: pending["busy"], sender=sender)
    service.note_print_activity(time.time() - (8 * HOUR))

    for _ in range(3):
        assert service.tick() == "deferred"
    assert sender.calls == []

    # Work drains; the moment is now in the future, so still nothing fires.
    pending["busy"] = False
    assert service.tick() is None
    assert sender.calls == []


def test_a_printing_job_also_holds_the_relay_open(tmp_path):
    """"Pending" covers printing as well as queued."""
    queue = PrintQueueService()
    sender = _Sender()
    service = RelayPowerService(
        state_file=str(tmp_path / "s.json"),
        settings_provider=_Settings(_settings()),
        reachability_probe=lambda _s: True,
        pending_work_probe=None,     # exercise the real queue-backed probe
        sender=sender,
    )
    with patch("src.services.queue_service.print_queue", queue):
        service.note_print_activity(time.time() - (8 * HOUR))
        # Nothing queued -> the overdue moment is honoured.
        assert service.tick() == ACTION_TURN_OFF

        sender.calls.clear()
        service.note_print_activity(time.time() - (8 * HOUR))
        # A job in the registry in the "printing" state defers it.
        queue._jobs["j1"] = {"id": "j1", "status": "printing"}
        queue._order.append("j1")
        assert service.tick() == "deferred"
        assert sender.calls == []


def test_the_schedule_survives_a_restart(tmp_path):
    """The moment is on disk, so a new process picks up the same one."""
    sender = _Sender()
    first = _make_service(tmp_path, sender=sender)
    origin = time.time()
    first.note_print_activity(origin)
    expected = first.scheduled_turn_off_at()
    assert expected is not None

    # The state file is real, readable JSON, not an in-memory nicety.
    state = json.loads((tmp_path / "relay_power.json").read_text(encoding="utf-8"))
    assert state["turn_off_at"] == pytest.approx(expected)
    assert state["turn_off_at_iso"]

    # Simulate the restart: a brand-new service over the same state file.
    second = _make_service(tmp_path, sender=_Sender())
    assert second.scheduled_turn_off_at() == pytest.approx(expected)


def test_a_moment_that_fell_due_while_the_app_was_down_fires_at_boot(tmp_path):
    """Otherwise a restart mid-window leaves the relay on forever."""
    first = _make_service(tmp_path)
    first.note_print_activity(time.time() - (4 * HOUR + 400))
    assert first.scheduled_turn_off_at() < time.time()

    sender = _Sender()
    rebooted = _make_service(tmp_path, sender=sender)
    assert rebooted.tick() == ACTION_TURN_OFF
    assert sender.actions == [ACTION_TURN_OFF]
    # And the recovered schedule is cleared on disk too.
    state = json.loads((tmp_path / "relay_power.json").read_text(encoding="utf-8"))
    assert state["turn_off_at"] is None


def test_a_restart_does_not_fire_a_moment_that_is_still_in_the_future(tmp_path):
    first = _make_service(tmp_path)
    first.note_print_activity()

    sender = _Sender()
    rebooted = _make_service(tmp_path, sender=sender)
    assert rebooted.tick() is None
    assert sender.calls == []


def test_the_state_file_defaults_to_sitting_beside_the_settings_file(tmp_path):
    """It belongs in the data volume, which is the part of the container that
    survives a restart, which is the whole point of persisting it."""
    service = RelayPowerService(
        settings_provider=_Settings(_settings(),
                                    settings_file=str(tmp_path / "settings.json")))
    assert service.state_file == str(tmp_path / "relay_power.json")


def test_a_corrupt_state_file_is_ignored_rather_than_obeyed(tmp_path):
    (tmp_path / "relay_power.json").write_text("{not json", encoding="utf-8")
    sender = _Sender()
    service = _make_service(tmp_path, sender=sender)
    assert service.scheduled_turn_off_at() is None
    assert service.tick() is None
    assert sender.calls == []


def test_disabling_turn_off_clears_a_schedule_made_under_the_old_settings(tmp_path):
    sender = _Sender()
    provider = _Settings(_settings())
    service = RelayPowerService(
        state_file=str(tmp_path / "s.json"), settings_provider=provider,
        reachability_probe=lambda _s: True, pending_work_probe=lambda: False,
        sender=sender)
    service.note_print_activity(time.time() - (4 * HOUR + 400))
    assert service.scheduled_turn_off_at() is not None

    provider.settings["relay_webhook_turn_off_enabled"] = False
    assert service.tick() == "cleared"
    assert service.scheduled_turn_off_at() is None
    assert sender.calls == []


def test_a_turn_off_delivery_failure_is_recorded_and_not_retried_forever(tmp_path):
    sender = _Sender(error=RelayWebhookError("Relay webhook could not be reached"))
    service = _make_service(tmp_path, sender=sender)
    service.note_print_activity(time.time() - (4 * HOUR + 400))

    assert service.tick() is None          # no turn_off happened
    assert len(sender.calls) == 1
    assert "could not be reached" in service.status()["last_error"]
    # The schedule is dropped rather than retried every tick for hours.
    assert service.scheduled_turn_off_at() is None
    assert service.tick() is None
    assert len(sender.calls) == 1


def test_note_print_activity_never_raises(tmp_path):
    """A scheduling problem must not fail a print that already succeeded."""
    service = _make_service(tmp_path)
    with patch.object(service, "arm", side_effect=RuntimeError("disk on fire")):
        service.note_print_activity()  # must not raise


# --------------------------------------------------------------------------- #
# The queue's pre-job gate
# --------------------------------------------------------------------------- #

def _drain(queue, timeout=5.0):
    """Wait until the queue has no queued or printing job left."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = queue.queue_status()
        if not status["queued"] and not status["printing"]:
            return True
        time.sleep(0.01)
    return False


def test_a_job_waits_in_the_queue_while_the_gate_runs(tmp_path):
    """The job stays "queued" — not "printing", not failed — while it waits."""
    queue = PrintQueueService()
    release = threading.Event()
    observed = []
    printed = threading.Event()

    def gate():
        release.wait(timeout=5)

    queue.set_pre_job_gate(gate)
    with patch.object(queue, "_sweep_job_files", lambda: None):
        queue.start()
        job_id = queue.submit("text", "waiting", lambda: printed.set())

        # While the gate blocks, the job is queued and counts as pending work --
        # which is exactly what stops the relay switching off underneath it.
        time.sleep(0.1)
        observed.append(queue.get(job_id)["status"])
        assert queue.queue_status()["queued"] == 1

        release.set()
        assert printed.wait(timeout=5)
        assert _drain(queue)

    assert observed == ["queued"]
    assert queue.get(job_id)["status"] == "done"


def test_a_failing_gate_fails_the_job_with_its_message(tmp_path):
    queue = PrintQueueService()
    ran = threading.Event()

    def gate():
        raise RelayWebhookError("Printer did not answer within 75s of the relay "
                                "being switched on")

    queue.set_pre_job_gate(gate)
    with patch.object(queue, "_sweep_job_files", lambda: None):
        queue.start()
        job_id = queue.submit("text", "doomed", lambda: ran.set())
        assert _drain(queue)

    job = queue.get(job_id)
    assert job["status"] == "failed"
    assert "did not answer" in job["error"]
    assert not ran.is_set(), "the print must not have run"


def test_a_failing_gate_does_not_kill_the_worker(tmp_path):
    queue = PrintQueueService()
    calls = {"n": 0}
    done = threading.Event()

    def gate():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RelayWebhookError("first one fails")

    queue.set_pre_job_gate(gate)
    with patch.object(queue, "_sweep_job_files", lambda: None):
        queue.start()
        first = queue.submit("text", "doomed", lambda: None)
        second = queue.submit("text", "fine", lambda: done.set())
        assert done.wait(timeout=5)
        assert _drain(queue)

    assert queue.get(first)["status"] == "failed"
    assert queue.get(second)["status"] == "done"


def test_a_queue_without_a_gate_behaves_exactly_as_before(tmp_path):
    queue = PrintQueueService()
    done = threading.Event()
    with patch.object(queue, "_sweep_job_files", lambda: None):
        queue.start()
        job_id = queue.submit("text", "plain", lambda: done.set())
        assert done.wait(timeout=5)
        assert _drain(queue)
    assert queue.get(job_id)["status"] == "done"


# --------------------------------------------------------------------------- #
# Off by default: no behavioural change, no outbound request
# --------------------------------------------------------------------------- #

def test_the_feature_is_off_in_the_shipped_defaults():
    assert DEFAULT_SETTINGS["relay_webhook_enabled"] is False
    assert DEFAULT_SETTINGS["relay_webhook_turn_off_enabled"] is False
    assert DEFAULT_SETTINGS["relay_webhook_turn_on_url"] == ""
    assert DEFAULT_SETTINGS["relay_webhook_turn_off_url"] == ""
    assert DEFAULT_SETTINGS["printer_auto_power_off_minutes"] == 10
    assert DEFAULT_SETTINGS["relay_webhook_turn_off_delay_minutes"] == 5


def test_disabled_means_no_webhook_no_probe_and_no_state_file(tmp_path):
    """The whole feature off: nothing is sent, nothing is probed, nothing written."""
    disabled = dict(DEFAULT_SETTINGS)
    disabled.update({"keep_alive_enabled": True, "keep_alive_mode": "timed",
                     "keep_alive_duration_seconds": 4 * HOUR})
    state_file = tmp_path / "relay_power.json"
    service = RelayPowerService(
        state_file=str(state_file),
        settings_provider=_Settings(disabled),
        reachability_probe=_forbidden_probe,
        pending_work_probe=_forbidden_probe,
        sender=_forbidden_sender,
    )

    service.ensure_printer_powered()
    assert service.tick() is None
    service.note_print_activity()
    assert service.tick() is None

    assert not state_file.exists(), "a disabled feature must not write state"


def test_disabled_leaves_the_keep_alive_window_exactly_as_configured():
    """No subtraction, so an install that ignores this sees no timing change."""
    disabled = dict(DEFAULT_SETTINGS)
    disabled.update({"keep_alive_enabled": True, "keep_alive_mode": "timed",
                     "keep_alive_duration_seconds": 7200})
    assert RelayPowerService.effective_keep_alive_seconds(disabled) == 7200
    assert RelayPowerService.turn_off_offset_seconds(disabled) is None


def test_default_settings_save_cleanly(tmp_path):
    """The shipped defaults validate; the feature does not block a plain save."""
    service, _ = _settings_service(tmp_path)
    assert service.save_settings(dict(DEFAULT_SETTINGS)) is True


# --------------------------------------------------------------------------- #
# The keep-alive worker honours the shortened window
#
# The arithmetic above is only worth anything if the heartbeat actually stops
# early, so these drive the real worker loop with a fake clock origin and watch
# whether it pings.
# --------------------------------------------------------------------------- #

def _keep_alive_pings(tmp_path, settings, elapsed_since_print):
    """Run the real keep-alive worker briefly; return the pings it sent."""
    from src.services.printer_service import PrinterService

    printer = PrinterService(upload_folder=str(tmp_path / "uploads"))
    printer._last_print_at = time.time() - elapsed_since_print
    pings = []
    stop = threading.Event()

    def write_keepalive(ip, *_a, **_k):
        pings.append(ip)
        return True

    with patch("src.services.printer_service.settings_service") as settings_mock, \
            patch("src.services.printer_service.guess_backend", lambda _u: "network"), \
            patch.object(printer, "_write_keepalive", write_keepalive), \
            patch.object(printer, "_ipp_ping", lambda _ip: False), \
            patch.object(printer, "_tcp_ping", lambda _ip: False):
        settings_mock.get_settings.return_value = settings
        worker = threading.Thread(
            target=printer._keep_alive_worker,
            args=("tcp://192.168.1.100", "QL-800", 0.01, stop),
            daemon=True)
        worker.start()
        time.sleep(0.2)
        stop.set()
        worker.join(timeout=5)
    return pings


def test_keep_alive_stops_early_by_the_hardware_interval(tmp_path):
    """3:55 after the last print, keep-alive is already done (it stopped at 3:50)."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10)
    assert _keep_alive_pings(tmp_path, settings, 3 * HOUR + 55 * 60) == []


def test_keep_alive_is_still_running_before_the_shortened_window_closes(tmp_path):
    """3:45 after the last print, it is still holding the printer awake."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10)
    assert _keep_alive_pings(tmp_path, settings, 3 * HOUR + 45 * 60) != []


def test_keep_alive_runs_the_full_window_when_the_relay_is_off(tmp_path):
    """Same 3:55, feature off -> unchanged behaviour, still pinging."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10,
                         relay_webhook_enabled=False,
                         relay_webhook_turn_off_enabled=False)
    assert _keep_alive_pings(tmp_path, settings, 3 * HOUR + 55 * 60) != []


def test_keep_alive_does_nothing_when_the_window_equals_the_hardware_interval(tmp_path):
    """The documented edge case, end to end: the hardware carries the window."""
    settings = _settings(keep_alive_duration_seconds=600,
                         printer_auto_power_off_minutes=10)
    assert _keep_alive_pings(tmp_path, settings, 30) == []


# --------------------------------------------------------------------------- #
# Status reporting and the warning
# --------------------------------------------------------------------------- #

def test_status_reports_the_timing_chain(tmp_path):
    service = _make_service(tmp_path)
    service.note_print_activity()
    status = service.status()

    assert status["enabled"] is True
    assert status["turn_off_enabled"] is True
    assert status["turn_on_url_configured"] is True
    assert status["turn_off_url_configured"] is True
    assert status["printer_auto_power_off_minutes"] == 10
    assert status["configured_window_seconds"] == 4 * HOUR
    assert status["effective_keep_alive_seconds"] == 4 * HOUR - 600
    assert status["turn_off_delay_seconds"] == 300
    assert status["scheduled_turn_off_at"] is not None
    assert status["seconds_until_turn_off"] == pytest.approx(4 * HOUR + 300, abs=5)


def test_status_carries_the_hardware_mismatch_warning_when_turn_off_is_on(tmp_path):
    """The one thing the app cannot verify has to be said where it matters."""
    service = _make_service(tmp_path)
    warning = service.status()["warning"]
    assert warning == AUTO_POWER_OFF_MISMATCH_WARNING
    # It has to name the danger plainly, not hint at it: which direction of
    # mismatch is dangerous, and what it costs.
    assert "cannot read" in warning
    assert "longer than the value configured here" in warning
    assert "cut mains power while the printer is still running" in warning
    assert "damage the printer" in warning


def test_the_warning_reads_as_prose_rather_than_as_shouting():
    """It is a hazard notice, so its severity has to survive the punctuation.

    Emphasis is the rendering's job: the UI already bolds it. Capitals and ASCII
    double hyphens in the text itself only make it harder to read the one
    paragraph that most needs reading.
    """
    warning = AUTO_POWER_OFF_MISMATCH_WARNING
    assert "--" not in warning
    shouted = [word for word in warning.split()
               if len(word) > 2 and word.strip(".,").isupper()]
    assert shouted == []
    # Whole sentences, each of which stands on its own.
    sentences = [s.strip() for s in warning.split(". ") if s.strip()]
    assert len(sentences) >= 4


def test_the_warning_is_sent_before_anything_can_cut_power(tmp_path):
    """The warning is most needed while someone decides whether to arm this.

    Sending it only once the turn-off is live would withhold it at exactly the
    moment it should be read, so it is always present; ``warning_armed`` says
    whether it describes a live hazard or a caution about what would happen.
    """
    armed = _make_service(tmp_path)
    assert armed.status()["warning"] == AUTO_POWER_OFF_MISMATCH_WARNING
    assert armed.status()["warning_armed"] is True

    no_turn_off = _make_service(
        tmp_path, settings=_settings(relay_webhook_turn_off_enabled=False),
        name="no_turn_off.json")
    assert no_turn_off.status()["warning"] == AUTO_POWER_OFF_MISMATCH_WARNING
    assert no_turn_off.status()["warning_armed"] is False

    off = _make_service(tmp_path, settings=_settings(relay_webhook_enabled=False),
                        name="off.json")
    assert off.status()["warning"] == AUTO_POWER_OFF_MISMATCH_WARNING
    assert off.status()["warning_armed"] is False


def test_status_never_leaks_the_authorization_value(tmp_path):
    service = _make_service(tmp_path)
    with patch.dict(os.environ, {AUTHORIZATION_ENV_VAR: "Bearer s3cret"}):
        status = service.status()
    assert status["authorization_configured"] is True
    assert "s3cret" not in json.dumps(status)


def test_status_does_not_send_anything_or_touch_the_printer(tmp_path):
    service = _make_service(tmp_path, reachable=_forbidden_probe,
                            pending=_forbidden_probe, sender=_forbidden_sender)
    service.status()  # must not raise


# --------------------------------------------------------------------------- #
# Why the two moments coincide
#
# With relay power control off, effective_keep_alive_seconds equals the
# configured window, because the subtraction belongs to the relay feature. That
# is correct, and it used to be unreportable: a client comparing the two numbers
# saw a difference of zero and could not tell "nothing was subtracted" from "ten
# minutes were subtracted from a window ten minutes longer". So it rendered the
# heartbeat stopping and the printer sleeping at the same moment, and explained
# the first as a subtraction that had not happened.
# --------------------------------------------------------------------------- #

def test_the_subtraction_is_reported_not_inferred():
    """With the feature on: the offset was applied, and the numbers show it."""
    settings = _settings(keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10)
    assert RelayPowerService.hardware_offset_applied(settings) is True
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 3 * HOUR + 50 * 60
    # The device's own timer then finishes at exactly the window asked for.
    assert RelayPowerService.printer_power_off_seconds(settings) == 4 * HOUR
    assert (RelayPowerService.printer_power_off_seconds(settings)
            == RelayPowerService.configured_window_seconds(settings))


def test_with_the_feature_off_no_subtraction_is_claimed():
    """The bug this fixes: same number, different reason, and now it says so."""
    settings = _settings(relay_webhook_enabled=False,
                         relay_webhook_turn_off_enabled=False,
                         keep_alive_duration_seconds=4 * HOUR,
                         printer_auto_power_off_minutes=10)
    assert RelayPowerService.hardware_offset_applied(settings) is False
    # The heartbeat runs the whole window, unchanged.
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 4 * HOUR
    # And the printer's own timer then runs on top of it, so the two moments are
    # not the same moment at all: 4:00 and 4:10.
    assert RelayPowerService.printer_power_off_seconds(settings) == 4 * HOUR + 600
    assert (RelayPowerService.printer_power_off_seconds(settings)
            != RelayPowerService.effective_keep_alive_seconds(settings))


def test_the_two_readings_are_indistinguishable_from_the_numbers_alone():
    """Why the flag has to exist: the arithmetic cannot recover the answer.

    A four-hour window with the feature off and a four-hour-ten window with it
    on produce the same effective window. Only the flag tells them apart.
    """
    off = _settings(relay_webhook_enabled=False, relay_webhook_turn_off_enabled=False,
                    keep_alive_duration_seconds=4 * HOUR,
                    printer_auto_power_off_minutes=10)
    on = _settings(keep_alive_duration_seconds=4 * HOUR + 600,
                   printer_auto_power_off_minutes=10)
    assert (RelayPowerService.effective_keep_alive_seconds(off)
            == RelayPowerService.effective_keep_alive_seconds(on) == 4 * HOUR)
    assert RelayPowerService.hardware_offset_applied(off) is False
    assert RelayPowerService.hardware_offset_applied(on) is True


def test_no_timed_window_means_nothing_to_apply_and_no_moment_to_name():
    for settings in (_settings(keep_alive_mode="forever",
                               relay_webhook_turn_off_enabled=False),
                     _settings(keep_alive_duration_seconds=0,
                               relay_webhook_turn_off_enabled=False)):
        assert RelayPowerService.hardware_offset_applied(settings) is False
        assert RelayPowerService.printer_power_off_seconds(settings) is None


def test_the_documented_edge_case_still_lines_up():
    """duration == hardware: the heartbeat does nothing, the device does it all."""
    settings = _settings(keep_alive_duration_seconds=600,
                         printer_auto_power_off_minutes=10)
    assert RelayPowerService.hardware_offset_applied(settings) is True
    assert RelayPowerService.effective_keep_alive_seconds(settings) == 0
    assert RelayPowerService.printer_power_off_seconds(settings) == 600


def test_status_carries_both_readings(tmp_path):
    armed = _make_service(tmp_path)
    status = armed.status()
    assert status["hardware_offset_applied"] is True
    assert status["effective_keep_alive_seconds"] == 4 * HOUR - 600
    assert status["printer_power_off_seconds"] == 4 * HOUR

    off = _make_service(tmp_path, name="off.json",
                        settings=_settings(relay_webhook_enabled=False,
                                           relay_webhook_turn_off_enabled=False))
    status = off.status()
    assert status["hardware_offset_applied"] is False
    assert status["effective_keep_alive_seconds"] == 4 * HOUR
    assert status["printer_power_off_seconds"] == 4 * HOUR + 600


def test_status_reports_both_readings_as_null_without_a_window(tmp_path):
    service = _make_service(tmp_path, name="forever.json",
                            settings=_settings(keep_alive_mode="forever",
                                               relay_webhook_turn_off_enabled=False))
    status = service.status()
    assert status["hardware_offset_applied"] is False
    assert status["effective_keep_alive_seconds"] is None
    assert status["printer_power_off_seconds"] is None


# --------------------------------------------------------------------------- #
# Sending one by hand
#
# Still offline: the sender is injected everywhere, and the two tests that
# exercise the real urllib path patch urlopen itself.
# --------------------------------------------------------------------------- #

def test_send_now_delivers_turn_on_and_reports_the_request(tmp_path):
    sender = _Sender(status=200)
    service = _make_service(tmp_path, reachable=_forbidden_probe,
                            pending=_forbidden_probe, sender=sender)

    report = service.send_now(ACTION_TURN_ON)

    assert report["success"] is True
    assert report["action"] == ACTION_TURN_ON
    assert report["url"] == "http://192.168.1.42/relay/0?turn=on"
    assert report["response_status"] == 200
    assert report["error"] is None
    assert report["mains_power"] == "on"
    # What was sent, exactly, and it is the body the print path sends.
    assert report["payload"]["action"] == ACTION_TURN_ON
    assert report["payload"]["printer_uri"] == "tcp://192.168.1.100"
    assert report["payload"] == sender.calls[0]["payload"]
    assert report["sent_at"] == report["payload"]["timestamp"]
    assert "http://192.168.1.42/relay/0?turn=on" in report["message"]
    assert "HTTP 200" in report["message"]


def test_send_now_says_plainly_that_it_cut_the_power(tmp_path):
    """The one place a user can cut mains power deliberately must say so."""
    sender = _Sender(status=200)
    service = _make_service(tmp_path, sender=sender)

    report = service.send_now(ACTION_TURN_OFF)

    assert report["success"] is True
    assert report["mains_power"] == "off"
    assert sender.actions == [ACTION_TURN_OFF]
    assert "cut" in report["message"] and "power" in report["message"].lower()


def test_send_now_uses_the_configured_turn_off_url(tmp_path):
    sender = _Sender(status=200)
    service = _make_service(
        tmp_path, sender=sender,
        settings=_settings(relay_webhook_turn_off_url="http://192.168.1.42/relay/0?turn=off"))
    report = service.send_now(ACTION_TURN_OFF)
    assert report["url"] == "http://192.168.1.42/relay/0?turn=off"
    assert sender.calls[0]["url"] == "http://192.168.1.42/relay/0?turn=off"


def test_send_now_falls_back_to_the_turn_on_url(tmp_path):
    sender = _Sender(status=200)
    service = _make_service(tmp_path, sender=sender)
    report = service.send_now(ACTION_TURN_OFF)
    assert report["url"] == "http://192.168.1.42/relay/0?turn=on"
    assert report["payload"]["action"] == ACTION_TURN_OFF


def test_send_now_reports_why_the_relay_refused_rather_than_raising(tmp_path):
    """A user testing a relay needs the reason, not just the failure."""
    sender = _Sender(error=RelayWebhookError(
        "Relay webhook returned HTTP 401", "RELAY_WEBHOOK_ERROR",
        {"url": "http://192.168.1.42/relay/0?turn=on", "status": 401}))
    service = _make_service(tmp_path, sender=sender)

    report = service.send_now(ACTION_TURN_ON)

    assert report["success"] is False
    assert report["response_status"] == 401
    assert "HTTP 401" in report["error"]
    assert "HTTP 401" in report["message"]
    # The relay may have acted before it answered, so neither state is claimed.
    assert report["mains_power"] == "unknown"
    # It still went out, and the report still names what went out.
    assert sender.actions == [ACTION_TURN_ON]
    assert report["payload"] == sender.calls[0]["payload"]


def test_send_now_reports_a_transport_failure_with_no_status(tmp_path):
    sender = _Sender(error=RelayWebhookError(
        "Relay webhook could not be reached: Connection refused",
        "RELAY_WEBHOOK_ERROR", {"url": "http://192.168.1.42/relay/0?turn=on"}))
    service = _make_service(tmp_path, sender=sender)

    report = service.send_now(ACTION_TURN_OFF)

    assert report["success"] is False
    assert report["response_status"] is None
    assert "Connection refused" in report["error"]
    assert report["mains_power"] == "unknown"


def test_an_unconfirmed_turn_off_does_not_claim_the_power_is_still_on(tmp_path):
    """SAFETY: an error after the relay acted is not evidence that it did not."""
    sender = _Sender(error=RelayWebhookError("Relay webhook failed: timed out"))
    service = _make_service(tmp_path, sender=sender)
    report = service.send_now(ACTION_TURN_OFF)
    assert report["mains_power"] == "unknown"
    assert "may" in report["message"], "the message has to admit the uncertainty"


def test_send_now_does_not_move_a_scheduled_turn_off(tmp_path):
    """A hand-sent turn_off leaves the automatic one exactly where it was."""
    sender = _Sender(status=200)
    service = _make_service(tmp_path, sender=sender)
    service.note_print_activity()
    scheduled = service.scheduled_turn_off_at()
    assert scheduled is not None

    report = service.send_now(ACTION_TURN_OFF)

    assert report["schedule_changed"] is False
    assert report["scheduled_turn_off_at"] == pytest.approx(scheduled)
    assert service.scheduled_turn_off_at() == pytest.approx(scheduled)
    # On disk too: a restart still finds the same moment.
    state = json.loads((tmp_path / "relay_power.json").read_text(encoding="utf-8"))
    assert state["turn_off_at"] == pytest.approx(scheduled)


def test_a_hand_sent_turn_on_does_not_arm_a_schedule(tmp_path):
    """Arming is measured from the last print, and this is not a print."""
    sender = _Sender(status=200)
    service = _make_service(tmp_path, sender=sender)
    assert service.scheduled_turn_off_at() is None

    report = service.send_now(ACTION_TURN_ON)

    assert report["schedule_changed"] is False
    assert report["scheduled_turn_off_at"] is None
    assert service.scheduled_turn_off_at() is None
    assert not (tmp_path / "relay_power.json").exists()


def test_send_now_does_not_require_the_schedule_to_be_armed(tmp_path):
    """turn_off by hand while its scheduled half is off, and with no window.

    Whether the relay switches off is exactly the thing worth establishing
    before arming something that will cut mains power unattended.
    """
    sender = _Sender(status=200)
    service = _make_service(
        tmp_path, sender=sender,
        settings=_settings(relay_webhook_turn_off_enabled=False,
                           keep_alive_mode="forever"))

    report = service.send_now(ACTION_TURN_OFF)

    assert report["success"] is True
    assert report["mains_power"] == "off"
    assert report["scheduled_turn_off_at"] is None
    assert sender.actions == [ACTION_TURN_OFF]


def test_send_now_is_refused_while_the_feature_is_off(tmp_path):
    """Nothing is sent, and the message says which switch to throw."""
    service = _make_service(tmp_path, sender=_forbidden_sender,
                            reachable=_forbidden_probe, pending=_forbidden_probe,
                            settings=_settings(relay_webhook_enabled=False,
                                               relay_webhook_turn_off_enabled=False))
    for action in (ACTION_TURN_ON, ACTION_TURN_OFF):
        with pytest.raises(ValueError) as exc:
            service.send_now(action)
        assert "relay_webhook_enabled" in str(exc.value)
        assert "nothing was sent" in str(exc.value)


def test_send_now_is_refused_without_a_url(tmp_path):
    service = _make_service(tmp_path, sender=_forbidden_sender,
                            settings=_settings(relay_webhook_turn_on_url="",
                                               relay_webhook_turn_off_url=""))
    with pytest.raises(ValueError) as exc:
        service.send_now(ACTION_TURN_ON)
    assert "relay_webhook_turn_on_url" in str(exc.value)


def test_send_now_refuses_an_unknown_action(tmp_path):
    service = _make_service(tmp_path, sender=_forbidden_sender)
    for action in ("reboot", "", "TURN_ON", None):
        with pytest.raises(ValueError) as exc:
            service.send_now(action)
        assert "turn_on" in str(exc.value) and "turn_off" in str(exc.value)


def test_send_now_never_probes_the_printer_or_the_queue(tmp_path):
    """It reports the webhook and nothing more; no waiting, no polling."""
    sender = _Sender(status=200)
    service = _make_service(tmp_path, sender=sender, reachable=_forbidden_probe,
                            pending=_forbidden_probe)
    assert service.send_now(ACTION_TURN_ON)["success"] is True
    assert len(sender.calls) == 1, "one webhook, no retry sequence"


def test_send_now_shows_up_in_the_status(tmp_path):
    """The hand-sent webhook really was the last one, so the status says so."""
    service = _make_service(tmp_path, sender=_Sender(status=200))
    service.send_now(ACTION_TURN_OFF)
    status = service.status()
    assert status["last_action"] == ACTION_TURN_OFF
    assert status["last_action_at"]
    assert status["last_error"] is None


def test_a_failed_send_now_shows_up_in_the_status(tmp_path):
    service = _make_service(tmp_path, sender=_Sender(
        error=RelayWebhookError("Relay webhook returned HTTP 500")))
    service.send_now(ACTION_TURN_ON)
    assert "HTTP 500" in service.status()["last_error"]


def test_send_now_never_leaks_the_authorization_value(tmp_path):
    service = _make_service(tmp_path, sender=_Sender(status=200))
    with patch.dict(os.environ, {AUTHORIZATION_ENV_VAR: "Bearer s3cret"}):
        report = service.send_now(ACTION_TURN_ON)
    assert report["authorization_sent"] is True
    assert "s3cret" not in json.dumps(report)


def test_send_now_reports_a_url_that_would_not_validate(tmp_path):
    """Defence in depth still applies, and the refusal is reported, not raised."""
    service = _make_service(
        tmp_path, sender=_forbidden_sender,
        settings=_settings(relay_webhook_turn_on_url="http://169.254.169.254/on"))
    report = service.send_now(ACTION_TURN_ON)
    assert report["success"] is False
    assert "Refusing to call the relay webhook" in report["error"]
    assert report["mains_power"] == "unknown"


def test_send_now_over_the_real_sender_reports_what_came_back(tmp_path):
    """End to end through urllib, with urlopen patched: nothing leaves here."""
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))
    with patch("src.services.relay_service.urllib.request.urlopen",
               lambda *_a, **_k: _FakeResponse(204)):
        report = service.send_now(ACTION_TURN_ON)
    assert report["success"] is True
    assert report["response_status"] == 204


def test_the_real_sender_returns_the_status_it_accepted(tmp_path):
    service = RelayPowerService(state_file=str(tmp_path / "s.json"),
                                settings_provider=_Settings(_settings()))
    with patch("src.services.relay_service.urllib.request.urlopen",
               lambda *_a, **_k: _FakeResponse(202)):
        assert service.send(ACTION_TURN_ON) == 202


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

class _RelayStub:
    """Stands in for the relay service singleton in the controller."""

    def __init__(self, report=None, error=None):
        self.report = report
        self.error = error
        self.calls = []

    def send_now(self, action):
        self.calls.append(action)
        if self.error is not None:
            raise self.error
        return dict(self.report)


def _report(**overrides):
    base = {
        "success": True, "action": ACTION_TURN_ON,
        "url": "http://192.168.1.42/relay/0?turn=on", "payload": {},
        "authorization_sent": False, "response_status": 200,
        "sent_at": "2026-01-01T00:00:00+00:00", "mains_power": "on",
        "message": "ok", "error": None, "schedule_changed": False,
        "scheduled_turn_off_at": None,
    }
    base.update(overrides)
    return base


def test_the_endpoint_returns_the_report(tmp_path):
    stub = _RelayStub(report=_report())
    with patch("src.api.printer_controller.relay_service", stub):
        response = printer_controller.send_relay_power_webhook({"action": "turn_on"})
    assert stub.calls == ["turn_on"]
    assert response["success"] is True
    assert response["mains_power"] == "on"


def test_the_endpoint_reports_a_refused_webhook_as_an_outcome(tmp_path):
    """A relay that said no is a result, not a server fault: 200, success false."""
    stub = _RelayStub(report=_report(success=False, response_status=502,
                                     mains_power="unknown",
                                     error="Relay webhook returned HTTP 502"))
    with patch("src.api.printer_controller.relay_service", stub):
        response = printer_controller.send_relay_power_webhook({"action": "turn_off"})
    assert response["success"] is False
    assert response["error"] == "Relay webhook returned HTTP 502"


@pytest.mark.parametrize("body", [{}, {"action": None}, {"action": 5}])
def test_the_endpoint_rejects_a_missing_or_non_string_action(body):
    stub = _RelayStub(report=_report())
    with patch("src.api.printer_controller.relay_service", stub):
        with pytest.raises(ValidationError):
            printer_controller.send_relay_power_webhook(body)
    assert stub.calls == [], "nothing was sent"


def test_the_endpoint_turns_a_refusal_into_a_bad_request():
    """Feature off, no URL, unknown action: a bad request, not a 500."""
    stub = _RelayStub(error=ValueError(
        "Relay power control is switched off, so nothing was sent."))
    with patch("src.api.printer_controller.relay_service", stub):
        with pytest.raises(ValidationError) as exc:
            printer_controller.send_relay_power_webhook({"action": "turn_on"})
    assert "switched off" in str(exc.value)
    assert exc.value.code == "VALIDATION_ERROR"


def test_the_endpoint_turns_an_unexpected_failure_into_a_printer_error():
    stub = _RelayStub(error=RuntimeError("the disk is on fire"))
    with patch("src.api.printer_controller.relay_service", stub):
        with pytest.raises(PrinterError):
            printer_controller.send_relay_power_webhook({"action": "turn_on"})


def test_the_status_endpoint_still_sends_nothing(tmp_path):
    """The read stayed a read; only the new endpoint acts."""
    stub = _RelayStub(report=_report())
    stub.status = lambda: {"enabled": True}
    with patch("src.api.printer_controller.relay_service", stub):
        assert printer_controller.get_relay_power_status() == {"enabled": True}
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# The chain as clock times
#
# The four moments used to be a diagram: offsets from an origin the client had
# to guess at. They are now a status display, which means each of them has to be
# an actual moment, has to be null when it does not exist, and has to say what
# the origin really was.
# --------------------------------------------------------------------------- #

def _chain(status):
    """The three moments after the origin, in the order they occur."""
    return [status["keep_alive_ends_at"],
            status["printer_power_off_at"],
            status["scheduled_turn_off_at"]]


def _iso_seconds(text):
    """Parse an ISO-8601 UTC string from the payload back into a timestamp."""
    return datetime.fromisoformat(text).timestamp()


# ---- the origin -------------------------------------------------------- #

def test_the_origin_starts_out_as_the_startup_fallback_not_a_print(tmp_path):
    """Nothing has printed, so the window runs from the process start time.

    That fallback is deliberate -- it gives keep-alive one window immediately --
    and it is exactly what must not be shown as "last print".
    """
    from src.services.printer_service import PrinterService

    printer = PrinterService(upload_folder=str(tmp_path / "uploads"))
    moment, printed = printer.last_print_origin()

    assert printed is False
    assert moment == pytest.approx(time.time(), abs=10)
    # And it is the same value the keep-alive worker measures its window from.
    assert moment == printer._last_print_at


def test_a_real_print_makes_the_origin_a_print(tmp_path):
    """Driven through the real print path, not by setting the flag by hand."""
    from PIL import Image

    from src.services import printer_service as printer_module

    printer = printer_module.PrinterService(upload_folder=str(tmp_path / "uploads"))
    started_at, printed = printer.last_print_origin()
    assert printed is False

    class _Backend:
        def __init__(self, _uri):
            pass

        def write(self, _instructions):
            pass

        def dispose(self):
            pass

    image_path = os.path.join(printer.upload_folder, "label.png")
    Image.new("RGB", (696, 120), "white").save(image_path)

    # The relay singleton is stubbed out so a test print cannot touch the real
    # data volume, and no webhook can leave the process.
    class _NoopRelay:
        def note_print_activity(self, origin=None):
            self.origin = origin

        def ensure_printer_powered(self):
            pass

    with patch.object(printer_module, "guess_backend", lambda _uri: "network"), \
            patch.object(printer_module, "backend_factory",
                         lambda _kind: {"backend_class": _Backend}), \
            patch.object(printer_module, "relay_service", _NoopRelay()):
        printer._send_to_printer(image_path, {
            "printer_uri": "tcp://192.0.2.10",
            "printer_model": "QL-800",
            "label_size": "62",
        })

    moment, printed = printer.last_print_origin()
    assert printed is True, "something really printed, so the origin is a print"
    assert moment >= started_at


def test_status_says_when_the_origin_is_only_the_startup_fallback(tmp_path):
    """A display reading this must be able to say "since the app started"."""
    started = time.time() - 600
    service = _make_service(tmp_path, origin=(started, False))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_STARTUP
    assert status["origin_at"] == pytest.approx(started)
    assert status["origin_at_iso"] == datetime.fromtimestamp(
        started, timezone.utc).isoformat()
    assert status["seconds_since_origin"] == pytest.approx(600, abs=5)
    # And there is no last print to report, which is the plainest way of saying
    # there has not been one.
    assert status["last_print_at"] is None
    assert status["last_print_at_iso"] is None
    assert status["seconds_since_last_print"] is None


def test_status_says_when_the_origin_is_a_real_print(tmp_path):
    printed_at = time.time() - 90
    service = _make_service(tmp_path, origin=(printed_at, True))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_PRINT
    assert status["origin_at"] == pytest.approx(printed_at)
    assert status["seconds_since_origin"] == pytest.approx(90, abs=5)
    # The chain is running from the print, so the two agree here.
    assert status["last_print_at"] == pytest.approx(printed_at)
    assert status["seconds_since_last_print"] == pytest.approx(90, abs=5)


def test_the_two_origins_are_told_apart_by_the_flag_and_nothing_else(tmp_path):
    """The timestamps are indistinguishable; only origin_source separates them."""
    moment = time.time() - 300
    startup = _make_service(tmp_path, origin=(moment, False)).status()
    printed = _make_service(tmp_path, name="printed.json",
                            origin=(moment, True)).status()

    assert startup["origin_at"] == printed["origin_at"]
    assert startup["origin_source"] != printed["origin_source"]
    # And the one field that answers "when did it last print" separates them too.
    assert startup["last_print_at"] is None
    assert printed["last_print_at"] == pytest.approx(moment)


def test_an_unreadable_origin_leaves_every_clock_time_null(tmp_path):
    """A chain drawn from a guessed origin would be wrong at every step."""
    def _broken():
        raise RuntimeError("the printer service is not up")

    service = _make_service(tmp_path, origin=_broken)
    status = service.status()

    assert status["origin_at"] is None
    assert status["origin_at_iso"] is None
    assert status["origin_source"] is None
    assert status["seconds_since_origin"] is None
    assert status["last_print_at"] is None
    assert status["keep_alive_ends_at"] is None
    assert status["printer_power_off_at"] is None
    assert status["next_step"] is None
    # The arithmetic is still reported: it does not depend on an origin.
    assert status["configured_window_seconds"] == 4 * HOUR


# ---- the moments, feature on and feature off --------------------------- #

def test_every_moment_of_the_chain_is_a_clock_time(tmp_path):
    """4 h window, 10 min device timer, 5 min margin: 0:00, 3:50, 4:00, 4:05."""
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    assert status["origin_at"] == pytest.approx(origin)
    assert status["keep_alive_ends_at"] == pytest.approx(origin + 3 * HOUR + 50 * 60)
    assert status["printer_power_off_at"] == pytest.approx(origin + 4 * HOUR)
    assert status["scheduled_turn_off_at"] == pytest.approx(origin + 4 * HOUR + 300)
    # In order, and the relay never opens before the printer has slept.
    assert [origin] + _chain(status) == sorted([origin] + _chain(status))


def test_the_clock_times_carry_the_iso_twin_of_every_moment(tmp_path):
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    for at_key, iso_key in (("origin_at", "origin_at_iso"),
                            ("keep_alive_ends_at", "keep_alive_ends_at_iso"),
                            ("printer_power_off_at", "printer_power_off_at_iso"),
                            ("scheduled_turn_off_at", "scheduled_turn_off_at_iso"),
                            ("next_step_at", "next_step_at_iso")):
        assert _iso_seconds(status[iso_key]) == pytest.approx(status[at_key], abs=1e-3)
        assert status[iso_key].endswith("+00:00"), "UTC, explicitly"


def test_the_clock_times_follow_the_hardware_offset_rule_with_the_feature_off(tmp_path):
    """The chain exists with relay control off, and it is a different chain.

    The heartbeat runs the whole window and the device's own timer then adds its
    interval on top, so the printer sleeps at 4:10 rather than 4:00. The absolute
    moments have to say that too, or the display goes back to contradicting
    hardware_offset_applied.
    """
    origin = time.time() - 60
    service = _make_service(
        tmp_path, origin=(origin, True),
        settings=_settings(relay_webhook_enabled=False,
                           relay_webhook_turn_off_enabled=False))

    status = service.status()

    assert status["hardware_offset_applied"] is False
    assert status["keep_alive_ends_at"] == pytest.approx(origin + 4 * HOUR)
    assert status["printer_power_off_at"] == pytest.approx(origin + 4 * HOUR + 600)
    # The two are not the same moment, and they differ by the device's interval.
    assert (status["printer_power_off_at"] - status["keep_alive_ends_at"]
            == pytest.approx(600))
    # Nothing will cut the power, so no moment is named for it.
    assert status["scheduled_turn_off_at"] is None
    assert status["scheduled_turn_off_at_iso"] is None
    assert status["seconds_until_turn_off"] is None


def test_with_the_feature_on_the_two_moments_are_the_subtraction(tmp_path):
    """The mirror of the case above: 3:50 and 4:00, ten minutes apart."""
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))

    status = service.status()

    assert status["hardware_offset_applied"] is True
    assert (status["printer_power_off_at"] - status["keep_alive_ends_at"]
            == pytest.approx(600))
    # And the device's own timer finishes at exactly the window that was asked
    # for, which is the whole point of the subtraction.
    assert status["printer_power_off_at"] == pytest.approx(
        status["origin_at"] + status["configured_window_seconds"])


def test_each_moment_is_its_offset_placed_on_the_clock(tmp_path):
    """The absolute view and the offsets it was built from never disagree."""
    origin = time.time() - 60
    for settings in (_settings(),
                     _settings(relay_webhook_enabled=False,
                               relay_webhook_turn_off_enabled=False)):
        service = _make_service(tmp_path, origin=(origin, True), settings=settings,
                                name=f"{settings['relay_webhook_enabled']}.json")
        status = service.status()
        assert status["keep_alive_ends_at"] == pytest.approx(
            status["origin_at"] + status["effective_keep_alive_seconds"])
        assert status["printer_power_off_at"] == pytest.approx(
            status["origin_at"] + status["printer_power_off_seconds"])


# ---- nulls where the chain does not apply ------------------------------ #

@pytest.mark.parametrize("overrides, why", [
    ({"keep_alive_mode": "forever"}, "no window ever expires"),
    ({"keep_alive_duration_seconds": 0}, "a zero window is no window"),
    ({"keep_alive_enabled": False}, "nothing is holding the window open"),
])
def test_no_running_window_means_no_moments_to_name(tmp_path, overrides, why):
    """A heartbeat that is not running does not stop at a time."""
    origin = time.time() - 60
    service = _make_service(
        tmp_path, origin=(origin, True),
        settings=_settings(relay_webhook_turn_off_enabled=False, **overrides))

    status = service.status()

    assert status["keep_alive_ends_at"] is None, why
    assert status["keep_alive_ends_at_iso"] is None
    assert status["seconds_until_keep_alive_end"] is None
    assert status["printer_power_off_at"] is None
    assert status["printer_power_off_at_iso"] is None
    assert status["seconds_until_printer_power_off"] is None
    assert status["scheduled_turn_off_at"] is None
    assert status["next_step"] is None
    assert status["next_step_at"] is None
    assert status["seconds_until_next_step"] is None
    # The origin is still real and still reported: something did start a clock.
    assert status["origin_at"] == pytest.approx(origin)


def test_keep_alive_switched_off_names_no_moment_even_though_the_window_exists(tmp_path):
    """The arithmetic still answers; the clock deliberately does not.

    effective_keep_alive_seconds describes the window a timed configuration
    *would* run and never consults the enabled flag, because the keep-alive
    worker's own rule must not change. A moment on the clock is a different
    claim: it says this will happen, at this time.
    """
    origin = time.time() - 60
    settings = _settings(keep_alive_enabled=False,
                         relay_webhook_turn_off_enabled=False)
    service = _make_service(tmp_path, origin=(origin, True), settings=settings)

    assert RelayPowerService.effective_keep_alive_seconds(settings) == 4 * HOUR - 600
    assert RelayPowerService.keep_alive_end_offset_seconds(settings) is None
    assert RelayPowerService.printer_power_off_offset_seconds(settings) is None
    assert service.status()["keep_alive_ends_at"] is None


def test_turn_off_switched_off_leaves_the_rest_of_the_chain_standing(tmp_path):
    """Only the last step goes away; the printer still sleeps on schedule."""
    origin = time.time() - 60
    service = _make_service(
        tmp_path, origin=(origin, True),
        settings=_settings(relay_webhook_turn_off_enabled=False))

    status = service.status()

    assert status["keep_alive_ends_at"] == pytest.approx(origin + 3 * HOUR + 50 * 60)
    assert status["printer_power_off_at"] == pytest.approx(origin + 4 * HOUR)
    assert status["scheduled_turn_off_at"] is None
    assert status["seconds_until_turn_off"] is None
    assert status["next_step"] == STEP_KEEP_ALIVE_END


def test_nothing_printed_yet_means_no_turn_off_moment_because_none_is_coming(tmp_path):
    """The honest case a startup origin produces, end to end.

    turn_off is armed by a print. With nothing printed since the app came up
    nothing is armed, and tick() will send nothing, so a time for it would be a
    time for something that is not going to happen. The two moments that *will*
    happen are still named: the heartbeat really does run its window from
    startup.
    """
    started = time.time() - 60
    sender = _Sender()
    service = _make_service(tmp_path, origin=(started, False), sender=sender)

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_STARTUP
    assert status["keep_alive_ends_at"] == pytest.approx(started + 3 * HOUR + 50 * 60)
    assert status["printer_power_off_at"] == pytest.approx(started + 4 * HOUR)
    assert status["scheduled_turn_off_at"] is None
    assert status["seconds_until_turn_off"] is None
    # And the schedule really is empty, so the null is the truth and not a gap.
    assert service.tick() is None
    assert sender.calls == []


# ---- absolute and relative are two views of one moment ----------------- #

def test_the_absolute_and_relative_views_of_every_moment_agree(tmp_path):
    """Each moment carries its own pair, so a countdown can be anchored on it.

    Comparing an absolute moment with the seconds-until for the *same* moment is
    how a client corrects for clock skew and then ticks locally without
    drifting; deriving three moments from one pair would spread that pair's
    error over all of them.
    """
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()
    now = status["server_time"]

    assert now == pytest.approx(time.time(), abs=5)
    assert status["seconds_since_origin"] == pytest.approx(now - status["origin_at"])
    for at_key, until_key in (
        ("keep_alive_ends_at", "seconds_until_keep_alive_end"),
        ("printer_power_off_at", "seconds_until_printer_power_off"),
        ("scheduled_turn_off_at", "seconds_until_turn_off"),
        ("next_step_at", "seconds_until_next_step"),
    ):
        assert status[until_key] == pytest.approx(max(0.0, status[at_key] - now))


def test_every_seconds_until_is_measured_from_the_same_instant(tmp_path):
    """One clock read per response, so the four numbers cannot disagree."""
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    assert (status["seconds_until_printer_power_off"]
            - status["seconds_until_keep_alive_end"]) == pytest.approx(600)
    assert (status["seconds_until_turn_off"]
            - status["seconds_until_printer_power_off"]) == pytest.approx(300)


def test_a_moment_that_has_passed_reads_zero_and_says_so_absolutely(tmp_path):
    """Clamped like seconds_until_turn_off always was, and nothing is lost.

    How long ago a step passed is still readable: server_time minus the moment.
    Taken part-way along the chain, where some steps have passed and the one
    still ahead keeps the origin where it is.
    """
    origin = time.time() - (4 * HOUR + 60)
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)   # turn_off still 4 min out

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_PRINT
    assert status["seconds_until_keep_alive_end"] == 0.0
    assert status["seconds_until_printer_power_off"] == 0.0
    assert status["keep_alive_ends_at"] < status["server_time"]
    assert (status["server_time"] - status["printer_power_off_at"]
            == pytest.approx(60, abs=5))
    assert status["seconds_until_turn_off"] > 0


# ---- which step is next ------------------------------------------------ #

def test_the_next_step_is_named_by_the_server(tmp_path):
    """Fresh print: the heartbeat stopping is what the chain is waiting on."""
    origin = time.time() - 60
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    assert status["next_step"] == STEP_KEEP_ALIVE_END
    assert status["next_step_at"] == pytest.approx(status["keep_alive_ends_at"])
    assert status["seconds_until_next_step"] == pytest.approx(
        status["seconds_until_keep_alive_end"])


@pytest.mark.parametrize("elapsed, expected", [
    (60, STEP_KEEP_ALIVE_END),                  # 0:01 -> the heartbeat stops next
    (3 * HOUR + 55 * 60, STEP_PRINTER_POWER_OFF),  # 3:55 -> it already stopped
    (4 * HOUR + 60, STEP_TURN_OFF),             # 4:01 -> the printer has slept
])
def test_the_next_step_advances_along_the_chain(tmp_path, elapsed, expected):
    origin = time.time() - elapsed
    service = _make_service(tmp_path, origin=(origin, True), name=f"{elapsed}.json")
    service.note_print_activity(origin)

    status = service.status()

    assert status["next_step"] == expected
    assert status["next_step_at"] > status["server_time"]


def test_there_is_no_next_step_when_there_is_no_chain_at_all(tmp_path):
    service = _make_service(
        tmp_path, origin=(time.time() - 60, True),
        settings=_settings(keep_alive_mode="forever",
                           relay_webhook_turn_off_enabled=False))

    status = service.status()

    assert status["next_step"] is None
    assert status["next_step_at"] is None
    assert status["next_step_at_iso"] is None
    assert status["seconds_until_next_step"] is None


def test_the_next_step_is_always_the_soonest_moment_still_ahead(tmp_path):
    """What the server named must be re-derivable from the timestamps.

    That is what keeps a client correct between polls: it can advance the
    highlight itself rather than waiting for the next response.
    """
    for elapsed in (60, 3 * HOUR + 55 * 60, 4 * HOUR + 60):
        origin = time.time() - elapsed
        service = _make_service(tmp_path, origin=(origin, True),
                                name=f"next_{elapsed}.json")
        service.note_print_activity(origin)
        status = service.status()

        ahead = sorted(moment for moment in _chain(status)
                       if moment is not None and moment > status["server_time"])
        if not ahead:
            assert status["next_step"] is None
        else:
            assert status["next_step_at"] == pytest.approx(ahead[0])


def test_the_next_step_never_names_a_step_the_settings_removed(tmp_path):
    """Which steps exist is settings logic, which is why the server answers it.

    Here the turn_off half is switched off, so the chain has three moments and
    not four however overdue the arithmetic looks. A client picking the soonest
    of three numbers it derived itself could easily name one that is never going
    to happen.
    """
    origin = time.time() - (3 * HOUR + 55 * 60)
    service = _make_service(
        tmp_path, origin=(origin, True),
        settings=_settings(relay_webhook_turn_off_enabled=False))
    status = service.status()
    assert status["next_step"] == STEP_PRINTER_POWER_OFF
    assert status["scheduled_turn_off_at"] is None
    assert status["seconds_until_turn_off"] is None


def test_status_is_still_a_read(tmp_path):
    """The clock times cost nothing: no probe, no webhook, no state written."""
    state_file = tmp_path / "quiet.json"
    service = RelayPowerService(
        state_file=str(state_file),
        settings_provider=_Settings(_settings()),
        reachability_probe=_forbidden_probe,
        pending_work_probe=_forbidden_probe,
        sender=_forbidden_sender,
        origin_provider=lambda: (time.time(), True),
    )
    status = service.status()
    assert status["keep_alive_ends_at"] is not None
    assert not state_file.exists()


# ---- a window that has run out re-bases to now ------------------------- #

def test_a_chain_that_has_run_out_is_shown_from_the_current_time(tmp_path):
    """A print three days ago must not leave a dead chain on the panel.

    Same rule the startup fallback is built on -- no window running, so start
    one from now -- applied a second time once the previous window expires.
    """
    origin = time.time() - (3 * 24 * HOUR)
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_IDLE
    assert status["origin_at"] == pytest.approx(status["server_time"])
    assert status["seconds_since_origin"] == pytest.approx(0, abs=1)
    # And the chain in front of it is live again.
    assert status["keep_alive_ends_at"] == pytest.approx(
        status["server_time"] + 3 * HOUR + 50 * 60)
    assert status["printer_power_off_at"] == pytest.approx(
        status["server_time"] + 4 * HOUR)
    assert status["next_step"] == STEP_KEEP_ALIVE_END


def test_re_basing_the_origin_never_hides_the_last_print(tmp_path):
    """The one question the re-base could have swallowed still has an answer."""
    printed_at = time.time() - (3 * 24 * HOUR)
    service = _make_service(tmp_path, origin=(printed_at, True))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_IDLE
    assert status["origin_at"] != pytest.approx(printed_at)
    assert status["last_print_at"] == pytest.approx(printed_at)
    assert status["last_print_at_iso"] == datetime.fromtimestamp(
        printed_at, timezone.utc).isoformat()
    assert status["seconds_since_last_print"] == pytest.approx(3 * 24 * HOUR, abs=5)


def test_nothing_ever_printed_re_bases_once_the_startup_window_runs_out(tmp_path):
    """The other half of the same rule: no last print, so take the current time.

    Up for five hours with nothing printed. The window the startup fallback
    opened closed an hour ago, so the chain restarts from now -- and there is
    still no last print to report, because there was none.
    """
    started = time.time() - (5 * HOUR)
    service = _make_service(tmp_path, origin=(started, False))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_IDLE
    assert status["origin_at"] == pytest.approx(status["server_time"])
    assert status["last_print_at"] is None
    assert status["seconds_since_last_print"] is None
    assert status["keep_alive_ends_at"] > status["server_time"]


def test_the_startup_window_is_left_alone_while_it_is_still_running(tmp_path):
    """It is a real window, not a placeholder, so it must not be re-based.

    The heartbeat genuinely runs from the process start time. Re-basing while it
    ran would freeze the countdown at a full window and quietly outlive the
    moment the heartbeat actually stops.
    """
    started = time.time() - HOUR
    service = _make_service(tmp_path, origin=(started, False))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_STARTUP
    assert status["origin_at"] == pytest.approx(started)
    assert status["keep_alive_ends_at"] == pytest.approx(
        started + 3 * HOUR + 50 * 60)


def test_a_scheduled_turn_off_still_ahead_holds_the_origin_where_it_is(tmp_path):
    """The chain has not run out while its last step is still to come."""
    origin = time.time() - (4 * HOUR + 60)     # turn_off due in another 4 min
    service = _make_service(tmp_path, origin=(origin, True))
    service.note_print_activity(origin)

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_PRINT
    assert status["origin_at"] == pytest.approx(origin)
    assert status["next_step"] == STEP_TURN_OFF


def test_an_idle_chain_is_never_given_a_turn_off_moment_it_has_not_been_armed_for(tmp_path):
    """SAFETY: the projection stops short of the step that cuts mains power.

    Everything else can be shown from now because nothing acts on it. A time for
    turn_off would be a time for a mains cut that nothing has armed, and the
    scheduler will not send one.
    """
    sender = _Sender()
    service = _make_service(tmp_path, origin=(time.time() - (3 * 24 * HOUR), True),
                            sender=sender)

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_IDLE
    assert status["scheduled_turn_off_at"] is None
    assert status["scheduled_turn_off_at_iso"] is None
    assert status["seconds_until_turn_off"] is None
    assert status["next_step"] != STEP_TURN_OFF
    # And the scheduler agrees: nothing is armed, so nothing goes out.
    assert service.tick() is None
    assert sender.calls == []


def test_an_overdue_turn_off_is_still_reported_while_the_chain_reads_idle(tmp_path):
    """A schedule that is armed and overdue is a fact, not a projection.

    It happens across a restart: the moment fell due while the app was down, and
    the scheduler's first tick has not run yet. The chain in front of it re-bases
    like any other expired window, but the armed moment is reported exactly as it
    stands -- it really is going to be sent, and it really is in the past.
    """
    origin = time.time() - (3 * 24 * HOUR)
    sender = _Sender()
    service = _make_service(tmp_path, origin=(origin, True), sender=sender)
    service.note_print_activity(origin)

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_IDLE
    assert status["scheduled_turn_off_at"] == pytest.approx(origin + 4 * HOUR + 300)
    assert status["scheduled_turn_off_at"] < status["server_time"], "overdue"
    assert status["seconds_until_turn_off"] == 0.0
    assert status["next_step"] == STEP_KEEP_ALIVE_END, "the past is not next"
    # And the scheduler does exactly what the payload implies on its next tick.
    assert service.tick() == ACTION_TURN_OFF
    assert sender.actions == [ACTION_TURN_OFF]


def test_nothing_is_re_based_when_there_is_no_window_to_run_out(tmp_path):
    """"forever" mode never expires, so the real origin stands however old."""
    origin = time.time() - (3 * 24 * HOUR)
    service = _make_service(
        tmp_path, origin=(origin, True),
        settings=_settings(keep_alive_mode="forever",
                           relay_webhook_turn_off_enabled=False))

    status = service.status()

    assert status["origin_source"] == ORIGIN_SOURCE_PRINT
    assert status["origin_at"] == pytest.approx(origin)
    assert status["last_print_at"] == pytest.approx(origin)
    assert status["keep_alive_ends_at"] is None


def test_the_re_base_does_not_touch_the_keep_alive_timing_itself(tmp_path):
    """It is a reporting decision, and it has to stay one.

    Re-basing the printer service's own timestamp would restart the heartbeat
    and hold the printer awake one window at a time forever, which is the
    opposite of what this feature is for.
    """
    from src.services.printer_service import PrinterService

    printer = PrinterService(upload_folder=str(tmp_path / "uploads"))
    printer._last_print_at = time.time() - (3 * 24 * HOUR)
    service = _make_service(tmp_path, origin=printer.last_print_origin)

    assert service.status()["origin_source"] == ORIGIN_SOURCE_IDLE
    # Untouched, so the worker still sees a window that expired days ago.
    assert printer.last_print_origin()[0] == pytest.approx(
        time.time() - (3 * 24 * HOUR), abs=5)


# ---- the payload against the spec -------------------------------------- #

def _relay_status_schema():
    """The RelayPowerStatus schema, straight out of the OpenAPI document."""
    yaml_module = pytest.importorskip("yaml")
    spec_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "api", "openapi.yaml")
    with open(spec_path, encoding="utf-8") as handle:
        spec = yaml_module.safe_load(handle)
    return spec["components"]["schemas"]["RelayPowerStatus"]


def test_every_field_the_status_returns_is_declared_in_the_spec(tmp_path):
    """The spec is the contract the UI is built against, so it has to be whole."""
    service = _make_service(tmp_path, origin=(time.time(), True))
    service.note_print_activity()
    declared = set(_relay_status_schema()["properties"])
    assert set(service.status()) == declared


def test_the_declared_enums_are_strings_and_not_yaml_booleans():
    """YAML reads bare on, off, yes and no as booleans.

    That silently turned an enum into [true, false, "unknown"] once and made
    every real response non-conforming. Anything of that shape has to stay
    quoted, and this is the check that notices when it does not.
    """
    yaml_module = pytest.importorskip("yaml")
    spec_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "api", "openapi.yaml")
    with open(spec_path, encoding="utf-8") as handle:
        spec = yaml_module.safe_load(handle)

    for name in ("RelayPowerStatus", "RelayPowerSendRequest", "RelayPowerSendResponse"):
        for field, schema in spec["components"]["schemas"][name]["properties"].items():
            for value in schema.get("enum", []):
                assert value is None or isinstance(value, str), (
                    f"{name}.{field} has a non-string enum member {value!r}; "
                    "quote it in the YAML")


@pytest.mark.parametrize("field", ["origin_source", "next_step"])
def test_the_values_the_status_reports_are_the_values_the_spec_allows(tmp_path, field):
    declared = _relay_status_schema()["properties"][field]["enum"]
    seen = set()
    for elapsed, origin_settings in ((60, _settings()),
                                     (3 * HOUR + 55 * 60, _settings()),
                                     (4 * HOUR + 60, _settings()),
                                     (5 * HOUR, _settings()),
                                     (60, _settings(keep_alive_mode="forever",
                                                    relay_webhook_turn_off_enabled=False))):
        for printed in (True, False):
            origin = time.time() - elapsed
            service = _make_service(tmp_path, origin=(origin, printed),
                                    settings=origin_settings,
                                    name=f"{field}_{elapsed}_{printed}.json")
            service.note_print_activity(origin)
            seen.add(service.status()[field])
    assert seen, "the sweep produced no values at all"
    assert seen <= set(declared)
