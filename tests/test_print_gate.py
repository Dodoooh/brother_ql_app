"""The pre-job gate: waiting for a printer, and saying so while it waits.

Two things are under test here, and they are two halves of one complaint.

*It fires too early.* The gate used to hand a job over on the first probe that
came back positive, and "positive" meant ``reachable`` -- a bit that is set by a
TCP connect succeeding on port 9100. A booting printer opens that socket well
before its print engine will take a raster, so a job could be pushed at a device
that answered and could not print, while a reprint a moment later worked. The
gate now waits for a *readiness* signal and then waits for it to hold.

*It says nothing while it does that.* A job could sit at ``queued`` for a minute
with the relay switching, the printer booting and the app waiting, none of which
was visible from outside. Every phase now names itself on the job.

Everything runs offline: no printer, no webhook, no real waiting. The durations
are shrunk to milliseconds on the instance, which is what they are instance
attributes for; the shipped constants are asserted for shape in
``test_relay_power.py``.
"""

import os
import threading
import time

import pytest

from src.services import queue_service
from src.services.queue_service import PrintQueueService
from src.services.relay_service import (
    ACTION_TURN_ON,
    PRINTER_STATE_BLOCKED,
    PRINTER_STATE_READY,
    PRINTER_STATE_UNKNOWN,
    PRINTER_STATE_UNREACHABLE,
    RelayPowerService,
    _normalise_probe_result,
)
from src.utils.exceptions import RelayWebhookError
from src.utils.job_activity import (
    ACTIVITY_PRINTER_SETTLING,
    ACTIVITY_PRINTING,
    ACTIVITY_RETRYING,
    ACTIVITY_SWITCHING_ON,
    ACTIVITY_WAITING_FOR_PRINTER,
    JOB_ACTIVITIES,
)

HOUR = 3600


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _settings(**overrides):
    base = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-820NWB",
        "label_size": "62",
        "keep_alive_enabled": True,
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


class _SettingsProvider:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.settings_file = ""

    def get_settings(self):
        return dict(self.settings)


class _Probe:
    """A scripted readiness probe that records when it was asked.

    ``states`` is consumed one entry per call; the last entry repeats forever,
    so a script says what changes and stops there.
    """

    def __init__(self, *states):
        self.states = list(states) or [PRINTER_STATE_UNREACHABLE]
        self.calls = 0
        self.at = []

    def __call__(self, _settings):
        index = min(self.calls, len(self.states) - 1)
        self.calls += 1
        self.at.append(time.monotonic())
        return self.states[index]


class _Sender:
    def __init__(self):
        self.calls = []
        self.at = []

    def __call__(self, url, payload, timeout):
        self.calls.append(payload)
        self.at.append(time.monotonic())
        return 200


class _Reporter:
    """Collects what the gate said it was doing."""

    def __init__(self):
        self.entries = []

    def __call__(self, activity, message=None):
        self.entries.append((activity, message))

    @property
    def activities(self):
        """The tokens, with consecutive repeats collapsed."""
        tokens = []
        for activity, _message in self.entries:
            if not tokens or tokens[-1] != activity:
                tokens.append(activity)
        return tokens


def _service(tmp_path, probe, sender=None, reporter=None, settings=None, **timings):
    """A gate wired to scripted collaborators, with millisecond timings."""
    service = RelayPowerService(
        state_file=str(tmp_path / "relay_power.json"),
        settings_provider=_SettingsProvider(settings or _settings()),
        readiness_probe=probe,
        pending_work_probe=lambda: False,
        sender=sender or _Sender(),
        origin_provider=lambda: (time.time(), True),
        activity_reporter=reporter,
    )
    service.turn_on_waits = (0.30, 0.15)
    service.blind_wait = 0.0
    service.probe_pause = 0.001
    service.ready_settle = 0.02
    service.answering_settle = 0.06
    for name, value in timings.items():
        setattr(service, name, value)
    return service


