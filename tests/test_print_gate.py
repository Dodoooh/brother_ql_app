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

def test_the_first_ready_probe_is_not_enough_on_its_own(tmp_path):
    """Readiness has to hold, so a single positive reading does not release."""
    probe = _Probe(PRINTER_STATE_READY)
    service = _service(tmp_path, probe, ready_settle=0.05, probe_pause=0.002)

    started = time.monotonic()
    outcome = service._wait_until_ready(_settings(), 1.0)
    elapsed = time.monotonic() - started

    assert outcome["ready"] is True
    assert outcome["stated_ready"] is True
    assert elapsed >= 0.05, "released before the settle had run"
    assert probe.calls >= 3, "the settle spanned more than one reading"


def test_a_printer_that_answers_but_is_not_ready_is_not_taken_as_ready(tmp_path):
    """The early mid-boot answer: reachable, saying nothing about readiness.

    This is the reading the old gate acted on. It must not release here; the
    only thing that releases is readiness that then holds.
    """
    # Answers on a bare TCP connect for four probes, then IPP comes up.
    probe = _Probe(PRINTER_STATE_UNKNOWN, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_UNKNOWN, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_READY)
    service = _service(tmp_path, probe, ready_settle=0.02, answering_settle=5.0,
                       probe_pause=0.001)

    outcome = service._wait_until_ready(_settings(), 1.0)

    assert outcome["ready"] is True
    assert outcome["state"] == PRINTER_STATE_READY
    assert outcome["stated_ready"] is True, (
        "it was released on the printer's own readiness, not on it answering")
    assert probe.calls > 5, (
        "released during the answers-but-not-ready window; a gate that fires on "
        "the first positive probe would have released at probe 1")


def test_readiness_that_goes_away_restarts_the_settle(tmp_path):
    """Ready, gone, ready again: the clock starts from the second one."""
    probe = _Probe(PRINTER_STATE_READY, PRINTER_STATE_UNREACHABLE,
                   PRINTER_STATE_READY)
    service = _service(tmp_path, probe, ready_settle=0.03, probe_pause=0.001)

    outcome = service._wait_until_ready(_settings(), 1.0)

    assert outcome["ready"] is True
    # Probe 1 ready, probe 2 unreachable (clock cleared), probe 3 onward ready:
    # the settle can only have been satisfied well after probe 3.
    first_ready_again = probe.at[2]
    assert probe.at[-1] - first_ready_again >= 0.03


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
                       ready_settle=0.01, probe_pause=0.001)

    service.ensure_printer_powered()

    assert reporter.activities == [
        ACTIVITY_SWITCHING_ON,
        ACTIVITY_WAITING_FOR_PRINTER,
        ACTIVITY_PRINTER_SETTLING,
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


def test_the_settling_message_distinguishes_the_two_kinds_of_settle(tmp_path):
    probe = _Probe(PRINTER_STATE_UNREACHABLE, PRINTER_STATE_UNKNOWN,
                   PRINTER_STATE_READY)
    reporter = _Reporter()
    service = _service(tmp_path, probe, reporter=reporter, blind_wait=0.0,
                       ready_settle=0.01, answering_settle=5.0, probe_pause=0.001)

    service.ensure_printer_powered()

    settling = [message for activity, message in reporter.entries
                if activity == ACTIVITY_PRINTER_SETTLING]
    assert any("has not reported itself ready" in message for message in settling)
    assert any("reports itself ready" in message for message in settling)


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
