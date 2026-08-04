"""
Relay power control for the printer's mains supply, driven by a webhook.

What this is for
----------------
A Brother QL sitting idle on a shelf still draws power. If its mains supply runs
through a relay — a Shelly, a Tasmota plug, an ESPHome switch, a Node-RED flow
in front of any of them — the app can switch the printer on when a job needs it
and off again once everything has wound down. Two events exist:

``turn_on``
    A print job arrives and the printer does not answer. The webhook fires, the
    job *waits in the queue* while the printer boots, and then prints. A job is
    never failed for arriving at a printer that was merely switched off.

    What "the printer is up" means is spelled out under *Waiting for the
    printer* below; it is not "a socket accepted a connection".

``turn_off``
    Optional. Sent once the configured window has closed and nothing is left to
    print.

Either event can also be sent by hand through :meth:`RelayPowerService.send_now`,
which is how somebody proves the relay answers without waiting for a print job or
for the window to run out.

Waiting for the printer
-----------------------
A relay closing is not a printer printing. Between the two sit a boot, a Wi-Fi
association and an IPP server starting, in that order, and the gate has to wait
out all three without waiting forever. It does that in three phases, per
``turn_on`` attempt:

1. **It does not look at all** for ``TURN_ON_BLIND_WAIT_SECONDS``. Nothing can
   answer that early, and asking costs 3.5 s a time when nothing is there.
2. **It probes** for up to the attempt's window, at a short pause between
   probes. A ceiling, not a fixed delay: a printer that appears early is used
   early.
3. **It tries.** The first answer is not the finish line -- this printer's IPP
   port goes away again about eight seconds after it first answers, while the
   network stack underneath it never falters. Rather than wait out that window,
   the job is *attempted* through it: the printer stating it is ready releases
   the job, and ``PRINT_ATTEMPT_DELAYS_SECONDS`` gives it three tries over 45 s,
   because a raster the printer accepted is the only proof that it was up. A
   printer that answers but will not say whether it is ready (no IPP), or says
   something is in the way, has no such statement to release on and must instead
   go on answering for ``PRINTER_ANSWERING_SETTLE_SECONDS``; probing tightens to
   ``PRINTER_SETTLE_PROBE_PAUSE_SECONDS`` there, because a gap the probes step
   over is a gap that settle cannot act on.

Readiness is preferred over liveness wherever the app has it.
``PrinterService.check_printer_status`` reports ``state`` over IPP, and the gate
reads that rather than the ``reachable`` bit underneath it: a TCP connect
succeeding on port 9100 says a socket is bound, which a booting device manages
well before its print engine will take a raster.

The whole thing is bounded, and the bound is named in the failure along with
what was actually done -- how long it waited before looking, how many times it
looked and over how long -- rather than an interval the code does not honour.
And the job says which phase it is in the whole time, through
:mod:`src.utils.job_activity`, so a queue holding a job for a minute is never
silently doing so.

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
turn-off moment is ``keep_alive_duration_seconds + delay``. Both are measured
from the same origin, the last print.

That subtraction is part of relay power control and applies only while the
feature is on. With it off the heartbeat runs the configured window in full and
the printer's own timer then adds its interval on top, so the device sleeps at
``duration + hardware`` rather than at ``duration``. The status payload says
which of the two is in force through ``hardware_offset_applied``, so nothing has
to infer a subtraction from two numbers that happen to be equal.

The chain as clock times
------------------------
The offsets above describe the chain; the status payload also places it on the
clock, because the app knows the origin and a diagram that could have been a
status display is a worse one. Every moment is reported twice — as a unix
timestamp and as the seconds remaining until it — so a client can correct for
clock skew per moment and then tick locally without drifting, rather than
deriving three moments from one pair and inheriting the error of that pair.

Two things keep those clock times honest:

* **The origin is labelled.** It comes from
  :meth:`PrinterService.last_print_origin`, the very timestamp the keep-alive
  worker measures its window from, and it arrives with a flag saying whether it
  is a real print or the process start time the app falls back to so that
  enabling keep-alive gives one window immediately. ``origin_source`` passes
  that on as ``"print"`` or ``"startup"``, so a display says "since the app
  started" instead of inventing a print that never happened. A window that has
  since run out re-bases to the current time and says ``"idle"`` — the same
  "there is no window, so start one from now" rule the startup fallback is
  built on, which is what stops the panel showing a dead chain from a print
  three days ago. ``last_print_at`` still carries the real print moment, or
  null when there has not been one, so the re-base hides nothing.
* **The nulls are real answers.** The moments exist only while a timed
  keep-alive window is actually running, since that is the window the app is
  holding open and therefore the only one it can say the end of. No timed mode,
  a zero duration, keep-alive switched off entirely: no moments. And the
  turn-off moment is the *scheduled* one, so it is null until something arms it
  — with nothing printed since startup, no ``turn_off`` will be sent, and a
  time for it would be a time for something that is not going to happen. That
  holds while the chain is being projected from an idle origin too: the one
  step that can cut mains power is never given a time it has not been armed
  for.

Two consequences fall out of that arithmetic and are handled explicitly:

* ``duration == hardware`` is valid and intended. The effective keep-alive
  window is zero, the heartbeat does nothing at all, and the printer's own timer
  carries the entire window. Nothing about that is a misconfiguration.
* ``duration < hardware`` cannot be expressed — the window would have to end
  before it began — and is rejected at settings-validation time with a message
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
* URLs go through :func:`validate_webhook_url`, which permits LAN addresses —
  a relay is on the LAN — while still refusing link-local and cloud metadata
  endpoints.
* A hand-sent webhook is the one place a user can cut mains power deliberately,
  so :meth:`RelayPowerService.send_now` reports in as many words that it did.

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
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from src.services.settings_service import settings_service
from src.utils.exceptions import RelayWebhookError
from src.utils.formatting import now_iso
from src.utils.job_activity import (
    ACTIVITY_PRINTER_SETTLING,
    ACTIVITY_SWITCHING_ON,
    ACTIVITY_WAITING_FOR_PRINTER,
)
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
#
# What one status check costs, because every number below was chosen against it:
#
#     printer answering  ~0.09 s
#     nothing there       3.50 s   = 2.0 s IPP timeout (ipp_client) +
#                                    1.5 s TCP connect timeout (_tcp_reachable)
#
# both measured against the live printer and against an unused address on the
# same subnet. A probe into an empty window is not free, and 3.5 s of it is a
# meaningful slice of any budget below.
# --------------------------------------------------------------------------- #

# What a cold start actually looks like, because every number below is set
# against it. Measured with ``tools/boot_timeline.py`` on the QL-820NWB this was
# built against, counting from mains-on:
#
#     15.7 s   first answer of any kind (ICMP and tcp/631 together)
#     17.1 s   tcp/9100 accepts
#     23.4 s   tcp/631 STOPS accepting
#     24.8 s   tcp/631 accepts again (a 1.4 s hole)
#     34.9 s   everything has held for 10 s
#
# The hole is the finding. ICMP and tcp/9100 never faltered, so nothing dropped
# off the network: the IPP server rebinds its port partway through the boot,
# which takes out exactly the service the gate reads readiness from, roughly
# eight seconds after it first answers. "It answered" is therefore not a fact
# the gate can act on -- but neither is "it went quiet", and that is what the
# print attempt schedule below is for rather than a longer wait.

# How long to wait after a turn_on webhook before looking at the printer AT ALL.
#
# Nothing on this printer answers before 15.7 s (above), and probing into that
# window is not merely useless: at 3.5 s per unanswered probe it spends the
# budget answering a question already known. So the gate does not look at all
# until the printer has had time to boot.
#
# 20 s keeps a margin over the measured 15.7 s, and costs nothing to hold: the
# first answer is a long way from readiness anyway. It is the floor of what is
# normal rather than the whole story, which is why everything after it is
# measured from the printer's first answer and not from here.
TURN_ON_BLIND_WAIT_SECONDS = 20.0

# How long to keep probing after the blind wait, per turn_on attempt.
#
# The previous 45 s + 30 s was demonstrably too small: a printer that had mains
# power for the whole attempt never answered inside it. A healthy cold start is
# settled at 35 s, so 120 s is more than three times the measured figure -- kept
# wide deliberately, because the measurement is one unit on one network, and the
# cost of being wrong the other way is a job that fails while the printer it was
# meant for is still on its way up.
TURN_ON_FIRST_WAIT_SECONDS = 120.0

# How long to keep probing after the SECOND turn_on webhook. Shorter, because
# the relay has already been closed for the whole first window. This attempt
# exists for the case where the first request was accepted but not acted on (a
# relay that dropped it, a flow that was mid-restart), not for a printer that is
# simply slow.
#
# Worst case end to end is then (20 + 120) + (20 + 60) = 220 s plus the settle:
# about four minutes for a printer that is never coming. That is a long time to
# wait, and it is the right way round -- giving up at 75 s while the printer is
# still on its way leaves somebody unable to tell whether to wait or to
# intervene, which is exactly what happened. It is bounded, it is named in the
# failure, and the queue says what it is doing throughout.
TURN_ON_SECOND_WAIT_SECONDS = 60.0

# The pause between two probes while nothing has answered yet.
#
# The probe itself costs 3.5 s while nothing answers and about 0.1 s once
# something does, so the real cadence is roughly 5.5 s through the empty part of
# the wait and roughly 2 s from the moment the printer starts answering. That is
# the way round that matters: a tight cadence buys nothing while the device is
# absent and buys promptness exactly when it appears.
#
# Nothing quotes this number as "the interval", and the failure message does not
# either. It reports the probes actually made over the time they actually took,
# because a stated interval that the probe cost silently doubles is worse than
# no interval at all.
PRINTER_PROBE_PAUSE_SECONDS = 2.0

# The pause between two probes once the printer has started answering, i.e.
# during the settle.
#
# Shorter than the pause above, because the settle is not waiting for the
# printer to appear, it is watching for it to disappear again -- and the gap it
# has to catch is 1.4 s wide (see the boot timeline above). Sampling every 2 s
# can step straight over a hole that size and never see it, which would make a
# settle of any length agree that a printer that vanished mid-boot was steady.
#
# A probe against a printer that answers costs about 0.09 s, so the finer
# cadence is cheap exactly where it is spent. Applied as a lower bound on the
# ordinary pause rather than a replacement for it, so shortening one for a test
# shortens both.
PRINTER_SETTLE_PROBE_PAUSE_SECONDS = 1.0

# When to try printing, once the printer has stated that it is ready. One entry
# per attempt, each the pause *before* that attempt, so the job is tried three
# times over 45 s and the failure that survives all three is the printer's own.
#
# The gate used to hold the job for a fixed settle instead, on the reasoning that
# a printer which has just answered may not mean it. The boot timeline says the
# reasoning is right -- the IPP port disappears again 7.7 s in -- but the remedy
# was wrong: waiting out the worst case makes every good cold start pay for the
# bad one, and no amount of waiting proves the printer will accept a raster.
# Trying does. So the attempt itself is the readiness test, and the schedule is
# what covers the hole:
#
#     +5 s    past the first ready, clear of nothing in particular -- a short
#             grace so the common case is not fired at the instant of a reading
#     +20 s   past a failed attempt, which is the whole flickering phase: every
#             signal had settled 19.2 s after the first answer
#     +20 s   the same again, because the second attempt can land in the tail of
#             a printer that is slower than the one this was measured on
#
# A printer that was already up when the job arrived gets none of this: it is
# printed to once, immediately, and a failure is its own. The schedule guards
# the window after *this app* switched a printer on, and there is no such window
# in that case.
PRINT_ATTEMPT_DELAYS_SECONDS = (5.0, 20.0, 20.0)

# How long a printer that answers but will not say whether it is ready has to go
# on answering before the attempts start.
#
# Not every printer serves IPP: with it disabled, or not yet listening, the app
# has only a TCP connect on port 9100, and a socket accepting a connection is a
# claim about the network stack rather than about the print engine. There is no
# readiness to release on, so time is the only evidence available and this is the
# one place a wait survives -- 20 s of continuous answering before the schedule
# above begins.
#
# 20 s was a judgement call and the boot timeline endorsed it: every signal on a
# real cold start had settled 19.2 s after the first of them answered.
#
# This rule also catches a printer that comes up reporting a blocking condition
# (media-empty is the common one, and Brother firmware reports it transiently
# during boot). Such a job is handed over once it has answered steadily for this
# long, deliberately: the gate's job is to wait for the printer to come up, not
# to adjudicate whether it can print. Letting the printer give the real error
# beats the gate inventing a plausible one.
PRINTER_ANSWERING_SETTLE_SECONDS = 20.0

# Socket timeout for a single webhook POST. A relay bridge on the LAN answers in
# milliseconds; ten seconds is the point past which it is not going to answer.
WEBHOOK_TIMEOUT_SECONDS = 10.0

# How often the scheduler re-evaluates the turn-off moment. The moment itself is
# hours away and the margin around it is minutes, so a 15 s granularity is far
# finer than the decision needs and costs one settings read per tick.
SCHEDULER_TICK_SECONDS = 15.0

# Optional bearer/credential for the webhook, read from the environment rather
# than from settings. It is a secret, and settings.json is world-readable in the
# data volume and is returned verbatim by GET /settings, so storing it there
# would publish it to every client that can read the configuration. An env var
# keeps the endpoint securable without the app ever holding the secret somewhere
# it hands out. Sent verbatim as the Authorization header when set.
AUTHORIZATION_ENV_VAR = "RELAY_WEBHOOK_AUTHORIZATION"

# Actions carried in the webhook body.
ACTION_TURN_ON = "turn_on"
ACTION_TURN_OFF = "turn_off"

# What a readiness probe can find. Deliberately the same four words
# ``PrinterService.check_printer_status`` already reports in its ``state``
# field, because they are the same four answers and a second vocabulary for
# them would only have to be kept in step with the first.
#
# ready        the printer states, over IPP, that nothing is stopping it.
# unknown      it accepted a TCP connection and said nothing more. Liveness,
#              not readiness: a bound socket is not a print engine.
# blocked      it answered and named something in the way (cover-open,
#              media-empty).
# unreachable  nothing answered.
PRINTER_STATE_READY = "ready"
PRINTER_STATE_UNKNOWN = "unknown"
PRINTER_STATE_BLOCKED = "blocked"
PRINTER_STATE_UNREACHABLE = "unreachable"

# What the origin of the timing chain is.
#
# "print"    something really printed, and the chain is running from it.
# "startup"  nothing has printed since the app came up, so the window runs from
#            the process start time. That is the deliberate fallback which gives
#            keep-alive one window immediately, and it is real: the heartbeat
#            genuinely runs a window from there.
# "idle"     that window has since run out. Nothing is running and nothing is
#            scheduled, so the chain is shown from the current time instead of
#            from a moment that is hours or days behind: the same "just take now"
#            rule the startup fallback uses, applied again once the previous
#            window expires. The moments that follow are then a projection of
#            what a print landing now would start, not a schedule.
#
# Reported rather than smoothed over, because the difference is the difference
# between a display saying "last print" truthfully and saying it about a print
# that never was — and, for "idle", between a live chain and a claim that one is
# running. ``last_print_at`` carries the real print moment regardless, so
# re-basing the origin never hides when the printer last did anything.
ORIGIN_SOURCE_PRINT = "print"
ORIGIN_SOURCE_STARTUP = "startup"
ORIGIN_SOURCE_IDLE = "idle"

# The steps of the chain after the origin, in the order they occur. These are
# the values ``next_step`` takes.
STEP_KEEP_ALIVE_END = "keep_alive_end"
STEP_PRINTER_POWER_OFF = "printer_power_off"
STEP_TURN_OFF = "turn_off"

# Name of the state file, written beside settings.json.
STATE_FILENAME = "relay_power.json"


def _iso(moment: Optional[float]) -> Optional[str]:
    """Render a unix timestamp as ISO-8601 UTC, or None.

    Best-effort by design: a moment that cannot be rendered (a timestamp beyond
    what the platform's calendar can express, say) becomes None rather than
    taking the whole status payload down with it. The unix value is reported
    alongside regardless, so nothing is actually lost.
    """
    if moment is None:
        return None
    try:
        return datetime.fromtimestamp(float(moment), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _normalise_probe_result(result: Any) -> Tuple[str, List[str]]:
    """Reduce whatever a readiness probe returned to a state and its reasons.

    Accepts the three shapes a probe can sensibly answer in:

    * the full status dict from ``check_printer_status``, which is what the
      shipped probe returns;
    * a bare ``PRINTER_STATE_*`` string;
    * a bool, from a probe that can only answer "is it there". Taken at its
      word -- True means ready -- because a caller who supplies a liveness-only
      probe has told us that is all the readiness it has.

    Anything unrecognised is read as no answer, which is the safe direction: it
    delays a job rather than pushing one at a printer that may not be there.
    """
    if isinstance(result, bool):
        return (PRINTER_STATE_READY if result else PRINTER_STATE_UNREACHABLE), []
    if isinstance(result, str):
        state = result.strip().lower()
        return (state or PRINTER_STATE_UNREACHABLE), []
    if isinstance(result, dict):
        state = str(result.get("state") or "").strip().lower()
        if not state:
            # No state at all: fall back to the reachability bit, which every
            # older status payload carried.
            state = (PRINTER_STATE_READY if result.get("reachable")
                     else PRINTER_STATE_UNREACHABLE)
        reasons = result.get("blocking_reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        return state, [str(reason) for reason in reasons]
    return PRINTER_STATE_UNREACHABLE, []


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
        readiness_probe: Optional[Callable[[Dict[str, Any]], Any]] = None,
        pending_work_probe: Optional[Callable[[], bool]] = None,
        sender: Optional[Callable[[str, Dict[str, Any], float], None]] = None,
        origin_provider: Optional[Callable[[], Optional[Tuple[float, bool]]]] = None,
        activity_reporter: Optional[Callable[[Optional[str], Optional[str]], Any]] = None,
    ) -> None:
        """Initialise the service.

        Every collaborator is injectable so the tests can drive the whole
        feature without a network, a printer or a queue. The defaults resolve
        lazily to the application singletons.

        Args:
            state_file: Where the scheduled turn-off moment is persisted.
                Defaults to ``relay_power.json`` beside the settings file.
            settings_provider: Object exposing ``get_settings()``.
            readiness_probe: ``probe(settings)`` answering "what is the printer
                doing right now", as one of the ``PRINTER_STATE_*`` values. It
                may return the bare string, or a dict carrying ``state`` and
                ``blocking_reasons`` -- which is what
                ``check_printer_status`` already returns. See
                :func:`_normalise_probe_result`.
            pending_work_probe: ``probe() -> bool``, "is anything queued or
                printing".
            sender: ``sender(url, payload, timeout) -> None``, performing the
                POST and raising on any delivery failure.
            origin_provider: ``provider() -> (timestamp, printed)``, the moment
                the timing chain is measured from and whether it is a real print
                rather than the startup fallback.
            activity_reporter: ``report(activity, message)``, saying what the
                job being held by the gate is currently doing. Defaults to the
                print queue's, which writes it onto the job it has in hand.
        """
        self._settings_provider = settings_provider or settings_service
        self._readiness_probe = readiness_probe or self._default_readiness_probe
        self._pending_work_probe = pending_work_probe or self._default_pending_work_probe
        self._sender = sender or self._default_sender
        self._origin_provider = origin_provider or self._default_origin_provider
        self._activity_reporter = activity_reporter or self._default_activity_reporter

        if state_file is None:
            settings_file = getattr(self._settings_provider, "settings_file", "")
            state_dir = os.path.dirname(settings_file) or "."
            state_file = os.path.join(state_dir, STATE_FILENAME)
        self.state_file = state_file

        # Waits are instance attributes rather than direct constant reads so a
        # test can shrink them without patching module state.
        self.turn_on_waits = (TURN_ON_FIRST_WAIT_SECONDS, TURN_ON_SECOND_WAIT_SECONDS)
        self.blind_wait = TURN_ON_BLIND_WAIT_SECONDS
        self.probe_pause = PRINTER_PROBE_PAUSE_SECONDS
        self.settle_probe_pause = PRINTER_SETTLE_PROBE_PAUSE_SECONDS
        self.print_attempt_delays = tuple(PRINT_ATTEMPT_DELAYS_SECONDS)
        self.answering_settle = PRINTER_ANSWERING_SETTLE_SECONDS
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

    @classmethod
    def url_for(cls, action: str, settings: Dict[str, Any]) -> str:
        """The URL an action is POSTed to, or "" when none is configured."""
        return (cls.turn_on_url(settings) if action == ACTION_TURN_ON
                else cls.turn_off_url(settings))

    @staticmethod
    def turn_off_enabled(settings: Dict[str, Any]) -> bool:
        """Whether the ``turn_off`` half of the feature is switched on."""
        return bool(settings.get("relay_webhook_turn_off_enabled", False))

    @staticmethod
    def hardware_power_off_seconds(settings: Dict[str, Any]) -> int:
        """The printer's own auto-power-off interval, in seconds.

        This is what the user says the device is set to. Nothing verifies it.
        See ``AUTO_POWER_OFF_MISMATCH_WARNING``.
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

        This is exactly the condition the keep-alive worker has always used —
        ``mode == "timed"`` with a non-zero duration — and deliberately does NOT
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
        holding it open there is no such moment. Scheduling mains power to be cut
        on the strength of a window that is not running is precisely the case
        worth refusing. Settings validation rejects the combination too; this is
        the same rule at the point of use.
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
        Whether it was applied is reported separately by
        :meth:`hardware_offset_applied`, because the two cases can produce the
        same number and a client has no way to tell them apart from the number.
        """
        window = cls.timed_window_seconds(settings)
        if window is None:
            return None
        if not cls.is_enabled(settings):
            return window
        return max(0, window - cls.hardware_power_off_seconds(settings))

    @classmethod
    def hardware_offset_applied(cls, settings: Dict[str, Any]) -> bool:
        """Whether the printer's own interval was subtracted from the window.

        True only while relay power control is on and a timed window exists,
        which is exactly the condition :meth:`effective_keep_alive_seconds`
        subtracts under.

        It exists because the subtraction cannot be read back out of the
        numbers. With the feature off, the effective window equals the
        configured one, and a client comparing the two sees a difference of zero
        that is indistinguishable from a hardware interval of zero. Reporting
        the answer rather than leaving it to be inferred is what stops a client
        explaining a moment as "the window minus the device's ten minutes" when
        no ten minutes were taken off it.
        """
        return cls.is_enabled(settings) and cls.timed_window_seconds(settings) is not None

    @classmethod
    def printer_power_off_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """Seconds from the last print until the printer's own timer expires.

        The device starts counting when the heartbeat stops, so this is the
        effective keep-alive window plus the interval the device is said to be
        set to. Which makes it the configured window exactly when the
        subtraction was applied, and the configured window plus that interval
        when it was not: ask for four hours with a ten-minute device timer and
        the printer sleeps at 4:00 with relay control on, at 4:10 without it.

        Returns None when no timed window applies, in which case the heartbeat
        never stops on its own and there is no moment to name.

        Like every other use of ``printer_auto_power_off_minutes``, this is only
        as true as that value is: see ``AUTO_POWER_OFF_MISMATCH_WARNING``.
        """
        effective = cls.effective_keep_alive_seconds(settings)
        if effective is None:
            return None
        return effective + cls.hardware_power_off_seconds(settings)

    @classmethod
    def keep_alive_end_offset_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """Seconds from the origin until the heartbeat actually stops, or None.

        The same number as :meth:`effective_keep_alive_seconds` under one extra
        condition: keep-alive has to be switched on. That method describes the
        *arithmetic* — how long a window would run — and answers it whenever a
        timed window is configured, deliberately without consulting the enabled
        flag, because the keep-alive worker's own rule must not change.

        This one describes a *moment on the clock*, and there is no moment at
        which a heartbeat that is not running stops. Naming one would put a time
        on the display for an event that is not going to occur, which is the
        same class of mistake as calling the startup fallback a print.
        """
        if cls.configured_window_seconds(settings) is None:
            return None
        return cls.effective_keep_alive_seconds(settings)

    @classmethod
    def printer_power_off_offset_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """Seconds from the origin until the printer's own timer expires, or None.

        :meth:`printer_power_off_seconds` under the same extra condition, and
        for the same reason: the device starts counting when the heartbeat
        stops, so with no heartbeat running there is nothing to count from.

        Everything the relay feature's state does to that number it does here
        too, because the number is that method's: with relay power control on
        this lands at the configured window, with it off at the configured
        window plus the device's own interval.
        """
        if cls.configured_window_seconds(settings) is None:
            return None
        return cls.printer_power_off_seconds(settings)

    @classmethod
    def turn_off_offset_seconds(cls, settings: Dict[str, Any]) -> Optional[int]:
        """Seconds from the last print until ``turn_off`` should be sent.

        This is the *configured* window plus the safety margin, deliberately
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
        schedule", which is the safe reading: it can only delay a turn-off,
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
            "updated_at": now_iso(),
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
        gone, but the moment it chose is still on disk and is honoured — or, if
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
        printers can tell which one asked. Kept flat and small on purpose. The
        shape is documented in the OpenAPI spec and the UI, and every field
        added is a field somebody's flow may come to depend on.

        A hand-sent webhook carries exactly this body too, deliberately: a test
        that sent something the print path would not send would prove nothing
        about the print path.
        """
        return {
            "action": action,
            "source": "brother_ql_app",
            "printer_uri": settings.get("printer_uri", ""),
            "printer_model": settings.get("printer_model", ""),
            "timestamp": now_iso(),
        }

    def _default_sender(self, url: str, payload: Dict[str, Any],
                        timeout: float) -> Optional[int]:
        """POST ``payload`` as JSON to ``url``.

        Returns:
            The HTTP status the endpoint answered with, or None when the
            response carried none. It is returned rather than merely accepted so
            that a hand-sent webhook can report what came back: "the relay
            answered 200" and "the relay answered something" are different
            things to a user working out why a switch did not move.

        Raises:
            RelayWebhookError: On any transport error, timeout or non-2xx
                response. Everything is surfaced: a webhook whose delivery
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
                if status is None:
                    return None
                if not (200 <= int(status) < 300):
                    raise RelayWebhookError(
                        f"Relay webhook returned HTTP {status}", "RELAY_WEBHOOK_ERROR",
                        {"url": url, "status": int(status)})
                return int(status)
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

    def send(self, action: str, settings: Optional[Dict[str, Any]] = None,
             payload: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Send one webhook.

        Args:
            action: ``turn_on`` or ``turn_off``.
            settings: Settings to read the URL from; loaded when omitted.
            payload: Body to send, built from ``action`` and ``settings`` when
                omitted. A caller passes one in when it needs to report the
                exact body that went out whether or not delivery succeeded.

        Returns:
            The HTTP status the relay answered with, or None when the sender
            reported none.

        Raises:
            RelayWebhookError: When no URL is configured for the action, when
                the URL fails validation, or when delivery fails.
        """
        settings = self._settings() if settings is None else settings
        url = self.url_for(action, settings)
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

        payload = self.build_payload(action, settings) if payload is None else payload
        try:
            status = self._sender(url, payload, self.webhook_timeout)
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
            self._last_action_at = now_iso()
            self._last_error = None
            self._last_error_at = None
        logger.info("Relay webhook sent", action=action)
        return status if isinstance(status, int) else None

    def _record_error(self, message: str) -> None:
        """Remember the most recent failure so the API can report it."""
        with self._lock:
            self._last_error = message
            self._last_error_at = now_iso()

    # ------------------------------------------------------------------ #
    # Sending one by hand
    # ------------------------------------------------------------------ #
    def send_now(self, action: str) -> Dict[str, Any]:
        """Send one webhook immediately, on request, and report what happened.

        Everywhere else in this module the app decides when to switch the relay.
        This is the one entry point where a person does, and it exists because
        the alternative way to find out whether a relay answers is to configure
        the whole timing chain and wait hours for it to come round.

        What it requires, and what it does not
        --------------------------------------
        Relay power control has to be switched on, and the action has to have a
        URL. Nothing else. In particular the schedule does not have to be armed:
        ``turn_off`` can be sent by hand while its scheduled half is off and
        while no timed keep-alive window exists at all, because "does this relay
        actually switch off" is a question worth answering *before* arming
        anything that will cut mains power unattended.

        The schedule is left exactly as it was, in both directions.

        * A hand-sent ``turn_off`` does not clear it. The scheduled moment says
          when the automatic turn-off is due after the last print; power coming
          back (someone presses the relay's own button, the next job sends
          ``turn_on``) does not make that moment wrong, and silently cancelling
          an armed safety schedule from a button labelled "send a webhook" would
          be a second, hidden effect. The redundant later ``turn_off`` costs
          nothing: it is already the ordinary case, since the documented chain
          fires ``turn_off`` at a printer that has powered itself off already.
        * A hand-sent ``turn_on`` does not arm it either, for the mirror reason.
          Arming is measured from the last print, and a button press is not a
          print, so arming here would schedule a mains cut from an origin that
          never existed. The next print arms it properly, and pending work still
          resets the clock unconditionally.

        The outcome is reported rather than raised. A relay that answered 401 or
        refused the connection is the *result* of the request, not a failure of
        the app to carry it out, and the difference between those two is the
        thing the user is trying to see.

        Args:
            action: ``turn_on`` or ``turn_off``.

        Returns:
            A report of the request and its outcome: ``success``, the ``action``,
            the ``url`` it went to, the ``payload`` that was sent, whether an
            ``Authorization`` header went with it, the ``response_status`` that
            came back, the ``error`` when one did, a ``message`` in plain words,
            and ``mains_power`` saying what the relay was told to do. A
            successful ``turn_off`` reports ``mains_power: "off"`` and says in
            the message that power has been cut, because this is the one place a
            user can cut it deliberately and the response must not be coy about
            it. An unconfirmed delivery reports ``"unknown"``: the request may
            have been acted on before the error, and claiming otherwise would be
            a guess.

        Raises:
            ValueError: When the action is not one of the two, when relay power
                control is switched off, or when no URL is configured for the
                action. Nothing is sent in any of those cases, and the message
                says which one it was.
        """
        if action not in (ACTION_TURN_ON, ACTION_TURN_OFF):
            raise ValueError(
                f"Unknown relay action '{action}'. It must be "
                f"'{ACTION_TURN_ON}' or '{ACTION_TURN_OFF}'.")

        settings = self._settings()
        if not self.is_enabled(settings):
            raise ValueError(
                "Relay power control is switched off, so nothing was sent. Switch "
                "relay_webhook_enabled on before sending a webhook by hand.")

        url = self.url_for(action, settings)
        if not url:
            raise ValueError(
                f"No relay webhook URL is configured for '{action}', so nothing was "
                "sent. Set relay_webhook_turn_on_url, which both actions fall back "
                "to, or relay_webhook_turn_off_url for a relay that switches on the "
                "address rather than on the body.")

        # Built here rather than inside send() so the report can name the exact
        # body that went out even when delivery then failed.
        payload = self.build_payload(action, settings)

        report: Dict[str, Any] = {
            "success": False,
            "action": action,
            "url": url,
            "payload": payload,
            "authorization_sent": bool(os.environ.get(AUTHORIZATION_ENV_VAR)),
            "response_status": None,
            "sent_at": payload["timestamp"],
            "mains_power": "unknown",
            "message": "",
            "error": None,
            "schedule_changed": False,
            "scheduled_turn_off_at": (
                self.scheduled_turn_off_at() if self.turn_off_enabled(settings) else None
            ),
        }

        logger.info("Sending a relay webhook on request", action=action)
        try:
            status = self.send(action, settings, payload=payload)
        except RelayWebhookError as e:
            details = getattr(e, "details", None) or {}
            refused_with = details.get("status")
            report["response_status"] = (
                refused_with if isinstance(refused_with, int) else None)
            report["error"] = str(e)
            report["message"] = (
                f"The {action} webhook to {url} was not confirmed. "
                f"{str(e).rstrip('.')}. The relay may or may not have switched, so "
                "check the printer before relying on either state."
            )
            return report

        report["success"] = True
        report["response_status"] = status
        report["mains_power"] = "off" if action == ACTION_TURN_OFF else "on"
        answered = f" The relay answered HTTP {status}." if status is not None else ""
        if action == ACTION_TURN_OFF:
            report["message"] = (
                f"turn_off was delivered to {url}.{answered} Mains power to the "
                "printer has been cut. It stays off until the relay is switched on "
                "again, which the next print job does while relay power control is "
                "enabled."
            )
        else:
            report["message"] = (
                f"turn_on was delivered to {url}.{answered} Mains power to the "
                "printer is on. This reports the webhook and nothing more: the "
                "printer is not probed here, so allow it a minute to finish booting."
            )
        return report

    # ------------------------------------------------------------------ #
    # Default collaborators
    # ------------------------------------------------------------------ #
    def _default_readiness_probe(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the printer service what the printer is doing.

        Reports the whole answer rather than reducing it to a boolean. The
        status check already distinguishes "the device answered" from "the
        device says it can print", and the gate needs both: it accepts a stated
        readiness quickly and an unexplained answer only after a longer settle,
        which it cannot do from one bit.
        """
        # Imported lazily: the printer service imports this module, so a
        # top-level import here would be a cycle.
        from src.services.printer_service import printer_service

        printer_uri = settings.get("printer_uri", "")
        if not printer_uri:
            return {"state": PRINTER_STATE_UNREACHABLE, "blocking_reasons": []}
        try:
            return printer_service.check_printer_status(
                printer_uri, settings.get("printer_model", ""))
        except Exception as e:  # noqa: BLE001 - an error here means "not answering"
            logger.debug("Readiness probe failed", error=str(e))
            return {"state": PRINTER_STATE_UNREACHABLE, "blocking_reasons": []}

    @staticmethod
    def _default_activity_reporter(activity: Optional[str],
                                   message: Optional[str] = None) -> None:
        """Tell the print queue what the job it is holding is doing.

        Best-effort in every direction: there may be no job (the gate can be
        called by hand), and a failure to describe a job must never be a reason
        to fail it.
        """
        from src.services.queue_service import print_queue

        try:
            print_queue.report_activity(activity, message)
        except Exception as e:  # noqa: BLE001 - reporting must not break a print
            logger.debug("Could not report job activity", error=str(e))

    def _default_origin_provider(self) -> Optional[Tuple[float, bool]]:
        """Ask the printer service what the timing chain runs from.

        Deliberately not tracked here. This service is told about prints through
        :meth:`note_print_activity`, so it could keep its own copy — and it
        would be wrong the moment the two diverged, which a restart guarantees:
        the persisted schedule survives, an in-memory origin does not, and the
        keep-alive window would then be measured from one instant and the
        display drawn from another. One timestamp, held where the keep-alive
        worker reads it, is the only version that cannot disagree with itself.
        """
        # Imported lazily: the printer service imports this module, so a
        # top-level import here would be a cycle.
        from src.services.printer_service import printer_service

        return printer_service.last_print_origin()

    def _default_pending_work_probe(self) -> bool:
        """Whether the print queue still has work of any kind.

        Anything queued or printing, and also a job the queue's worker is still
        holding after it reached a terminal state -- which is the case that
        matters most here. Cancelling a job does not stop the gate that job is
        sitting in, and that gate may be halfway through switching this very
        printer's mains supply on. Reading only the queued/printing counts made
        the queue look idle from that moment on, and a turn-off falling due in
        the same window would then cut power to a printer the app was in the
        middle of booting.
        """
        from src.services.queue_service import print_queue

        try:
            return bool(print_queue.has_pending_work())
        except Exception as e:  # noqa: BLE001 - unknown means "assume busy"
            logger.warning("Could not read queue status; treating it as busy",
                           error=str(e))
            return True

    # ------------------------------------------------------------------ #
    # turn_on
    # ------------------------------------------------------------------ #
    def _report(self, activity: Optional[str], message: Optional[str] = None) -> None:
        """Say what the job the gate is holding is doing. Never raises."""
        try:
            self._activity_reporter(activity, message)
        except Exception as e:  # noqa: BLE001 - describing a job must not fail it
            logger.debug("Could not report job activity", error=str(e))

    def _probe(self, settings: Dict[str, Any]) -> Tuple[str, List[str]]:
        """Ask the printer what it is doing, as (state, blocking_reasons)."""
        try:
            result = self._readiness_probe(settings)
        except Exception as e:  # noqa: BLE001 - an error here means "no answer"
            logger.debug("Readiness probe raised; reading it as no answer",
                         error=str(e))
            return PRINTER_STATE_UNREACHABLE, []
        return _normalise_probe_result(result)

    def _sleep(self, seconds: float) -> bool:
        """Wait, returning True when the app is shutting down."""
        return self._stop_event.wait(max(0.0, seconds))

    def _wait_until_ready(self, settings: Dict[str, Any], seconds: float,
                          blind_wait: float = 0.0,
                          report: bool = False) -> Dict[str, Any]:
        """Wait for the printer to come up and stay up.

        Three things happen here, in order:

        1. **Nothing, for ``blind_wait`` seconds.** A printer whose mains have
           just been switched on cannot answer, and asking costs 3.5 s a time.
        2. **Probing, for up to ``seconds``.** A ceiling and not a fixed delay:
           a printer that appears after 12 s is not made to wait out the window.
        3. **Release.** A printer that states it is ready releases the job on
           that reading, without a settle: the proof that a booting printer will
           take a raster is that it took one, and
           :data:`PRINT_ATTEMPT_DELAYS_SECONDS` is what covers it having spoken
           too soon. A printer that answers but will not say (no IPP) or says
           something is in the way has no such reading to release on, so there
           the answering itself has to hold for :attr:`answering_settle` --
           time being the only evidence available.

        That settle, when it applies, is allowed to finish past the deadline --
        failing a printer for answering one probe before the end would be
        perverse -- but only by its own length, so a device that flickers in and
        out cannot stretch the wait indefinitely.

        Args:
            settings: Settings the probe reads the printer address from.
            seconds: Ceiling on the probing phase, after the blind wait.
            blind_wait: How long not to look at all before probing starts.
            report: Whether to describe each phase to the queue.

        Returns:
            A record of what happened: ``ready`` (did it come up), ``probes``
            (how many times it was asked), ``probing_seconds`` (over how long),
            ``blind_wait``, the last ``state`` and ``blocking_reasons`` seen,
            and ``stated_ready`` -- whether the printer said so itself or was
            accepted on the strength of answering steadily.

            ``answered_state`` is the last state that was not "nothing there",
            or None if nothing ever was. It is kept apart from ``state``
            because the two differ in exactly the case worth describing: a
            printer that flickers in and out is very likely to be absent on the
            probe that happens to be last, and "it never answered" would then be
            the wrong thing to tell somebody.
        """
        outcome: Dict[str, Any] = {
            "ready": False,
            "stated_ready": False,
            "probes": 0,
            "probing_seconds": 0.0,
            "blind_wait": max(0.0, blind_wait),
            "state": PRINTER_STATE_UNREACHABLE,
            "blocking_reasons": [],
            "answered_state": None,
            "answered_blocking": [],
        }

        if blind_wait > 0:
            if report:
                self._report(
                    ACTIVITY_WAITING_FOR_PRINTER,
                    f"Switched on at the relay. Leaving the printer alone for "
                    f"{blind_wait:.0f}s while it boots.")
            if self._sleep(blind_wait):
                return outcome
        if report:
            self._report(ACTIVITY_WAITING_FOR_PRINTER,
                         "Waiting for the printer to come up.")

        started = time.monotonic()
        deadline = started + max(0.0, seconds)
        hard_deadline = deadline + self.answering_settle

        answering_since: Optional[float] = None  # answering at all, since
        reported_settle: Optional[str] = None

        while True:
            state, blocking = self._probe(settings)
            now = time.monotonic()
            outcome["probes"] += 1
            outcome["probing_seconds"] = now - started
            outcome["state"] = state
            outcome["blocking_reasons"] = blocking
            if state != PRINTER_STATE_UNREACHABLE:
                outcome["answered_state"] = state
                outcome["answered_blocking"] = blocking

            if state == PRINTER_STATE_READY:
                # The printer says it can print. That is the reading the job is
                # released on, and the attempt schedule -- not another wait --
                # is what covers it having said so a few seconds early.
                outcome["ready"] = True
                outcome["stated_ready"] = True
                logger.info("Printer reports itself ready; releasing the job",
                            probes=outcome["probes"],
                            probing_seconds=round(outcome["probing_seconds"], 1))
                return outcome

            if state in (PRINTER_STATE_UNKNOWN, PRINTER_STATE_BLOCKED):
                # It is there but is not stating that it can print, so there is
                # nothing to release on and the answering itself has to hold.
                if answering_since is None:
                    answering_since = now
            else:
                answering_since = None

            steady = (answering_since is not None
                      and now - answering_since >= self.answering_settle)
            if steady:
                outcome["ready"] = True
                outcome["stated_ready"] = False
                logger.info("Printer answered steadily without stating readiness",
                            state=state, probes=outcome["probes"],
                            probing_seconds=round(outcome["probing_seconds"], 1))
                return outcome

            settle = "answering" if answering_since is not None else None
            if report and settle != reported_settle:
                reported_settle = settle
                if settle == "answering":
                    self._report(
                        ACTIVITY_PRINTER_SETTLING,
                        f"The printer is answering but has not reported itself "
                        f"ready. Giving it {self.answering_settle:.0f}s before "
                        f"printing anyway.")
                else:
                    self._report(ACTIVITY_WAITING_FOR_PRINTER,
                                 "The printer stopped answering. Still waiting "
                                 "for it to come up.")

            if now >= hard_deadline:
                return outcome
            if now >= deadline and answering_since is None:
                return outcome

            # Probe faster once something is answering. Until then the pause is
            # padding on a probe that already costs 3.5 s; from here it decides
            # whether a printer that drops out for a second or two is seen doing
            # it, and a printer that drops out has not settled.
            pause = self.probe_pause
            if answering_since is not None:
                pause = min(pause, self.settle_probe_pause)
            if self._sleep(pause):
                return outcome

    @staticmethod
    def _gate_answer(delays: Tuple[float, ...] = (),
                     message: Optional[str] = None) -> Dict[str, Any]:
        """The gate's answer to the print queue.

        Two fields and nothing else: how to schedule the print attempts, and
        what the pause before the first one is waiting on. The queue displays
        the message and never inspects it, so nothing about relays, webhooks or
        printer states crosses over -- the queue still only knows that something
        may have to happen before a job starts, and that it may have to be
        described while it does.
        """
        return {"delays": tuple(delays), "message": message}

    def _first_pause_message(self, delays: Tuple[float, ...],
                             stated_ready: bool) -> Optional[str]:
        """Say what the pause before the first print attempt is waiting on.

        The gate releases a job on either of two readings, and they are not the
        same claim (see :meth:`_wait_until_ready`):

        * the printer stated over IPP that it can print, or
        * it merely answered steadily for :attr:`answering_settle` without ever
          saying so -- no IPP, or something reported as in the way.

        The queue used to announce the first of those in both cases, because the
        gate handed back a schedule and nothing else. It is the difference
        between "the printer says it is ready" and "the printer is there and has
        not said", which is exactly what somebody watching a job that will not
        print needs to know.

        Returns None when there is no pause to describe.
        """
        if not delays or delays[0] <= 0:
            return None
        seconds = delays[0]
        if stated_ready:
            return (f"The printer reports itself ready. Giving it {seconds:.0f}s "
                    f"before printing.")
        return (f"The printer is answering but has not reported itself ready. "
                f"Giving it {seconds:.0f}s before printing anyway.")

    def ensure_printer_powered(self) -> Dict[str, Any]:
        """Make sure the printer is up before a job is started.

        This is the queue's pre-job gate. It returns quietly in the overwhelming
        majority of cases: the feature off, no URL configured, or a printer that
        already answers. Only a printer that does not answer at all, at an
        enabled and configured relay, causes a webhook.

        A printer that answers is left alone entirely -- no webhook, no wait, no
        retries. Those guard the window after *this app* switched a printer on,
        and there is no such window when the device was already there. That also
        keeps the gate out of a judgement it should not be making: a printer
        answering with its cover open is a job that should fail at the printer,
        with the printer's reason, rather than in something named after power.

        The sequence for a printer that is not there is: send, wait, send again,
        wait less, then fail naming what was actually done. A *delivery* failure
        is raised straight away instead of being retried past: retrying a URL
        that refused the connection mostly buys a slower, vaguer error, and the
        requirement is that a webhook which did not arrive is reported rather
        than swallowed.

        Returns:
            The queue's marching orders, as ``{"delays": ..., "message": ...}``.
            When the printer was switched on here, ``delays`` is the pauses
            before each print attempt (:data:`PRINT_ATTEMPT_DELAYS_SECONDS`):
            the caller is being told it is printing into a boot window and
            should try more than once, and ``message`` says what the first of
            those pauses is waiting on. An empty schedule otherwise, meaning
            "print once, now" -- which is every job at a printer that was
            already up, and has nothing to describe.

        Raises:
            RelayWebhookError: When the webhook cannot be delivered, or when the
                printer never comes up. The job then fails carrying that
                message, which is the only place a user would look for it.
        """
        settings = self._settings()

        # Nothing configured -> return before probing anything. The feature
        # being off must mean no outbound request AND no extra work at all, so
        # an install that ignores it behaves exactly as it did before.
        if not self.is_enabled(settings):
            return self._gate_answer()
        url = self.turn_on_url(settings)
        if not url:
            logger.warning("Relay power control is enabled but no turn_on URL is "
                           "configured; not switching the printer on")
            return self._gate_answer()

        state, blocking = self._probe(settings)
        if state != PRINTER_STATE_UNREACHABLE:
            logger.debug("Printer already answering; no relay turn_on needed",
                         state=state, blocking_reasons=blocking)
            return self._gate_answer()

        logger.info("Printer not reachable; switching it on via the relay")
        waits = tuple(self.turn_on_waits)
        started = time.monotonic()
        probes = 0
        probing_seconds = 0.0
        answered_state: Optional[str] = None
        answered_blocking: List[str] = []

        for attempt, wait_seconds in enumerate(waits, start=1):
            self._report(
                ACTIVITY_SWITCHING_ON,
                "Switching the printer on at the relay."
                if attempt == 1 else
                f"The printer has not come up yet. Switching it on at the relay "
                f"again (attempt {attempt} of {len(waits)}).")
            self.send(ACTION_TURN_ON, settings)
            logger.info("Relay turn_on sent, waiting for the printer",
                        attempt=attempt, blind_wait_seconds=self.blind_wait,
                        wait_seconds=wait_seconds)

            outcome = self._wait_until_ready(settings, wait_seconds,
                                             blind_wait=self.blind_wait,
                                             report=True)
            probes += outcome["probes"]
            probing_seconds += outcome["probing_seconds"]
            if outcome["answered_state"]:
                answered_state = outcome["answered_state"]
                answered_blocking = outcome["answered_blocking"]
            if outcome["ready"]:
                logger.info("Printer came up after relay turn_on", attempt=attempt,
                            stated_ready=outcome["stated_ready"])
                # Arm from here rather than waiting for the print to land: if
                # the job goes on to fail, the relay must still be scheduled to
                # switch off instead of being left on indefinitely.
                self.arm()
                delays = tuple(self.print_attempt_delays)
                return self._gate_answer(
                    delays,
                    self._first_pause_message(delays, outcome["stated_ready"]))

        elapsed = time.monotonic() - started
        # Say what it saw, when it saw anything. "Never answered" and "answered
        # but never steadily" are different faults with different fixes, and the
        # message is the only place either of them surfaces.
        seen = ""
        if answered_state:
            named = f" ({', '.join(answered_blocking)})" if answered_blocking else ""
            seen = (f" It did answer at some point, last reporting "
                    f"'{answered_state}'{named}, but never steadily enough to "
                    f"print to.")
        message = (
            f"Printer did not come up within {elapsed:.0f}s of the relay being "
            f"switched on ({len(waits)} turn_on webhook(s) delivered to {url}; after "
            f"each one it was left alone for {self.blind_wait:.0f}s to boot, then "
            f"checked {probes} times over {probing_seconds:.0f}s)."
            f"{seen} The webhook was accepted, so either the relay did not close, the "
            "printer is not on that outlet, or it takes longer than this to come up."
        )
        self._record_error(message)
        logger.error("Printer did not come up after relay turn_on",
                     attempts=len(waits), elapsed_seconds=round(elapsed, 1),
                     probes=probes, answered_state=answered_state)
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
    def _origin(self) -> Tuple[Optional[float], Optional[str]]:
        """The moment the chain is measured from, and what kind of moment it is.

        Returns ``(None, None)`` when the origin cannot be read at all, which is
        the honest answer and leaves every clock time in the payload null: a
        chain drawn from a guessed origin would be wrong at every step. Never
        raises — the status endpoint is a read, and a read must not fail because
        one of its inputs was unavailable.
        """
        try:
            origin = self._origin_provider()
        except Exception as e:  # noqa: BLE001 - a status read must survive anything
            logger.warning("Could not read the print origin for the relay status",
                           error=str(e))
            return None, None
        if not origin:
            return None, None
        try:
            moment, printed = origin
            moment = float(moment)
        except (TypeError, ValueError):
            logger.warning("The print origin was not a usable timestamp",
                           origin=repr(origin))
            return None, None
        return moment, (ORIGIN_SOURCE_PRINT if printed else ORIGIN_SOURCE_STARTUP)

    @staticmethod
    def _at(origin: Optional[float], offset: Optional[int]) -> Optional[float]:
        """Place an offset from the origin on the clock, or None."""
        if origin is None or offset is None:
            return None
        return origin + offset

    @staticmethod
    def _until(moment: Optional[float], now: float) -> Optional[float]:
        """Seconds remaining until a moment, never negative, or None.

        Clamped like ``seconds_until_turn_off`` has always been. Nothing is lost
        by it: the absolute moment is reported beside every one of these, so how
        long ago a step passed is ``server_time`` minus that moment.
        """
        if moment is None:
            return None
        return max(0.0, moment - now)

    def status(self) -> Dict[str, Any]:
        """Return a summary of the feature for the API and the UI."""
        settings = self._settings()
        now = time.time()
        enabled = self.is_enabled(settings)
        turn_off_enabled = self.turn_off_enabled(settings)
        scheduled = self.scheduled_turn_off_at() if enabled and turn_off_enabled else None
        effective = self.effective_keep_alive_seconds(settings)

        # The chain on the clock. The origin starts as whatever the keep-alive
        # worker measures from, carrying the label that says whether it was a
        # print; ``last_print_at`` keeps hold of the real print moment so that
        # the re-base below can never hide it.
        origin_at, origin_source = self._origin()
        last_print_at = origin_at if origin_source == ORIGIN_SOURCE_PRINT else None

        keep_alive_offset = self.keep_alive_end_offset_seconds(settings)
        printer_power_off_offset = self.printer_power_off_offset_seconds(settings)

        # If that origin's window has already run out, show the chain from now
        # instead. It is the same rule the startup fallback is built on — no
        # window running, so start one from the current time — applied a second
        # time, and it is what stops the panel displaying a dead chain from a
        # print three days ago. Nothing is claimed by it that is not said out
        # loud: origin_source becomes "idle", which means the moments below are
        # what a print landing now would start rather than what is scheduled.
        #
        # Guarded on a chain existing at all. With no timed window nothing ever
        # expires, so there is nothing to re-base and the real origin stands.
        if origin_at is not None and keep_alive_offset is not None:
            elapsed = [origin_at + keep_alive_offset,
                       origin_at + printer_power_off_offset]
            if scheduled is not None:
                elapsed.append(scheduled)
            if max(elapsed) <= now:
                origin_at, origin_source = now, ORIGIN_SOURCE_IDLE

        keep_alive_ends_at = self._at(origin_at, keep_alive_offset)
        printer_power_off_at = self._at(origin_at, printer_power_off_offset)

        # Which step is next is decided here rather than left to the client. The
        # question is not "which of these timestamps is smallest" — that part is
        # arithmetic anyone can do — but "which of these steps exists at all",
        # and that is settings logic: a window that is not running has no end, a
        # turn-off that is not armed has no moment, and the hardware offset
        # moves one of the moments without moving the other. Answering it once,
        # on the side that already holds those rules, gives every client the
        # same answer instead of one answer per client's reading of them.
        #
        # It does not go stale between polls either, because it is not a claim
        # about the future: every moment is in the payload absolutely, so a
        # client ticking locally past a boundary can advance the highlight
        # itself, against the same server_time it corrects its clock with.
        next_step, next_step_at = None, None
        for step, moment in (
            (STEP_KEEP_ALIVE_END, keep_alive_ends_at),
            (STEP_PRINTER_POWER_OFF, printer_power_off_at),
            (STEP_TURN_OFF, scheduled),
        ):
            if moment is None or moment <= now:
                continue
            if next_step_at is None or moment < next_step_at:
                next_step, next_step_at = step, moment

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
            # Why the heartbeat stops when it does, not just when. With the
            # feature off the effective window equals the configured one, and
            # from the numbers alone that is indistinguishable from a
            # subtraction of zero; a client rendering the timing chain would
            # have to guess, and guessing produces a reason ("the window minus
            # the device's ten minutes") for an arithmetic that never ran.
            "hardware_offset_applied": self.hardware_offset_applied(settings),
            # And when the device's own timer expires, which is the configured
            # window while the subtraction applies and that window plus the
            # device's interval while it does not. Reported rather than left to
            # be derived, so the two moments are never both rendered from the
            # same number.
            "printer_power_off_seconds": self.printer_power_off_seconds(settings),
            "turn_off_delay_seconds": self.turn_off_delay_seconds(settings),

            # ---- the same chain, on the clock ---------------------------- #
            # The server's own now, so a client can measure its skew once and
            # then read every absolute moment below in its own time. Each moment
            # additionally carries its own seconds-until, so a countdown can be
            # anchored per moment rather than on this one value.
            "server_time": now,
            # Where the chain starts, and what that instant actually is. Nothing
            # else in the payload can tell the three apart, and rendering the
            # startup fallback or an idle re-base as "last print" would name a
            # print that never happened.
            "origin_at": origin_at,
            "origin_at_iso": _iso(origin_at),
            "origin_source": origin_source,
            "seconds_since_origin": (
                None if origin_at is None else max(0.0, now - origin_at)
            ),
            # When the printer last actually printed, independent of all that.
            # Null when it has not printed since the app came up. It is reported
            # separately precisely because the origin can be re-based away from
            # it: "the chain runs from here" and "the printer last did something
            # here" are different questions, and only one of them has an answer
            # while the app is idle.
            "last_print_at": last_print_at,
            "last_print_at_iso": _iso(last_print_at),
            "seconds_since_last_print": (
                None if last_print_at is None else max(0.0, now - last_print_at)
            ),
            # When the heartbeat stops, and when the device's own timer then
            # expires. Both null unless a timed keep-alive window is actually
            # running: those are moments in a window the app is holding open,
            # and it is not holding one open otherwise. The hardware offset is
            # already inside these numbers, so with relay power control off the
            # two are the configured window and that window plus the device's
            # interval — the same rule hardware_offset_applied reports.
            "keep_alive_ends_at": keep_alive_ends_at,
            "keep_alive_ends_at_iso": _iso(keep_alive_ends_at),
            "seconds_until_keep_alive_end": self._until(keep_alive_ends_at, now),
            "printer_power_off_at": printer_power_off_at,
            "printer_power_off_at_iso": _iso(printer_power_off_at),
            "seconds_until_printer_power_off": self._until(printer_power_off_at, now),
            # The last step is the *scheduled* moment, not an offset from the
            # origin: it is armed by a print and reset by pending work, so it
            # can legitimately sit further out than origin + window, and it is
            # null while nothing is armed — which is exactly when no turn_off
            # will be sent. It is never projected either, not even from an idle
            # origin: the one step that can cut mains power only ever carries a
            # time it has actually been armed for. An armed moment that is
            # already overdue is reported as it stands, because it really is
            # about to be sent.
            "scheduled_turn_off_at": scheduled,
            "scheduled_turn_off_at_iso": _iso(scheduled),
            "seconds_until_turn_off": self._until(scheduled, now),
            # Which step the chain is waiting on, as of server_time.
            "next_step": next_step,
            "next_step_at": next_step_at,
            "next_step_at_iso": _iso(next_step_at),
            "seconds_until_next_step": self._until(next_step_at, now),

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