def _eventually(predicate, timeout=3.0, what="the expected state"):
    """Poll a predicate until it is truthy. Used only to observe a worker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.002)
    raise AssertionError(f"timed out waiting for {what}")


def _queue():
    """A queue with its own worker and no filesystem side effects."""
    queue = PrintQueueService()
    queue._sweep_job_files = lambda: None  # no uploads/ directory in a unit test
    queue.start()
    return queue


# --------------------------------------------------------------------------- #
# What counts as ready
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("result, expected", [
    # The shape the shipped probe returns: readiness is read from `state`, not
    # from the `reachable` bit underneath it. This pair is the whole point --
    # the device answered, and it has not said it can print.
    ({"reachable": True, "state": PRINTER_STATE_UNKNOWN, "blocking_reasons": []},
     (PRINTER_STATE_UNKNOWN, [])),
    ({"reachable": True, "state": PRINTER_STATE_READY, "blocking_reasons": []},
     (PRINTER_STATE_READY, [])),
    ({"reachable": True, "state": PRINTER_STATE_BLOCKED,
      "blocking_reasons": ["cover-open"]}, (PRINTER_STATE_BLOCKED, ["cover-open"])),
    ({"reachable": False, "state": PRINTER_STATE_UNREACHABLE},
     (PRINTER_STATE_UNREACHABLE, [])),
    # A payload with no state at all falls back to reachability.
    ({"reachable": True}, (PRINTER_STATE_READY, [])),
    # Bare strings and bools, for a probe that answers more simply.
    (PRINTER_STATE_READY, (PRINTER_STATE_READY, [])),
    (True, (PRINTER_STATE_READY, [])),
    (False, (PRINTER_STATE_UNREACHABLE, [])),
    # Anything unrecognised delays a job rather than pushing one at a printer
    # that may not be there.
    (None, (PRINTER_STATE_UNREACHABLE, [])),
    (42, (PRINTER_STATE_UNREACHABLE, [])),
])
def test_a_probe_result_is_read_for_readiness_not_liveness(result, expected):
    assert _normalise_probe_result(result) == expected


def test_the_shipped_probe_reports_the_state_rather_than_the_reachable_bit(tmp_path):
    """A TCP-only answer must reach the gate as "unknown", not as "ready".

    ``check_printer_status`` reports both, and the difference between them is
    the difference between "a socket is bound" and "this device will print".
    """
    from unittest.mock import patch

    service = RelayPowerService(
        state_file=str(tmp_path / "s.json"),
        settings_provider=_SettingsProvider(_settings()))
    answer = {"reachable": True, "state": PRINTER_STATE_UNKNOWN,
              "blocking_reasons": [], "status": "Printer reachable (no IPP status)"}
    with patch("src.services.printer_service.printer_service.check_printer_status",
               return_value=answer) as check:
        assert service._probe(_settings()) == (PRINTER_STATE_UNKNOWN, [])
    assert check.called


def test_a_probe_that_raises_is_read_as_no_answer(tmp_path):
    def boom(_settings):
        raise RuntimeError("network down")

    service = _service(tmp_path, boom)
    assert service._probe(_settings()) == (PRINTER_STATE_UNREACHABLE, [])


# --------------------------------------------------------------------------- #
# The settle
# --------------------------------------------------------------------------- #

def test_a_printer_that_states_it_is_ready_releases_the_job_at_once(tmp_path):
    """The printer's own "ready" releases; the attempts cover it speaking early.

    It used to have to hold that for a fixed settle, which made every good cold
    start pay for the bad one -- and no length of settle proves the printer will
    accept a raster. Trying does, so the trying starts here and
    ``PRINT_ATTEMPT_DELAYS_SECONDS`` covers a printer that spoke too soon.
    """
    probe = _Probe(PRINTER_STATE_READY)
    service = _service(tmp_path, probe, probe_pause=0.05)

    started = time.monotonic()
    outcome = service._wait_until_ready(_settings(), 1.0)
    elapsed = time.monotonic() - started

    assert outcome["ready"] is True
    assert outcome["stated_ready"] is True
    assert probe.calls == 1, "asked more than once about a printer that said yes"
    assert elapsed < 0.05, "held a printer that had stated it was ready"


def test_a_printer_that_answers_but_is_not_ready_is_not_taken_as_ready(tmp_path):
    """The early mid-boot answer: reachable, saying nothing about readiness.

    This is the reading the old gate acted on. It must not release here; the
    only thing that releases is readiness that then holds.
    """
    # Answers on a bare TCP connect for four probes, then IPP comes up.
    probe = _Probe(PRINTER_STATE_UNKNOWN, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_UNKNOWN, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_READY)
    service = _service(tmp_path, probe, answering_settle=5.0, probe_pause=0.001)

    outcome = service._wait_until_ready(_settings(), 1.0)

    assert outcome["ready"] is True
    assert outcome["state"] == PRINTER_STATE_READY
    assert outcome["stated_ready"] is True, (
        "it was released on the printer's own readiness, not on it answering")
    assert probe.calls == 5, (
        "released during the answers-but-not-ready window; a gate that fires on "
        "the first positive probe would have released at probe 1")


def test_a_cold_start_hands_back_the_attempt_schedule(tmp_path):
    """The gate tells the queue it is printing into a boot window.

    It says so as data rather than by making the queue know what a relay is: a
    list of pauses, one per attempt. An empty one means "print once, now", and
    that is what a printer which was up all along gets.
    """
    cold = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    service = _service(tmp_path, cold, blind_wait=0.0, probe_pause=0.001)
    service.print_attempt_delays = (5.0, 20.0, 20.0)

    assert service.ensure_printer_powered()["delays"] == (5.0, 20.0, 20.0)

    warm = _Probe(PRINTER_STATE_READY)
    service = _service(tmp_path, warm, probe_pause=0.001)
    service.print_attempt_delays = (5.0, 20.0, 20.0)

    answer = service.ensure_printer_powered()
    assert answer["delays"] == (), (
        "a printer that was already up is printed to once, like it always was")
    assert answer["message"] is None, "nothing was waited for, so nothing is described"


def test_the_gate_says_it_was_the_printer_that_reported_itself_ready(tmp_path):
    """Only the gate knows what it released a job on, so only it can say.

    The pause before the first attempt is a grace after the release, and the
    queue cannot describe it truthfully: it does not know what a printer is,
    let alone whether this one said anything. So the gate hands the sentence
    over with the schedule.
    """
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    service = _service(tmp_path, probe, blind_wait=0.0, probe_pause=0.001)
    service.print_attempt_delays = (5.0, 20.0, 20.0)

    message = service.ensure_printer_powered()["message"]

    assert "The printer reports itself ready" in message, (
        "it did report itself ready, and that is worth saying")
    assert "5s" in message, "the wording names the pause it is describing"


@pytest.mark.parametrize("state", [PRINTER_STATE_UNKNOWN, PRINTER_STATE_BLOCKED])
def test_the_gate_does_not_claim_a_readiness_it_never_heard(tmp_path, state):
    """The other way a job is released, and it must not borrow the first's words.

    A printer with IPP switched off has no readiness to state, and one reporting
    a blocking condition has stated the opposite. Both are handed the job anyway,
    on the strength of having answered steadily -- deliberately, because the gate
    waits for printers to come up and does not adjudicate whether they can print.
    What it must not do is tell the user the printer said it was ready.
    """
    probe = _Probe(PRINTER_STATE_UNREACHABLE, state)
    service = _service(tmp_path, probe, blind_wait=0.0, probe_pause=0.001,
                       answering_settle=0.02)
    service.print_attempt_delays = (5.0, 20.0, 20.0)

    answer = service.ensure_printer_powered()

    assert answer["delays"] == (5.0, 20.0, 20.0), "it was still switched on here"
    assert "The printer reports itself ready" not in answer["message"]
    assert "has not reported itself ready" in answer["message"]
    assert "5s" in answer["message"]


def test_a_schedule_without_a_first_pause_has_nothing_to_describe(tmp_path):
    """No pause, no sentence: the queue would have nowhere to show it."""
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    service = _service(tmp_path, probe, blind_wait=0.0, probe_pause=0.001)
    service.print_attempt_delays = (0.0, 20.0)

    assert service.ensure_printer_powered()["message"] is None


def test_the_settle_looks_more_often_than_the_wait_that_precedes_it(tmp_path):
    """A drop-out shorter than the pause between probes is one nobody sees.

    Measured on the printer this was built against: the IPP port stops
    accepting about eight seconds after it first answers and comes back 1.4 s
    later, while ICMP and port 9100 never falter -- the print service rebinding,
    not the network going away. A gate sampling every 2 s can step straight over
    a hole that size, and a settle that never sees the hole will certify a
    printer that disappeared in the middle of it.

    So the cadence tightens the moment anything answers. That is also where it
    is affordable: a probe costs 0.09 s against a printer that answers and 3.5 s
    against one that does not.

    Only the answering settle is left to guard: a printer that states its own
    readiness is released on that reading, and the print attempts cover it.
    """
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_UNREACHABLE,
                   PRINTER_STATE_UNKNOWN)
    service = _service(tmp_path, probe, blind_wait=0.0, probe_pause=0.05,
                       settle_probe_pause=0.002, answering_settle=0.05)

    outcome = service._wait_until_ready(_settings(), 2.0)

    assert outcome["ready"] is True
    # Probes 1 and 2 found nothing; probe 3 was the first answer, so every gap
    # from there on is the settle's own cadence.
    assert probe.at[1] - probe.at[0] >= 0.05, "hurried the empty part of the wait"
    settle_gaps = [b - a for a, b in zip(probe.at[2:], probe.at[3:], strict=False)]
    assert settle_gaps, "the settle spanned a single probe"
    assert max(settle_gaps) < 0.05, (
        "kept the slow cadence through the settle, where a short drop-out hides")


def test_a_printer_that_never_states_its_readiness_is_still_printed_to(tmp_path):
    """No IPP means no readiness signal, and those printers still have to work.

    Time is the only evidence available, so more of it is required -- but the
    answer is yes in the end, or a printer with IPP switched off could never
    print again.
    """
    probe = _Probe(PRINTER_STATE_UNKNOWN)
    service = _service(tmp_path, probe, ready_settle=0.01, answering_settle=0.05,
                       probe_pause=0.002)

    started = time.monotonic()
    outcome = service._wait_until_ready(_settings(), 1.0)
    elapsed = time.monotonic() - started

    assert outcome["ready"] is True
    assert outcome["stated_ready"] is False, "it never said so itself"
    assert elapsed >= 0.05, "the longer settle applies when nothing states readiness"


def test_a_printer_reporting_a_blocking_condition_waits_out_the_longer_settle(tmp_path):
    """A blocked printer is handed the job, late, rather than failed by the gate.

    Brother firmware reports media-empty transiently while booting. The gate's
    job is to wait for the printer to come up, not to decide whether it can
    print: the printer gives the real error far better than the gate can guess
    one.
    """
    probe = _Probe({"state": PRINTER_STATE_BLOCKED,
                    "blocking_reasons": ["media-empty"]})
    service = _service(tmp_path, probe, ready_settle=0.01, answering_settle=0.05,
                       probe_pause=0.002)

    started = time.monotonic()
    outcome = service._wait_until_ready(_settings(), 1.0)

    assert outcome["ready"] is True
    assert outcome["stated_ready"] is False
    assert outcome["blocking_reasons"] == ["media-empty"]
    assert time.monotonic() - started >= 0.05


# --------------------------------------------------------------------------- #
# The blind wait
# --------------------------------------------------------------------------- #

def test_the_printer_is_not_probed_at_all_until_it_has_had_time_to_boot(tmp_path):
    """Nothing can answer in the first seconds after mains-on, so nothing asks.

    Each unanswered probe costs 3.5 s of wall clock (a 2 s IPP timeout plus a
    1.5 s TCP connect), so probing into the boot window does not merely learn
    nothing, it spends the budget.
    """
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    sender = _Sender()
    service = _service(tmp_path, probe, sender=sender,
                       blind_wait=0.08, ready_settle=0.001, probe_pause=0.001)

    service.ensure_printer_powered()

    assert len(sender.calls) == 1
    # Probe 1 is the pre-check, before the webhook. Probe 2 is the first look
    # after it, and it must be a whole blind wait later.
    assert probe.at[1] - sender.at[0] >= 0.08, (
        "the printer was probed before it could possibly have booted")


def test_the_waits_stay_ceilings_and_a_quick_printer_is_not_held_back(tmp_path):
    """A printer ready early prints early; it is not made to wait out the window."""
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    service = _service(tmp_path, probe, blind_wait=0.0, ready_settle=0.01,
                       probe_pause=0.001)
    service.turn_on_waits = (5.0, 2.5)   # a window far longer than this can take

    started = time.monotonic()
    service.ensure_printer_powered()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"a printer that came up immediately waited {elapsed:.2f}s of a 5s window")


def test_a_printer_that_already_answers_is_left_entirely_alone(tmp_path):
    """No webhook, no wait, no settle: nothing was switched on, so nothing booted."""
    probe = _Probe(PRINTER_STATE_READY)
    sender = _Sender()
    reporter = _Reporter()
    service = _service(tmp_path, probe, sender=sender, reporter=reporter,
                       blind_wait=5.0, ready_settle=5.0)

    started = time.monotonic()
    service.ensure_printer_powered()

    assert time.monotonic() - started < 0.5
    assert sender.calls == [], "a printer that is up does not need switching on"
    assert probe.calls == 1, "one look was enough"
    assert reporter.entries == [], "nothing worth reporting happened"


@pytest.mark.parametrize("state", [PRINTER_STATE_UNKNOWN, PRINTER_STATE_BLOCKED])
def test_a_printer_that_answers_without_ipp_is_also_left_alone(tmp_path, state):
    """Anything answering means the mains are already on, so the relay is not it.

    Making these wait would tax every job on an IPP-less printer with a settle
    that exists to cover a boot which is not happening.
    """
    probe = _Probe(state)
    sender = _Sender()
    service = _service(tmp_path, probe, sender=sender, blind_wait=5.0,
                       answering_settle=5.0)

    started = time.monotonic()
    service.ensure_printer_powered()

    assert time.monotonic() - started < 0.5
    assert sender.calls == []
    assert probe.calls == 1


# --------------------------------------------------------------------------- #
# The bounded failure
# --------------------------------------------------------------------------- #

def test_the_wait_is_bounded_and_the_failure_says_what_was_done(tmp_path):
    probe = _Probe(PRINTER_STATE_UNREACHABLE)
    sender = _Sender()
    service = _service(tmp_path, probe, sender=sender, blind_wait=0.01,
                       probe_pause=0.001)
    service.turn_on_waits = (0.05, 0.02)

    started = time.monotonic()
    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, "the wait is bounded, not open-ended"
    assert [call["action"] for call in sender.calls] == [ACTION_TURN_ON, ACTION_TURN_ON]

    message = str(exc.value)
    assert "did not come up" in message
    assert "2 turn_on webhook(s) delivered" in message
    assert "left alone for" in message, "names the wait before the first look"
    assert f"checked {probe.calls - 1} times" in message, (
        "names the looks actually taken, not a nominal interval")


def test_a_printer_that_answered_but_never_settled_is_described_as_such(tmp_path):
    """"Never answered" and "answered, never steadily" want different fixes."""
    # Answers on alternate probes and so never holds long enough for either
    # settle: exactly the flapping device the settle exists to catch. The first
    # answer is "nothing there", or the gate would never switch the relay on.
    flips = {"n": 0}

    def flapping(_settings):
        flips["n"] += 1
        return (PRINTER_STATE_UNKNOWN if flips["n"] % 2 == 0
                else PRINTER_STATE_UNREACHABLE)

    service = _service(tmp_path, flapping, blind_wait=0.0, probe_pause=0.001,
                       ready_settle=0.05, answering_settle=0.05)
    service.turn_on_waits = (0.05, 0.02)

    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()

    message = str(exc.value)
    assert "It did answer at some point" in message
    assert "never steadily enough to print to" in message
    # And it still enumerates the three causes, which is what pointed at the
    # real one the first time this failed on hardware.
    assert "the relay did not close" in message


def test_a_flapping_printer_cannot_stretch_the_wait_indefinitely(tmp_path):
    """The settle may overrun the deadline, but only by one settle."""
    flips = {"n": 0}

    def flapping(_settings):
        flips["n"] += 1
        return (PRINTER_STATE_UNKNOWN if flips["n"] % 2 else PRINTER_STATE_UNREACHABLE)

    service = _service(tmp_path, flapping, probe_pause=0.001,
                       ready_settle=0.02, answering_settle=0.05)

    started = time.monotonic()
    outcome = service._wait_until_ready(_settings(), 0.05)
    elapsed = time.monotonic() - started

    assert outcome["ready"] is False
    assert elapsed < 0.05 + 0.05 + 0.05, "overran by more than one settle"


def test_a_delivery_failure_is_still_raised_straight_away(tmp_path):
    """A webhook that did not arrive is not waited out; it is reported."""
    def refuse(_url, _payload, _timeout):
        raise RelayWebhookError("Relay webhook returned HTTP 500")

    probe = _Probe(PRINTER_STATE_UNREACHABLE)
    service = _service(tmp_path, probe, sender=refuse, blind_wait=5.0)

    started = time.monotonic()
    with pytest.raises(RelayWebhookError) as exc:
        service.ensure_printer_powered()

    assert "HTTP 500" in str(exc.value)
    assert time.monotonic() - started < 0.5, "it did not sit out a boot window first"


# --------------------------------------------------------------------------- #
# Saying what it is doing
# --------------------------------------------------------------------------- #

def test_the_gate_names_every_phase_it_passes_through(tmp_path):
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_UNREACHABLE,
                   PRINTER_STATE_READY)
    reporter = _Reporter()
    service = _service(tmp_path, probe, reporter=reporter, blind_wait=0.01,
                       probe_pause=0.001)

    service.ensure_printer_powered()

    # Settling is no longer one of the gate's phases for a printer that states
    # its readiness: it is released there and then, and the queue names the
    # pause before the first attempt instead.
    assert reporter.activities == [
        ACTIVITY_SWITCHING_ON,
        ACTIVITY_WAITING_FOR_PRINTER,
    ]
    # Every token is one the API declares, and every one carries wording.
    for activity, message in reporter.entries:
        assert activity in JOB_ACTIVITIES
        assert message and isinstance(message, str)


def test_the_second_attempt_says_it_is_a_second_attempt(tmp_path):
    probe = _Probe(PRINTER_STATE_UNREACHABLE)
    sender = _Sender()
    reporter = _Reporter()
    service = _service(tmp_path, probe, sender=sender, reporter=reporter,
                       blind_wait=0.0, probe_pause=0.001)
    service.turn_on_waits = (0.02, 0.02)

    with pytest.raises(RelayWebhookError):
        service.ensure_printer_powered()

    switching = [message for activity, message in reporter.entries
                 if activity == ACTIVITY_SWITCHING_ON]
    assert len(switching) == 2
    assert "attempt 2 of 2" in switching[1]


def test_only_a_printer_that_will_not_state_its_readiness_is_made_to_settle(tmp_path):
    """The one wait left, and it says which case it is.

    A printer that answers without stating readiness gives nothing to release
    on, so time is the only evidence there is and the gate spends it. The same
    printer stating "ready" one probe later ends that immediately.
    """
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_UNKNOWN, PRINTER_STATE_READY)
    reporter = _Reporter()
    service = _service(tmp_path, probe, reporter=reporter, blind_wait=0.0,
                       answering_settle=5.0, probe_pause=0.001)

    service.ensure_printer_powered()

    settling = [message for activity, message in reporter.entries
                if activity == ACTIVITY_PRINTER_SETTLING]
    assert settling, "the answering-but-silent phase went unnamed"
    assert all("has not reported itself ready" in message for message in settling)
    assert probe.calls == 4, "the settle outlived the printer stating it was ready"


def test_a_reporter_that_throws_never_reaches_the_job(tmp_path):
    """Describing a job must not be a way of failing it."""
    def broken(_activity, _message=None):
        raise RuntimeError("no queue")

    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_READY)
    service = _service(tmp_path, probe, reporter=broken, blind_wait=0.0,
                       ready_settle=0.001, probe_pause=0.001)

    service.ensure_printer_powered()  # must not raise


# --------------------------------------------------------------------------- #
# ... and it reaching the queue
# --------------------------------------------------------------------------- #

def test_the_activity_reaches_the_job_and_the_queue_status(tmp_path):
    """One value, read from one place, by both endpoints the UI polls."""
    queue = _queue()
    reached = threading.Event()
    release = threading.Event()

    def gate():
        queue.report_activity(ACTIVITY_WAITING_FOR_PRINTER,
                              "Waiting for the printer to come up.")
        reached.set()
        release.wait(3.0)

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)

    job = queue.get(job_id)
    assert job["status"] == "queued", "the status enum is untouched"
    assert job["activity"] == ACTIVITY_WAITING_FOR_PRINTER
    assert job["activity_message"] == "Waiting for the printer to come up."
    assert job["activity_at"]

    status = queue.queue_status()
    assert status["queued"] == 1
    assert status["activity"] == ACTIVITY_WAITING_FOR_PRINTER
    assert status["activity_message"] == job["activity_message"]
    assert status["activity_job_id"] == job_id

    # And through the API controller the UI actually calls.
    from unittest.mock import patch

    from src.api import jobs_controller
    with patch.object(jobs_controller, "print_queue", queue):
        listed = jobs_controller.list_jobs()["jobs"][0]
        assert listed["activity"] == ACTIVITY_WAITING_FOR_PRINTER
        assert jobs_controller.get_queue_status()["activity"] == (
            ACTIVITY_WAITING_FOR_PRINTER)

    release.set()
    _eventually(lambda: queue.get(job_id)["status"] == "done", what="the job to finish")


def test_the_activity_survives_being_read_twice(tmp_path):
    """It is state, not an event: polling it does not consume it.

    A value that vanished after the first read would flicker to nothing between
    two polls of a job that had not changed, and would make ``GET /jobs`` and
    ``GET /jobs/queue`` disagree depending on which was called first.
    """
    queue = _queue()
    reached = threading.Event()
    release = threading.Event()

    def gate():
        queue.report_activity(ACTIVITY_SWITCHING_ON)
        reached.set()
        release.wait(3.0)

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)

    seen = [queue.get(job_id)["activity"] for _ in range(5)]
    assert seen == [ACTIVITY_SWITCHING_ON] * 5
    assert [queue.queue_status()["activity"] for _ in range(3)] == (
        [ACTIVITY_SWITCHING_ON] * 3)

    release.set()
    _eventually(lambda: queue.get(job_id)["status"] == "done", what="the job to finish")


def test_the_queue_reports_each_stage_in_turn(tmp_path):
    """Switching on, waiting, settling, printing -- then nothing."""
    queue = _queue()
    seen = []

    def gate():
        for activity in (ACTIVITY_SWITCHING_ON, ACTIVITY_WAITING_FOR_PRINTER,
                         ACTIVITY_PRINTER_SETTLING):
            queue.report_activity(activity)
            seen.append(queue.queue_status()["activity"])

    def printing():
        seen.append(queue.queue_status()["activity"])

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done", what="the job to finish")

    assert seen == [ACTIVITY_SWITCHING_ON, ACTIVITY_WAITING_FOR_PRINTER,
                    ACTIVITY_PRINTER_SETTLING, ACTIVITY_PRINTING]

    finished = queue.get(job_id)
    assert finished["status"] == "done"
    assert finished["activity"] is None, "a finished job is not doing anything"
    assert finished["activity_message"] is None
    assert finished["activity_at"] is None
    assert queue.queue_status()["activity"] is None
    assert queue.queue_status()["activity_job_id"] is None


# --------------------------------------------------------------------------- #
# Printing into a boot window
#
# The gate releases a job the moment the printer states it is ready, which is a
# claim a device eight seconds into its boot is quite capable of making and then
# withdrawing. What covers that is trying again, not waiting longer: the attempt
# is the only test of readiness that cannot be wrong.
# --------------------------------------------------------------------------- #

def test_a_first_attempt_that_lands_in_the_boot_is_tried_again(tmp_path):
    """Two refusals from a printer that had just been switched on, then a label."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Printer is not ready")

    queue.set_pre_job_gate(lambda: (0.0, 0.01, 0.01))
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 3
    job = queue.get(job_id)
    assert job["error"] is None, "a job that printed is not carrying a failure"


