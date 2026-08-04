"""
In-process print-queue service.

Provides a FIFO print queue backed by a single daemon worker thread. Print
jobs are submitted as argument-less callables; the worker executes them one at
a time so that the printer (which accepts only one connection at a time) is
never driven concurrently.

The design assumes a single process (gunicorn ``--workers 1``): there is exactly
one queue and one worker thread per process. A ``threading.Lock`` guards every
access to the job registry, and all returned job dicts are copies so callers can
never mutate the internal state.

Saying what a job is doing
--------------------------
A job is not always merely waiting its turn. The pre-job gate (see
:meth:`PrintQueueService.set_pre_job_gate`) can hold a job for minutes while the
printer's mains supply is switched on and the device boots, and all of that used
to look exactly like an idle queue. Every job therefore carries an ``activity``
alongside its ``status``: a token from :mod:`src.utils.job_activity` naming the
phase, plus a human-readable ``activity_message`` and the ``activity_at`` moment
it was set.

The status enum is untouched -- a job in the gate is still ``queued``, which is
what it is -- so no client that switches on ``status`` has to change. The queue
sets the activity for the phases it owns (printing, and clearing it when the job
finishes); the gate reports its own through :meth:`report_activity`.

A job that has finished takes no more activities. The gate is a plain callable
that does not know which job it is holding and cannot be interrupted, so it goes
on describing its phases for as long as its wait lasts -- possibly minutes after
the job was cancelled underneath it. Those reports are dropped rather than
written onto a terminal job, which is what keeps :meth:`queue_status` from
answering "nothing is queued, nothing is printing, and here is what is currently
happening" in the same breath.
"""

import copy
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import structlog

from src.utils.job_activity import (
    ACTIVITY_PRINTER_SETTLING,
    ACTIVITY_PRINTING,
    ACTIVITY_RETRYING,
    activity_message,
)

logger = structlog.get_logger()

# Terminal job states (no further transitions are possible).
_FINISHED_STATES = ("done", "failed", "cancelled")

# Upper bound on the number of jobs retained in the registry. Once exceeded the
# oldest *finished* jobs are evicted first so memory does not grow unbounded.
_MAX_HISTORY = 100

# Default time-to-live for persisted job files (image/pdf) in seconds (24 h).
_DEFAULT_JOB_FILE_TTL_SECONDS = 86400


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _attempt_delays(value: Any) -> tuple:
    """Read a print attempt schedule out of whatever the gate handed back.

    The gate is a plain callable the queue knows nothing else about, so what it
    hands back is treated as advice rather than as a contract: anything that is
    not a sequence of non-negative numbers becomes an empty schedule, i.e. print
    once, immediately. A gate that returns None -- which every gate did before
    this existed -- lands there too.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    delays = []
    for entry in value:
        try:
            seconds = float(entry)
        except (TypeError, ValueError):
            return ()
        delays.append(max(0.0, seconds))
    return tuple(delays)


def _gate_advice(value: Any) -> tuple:
    """Read the gate's return value as ``(delays, first_pause_message)``.

    Two shapes are accepted, and the second is a superset of the first:

    * a bare sequence of pauses (or None), which is what a gate that has nothing
      to say hands back;
    * a mapping carrying ``delays`` and a ``message`` describing what the pause
      before the *first* attempt is waiting on.

    The message exists because only the gate knows why it let the job through.
    It may have been released on the printer saying it was ready, or on the
    printer merely answering steadily without ever saying so -- two different
    things to be told, and the queue cannot tell them apart because it does not
    know what a printer is. So the gate supplies the sentence and the queue
    displays it, and neither has to learn the other's vocabulary. A gate that
    supplies none gets wording that claims nothing the queue cannot see for
    itself.

    Read as leniently as the schedule itself: an unusable mapping degrades to
    "print once, now" rather than failing the job.
    """
    if isinstance(value, dict):
        message = value.get("message")
        return _attempt_delays(value.get("delays")), (
            str(message) if message else None)
    return _attempt_delays(value), None


def _writes_begun() -> int:
    """How many raster writes the printer service has begun, ever.

    Only the change across one print attempt is used, and only to decide
    whether repeating that attempt could print something twice. Imported
    lazily, like every other reach into the printer service from here, because
    the two modules would otherwise import each other at load time.

    Returns 0 when the service cannot be asked at all. That reads as "nothing
    went to the printer", which is the same answer this whole mechanism gave
    before it existed: the schedule runs, and a job that did print part of
    itself is the case a caller was already living with.
    """
    try:
        from src.services.printer_service import printer_service
        return printer_service.writes_begun()
    except Exception:  # noqa: BLE001 - a counter must not be able to fail a print
        return 0


def _short(text: Optional[str], limit: int = 120) -> str:
    """A one-line, length-capped rendering of an error, for prose about it."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit - 1].rstrip() + "…"


