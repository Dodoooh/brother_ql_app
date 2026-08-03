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
from unittest.mock import patch

import pytest

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
    RelayPowerService,
)
from src.services.settings_service import SettingsService
from src.utils.exceptions import RelayWebhookError
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
    """Records webhook sends; optionally fails."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if self.error is not None:
            raise self.error

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
                  sender=None, name="relay_power.json"):
    """Build a fully injected RelayPowerService with a temporary state file."""
    service = RelayPowerService(
        state_file=str(tmp_path / name),
        settings_provider=_Settings(settings if settings is not None else _settings()),
        reachability_probe=(reachable if callable(reachable) else (lambda _s: reachable)),
        pending_work_probe=(pending if callable(pending) else (lambda: pending)),
        sender=sender if sender is not None else _Sender(),
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

    A turn-off moment is refused when keep-alive is not running -- there is no
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

    # The state file is real, readable JSON -- not an in-memory nicety.
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
    survives a restart -- the whole point of persisting it."""
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
    """The job stays "queued" -- not "printing", not failed -- while it waits."""
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
    # It has to name the danger plainly, not hint at it.
    assert "cannot read" in warning
    assert "LONGER" in warning
    assert "damage the printer" in warning


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