def test_only_the_last_attempt_decides_the_job(tmp_path):
    """Three refusals is a printer saying no, and the last word is its own."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1
        raise RuntimeError(f"Printer is not ready (attempt {calls['n']})")

    queue.set_pre_job_gate(lambda: (0.0, 0.01, 0.01))
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "failed",
                what="the job to fail")

    assert calls["n"] == 3, "gave up before the schedule was spent"
    assert queue.get(job_id)["error"] == "Printer is not ready (attempt 3)"


def test_a_failure_that_reached_the_printer_is_not_tried_again(tmp_path, monkeypatch):
    """Half a PDF is not a reason to print the first half again.

    A refusal before anything went out costs nothing to repeat. A failure
    *after* bytes reached the printer may mean part of the job printed -- a
    page, one of several copies -- and repeating it prints that part twice. The
    write counter is what tells the two apart.
    """
    queue = _queue()
    calls = {"n": 0}
    writes = {"n": 0}
    monkeypatch.setattr(queue_service, "_writes_begun", lambda: writes["n"])

    def printing():
        calls["n"] += 1
        writes["n"] += 1        # bytes went out...
        raise RuntimeError("Printer went away mid-page")   # ...and then it broke

    queue.set_pre_job_gate(lambda: (0.0, 0.01, 0.01))
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "failed",
                what="the job to fail")

    assert calls["n"] == 1, "reprinted a job that had already reached the printer"
    assert queue.get(job_id)["error"] == "Printer went away mid-page"


def test_a_failure_that_never_reached_the_printer_is_tried_again(tmp_path, monkeypatch):
    """The other half of the same rule, so it cannot pass by refusing always."""
    queue = _queue()
    calls = {"n": 0}
    monkeypatch.setattr(queue_service, "_writes_begun", lambda: 7)  # never moves

    def printing():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("Connection refused")

    queue.set_pre_job_gate(lambda: (0.0, 0.01, 0.01))
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 2


def test_stopping_the_queue_ends_the_attempt_schedule(tmp_path):
    """Stop means stop, including the attempts a failed job still had coming.

    A job whose print is in flight cannot be cancelled -- it is not queued -- so
    stopping the queue used to leave its remaining attempts to run, minutes
    after everything else had halted.
    """
    queue = _queue()
    calls = {"n": 0}
    failed_once = threading.Event()

    def printing():
        calls["n"] += 1
        failed_once.set()
        raise RuntimeError("Printer is not ready")

    queue.set_pre_job_gate(lambda: (0.0, 2.0, 2.0))
    job_id = queue.submit("text", "Hello", printing)
    assert failed_once.wait(3.0)

    queue.pause()
    _eventually(lambda: queue.get(job_id)["status"] == "failed",
                what="the job to give up")

    assert calls["n"] == 1, "kept printing after the queue was stopped"
    assert queue.get(job_id)["error"] == "Printer is not ready", (
        "the printer's own words are what the job failed with")


def test_a_printer_that_was_already_up_is_not_retried(tmp_path):
    """Retrying there would hide a real fault behind three identical failures.

    The schedule guards the window after this app switched a printer on. A job
    at a printer that was up all along either prints or does not, exactly as
    before, and a bad label size is not something to try three times.
    """
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1
        raise RuntimeError("Unsupported label size")

    queue.set_pre_job_gate(lambda: ())  # nothing was switched on
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "failed",
                what="the job to fail")

    assert calls["n"] == 1
    assert queue.get(job_id)["error"] == "Unsupported label size"


def test_a_gate_that_says_nothing_prints_once_as_it_always_did(tmp_path):
    """Every gate returned None before the schedule existed."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1

    queue.set_pre_job_gate(lambda: None)
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 1