def _job_file_ttl_seconds() -> int:
    """Return the configured job-file TTL in seconds (robustly parsed).

    Reads ``JOB_FILE_TTL_SECONDS`` from the environment; falls back to the
    24-hour default on any missing/invalid value.
    """
    raw = os.environ.get("JOB_FILE_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_JOB_FILE_TTL_SECONDS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return _DEFAULT_JOB_FILE_TTL_SECONDS
    return value if value > 0 else _DEFAULT_JOB_FILE_TTL_SECONDS


class PrintQueueService:
    """FIFO print queue with a single background worker thread."""

    def __init__(self) -> None:
        # Underlying FIFO queue carrying (job_id, fn) tuples.
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        # job_id -> job dict registry (the single source of truth for status).
        self._jobs: Dict[str, Dict[str, Any]] = {}
        # job_id -> callable performing the print (kept internal, never exposed
        # via the API). Its presence drives the job's ``can_reprint`` flag.
        self._executors: Dict[str, Callable[[], Any]] = {}
        # job_id -> path of the persisted image/pdf file (or None). Internal.
        self._files: Dict[str, Optional[str]] = {}
        # Insertion order of job ids (oldest first) for bounded-history pruning.
        self._order: List[str] = []
        # Guards every access to _jobs / _order / _executors / _files.
        self._lock = threading.Lock()
        # Worker thread + idempotent-start guard.
        self._worker: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        # Processing gate: when set, the worker processes jobs; when cleared the
        # queue is *paused* and the worker holds before starting the next job
        # (a job already printing is allowed to finish). Starts in the running
        # state.
        self._resume_event = threading.Event()
        self._resume_event.set()
        # Optional callable run once per job, immediately before the job is
        # marked "printing". See set_pre_job_gate.
        self._pre_job_gate: Optional[Callable[[], Any]] = None
        # The job the worker currently has in hand, from the moment it is
        # dequeued until its outcome is recorded. This is what report_activity
        # writes to, so a gate that knows nothing about job ids can still say
        # what it is doing. Guarded by _lock like everything else here.
        self._current_job_id: Optional[str] = None

    def set_pre_job_gate(self, gate: Optional[Callable[[], Any]]) -> None:
        """Install a callable run just before each job starts.

        The gate runs while the job is still in the "queued" state and may block
        for as long as it needs to; anything it raises fails that job with the
        raised message and the worker moves on to the next one.

        This is how relay power control hooks in: a job that arrives at a
        printer whose mains supply is switched off waits here, in the queue,
        while the relay is closed and the printer boots, rather than failing
        because the printer happened to be off. The queue keeps knowing nothing
        about relays or printers; it only knows that something may need to
        happen before a job may start.

        The gate takes no arguments and is not told which job it is holding. If
        it wants to say what it is doing it calls :meth:`report_activity`, which
        finds the job for it -- so the gate stays a plain callable and the queue
        stays the only thing that knows the job registry exists. Once that job
        has finished, been cancelled included, those reports are dropped and
        :meth:`report_activity` says so by returning False; a gate that is still
        waiting is never interrupted and its own run always finishes normally.

        It may hand back a print attempt schedule: a sequence of pauses, one per
        attempt, taken before each. That is how a gate says "I have just powered
        this printer up, so the first refusal is probably the boot and not the
        job" without the queue learning what a relay or a printer is. Returning
        nothing means what it always meant: print once, now.

        A gate with something to say about the pause *before the first attempt*
        hands back a mapping instead::

            {"delays": (5.0, 20.0, 20.0),
             "message": "The printer reports itself ready. Giving it 5s before
                         printing."}

        That first pause is the one wait the queue cannot describe truthfully on
        its own: it is a grace after the gate released the job, and only the gate
        knows what it released on. The queue displays the sentence verbatim under
        the ``printer_settling`` activity and falls back to wording that claims
        nothing when there is none. Every later pause is a retry, which the queue
        does know about because it has the failure in hand.

        Args:
            gate: ``gate() -> None | Sequence[float] | {"delays": Sequence[float],
                "message": str | None}``, or None to remove the current one.
        """
        self._pre_job_gate = gate

    # ------------------------------------------------------------------ #
    # Activity reporting
    # ------------------------------------------------------------------ #
    def _set_activity_locked(self, job_id: Optional[str], activity: Optional[str],
                             message: Optional[str] = None) -> bool:
        """Write an activity onto a job. Caller must hold ``self._lock``.

        Clearing (``activity=None``) drops the message and the timestamp with
        it, so a job that is not doing anything never carries the description of
        something it was doing a minute ago.

        A job in a terminal state takes no new activity. It is done, failed or
        cancelled, and nothing is happening to it by definition -- yet the gate
        holding it can go on reporting phases for minutes after a cancel,
        because it is a plain callable that cannot be told to stop. Writing
        those onto the job would resurrect it in every display, and would make
        :meth:`queue_status` report a live activity while counting nothing as
        queued or printing. Clearing is still allowed, and has to be: it is how
        the worker tidies up after the outcome has been recorded.
        """
        if job_id is None:
            return False
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if activity and job.get("status") in _FINISHED_STATES:
            return False
        job["activity"] = activity
        job["activity_message"] = activity_message(activity, message)
        job["activity_at"] = _now_iso() if activity else None
        return True

    def report_activity(self, activity: Optional[str],
                        message: Optional[str] = None) -> bool:
        """Record what the job currently being processed is doing.

        Called by the pre-job gate, which does not know (and should not need to
        know) which job it is holding: the worker has already recorded that.

        The value is plain state, not an event. It stays on the job until
        something replaces or clears it, so polling it twice returns it twice --
        which is what stops a UI that polls every second from flickering between
        "waiting for the printer" and nothing at all.

        Args:
            activity: A token from :mod:`src.utils.job_activity`, or None to
                clear.
            message: Human-readable wording; the token's default is used when
                omitted.

        Returns:
            True when it was recorded, False when there is no job to record it
            on -- either nothing is being processed (a gate called by hand, say)
            or the job in hand has already finished or been cancelled. Never
            raises: a job must not fail because the app could not describe it,
            and a gate that is mid-wait when its job is cancelled must be able to
            run to the end of that wait without noticing.
        """
        with self._lock:
            return self._set_activity_locked(self._current_job_id, activity, message)

    def _current_activity_locked(self) -> Dict[str, Any]:
        """The current job's activity. Caller must hold ``self._lock``.

        A job the worker still has in hand but which has already finished (it
        was cancelled while the gate held it, most often) reports no activity,
        whatever is left on it. The counts in :meth:`queue_status` are taken
        under this same lock and no longer count such a job, so reporting an
        activity for it would make one answer contradict the other.
        """
        job = self._jobs.get(self._current_job_id) if self._current_job_id else None
        if job is not None and job.get("status") in _FINISHED_STATES:
            job = None
        activity = job.get("activity") if job else None
        return {
            "activity": activity,
            "activity_message": job.get("activity_message") if job else None,
            # When the phase started, so a client can say how long it has been
            # running without also fetching the job list. The header says that
            # from every tab, and one small request is what it should cost.
            "activity_at": job.get("activity_at") if job else None,
            # Named only while there is an activity to attribute: the id of a
            # job that is doing nothing describable is not information.
            "activity_job_id": self._current_job_id if activity else None,
        }

    # ------------------------------------------------------------------ #
    # Persisted job-file helpers
    # ------------------------------------------------------------------ #
    def _jobs_dir(self) -> str:
        """Return (and ensure) the directory holding persisted job files."""
        # Imported lazily to avoid an import cycle at module load time.
        from src.services.printer_service import printer_service

        path = os.path.join(printer_service.upload_folder, "jobs")
        os.makedirs(path, exist_ok=True)
        return path

    def _sweep_job_files(self) -> None:
        """Delete persisted job files older than the configured TTL.

        Idempotent and best-effort: any error while listing or deleting is
        logged and swallowed so it never disrupts job submission/processing.
        """
        ttl = _job_file_ttl_seconds()
        try:
            jobs_dir = self._jobs_dir()
            entries = os.listdir(jobs_dir)
        except Exception as e:  # noqa: BLE001 - sweeping must never raise
            logger.warning("Job-file sweep skipped", error=str(e))
            return
        now = time.time()
        removed = 0
        for name in entries:
            full = os.path.join(jobs_dir, name)
            try:
                if not os.path.isfile(full):
                    continue
                if now - os.path.getmtime(full) > ttl:
                    os.remove(full)
                    removed += 1
            except Exception as e:  # noqa: BLE001 - log and skip this file
                logger.warning("Failed to sweep job file", path=full,
                               error=str(e))
        if removed:
            logger.info("Swept expired job files", count=removed, ttl=ttl)

    def get_file_path(self, job_id: str) -> Optional[str]:
        """Return the persisted file path for a job, or None."""
        with self._lock:
            return self._files.get(job_id)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def submit(
        self,
        job_type: str,
        label: str,
        fn: Callable[[], Any],
        params: Optional[Dict[str, Any]] = None,
        file_path: Optional[str] = None,
    ) -> str:
        """Create a queued job and enqueue its callable for execution.

        Args:
            job_type: Short type tag (e.g. "text", "image", "qrcode").
            label: Human-readable label describing the job.
            fn: Argument-less callable performing the actual print. May raise.
            params: Optional serializable dict describing the job inputs
                (e.g. text/settings or filename/settings/pages). Stored on the
                API-exposed job dict so the job can be re-opened/inspected.
            file_path: Optional path to the persisted image/pdf file backing
                this job. Stored internally only (never exposed via the API).

        Returns:
            The generated job id (uuid hex).
        """
        # Opportunistic TTL cleanup of stale persisted job files.
        self._sweep_job_files()

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "type": job_type,
            "status": "queued",
            "label": label,
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "params": copy.deepcopy(params) if params else {},
            "can_reprint": True,
            # Nothing is happening to it yet; it is waiting its turn, which the
            # status already says.
            "activity": None,
            "activity_message": None,
            "activity_at": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._executors[job_id] = fn
            self._files[job_id] = file_path
            self._order.append(job_id)
            self._prune_locked()
        self._queue.put((job_id, fn))
        logger.info("Print job submitted", job_id=job_id, type=job_type, label=label)
        return job_id

    def reprint(self, job_id: str) -> Optional[str]:
        """Re-queue a previous job's executor as a brand-new job.

        Looks up the stored callable for ``job_id``; if present, a new job is
        created (new id, ``queued`` status) reusing the same callable, params
        and persisted file path, with " (reprint)" appended to the label.

        Returns:
            The new job id, or None when no executor exists for ``job_id``.
        """
        with self._lock:
            fn = self._executors.get(job_id)
            source = self._jobs.get(job_id)
            if fn is None or source is None:
                return None
            params = copy.deepcopy(source.get("params") or {})
            file_path = self._files.get(job_id)
            job_type = source.get("type", "reprint")
            label = f"{source.get('label', '')} (reprint)"

            new_id = uuid.uuid4().hex
            new_job = {
                "id": new_id,
                "type": job_type,
                "status": "queued",
                "label": label,
                "created_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "params": params,
                "can_reprint": True,
                "activity": None,
                "activity_message": None,
                "activity_at": None,
            }
            self._jobs[new_id] = new_job
            self._executors[new_id] = fn
            self._files[new_id] = file_path
            self._order.append(new_id)
            self._prune_locked()
        self._queue.put((new_id, fn))
        logger.info("Print job re-queued", job_id=new_id, source_id=job_id,
                    type=job_type, label=label)
        return new_id

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of the job dict, or None if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return up to ``limit`` jobs, newest first (copies)."""
        with self._lock:
            # _order is oldest-first; reverse for newest-first.
            selected = list(reversed(self._order))[:limit]
            return [copy.deepcopy(self._jobs[jid]) for jid in selected if jid in self._jobs]

    def cancel(self, job_id: str) -> bool:
        """Cancel a job if it is still queued.

        Returns True only when the job existed and was in the "queued" state
        (it is then marked "cancelled"); otherwise False. The worker skips any
        job it dequeues that has been cancelled.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "queued":
                return False
            job["status"] = "cancelled"
            job["finished_at"] = _now_iso()
            # A job cancelled while the gate is holding it stops doing whatever
            # the gate said it was doing, even though the gate itself carries on
            # until its wait is over. Nothing the gate says from here lands on
            # it: _set_activity_locked refuses a terminal job.
            #
            # _current_job_id is deliberately left alone. The worker is still
            # holding this job, and that is the only signal anything has that a
            # gate is running -- see has_pending_work, which is what stops mains
            # power being cut while a printer is being brought up for a job the
            # user has since cancelled.
            self._set_activity_locked(job_id, None)
        logger.info("Print job cancelled", job_id=job_id)
        return True

    def clear_finished(self) -> int:
        """Remove all jobs in a terminal state from the registry.

        Returns the number of removed jobs.
        """
        with self._lock:
            finished = [jid for jid in self._order
                        if self._jobs.get(jid, {}).get("status") in _FINISHED_STATES]
            for jid in finished:
                self._jobs.pop(jid, None)
                # Drop the internal executor/file refs along with the job. The
                # persisted file itself is left for the TTL sweep (other
                # jobs/reprints may still reference the same file).
                self._executors.pop(jid, None)
                self._files.pop(jid, None)
            self._order = [jid for jid in self._order if jid not in set(finished)]
        if finished:
            logger.info("Cleared finished print jobs", count=len(finished))
        return len(finished)

    def start(self) -> None:
        """Start the daemon worker thread (idempotent)."""
        with self._start_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run,
                name="print-queue-worker",
                daemon=True,
            )
            self._worker.start()
            logger.info("Print queue worker started")

    # ------------------------------------------------------------------ #
    # Queue control (pause / resume / stop)
    # ------------------------------------------------------------------ #
    def pause(self) -> None:
        """Pause processing: the worker holds before starting the next job.

        A job that is already in the "printing" state is allowed to finish; only
        the start of subsequent jobs is held until :meth:`resume` is called.
        """
        self._resume_event.clear()
        logger.info("Print queue paused")

    def resume(self) -> None:
        """Resume processing previously paused with :meth:`pause`."""
        self._resume_event.set()
        logger.info("Print queue resumed")

    def is_paused(self) -> bool:
        """Return True when the queue is currently paused."""
        return not self._resume_event.is_set()

    def cancel_all_queued(self) -> int:
        """Cancel every job still in the "queued" state.

        Returns the number of jobs transitioned to "cancelled". A job currently
        "printing" is not touched (it cannot be safely interrupted mid-send).
        """
        cancelled = 0
        with self._lock:
            for jid in self._order:
                job = self._jobs.get(jid)
                if job is not None and job["status"] == "queued":
                    job["status"] = "cancelled"
                    job["finished_at"] = _now_iso()
                    self._set_activity_locked(jid, None)
                    cancelled += 1
        if cancelled:
            logger.info("Cancelled all queued print jobs", count=cancelled)
        return cancelled

    def stop(self) -> int:
        """Emergency stop: pause the queue and cancel all queued jobs.

        The currently printing job (if any) still finishes; everything waiting
        behind it is cancelled so it will not print once resumed. Returns the
        number of jobs cancelled.
        """
        self.pause()
        count = self.cancel_all_queued()
        logger.info("Print queue stopped", cancelled=count)
        return count

    def remove(self, job_id: str) -> bool:
        """Delete a single job from the registry.

        A "queued" job is cancelled first (so the worker skips it if already
        dequeued) and then removed. A job currently "printing" cannot be removed
        and returns False. Finished jobs are simply removed.

        Returns True when a job was removed, False otherwise.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["status"] == "printing":
                return False
            # Drop the job and its internal refs. The persisted file is left for
            # the TTL sweep (other jobs/reprints may reference the same file).
            self._jobs.pop(job_id, None)
            self._executors.pop(job_id, None)
            self._files.pop(job_id, None)
            self._order = [jid for jid in self._order if jid != job_id]
        logger.info("Print job removed", job_id=job_id)
        return True

    def clear_all(self) -> int:
        """Cancel all queued jobs and remove every job except one printing.

        Returns the number of jobs removed from the registry. Any job currently
        "printing" is retained so its eventual result is still recorded.
        """
        self.cancel_all_queued()
        with self._lock:
            removable = [jid for jid in self._order
                         if self._jobs.get(jid, {}).get("status") != "printing"]
            for jid in removable:
                self._jobs.pop(jid, None)
                self._executors.pop(jid, None)
                self._files.pop(jid, None)
            self._order = [jid for jid in self._order if jid not in set(removable)]
        if removable:
            logger.info("Cleared all print jobs", count=len(removable))
        return len(removable)

    def queue_status(self) -> Dict[str, Any]:
        """Return a small status summary for the queue control UI.

        Carries the current job's activity as well as the counts, because this
        is the endpoint the UI polls: a job held by the gate is counted under
        ``queued`` (it has not started printing, and it really is still in the
        queue), and without the activity that is indistinguishable from a queue
        sitting idle. It is the same value ``GET /jobs`` reports on the job
        itself, read from the same place, so the two cannot disagree.
        """
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j["status"] == "queued")
            printing = sum(1 for j in self._jobs.values() if j["status"] == "printing")
            activity = self._current_activity_locked()
        status = {
            "paused": self.is_paused(),
            "queued": queued,
            "printing": printing,
        }
        status.update(activity)
        return status

    def has_pending_work(self) -> bool:
        """Whether the queue has anything left to do.

        Broader than the counts in :meth:`queue_status`, and deliberately so.
        Anything queued or printing counts, as it always has -- and so does a job
        the worker still has in hand even after it reached a terminal state.
        That last case is real work: the pre-job gate can be holding a job it was
        given minutes ago, and cancelling that job does not stop the gate, which
        may well be halfway through switching a printer's mains supply on and
        waiting for it to boot.

        Whoever asks this question is asking whether it is safe to act as though
        the queue were idle, and while a gate is still running it is not. The
        answer errs towards "busy" for the length of one gate, which is bounded.
        """
        with self._lock:
            if self._current_job_id is not None:
                return True
            return any(job["status"] in ("queued", "printing")
                       for job in self._jobs.values())

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _prune_locked(self) -> None:
        """Evict oldest finished jobs while the registry exceeds the cap.

        Caller must hold ``self._lock``.
        """
        while len(self._order) > _MAX_HISTORY:
            removed = None
            for idx, jid in enumerate(self._order):
                if self._jobs.get(jid, {}).get("status") in _FINISHED_STATES:
                    removed = idx
                    break
            if removed is None:
                # No finished job to evict; nothing to do without dropping an
                # active/queued job, so stop pruning.
                break
            jid = self._order.pop(removed)
            self._jobs.pop(jid, None)
            # Drop internal refs too; the persisted file is left for TTL sweep.
            self._executors.pop(jid, None)
            self._files.pop(jid, None)

    def _print_job(self, job_id: str, fn: Callable[[], Any],
                   delays: tuple, first_pause_message: Optional[str] = None) -> None:
        """Run the print, once, or on the attempt schedule the gate handed back.

        ``delays`` is one pause per attempt, taken *before* that attempt, and is
        empty for the ordinary case of a printer that was already up: print now,
        once, and a failure is the printer's answer. A non-empty schedule means
        the printer was switched on for this job and the first attempt is being
        made into its boot window, where a refusal says as much about the moment
        as about the job -- so it is tried again rather than failed.

        Only the last attempt's failure is the job's failure. The earlier ones
        are reported as an activity while the job waits, and the job stays
        ``queued`` between attempts: it is not on the wire, and it can still be
        cancelled, which is the difference a user acts on.

        Two things end the schedule early, both because repeating the job would
        be worse than failing it:

        *The printer was written to.* A failure that happened after bytes went
        out may mean part of the job printed -- a page of a PDF, one of several
        copies -- and trying again prints that part twice. Only a failure that
        never reached the wire is safe to repeat, and
        :meth:`PrinterService.writes_begun` is what tells the two apart.

        *Somebody said stop.* Pausing the queue stops the next job from
        starting, but a job already printing is allowed to finish -- which used
        to mean its remaining attempts ran too, long after the queue had been
        stopped. The schedule is abandoned instead, and the job fails carrying
        the printer's own last words.

        ``first_pause_message`` is the gate's own wording for the pause before
        the first attempt, and is None when it had none. See :func:`_gate_advice`.
        """
        attempts = max(1, len(delays))
        error: Optional[str] = None
        for attempt in range(1, attempts + 1):
            pause = delays[attempt - 1] if attempt <= len(delays) else 0.0
            if pause > 0:
                held = self._hold_before_attempt(job_id, pause, attempt,
                                                 attempts, error,
                                                 first_pause_message)
                if held == "gone":
                    return  # cancelled meanwhile; the worker's finally cleans up
                if held == "stopped":
                    # The queue was stopped while this job waited for its next
                    # try. It keeps the failure it already has, rather than
                    # being left queued for a worker that has moved on.
                    logger.info("Attempt schedule abandoned: the queue was stopped",
                                job_id=job_id, attempt=attempt)
                    self._finish(job_id, "failed",
                                 error or "The queue was stopped before this job printed.")
                    return

            with self._lock:
                job = self._jobs.get(job_id)
                # The gate and the pauses take time; the job may be gone.
                if job is None or job["status"] == "cancelled":
                    return
                job["status"] = "printing"
                # The moment the job first went on the wire, kept across
                # retries: "started" is when the printing began, not when the
                # attempt that happened to work did.
                job["started_at"] = job.get("started_at") or _now_iso()
                self._set_activity_locked(
                    job_id, ACTIVITY_PRINTING,
                    None if attempts == 1 else
                    f"Printing (attempt {attempt} of {attempts}).")
            logger.info("Print job started", job_id=job_id,
                        attempt=attempt, attempts=attempts)

            writes_before = _writes_begun()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - record any print failure
                error = str(e)
                reached_printer = _writes_begun() != writes_before
                last = attempt >= attempts
                logger.error("Print job failed", job_id=job_id, attempt=attempt,
                             attempts=attempts, error=error,
                             reached_printer=reached_printer,
                             exc_info=last)
                if not last and reached_printer:
                    logger.info("Not retrying: the printer was already written to",
                                job_id=job_id, attempt=attempt)
                elif not last and self.is_paused():
                    logger.info("Not retrying: the queue was stopped",
                                job_id=job_id, attempt=attempt)
                elif not last:
                    continue
                self._finish(job_id, "failed", error)
                return

            self._finish(job_id, "done", None)
            logger.info("Print job done", job_id=job_id, attempt=attempt)
            return

    def _hold_before_attempt(self, job_id: str, seconds: float, attempt: int,
                             attempts: int, error: Optional[str],
                             first_pause_message: Optional[str] = None) -> str:
        """Wait before a print attempt, saying why, and stop if told to.

        The first pause and the later ones are different waits and are worded by
        different owners. A later pause follows a failed attempt, and the queue
        has that failure in hand, so it says so itself. The first pause follows
        nothing: it is a grace the gate asked for after releasing the job, and
        why the gate released it is something the queue does not and should not
        know. So the gate's own sentence is displayed when it supplied one, and
        the fallback states only what is true from here -- that a pause is
        running before the first attempt. It used to assert that the printer had
        reported itself ready, which is one of two ways the gate releases a job
        and was simply wrong for the other.

        Returns:
            ``"go"`` when the wait ran to the end and the attempt should be
            made, ``"gone"`` when the job was cancelled or removed meanwhile
            (it is already in a terminal state, and nothing more is owed to
            it), or ``"stopped"`` when the queue was paused during the wait --
            the job is still the caller's to finish.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] == "cancelled":
                return "gone"
            # Back to "queued": the job is waiting, not printing, and a waiting
            # job is one a user can still cancel.
            job["status"] = "queued"
            if attempt == 1:
                self._set_activity_locked(
                    job_id, ACTIVITY_PRINTER_SETTLING,
                    first_pause_message or
                    f"Waiting {seconds:.0f}s before the first print attempt.")
            else:
                because = f" ({_short(error)})" if error else ""
                self._set_activity_locked(
                    job_id, ACTIVITY_RETRYING,
                    f"Attempt {attempt - 1} of {attempts} did not go through"
                    f"{because}. Trying again in {seconds:.0f}s.")

        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "go"
            # Slices rather than one sleep, so a cancel or a stop takes effect
            # while the job is waiting, instead of a retry landing at a printer
            # nobody is waiting for any more.
            time.sleep(min(0.25, remaining))
            if self.is_paused():
                return "stopped"
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job["status"] == "cancelled":
                    return "gone"

    def _finish(self, job_id: str, status: str, error: Optional[str]) -> None:
        """Record a job's outcome."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["status"] = status
                job["error"] = error
                job["finished_at"] = _now_iso()

    def _run(self) -> None:
        """Worker loop: process queued jobs FIFO, one at a time."""
        while True:
            job_id, fn = self._queue.get()
            # Hold here while the queue is paused. A job already dequeued waits
            # in its "queued" state and only starts once the queue is resumed;
            # a job that gets cancelled meanwhile is skipped below.
            self._resume_event.wait()
            # Opportunistic TTL cleanup on each processed job.
            self._sweep_job_files()
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                    # Skip vanished or already-cancelled jobs.
                    if job is None or job["status"] == "cancelled":
                        continue
                    # From here on this is the job in hand, and report_activity
                    # writes to it.
                    self._current_job_id = job_id

                # Run the pre-job gate while the job is still "queued", so a job
                # waiting for its printer to be powered up shows as waiting
                # rather than as printing, and still counts as pending work,
                # which is what stops the relay switching off underneath it.
                gate = self._pre_job_gate
                attempt_delays: tuple = ()
                first_pause_message: Optional[str] = None
                if gate is not None:
                    try:
                        attempt_delays, first_pause_message = _gate_advice(gate())
                    except Exception as e:  # noqa: BLE001 - fail this job, keep the worker
                        error = str(e)
                        logger.error("Pre-job gate failed", job_id=job_id,
                                     error=error, exc_info=True)
                        with self._lock:
                            job = self._jobs.get(job_id)
                            if job is not None:
                                job["status"] = "failed"
                                job["error"] = error
                                job["finished_at"] = _now_iso()
                            # A gate failure is described by the error, not by a
                            # lingering activity: the message the gate raises
                            # says what it was doing and for how long, and that
                            # is where a user looks. The activity field is for
                            # what a job is doing now, and a failed job is not
                            # doing anything.
                            self._set_activity_locked(job_id, None)
                        continue

                self._print_job(job_id, fn, attempt_delays, first_pause_message)
            finally:
                # Whatever happened -- printed, failed, cancelled under us,
                # skipped -- the job is no longer doing anything, and nothing is
                # in hand until the next one is dequeued.
                with self._lock:
                    self._set_activity_locked(job_id, None)
                    self._current_job_id = None
                self._queue.task_done()


# Module-level singleton (mirrors printer_service / settings_service).
print_queue = PrintQueueService()
