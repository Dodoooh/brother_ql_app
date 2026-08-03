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

from src.utils.job_activity import ACTIVITY_PRINTING, activity_message

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
        finds the job for it -- so the gate stays a plain ``gate() -> None`` and
        the queue stays the only thing that knows the job registry exists.

        Args:
            gate: ``gate() -> None``, or None to remove the current one.
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
        """
        if job_id is None:
            return False
        job = self._jobs.get(job_id)
        if job is None:
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
            True when it was recorded, False when no job is being processed (a
            gate called by hand, say). Never raises: a job must not fail because
            the app could not describe it.
        """
        with self._lock:
            return self._set_activity_locked(self._current_job_id, activity, message)

    def current_activity(self) -> Dict[str, Any]:
        """Return the activity of the job being processed, as a small dict."""
        with self._lock:
            return self._current_activity_locked()

    def _current_activity_locked(self) -> Dict[str, Any]:
        """The current job's activity. Caller must hold ``self._lock``."""
        job = self._jobs.get(self._current_job_id) if self._current_job_id else None
        activity = job.get("activity") if job else None
        return {
            "activity": activity,
            "activity_message": job.get("activity_message") if job else None,
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
            # until its wait is over.
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
                if gate is not None:
                    try:
                        gate()
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

                with self._lock:
                    job = self._jobs.get(job_id)
                    # The gate may have taken a while; re-check the job is still
                    # there and was not cancelled meanwhile.
                    if job is None or job["status"] == "cancelled":
                        continue
                    job["status"] = "printing"
                    job["started_at"] = _now_iso()
                    self._set_activity_locked(job_id, ACTIVITY_PRINTING)
                logger.info("Print job started", job_id=job_id)
                try:
                    fn()
                    final_status = "done"
                    error = None
                except Exception as e:  # noqa: BLE001 - record any print failure
                    final_status = "failed"
                    error = str(e)
                    logger.error("Print job failed", job_id=job_id, error=error,
                                 exc_info=True)
                finally:
                    with self._lock:
                        job = self._jobs.get(job_id)
                        if job is not None:
                            job["status"] = final_status
                            job["error"] = error
                            job["finished_at"] = _now_iso()
                if final_status == "done":
                    logger.info("Print job done", job_id=job_id)
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