def test_a_job_waiting_for_its_next_attempt_is_queued_and_cancellable(tmp_path):
    """Between attempts the job is not on the wire, and says so two ways.

    It reads as ``queued`` with a ``retrying`` activity naming the failure it is
    waiting out -- and because it is queued, it can still be cancelled, which is
    the whole difference between a wait somebody can act on and one they cannot.
    """
    queue = _queue()
    calls = {"n": 0}
    failed_once = threading.Event()

    def printing():
        calls["n"] += 1
        failed_once.set()
        raise RuntimeError("Printer is not ready")

    queue.set_pre_job_gate(lambda: (0.0, 2.0, 2.0))
    job_id = queue.submit("text", "Hello", printing)
    assert failed_once.wait(3.0)

    waiting = _eventually(
        lambda: (queue.get(job_id)["activity"] == ACTIVITY_RETRYING
                 and queue.get(job_id)),
        what="the job to be waiting for its next attempt")
    assert waiting["status"] == "queued", "a job between attempts is not printing"
    assert "Printer is not ready" in waiting["activity_message"], (
        "the wait does not say what it is waiting out")
    assert "Attempt 1 of 3" in waiting["activity_message"]

    assert queue.cancel(job_id) is True
    _eventually(lambda: queue.queue_status()["activity"] is None,
                what="the worker to move on")
    assert calls["n"] == 1, "printed at a printer nobody was waiting for any more"
    assert queue.get(job_id)["status"] == "cancelled"


