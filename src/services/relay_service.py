"""
Relay power control for the printer's mains supply, driven by a webhook.

What this is for
----------------
A Brother QL sitting idle on a shelf still draws power. If its mains supply runs
through a relay -- a Shelly, a Tasmota plug, an ESPHome switch, a Node-RED flow
in front of any of them -- the app can switch the printer on when a job needs it
and off again once everything has wound down. Two events exist:

``turn_on``
    A print job arrives and the printer does not answer. The webhook fires, the
    job *waits in the queue* while the printer boots, and then prints. A job is
    never failed for arriving at a printer that was merely switched off.

``turn_off``
    Optional. Sent once the configured window has closed and nothing is left to
    print.

The timing chain
----------------
The printer has its own auto-power-off timer. The app can neither read it nor
set it, but it is told what it is, and that number is *subtracted from the
keep-alive window* so the total the user configured is the total they get::

    0:00   last print, keep-alive running
    3:50   keep-alive stops              (configured 4 h minus 10 hardware min)
    4:00   the printer powers itself off (exactly the configured 4 h)
    4:05   turn_off is sent              (plus the safety margin; already off)

So the keep-alive heartbeat's effective window is
``keep_alive_duration_seconds - printer_auto_power_off_minutes * 60`` and the
turn-off moment is ``keep_alive_duration_seconds + delay`` -- both measured from
the same origin, the last print.

Two consequences fall out of that arithmetic and are handled explicitly:

* ``duration == hardware`` is valid and intended. The effective keep-alive
  window is zero, the heartbeat does nothing at all, and the printer's own timer
  carries the entire window. Nothing about that is a misconfiguration.
* ``duration < hardware`` cannot be expressed -- the window would have to end
  before it began -- and is rejected at settings-validation time with a message
  saying so, rather than clamped to something that would quietly not be what was
  asked for.

Safety
------
* ``turn_off`` never fires while a job is queued or printing. Pending work
  resets the clock, and that holds whatever the settings say.
* The scheduled turn-off moment is persisted next to the settings file. A
  restart in the middle of a window picks it up again; without that, a restart
  would strand the relay in the on state forever.
* Delivery failures are raised (``turn_on``) or recorded and logged at error
  level (``turn_off``). A webhook that did not arrive is never treated as one
  that did.
* URLs go through :func:`validate_webhook_url`, which permits LAN addresses --
  a relay is on the LAN -- while still refusing link-local and cloud metadata
  endpoints.

No new dependency: the request is a plain ``urllib.request`` POST. The app does
not ship ``requests``, and a webhook is one POST of a small JSON body.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import structlog

from src.services.settings_service import settings_service
from src.utils.exceptions import RelayWebhookError
from src.utils.uri_validation import validate_webhook_url

try:
    from src.config.default_settings import (
        AUTO_POWER_OFF_MISMATCH_WARNING,
        DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES,
        DEFAULT_TURN_OFF_DELAY_MINUTES,
    )
except ImportError:  # pragma: no cover - mirrors settings_service's fallback
    AUTO_POWER_OFF_MISMATCH_WARNING = (
        "This app cannot read or change the printer's built-in auto-power-off time."
    )
    DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES = 10
    DEFAULT_TURN_OFF_DELAY_MINUTES = 5

logger = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Named constants, deliberately not settings.
#
# These are properties of how a mains-switched label printer comes up, not
# preferences. A user who could set them would have to guess at numbers whose
# only correct value is "long enough for this class of device to boot", and a
# wrong guess turns into print jobs that fail for no visible reason.
# --------------------------------------------------------------------------- #

# How long to wait for the printer to answer after the FIRST turn_on webhook.
#
# Mains-on to network-reachable on a QL-810W/820NWB is dominated by Wi-Fi
# association after the boot itself, and 25-35 s is the range actually observed;
# wired models are quicker. 45 s clears that with margin. It is not expensive to
# be generous here: by this point the HTTP request that queued the job has long
# since returned, so the only thing waiting is the queue worker.
TURN_ON_FIRST_WAIT_SECONDS = 45.0

# How long to wait after the SECOND turn_on webhook. Shorter, because the relay
# has already been closed for the whole first window -- this attempt exists for
# the case where the first request was accepted but not acted on (a relay that
# dropped it, a flow that was mid-restart), not for a printer that is simply
# slow. 30 s puts the worst case at 75 s total, which is a bounded wait a user
# can be told about rather than an open-ended one.
TURN_ON_SECOND_WAIT_SECONDS = 30.0

# How often the printer is re-probed while waiting. The waits above are ceilings,
# not fixed delays: a printer that comes up in 12 s starts printing at ~12 s.
TURN_ON_POLL_INTERVAL_SECONDS = 3.0

# Socket timeout for a single webhook POST. A relay bridge on the LAN answers in
# milliseconds; ten seconds is the point past which it is not going to answer.
WEBHOOK_TIMEOUT_SECONDS = 10.0

# How often the scheduler re-evaluates the turn-off moment. The moment itself is
# hours away and the margin around it is minutes, so a 15 s granularity is far
# finer than the decision needs and costs one settings read per tick.
SCHEDULER_TICK_SECONDS = 15.0

# Optional bearer/credential for the webhook, read from the environment rather
# than from settings. It is a secret, and settings.json is world-readable in the
# data volume and is returned verbatim by GET /settings -- so storing it there
# would publish it to every client that can read the configuration. An env var
# keeps the endpoint securable without the app ever holding the secret somewhere
# it hands out. Sent verbatim as the Authorization header when set.
AUTHORIZATION_ENV_VAR = "RELAY_WEBHOOK_AUTHORIZATION"

# Actions carried in the webhook body.
ACTION_TURN_ON = "turn_on"
ACTION_TURN_OFF = "turn_off"

# Name of the state file, written beside settings.json.
STATE_FILENAME = "relay_power.json"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort int conversion that never raises."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RelayPowerService:
    """Switches the printer's mains supply through a webhook-driven relay."""

    def __init__(
        self,
        state_file: Optional[str] = None,
        settings_provider: Any = None,
        reachability_probe: Optional[Callable[[Dict[str, Any]], bool]] = None,
        pending_work_probe: Optional[Callable[[], bool]] = None,
        sender: Optional[Callable[[str, Dict[str, Any], float], None]] = None,
    ) -> None:
        """Initialise the service.

        Every collaborator is injectable so the tests can drive the whole
        feature without a network, a printer or a queue. The defaults resolve
        lazily to the application singletons.

        Args:
            state_file: Where the scheduled turn-off moment is persisted.
                Defaults to ``relay_power.json`` beside the settings file.
            settings_provider: Object exposing ``get_settings()``.
            reachability_probe: ``probe(settings) -> bool``, "is the printer
                answering right now".
            pending_work_probe: ``probe() -> bool``, "is anything queued or
                printing".
            sender: ``sender(url, payload, timeout) -> None``, performing the
                POST and raising on any delivery failure.
        """
        self._settings_provider = settings_provider or settings_service
        self._reachability_probe = reachability_probe or self._default_reachability_probe
        self._pending_work_probe = pending_work_probe or self._default_pending_work_probe
        self._sender = sender or self._default_sender

        if state_file is None:
            settings_file = getattr(self._settings_provider, "settings_file", "")
            state_dir = os.path.dirname(settings_file) or "."
            state_file = os.path.join(state_dir, STATE_FILENAME)
        self.state_file = state_file

        # Waits are instance attributes rather than direct constant reads so a
        # test can shrink them without patching module state.
        self.turn_on_waits = (TURN_ON_FIRST_WAIT_SECONDS, TURN_ON_SECOND_WAIT_SECONDS)
        self.poll_interval = TURN_ON_POLL_INTERVAL_SECONDS
        self.webhook_timeout = WEBHOOK_TIMEOUT_SECONDS
        self.tick_interval = SCHEDULER_TICK_SECONDS

        # Guards the in-memory mirror of the persisted schedule.
        self._lock = threading.Lock()
        self._turn_off_at: Optional[float] = None
        self._state_loaded = False

        # Last outcome, surfaced by :meth:`status` so a delivery failure is
        # visible in the UI and not only in the log.
        self._last_action: Optional[str] = None
        self._last_action_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None

        # Scheduler thread.
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._start_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Settings readers
    # ------------------------------------------------------------------ #
    def _settings(self) -> Dict[str, Any]:
        """Return the current settings (fresh; the provider caches for us)."""
        return self._settings_provider.get_settings()

    @staticmethod
    def is_enabled(settings: Dict[str, Any]) -> bool:
        """Whether relay power control is switched on at all."""
        return bool(settings.get("relay_webhook_enabled", False))

    @staticmethod
    def turn_on_url(settings: Dict[str, Any]) -> str:
        """The URL POSTed to wake the printer, or "" when none is configured."""
        return str(settings.get("relay_webhook_turn_on_url") or "").strip()

    @classmethod
    def turn_off_url(cls, settings: Dict[str, Any]) -> str:
        """The URL POSTed to cut power.

        Falls back to the ``turn_on`` URL when no separate one is configured,
        which is the correct behaviour for a relay that switches on the body it
        is sent rather than on the address it is called at.
        """
        explicit = str(settings.get("relay_webhook_turn_off_url") or "").strip()
        return explicit or cls.turn_on_url(settings)

    @staticmethod
    def turn_off_enabled(settings: Dict[str, Any]) -> bool:
        """Whether the ``turn_off`` half of the feature is switched on."""
        return bool(settings.get("relay_webhook_turn_off_enabled", False))

    @staticmethod
    def hardware_power_off_seconds(settings: Dict[str, Any]) -> int:
        """The printer's own auto-power-off interval, in seconds.

        This is what the user says the device is set to. Nothing verifies it --
        see ``AUTO_POWER_OFF_MISMATCH_WARNING``.
        """
        minutes = _as_int(
            settings.get("printer_auto_power_off_minutes",
                         DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES),
            DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES,
        )
        return max(0, minutes) * 60

    @staticmethod
    def turn_off_delay_seconds(settings: Dict[str, Any]) -> int:
        """The safety margin between the window closing and ``turn_off``."""
        minutes = _as_int(
            settings.get("relay_webhook_turn_off_delay_minutes",
                         DEFAULT_TURN_OFF_DELAY_MINUTES),
            DEFAULT_TURN_OFF_DELAY_MINUTES,
        )
        return max(0, minutes) * 60

    @staticmethod
    def timed_window_seconds(settings: Dict[str, Any]) -> Optional[int]:
        """The timed keep-alive window as configured, or None when there is none.

        This is exactly the condition the keep-alive worker has always used --
        ``mode == "timed"`` with a non-zero duration -- and deliberately does NOT
        also test ``keep_alive_enabled``. The worker only runs while keep-alive
        is on, so the extra test would change nothing in practice; but were the
        flag ever out of step with the running thread, adding it here would flip
        the worker from "pause outside the window" to "ping forever", which is
        the wrong way for that edge to fail.
        """
        if settings.get("keep_alive_mode", "forever") != "timed":
            return None
        duration = _as_int(settings.get("keep_alive_duration_seconds", 0))
        return duration if duration > 0 else None

    @classmethod
    def configured_window_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """The window a turn-off moment may be measured from, or None.

        Stricter than :meth:`timed_window_seconds` by one condition: keep-alive
        must actually be enabled. The turn-off moment is derived from the end of
        a window the keep-alive heartbeat is holding open, so if nothing is
        holding it open there is no such moment -- and scheduling mains power to
        be cut on the strength of a window that is not running is precisely the
        case worth refusing. Settings validation rejects the combination too;
        this is the same rule at the point of use.
        """
        if not settings.get("keep_alive_enabled", False):
            return None
        return cls.timed_window_seconds(settings)

    @classmethod
    def effective_keep_alive_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """How long the keep-alive heartbeat should actually run after a print.

        Returns None when no timed window applies, in which case the heartbeat
        runs continuously exactly as it always has.

        With relay power control ON, the printer's own auto-power-off interval
        is subtracted, so the device switches itself off at the moment the user
        configured rather than that moment plus its own timer. A result of zero
        is a real answer, not a disabled feature: the heartbeat sends nothing
        and the hardware carries the whole window.

        With relay power control OFF this returns the configured duration
        unchanged. The subtraction is part of the relay feature, and an install
        that does not use it must keep the keep-alive timing it always had.
        """
        window = cls.timed_window_seconds(settings)
        if window is None:
            return None
        if not cls.is_enabled(settings):
            return window
        return max(0, window - cls.hardware_power_off_seconds(settings))

    @classmethod
    def turn_off_offset_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """Seconds from the last print until ``turn_off`` should be sent.

        This is the *configured* window plus the safety margin -- deliberately
        not the effective (shortened) keep-alive window. Both are measured from
        the same origin: keep-alive stops early precisely so the hardware's own
        timer finishes at the configured moment, and the relay opens a margin
        after that.

        Returns None when no turn-off should be scheduled at all.
        """
        if not cls.is_enabled(settings) or not cls.turn_off_enabled(settings):
            return None
        window = cls.configured_window_seconds(settings)
        if window is None:
            return None
        return window + cls.turn_off_delay_seconds(settings)

    # ------------------------------------------------------------------ #
    # Persisted schedule
    # ------------------------------------------------------------------ #
    def _read_state_file(self) -> Optional[float]:
        """Read the persisted turn-off moment, or None.

        Best-effort: a missing, unreadable or malformed file simply means "no
        schedule", which is the safe reading -- it can only delay a turn-off,
        never cause one.
        """
        try:
            with open(self.state_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("Could not read relay power state, ignoring it",
                           path=self.state_file, error=str(e))
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("turn_off_at")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _write_state_file(self, turn_off_at: Optional[float]) -> None:
        """Persist the turn-off moment atomically (temp file + os.replace).

        Written the same way settings are, for the same reason: a half-written
        state file read after a crash would be a schedule nobody chose.
        """
        payload = {
            "turn_off_at": turn_off_at,
            "turn_off_at_iso": (
                datetime.fromtimestamp(turn_off_at, timezone.utc).isoformat()
                if turn_off_at is not None else None
            ),
            "updated_at": _now_iso(),
        }
        temp_path = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_file)
        except OSError as e:
            logger.warning("Could not persist relay power state",
                           path=self.state_file, error=str(e))
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    def scheduled_turn_off_at(self) -> Optional[float]:
        """The moment ``turn_off`` is due, as a unix timestamp, or None.

        The first call after construction loads the persisted value, which is
        what makes a schedule survive a restart: the process that armed it is
        gone, but the moment it chose is still on disk and is honoured -- or, if
        the app was down past it, acted on immediately.
        """
        with self._lock:
            if not self._state_loaded:
                self._turn_off_at = self._read_state_file()
                self._state_loaded = True
                if self._turn_off_at is not None:
                    logger.info("Recovered relay turn-off schedule from disk",
                                turn_off_at=self._turn_off_at, path=self.state_file)
            return self._turn_off_at

    def arm(self, origin: Optional[float] = None) -> Optional[float]:
        """Schedule ``turn_off`` for ``origin`` plus the configured offset.

        Args:
            origin: The moment the window starts from (the last print).
                Defaults to now.

        Returns:
            The scheduled moment, or None when nothing should be scheduled (in
            which case any existing schedule is cleared).
        """
        settings = self._settings()
        offset = self.turn_off_offset_seconds(settings)
        if offset is None:
            self.disarm("turn_off is not configured")
            return None
        moment = (time.time() if origin is None else origin) + offset
        with self._lock:
            self._turn_off_at = moment
            self._state_loaded = True
            self._write_state_file(moment)
        logger.debug("Relay turn-off scheduled", turn_off_at=moment, offset_seconds=offset)
        return moment

    def disarm(self, reason: str = "") -> None:
        """Clear any scheduled turn-off, on disk as well as in memory.

        Writes nothing when there was nothing scheduled, so an installation with
        the feature switched off never touches the filesystem on account of it.
        """
        # Outside the lock: this loads the persisted value on first use, and the
        # lock is not reentrant.
        current = self.scheduled_turn_off_at()
        with self._lock:
            self._turn_off_at = None
            self._state_loaded = True
            if current is not None:
                self._write_state_file(None)
        if reason and current is not None:
            logger.debug("Relay turn-off schedule cleared", reason=reason)

    # ------------------------------------------------------------------ #
    # Webhook delivery
    # ------------------------------------------------------------------ #
    @staticmethod
    def build_payload(action: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Build the JSON body of a webhook request.

        ``action`` is the load-bearing field and the only one a relay flow needs
        to branch on; the rest is context, so a Node-RED flow shared between two
        printers can tell which one asked. Kept flat and small on purpose -- the
        shape is documented in the OpenAPI spec and the UI, and every field
        added is a field somebody's flow may come to depend on.
        """
        return {
            "action": action,
            "source": "brother_ql_app",
            "printer_uri": settings.get("printer_uri", ""),
            "printer_model": settings.get("printer_model", ""),
            "timestamp": _now_iso(),
        }

    def _default_sender(self, url: str, payload: Dict[str, Any], timeout: float) -> None:
        """POST ``payload`` as JSON to ``url``.

        Raises:
            RelayWebhookError: On any transport error, timeout or non-2xx
                response. Everything is surfaced -- a webhook whose delivery
                cannot be confirmed is treated as not delivered.
        """
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "brother_ql_app",
        }
        authorization = os.environ.get(AUTHORIZATION_ENV_VAR)
        if authorization:
            headers["Authorization"] = authorization

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status is not None and not (200 <= int(status) < 300):
                    raise RelayWebhookError(
                        f"Relay webhook returned HTTP {status}", "RELAY_WEBHOOK_ERROR",
                        {"url": url, "status": int(status)})
        except urllib.error.HTTPError as e:
            raise RelayWebhookError(
                f"Relay webhook returned HTTP {e.code}", "RELAY_WEBHOOK_ERROR",
                {"url": url, "status": e.code}) from e
        except urllib.error.URLError as e:
            raise RelayWebhookError(
                f"Relay webhook could not be reached: {e.reason}", "RELAY_WEBHOOK_ERROR",
                {"url": url}) from e
        except RelayWebhookError:
            raise
        except Exception as e:  # noqa: BLE001 - timeouts and socket errors alike
            raise RelayWebhookError(
                f"Relay webhook failed: {e}", "RELAY_WEBHOOK_ERROR",
                {"url": url}) from e

    def send(self, action: str, settings: Optional[Dict[str, Any]] = None) -> None:
        """Send one webhook.

        Args:
            action: ``turn_on`` or ``turn_off``.
            settings: Settings to read the URL from; loaded when omitted.

        Raises:
            RelayWebhookError: When no URL is configured for the action, when
                the URL fails validation, or when delivery fails.
        """
        settings = self._settings() if settings is None else settings
        url = (self.turn_on_url(settings) if action == ACTION_TURN_ON
               else self.turn_off_url(settings))
        if not url:
            raise RelayWebhookError(
                f"No relay webhook URL is configured for '{action}'.",
                "RELAY_WEBHOOK_ERROR", {"action": action})

        # Defence in depth: the URL was validated when it was saved, but it is
        # validated again immediately before the request, exactly as a printer
        # URI is before it reaches a backend.
        try:
            validate_webhook_url(url)
        except ValueError as e:
            raise RelayWebhookError(
                f"Refusing to call the relay webhook: {e}", "RELAY_WEBHOOK_ERROR",
                {"action": action}) from e

        payload = self.build_payload(action, settings)
        try:
            self._sender(url, payload, self.webhook_timeout)
        except RelayWebhookError as e:
            self._record_error(str(e))
            logger.error("Relay webhook failed", action=action, error=str(e))
            raise
        except Exception as e:  # noqa: BLE001 - an injected sender may raise anything
            message = f"Relay webhook failed: {e}"
            self._record_error(message)
            logger.error("Relay webhook failed", action=action, error=str(e))
            raise RelayWebhookError(message, "RELAY_WEBHOOK_ERROR",
                                    {"action": action}) from e

        with self._lock:
            self._last_action = action
            self._last_action_at = _now_iso()
            self._last_error = None
            self._last_error_at = None
        logger.info("Relay webhook sent", action=action)

    def _record_error(self, message: str) -> None:
        """Remember the most recent failure so the API can report it."""
        with self._lock:
            self._last_error = message
            self._last_error_at = _now_iso()

    # ------------------------------------------------------------------ #
    # Default collaborators
    # ------------------------------------------------------------------ #
    def _default_reachability_probe(self, settings: Dict[str, Any]) -> bool:
        """Ask the printer service whether the printer is answering."""
        # Imported lazily: the printer service imports this module, so a
        # top-level import here would be a cycle.
        from src.services.printer_service import printer_service

        printer_uri = settings.get("printer_uri", "")
        if not printer_uri:
            return False
        try:
            status = printer_service.check_printer_status(
                printer_uri, settings.get("printer_model", ""))
            return bool(status.get("reachable"))
        except Exception as e:  # noqa: BLE001 - an error here means "not answering"
            logger.debug("Reachability probe failed", error=str(e))
            return False

    def _default_pending_work_probe(self) -> bool:
        """Whether the print queue holds anything queued or printing."""
        from src.services.queue_service import print_queue

        try:
            status = print_queue.queue_status()
        except Exception as e:  # noqa: BLE001 - unknown means "assume busy"
            logger.warning("Could not read queue status; treating it as busy",
                           error=str(e))
            return True
        return bool(status.get("queued") or status.get("printing"))

    # ------------------------------------------------------------------ #
    # turn_on
    # ------------------------------------------------------------------ #
    def _wait_for_printer(self, settings: Dict[str, Any], seconds: float) -> bool:
        """Poll for reachability for up to ``seconds``.

        Returns as soon as the printer answers, so the wait constants above are
        ceilings rather than fixed delays.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._stop_event.wait(min(self.poll_interval, max(0.0, deadline - time.monotonic()))):
                # Shutting down; report the last known truth rather than looping.
                return self._reachability_probe(settings)
            if self._reachability_probe(settings):
                return True
            if time.monotonic() >= deadline:
                return False

    def ensure_printer_powered(self) -> None:
        """Make sure the printer is powered before a job is started.

        This is the queue's pre-job gate. It returns quietly in the overwhelming
        majority of cases: the feature off, no URL configured, or a printer that
        is already answering. Only an unreachable printer at an enabled,
        configured relay causes a webhook.

        The sequence is send, wait, send again, wait a shorter time, then fail
        naming what happened. A *delivery* failure is raised straight away
        instead of being retried past: retrying a URL that refused the
        connection mostly buys a slower, vaguer error, and the requirement is
        that a webhook which did not arrive is reported rather than swallowed.

        Raises:
            RelayWebhookError: When the webhook cannot be delivered, or when the
                printer never answers. The job then fails carrying that message,
                which is the only place a user would look for it.
        """
        settings = self._settings()

        # Nothing configured -> return before probing anything. The feature
        # being off must mean no outbound request AND no extra work at all, so
        # an install that ignores it behaves exactly as it did before.
        if not self.is_enabled(settings):
            return
        url = self.turn_on_url(settings)
        if not url:
            logger.warning("Relay power control is enabled but no turn_on URL is "
                           "configured; not switching the printer on")
            return

        if self._reachability_probe(settings):
            logger.debug("Printer already reachable; no relay turn_on needed")
            return

        logger.info("Printer not reachable; switching it on via the relay")
        waits = tuple(self.turn_on_waits)
        for attempt, wait_seconds in enumerate(waits, start=1):
            self.send(ACTION_TURN_ON, settings)
            logger.info("Relay turn_on sent, waiting for the printer",
                        attempt=attempt, wait_seconds=wait_seconds)
            if self._wait_for_printer(settings, wait_seconds):
                logger.info("Printer answered after relay turn_on", attempt=attempt)
                # Arm from here rather than waiting for the print to land: if
                # the job goes on to fail, the relay must still be scheduled to
                # switch off instead of being left on indefinitely.
                self.arm()
                return

        total = sum(waits)
        message = (
            f"Printer did not answer within {total:.0f}s of the relay being switched on "
            f"({len(waits)} turn_on webhook(s) delivered to {url}, "
            f"re-checked every {self.poll_interval:.0f}s). The webhook was accepted, so "
            "either the relay did not close, the printer is not on that outlet, or it "
            "takes longer than this to come up."
        )
        self._record_error(message)
        logger.error("Printer did not come up after relay turn_on",
                     attempts=len(waits), total_wait_seconds=total)
        raise RelayWebhookError(message, "RELAY_WEBHOOK_ERROR",
                                {"action": ACTION_TURN_ON, "attempts": len(waits)})

    # ------------------------------------------------------------------ #
    # turn_off
    # ------------------------------------------------------------------ #
    def note_print_activity(self, origin: Optional[float] = None) -> None:
        """Record that a print happened, restarting the turn-off clock.

        Called from the print path next to the keep-alive timestamp so both
        windows are measured from exactly the same origin. Never raises: a
        scheduling problem must not fail a print that already succeeded.
        """
        try:
            self.arm(origin)
        except Exception as e:  # noqa: BLE001 - never let this break a print
            logger.warning("Could not schedule the relay turn-off", error=str(e))

    def tick(self, now: Optional[float] = None) -> Optional[str]:
        """Run one scheduler step.

        Returns:
            ``"turn_off"`` when the webhook was sent, ``"deferred"`` when the
            clock was reset because work is pending, ``"cleared"`` when a stale
            schedule was dropped, and None when there was nothing to do.
        """
        now = time.time() if now is None else now
        try:
            settings = self._settings()
        except Exception as e:  # noqa: BLE001 - a settings read must not kill the thread
            logger.warning("Relay scheduler could not read settings", error=str(e))
            return None

        if self.turn_off_offset_seconds(settings) is None:
            # The feature or its turn_off half was switched off, or the timed
            # window went away. A schedule made under the old configuration must
            # not outlive it.
            if self.scheduled_turn_off_at() is not None:
                self.disarm("turn_off is no longer configured")
                return "cleared"
            return None

        # SAFETY: never cut power while anything is queued or printing, whatever
        # the rest of the configuration says. Pending work also pushes the
        # moment out, so the window is measured from the end of the work rather
        # than from a print that happened before it.
        if self._pending_work_probe():
            self.arm(now)
            return "deferred"

        scheduled = self.scheduled_turn_off_at()
        if scheduled is None or now < scheduled:
            return None

        try:
            self.send(ACTION_TURN_OFF, settings)
        except RelayWebhookError:
            # Already recorded and logged by send(). The schedule is cleared
            # regardless: retrying a failing endpoint every 15 seconds for hours
            # would bury the log without ever being more likely to work, and the
            # failure is visible on the status endpoint.
            self.disarm("turn_off delivery failed")
            return None
        self.disarm("turn_off sent")
        return ACTION_TURN_OFF

    # ------------------------------------------------------------------ #
    # Scheduler thread
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the scheduler thread (idempotent).

        Ticks once immediately, which is what performs restart recovery: a
        moment that fell due while the app was down is acted on at boot instead
        of leaving the relay on forever.
        """
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="relay-power-scheduler", daemon=True)
            self._thread.start()
            logger.info("Relay power scheduler started",
                        state_file=self.state_file, tick_seconds=self.tick_interval)

    def stop(self) -> None:
        """Stop the scheduler thread."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        """Scheduler loop: evaluate the turn-off moment on every tick."""
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the thread must survive anything
                logger.error("Relay power scheduler tick failed", error=str(e),
                             exc_info=True)
            if self._stop_event.wait(self.tick_interval):
                break
        logger.info("Relay power scheduler stopped")

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        """Return a summary of the feature for the API and the UI."""
        settings = self._settings()
        enabled = self.is_enabled(settings)
        turn_off_enabled = self.turn_off_enabled(settings)
        scheduled = self.scheduled_turn_off_at() if enabled and turn_off_enabled else None
        effective = self.effective_keep_alive_seconds(settings)

        with self._lock:
            last_action = self._last_action
            last_action_at = self._last_action_at
            last_error = self._last_error
            last_error_at = self._last_error_at

        payload: Dict[str, Any] = {
            "enabled": enabled,
            "turn_off_enabled": turn_off_enabled,
            "turn_on_url_configured": bool(self.turn_on_url(settings)),
            "turn_off_url_configured": bool(self.turn_off_url(settings)),
            "authorization_configured": bool(os.environ.get(AUTHORIZATION_ENV_VAR)),
            "printer_auto_power_off_minutes":
                self.hardware_power_off_seconds(settings) // 60,
            "configured_window_seconds": self.configured_window_seconds(settings),
            "effective_keep_alive_seconds": effective,
            "turn_off_delay_seconds": self.turn_off_delay_seconds(settings),
            "scheduled_turn_off_at": scheduled,
            "seconds_until_turn_off": (
                max(0.0, scheduled - time.time()) if scheduled is not None else None
            ),
            "last_action": last_action,
            "last_action_at": last_action_at,
            "last_error": last_error,
            "last_error_at": last_error_at,
        }
        # The warning rides along with the status so the UI has one authoritative
        # copy of the wording rather than its own paraphrase. It is sent
        # unconditionally: the moment it is most needed is while someone is
        # deciding whether to arm the turn-off, and emitting it only once armed
        # would withhold it exactly then. "armed" says whether it applies right
        # now, so a client can show it as a caution or as a live hazard.
        payload["warning"] = AUTO_POWER_OFF_MISMATCH_WARNING
        payload["warning_armed"] = bool(enabled and turn_off_enabled)
        return payload


# Module-level singleton (mirrors printer_service / settings_service).
relay_service = RelayPowerService()