def test_the_pause_before_the_first_attempt_is_a_grace_and_not_a_retry(tmp_path):
    """The first pause is a grace, not a failure, and is worded as one.

    A gate that hands back a bare schedule says nothing about why, so the queue
    says only what it can see from where it stands: a pause is running before
    the first attempt. It used to assert here that the printer had reported
    itself ready -- which the queue has no way of knowing, and which is untrue
    whenever the gate released the job on it merely answering.
    """
    queue = _queue()
    printed = threading.Event()

    queue.set_pre_job_gate(lambda: (2.0, 2.0, 2.0))
    job_id = queue.submit("text", "Hello", printed.set)

    settling = _eventually(
        lambda: (queue.get(job_id)["activity"] == ACTIVITY_PRINTER_SETTLING
                 and queue.get(job_id)),
        what="the pause before the first attempt")
    assert settling["status"] == "queued"
    assert settling["activity_message"] == "Waiting 2s before the first print attempt."
    assert "reports itself ready" not in settling["activity_message"], (
        "claimed a readiness nobody told the queue about")
    assert not printed.is_set(), "printed before the grace had run"

    queue.cancel(job_id)
    _eventually(lambda: queue.queue_status()["activity"] is None,
                what="the worker to move on")


@pytest.mark.parametrize("said", [
    "The printer reports itself ready. Giving it 5s before printing.",
    "The printer is answering but has not reported itself ready. Giving it 5s "
    "before printing anyway.",
])
def test_the_first_pause_shows_the_gate_wording_verbatim(tmp_path, said):
    """Both of the gate's answers reach the job unaltered.

    The queue displays the sentence and never inspects it, which is what lets
    the gate distinguish the two releases without the queue learning what a
    printer state is. Both wordings are exercised so a change to either is a
    change this test sees.
    """
    queue = _queue()
    printed = threading.Event()

    queue.set_pre_job_gate(lambda: {"delays": (2.0, 2.0), "message": said})
    job_id = queue.submit("text", "Hello", printed.set)

    settling = _eventually(
        lambda: (queue.get(job_id)["activity"] == ACTIVITY_PRINTER_SETTLING
                 and queue.get(job_id)),
        what="the pause before the first attempt")
    assert settling["activity_message"] == said
    assert settling["status"] == "queued", "a job in the grace is not on the wire"

    queue.cancel(job_id)
    _eventually(lambda: queue.queue_status()["activity"] is None,
                what="the worker to move on")


def test_the_gate_wording_covers_only_the_first_pause(tmp_path):
    """Later pauses follow a failure, and the queue owns that story.

    It has the printer's own refusal in hand there, which is more use than
    anything the gate could have said before the job was ever tried.
    """
    queue = _queue()
    failed_once = threading.Event()

    def printing():
        failed_once.set()
        raise RuntimeError("Printer is not ready")

    queue.set_pre_job_gate(lambda: {
        "delays": (0.0, 2.0, 2.0),
        "message": "The printer reports itself ready. Giving it 5s before printing.",
    })
    job_id = queue.submit("text", "Hello", printing)
    assert failed_once.wait(3.0)

    waiting = _eventually(
        lambda: (queue.get(job_id)["activity"] == ACTIVITY_RETRYING
                 and queue.get(job_id)),
        what="the job to be waiting for its next attempt")
    assert "Attempt 1 of 3" in waiting["activity_message"]
    assert "Printer is not ready" in waiting["activity_message"]
    assert "reports itself ready" not in waiting["activity_message"]

    queue.cancel(job_id)
    _eventually(lambda: queue.queue_status()["activity"] is None,
                what="the worker to move on")


def test_a_schedule_handed_over_as_a_mapping_still_schedules_the_attempts(tmp_path):
    """The wording rides along with the schedule; it does not replace it."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("Printer is not ready")

    queue.set_pre_job_gate(lambda: {"delays": (0.0, 0.01, 0.01),
                                    "message": "Giving it a moment."})
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 3


@pytest.mark.parametrize("answer", [
    {},                                   # a mapping saying nothing
    {"message": "words, no schedule"},    # wording without a schedule to hang it on
    {"delays": "5, 20, 20"},              # a schedule that is not one
])
def test_a_mapping_the_queue_cannot_use_prints_once_rather_than_failing(tmp_path,
                                                                        answer):
    """Still advice from a plain callable, in the richer shape as in the plain one."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1

    queue.set_pre_job_gate(lambda: answer)
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 1
    assert queue.get(job_id)["activity"] is None


def test_a_gate_returning_nonsense_prints_once_rather_than_failing(tmp_path):
    """The schedule is advice from a plain callable, not a contract."""
    queue = _queue()
    calls = {"n": 0}

    def printing():
        calls["n"] += 1

    queue.set_pre_job_gate(lambda: "5, 20, 20")
    job_id = queue.submit("text", "Hello", printing)
    _eventually(lambda: queue.get(job_id)["status"] == "done",
                what="the job to print")

    assert calls["n"] == 1


def test_a_gate_failure_is_described_by_the_error_not_by_a_stale_activity(tmp_path):
    """The message the gate raises says what it was doing and for how long."""
    queue = _queue()
    message = ("Printer did not come up within 220s of the relay being switched "
               "on (2 turn_on webhook(s) delivered to http://relay/on; ...).")

    def gate():
        queue.report_activity(ACTIVITY_WAITING_FOR_PRINTER)
        raise RelayWebhookError(message)

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    _eventually(lambda: queue.get(job_id)["status"] == "failed",
                what="the job to fail")

    job = queue.get(job_id)
    assert job["error"] == message, "the whole account is in the error"
    assert job["activity"] is None, "a failed job is not still waiting"
    assert job["activity_message"] is None
    assert queue.queue_status()["activity"] is None


def test_cancelling_a_job_in_the_gate_stops_it_claiming_to_be_busy(tmp_path):
    queue = _queue()
    reached = threading.Event()
    release = threading.Event()

    def gate():
        queue.report_activity(ACTIVITY_WAITING_FOR_PRINTER)
        reached.set()
        release.wait(3.0)

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)

    assert queue.cancel(job_id) is True
    job = queue.get(job_id)
    assert job["status"] == "cancelled"
    assert job["activity"] is None

    release.set()
    _eventually(lambda: queue.queue_status()["activity"] is None,
                what="the worker to move on")


def test_a_cancelled_job_takes_no_more_of_what_the_gate_says(tmp_path):
    """The gate cannot be interrupted, so its later reports must not land.

    Cancelling a job held by the gate does not stop the gate -- it is a plain
    callable that may have a printer's whole boot still ahead of it, up to a few
    minutes. It goes on reporting each phase to a queue that hands those reports
    to whichever job the worker has in hand, which is still this one. Written
    through, they put a live activity back onto a job the user has cancelled,
    and the UI shows "waiting for the printer" for minutes on an empty queue.
    """
    queue = _queue()
    reached = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    accepted = []

    def gate():
        queue.report_activity(ACTIVITY_SWITCHING_ON)
        reached.set()
        assert cancelled.wait(3.0)
        # Exactly what the real gate does next: name the phases it goes through.
        accepted.append(queue.report_activity(ACTIVITY_WAITING_FOR_PRINTER))
        accepted.append(queue.report_activity(
            ACTIVITY_PRINTER_SETTLING, "The printer is answering."))
        finished.set()

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)
    assert queue.get(job_id)["activity"] == ACTIVITY_SWITCHING_ON

    assert queue.cancel(job_id) is True
    cancelled.set()
    assert finished.wait(3.0), "the gate did not run to the end of its own wait"

    assert accepted == [False, False], (
        "a terminal job accepted an activity, or the gate was told it had")
    job = queue.get(job_id)
    assert job["status"] == "cancelled"
    assert job["activity"] is None, "a cancelled job was made to look busy again"
    assert job["activity_message"] is None
    assert job["activity_at"] is None


def test_the_queue_is_never_idle_and_busy_in_the_same_answer(tmp_path):
    """queued=0, printing=0 and "here is what is happening" cannot both be true.

    They were: the counts stopped including a job the moment it was cancelled,
    while the activity carried on being written to it by the gate that was still
    holding it. One lock, one read, and now one story.
    """
    queue = _queue()
    reached = threading.Event()
    cancelled = threading.Event()
    reported = threading.Event()

    def gate():
        reached.set()
        assert cancelled.wait(3.0)
        queue.report_activity(ACTIVITY_PRINTER_SETTLING,
                              "The printer is answering.")
        reported.set()

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)
    assert queue.cancel(job_id) is True
    cancelled.set()
    assert reported.wait(3.0)

    status = queue.queue_status()
    assert status["queued"] == 0 and status["printing"] == 0
    assert status["activity"] is None
    assert status["activity_message"] is None
    assert status["activity_at"] is None
    assert status["activity_job_id"] is None


def test_a_gate_that_is_still_running_is_work_the_queue_has_in_hand(tmp_path):
    """Cancelling the job does not un-switch-on the printer the gate is booting.

    The counts alone said the queue was idle from the moment of the cancel, and
    the one thing that reads them is the check that stops mains power being cut
    while there is work. A gate mid-boot is work: it has already closed the relay
    and is waiting for the device to come up.
    """
    queue = _queue()
    reached = threading.Event()
    release = threading.Event()

    def gate():
        reached.set()
        release.wait(3.0)

    queue.set_pre_job_gate(gate)
    job_id = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)
    assert queue.has_pending_work() is True

    assert queue.cancel(job_id) is True
    status = queue.queue_status()
    assert status["queued"] == 0 and status["printing"] == 0, (
        "the counts really do read as idle here; that is the whole problem")
    assert queue.has_pending_work() is True, (
        "the gate is still holding a printer it switched on")

    release.set()
    _eventually(lambda: queue.has_pending_work() is False,
                what="the gate to finish and the queue to fall idle")


def test_the_next_job_is_unaffected_by_the_one_cancelled_under_the_gate(tmp_path):
    """The gate ends its run normally, and the worker carries straight on."""
    queue = _queue()
    reached = threading.Event()
    release = threading.Event()
    gates = {"n": 0}

    def gate():
        gates["n"] += 1
        if gates["n"] == 1:
            queue.report_activity(ACTIVITY_WAITING_FOR_PRINTER)
            reached.set()
            release.wait(3.0)
            queue.report_activity(ACTIVITY_PRINTER_SETTLING)

    queue.set_pre_job_gate(gate)
    doomed = queue.submit("text", "Hello", lambda: None)
    assert reached.wait(3.0)
    assert queue.cancel(doomed) is True

    printed = threading.Event()
    second = queue.submit("text", "Again", printed.set)
    release.set()

    assert printed.wait(3.0), "the worker did not get past the cancelled job"
    _eventually(lambda: queue.get(second)["status"] == "done",
                what="the second job to be recorded as done")
    assert queue.get(second)["activity"] is None
    assert queue.get(doomed)["activity"] is None
    assert gates["n"] == 2, "the second job ran the gate exactly once"


def test_a_job_simply_waiting_its_turn_reports_no_activity(tmp_path):
    """Null means "nothing in particular", which the status already covers."""
    queue = PrintQueueService()
    queue._sweep_job_files = lambda: None
    job_id = queue.submit("text", "Hello", lambda: None)   # no worker started

    job = queue.get(job_id)
    assert job["status"] == "queued"
    assert job["activity"] is None
    assert job["activity_message"] is None
    assert job["activity_at"] is None
    assert queue.queue_status()["activity"] is None


def test_reporting_with_no_job_in_hand_is_a_quiet_no_op(tmp_path):
    """The gate can be called outside the worker; it must not blow up."""
    queue = PrintQueueService()
    assert queue.report_activity(ACTIVITY_SWITCHING_ON) is False
    assert queue.queue_status()["activity"] is None


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #

def _spec():
    yaml_module = pytest.importorskip("yaml")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "api", "openapi.yaml")
    with open(path, encoding="utf-8") as handle:
        return yaml_module.safe_load(handle)


@pytest.mark.parametrize("schema_name", ["JobStatus", "QueueStatus"])
def test_the_declared_activity_enum_is_the_one_the_app_emits(schema_name):
    declared = _spec()["components"]["schemas"][schema_name]["properties"]["activity"]
    assert declared["nullable"] is True
    assert declared["enum"] == list(JOB_ACTIVITIES) + [None]


@pytest.mark.parametrize("schema_name", ["JobStatus", "QueueStatus"])
def test_the_activity_enum_is_not_read_as_yaml_booleans(schema_name):
    """Bare on, off, yes and no parse as booleans and silently break an enum.

    None of the tokens is one of those words, and this is the check that
    notices if one ever becomes one.
    """
    properties = _spec()["components"]["schemas"][schema_name]["properties"]
    for field, schema in properties.items():
        for value in schema.get("enum", []):
            assert value is None or isinstance(value, str), (
                f"{schema_name}.{field} has a non-string enum member {value!r}; "
                "quote it in the YAML")


def test_the_status_enum_was_not_widened():
    """Activity is a detail alongside the status, not a sixth status.

    Redefining what `queued` means, or adding to the set, breaks every client
    that already switches on it.
    """
    declared = _spec()["components"]["schemas"]["JobStatus"]["properties"]["status"]
    assert declared["enum"] == ["queued", "printing", "done", "failed", "cancelled"]


def test_every_field_a_job_carries_is_declared_in_the_spec():
    queue = PrintQueueService()
    queue._sweep_job_files = lambda: None
    job_id = queue.submit("text", "Hello", lambda: None)
    declared = set(_spec()["components"]["schemas"]["JobStatus"]["properties"])
    assert set(queue.get(job_id)) <= declared


def test_every_field_the_queue_status_carries_is_declared_in_the_spec():
    queue = PrintQueueService()
    declared = set(_spec()["components"]["schemas"]["QueueStatus"]["properties"])
    assert set(queue.queue_status()) <= declared
