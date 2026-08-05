"""
Controller for print-queue job API endpoints.

Exposes read/cancel/clear operations over the in-process print queue
(``src.services.queue_service.print_queue``). Status transitions and execution
are handled entirely by the queue's worker thread; these handlers only inspect
or mutate job metadata.
"""

import mimetypes
import os

import structlog
from typing import Any, Dict

from flask import send_file

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.utils.exceptions import AppError, ResourceNotFoundError, internal_error

logger = structlog.get_logger()


def list_jobs() -> Dict[str, Any]:
    """Return the most recent print-queue jobs (newest first)."""
    try:
        jobs = print_queue.list_jobs()
        logger.info("Listing print jobs", count=len(jobs))
        return {"jobs": jobs}
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Same treatment as every other controller: the record stays whole, the
        # response says only "internal error" plus the id that finds it.
        raise internal_error(e, "Error listing print jobs") from e


def get_job(job_id: str) -> Dict[str, Any]:
    """Return a single job by id, or 404 when it does not exist."""
    job = print_queue.get(job_id)
    if job is None:
        logger.warning("Print job not found", job_id=job_id)
        raise ResourceNotFoundError(
            "Print job not found",
            resource_type="job",
            resource_id=job_id,
        )
    logger.info("Fetched print job", job_id=job_id, status=job.get("status"))
    return job


def cancel_job(job_id: str) -> Dict[str, Any]:
    """Cancel a still-queued job. 404 when the job does not exist."""
    job = print_queue.get(job_id)
    if job is None:
        logger.warning("Cannot cancel: print job not found", job_id=job_id)
        raise ResourceNotFoundError(
            "Print job not found",
            resource_type="job",
            resource_id=job_id,
        )
    cancelled = print_queue.cancel(job_id)
    logger.info("Cancel print job requested", job_id=job_id, cancelled=cancelled)
    return {"cancelled": cancelled}


def reprint_job(job_id: str) -> Dict[str, Any]:
    """Re-queue a previous job's executor as a new job.

    Returns the new job id, or 404 when the original job has no stored
    executor (unknown id or already evicted from the registry).
    """
    new_id = print_queue.reprint(job_id)
    if new_id is None:
        logger.warning("Cannot reprint: print job not found", job_id=job_id)
        raise ResourceNotFoundError(
            "Print job not found",
            resource_type="job",
            resource_id=job_id,
        )
    logger.info("Reprint requested", job_id=job_id, new_job_id=new_id)
    return {"job_id": new_id}


# The content types this endpoint may answer with. It serves whatever was
# uploaded -- an image or a PDF -- and naming the type lets a browser show it
# instead of downloading it blindly. The list is duplicated in openapi.yaml as
# the declared 200 content, and the two have to agree: Connexion validates the
# outgoing type against the document.
SERVABLE_JOB_FILE_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/bmp",
    "image/webp",
    "image/tiff",
    "application/pdf",
})


def get_job_file(job_id: str):
    """Serve the persisted image/pdf file backing a job.

    Returns 404 when no file is associated with the job or the file no longer
    exists (e.g. removed by the TTL sweep). The served path is taken from the
    queue's internal map (never from user input); a realpath/commonpath check
    against the ``uploads/jobs`` directory guards against path traversal.
    """
    file_path = print_queue.get_file_path(job_id)
    if not file_path or not os.path.isfile(file_path):
        logger.warning("Job file not found", job_id=job_id)
        raise ResourceNotFoundError(
            "Print job file not found",
            resource_type="job_file",
            resource_id=job_id,
        )

    # Strict containment check against the jobs directory.
    jobs_dir = os.path.realpath(os.path.join(printer_service.upload_folder, "jobs"))
    real_path = os.path.realpath(file_path)
    if os.path.commonpath([jobs_dir, real_path]) != jobs_dir:
        logger.error("Refusing to serve job file outside jobs dir",
                     job_id=job_id, path=real_path)
        raise ResourceNotFoundError(
            "Print job file not found",
            resource_type="job_file",
            resource_id=job_id,
        )

    # Only the types the specification declares for this response. Anything
    # else is served as a download rather than named: the response validator
    # checks the outgoing content type against the document, so a type that is
    # not in it turns a working download into a 500.
    guessed = mimetypes.guess_type(real_path)[0]
    mimetype = guessed if guessed in SERVABLE_JOB_FILE_TYPES else "application/octet-stream"
    logger.info("Serving job file", job_id=job_id, mimetype=mimetype, guessed=guessed)
    return send_file(real_path, mimetype=mimetype)


def clear_jobs() -> Dict[str, Any]:
    """Remove all finished (done/failed/cancelled) jobs from the registry."""
    try:
        cleared = print_queue.clear_finished()
        logger.info("Cleared finished print jobs", cleared=cleared)
        return {"cleared": cleared}
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        raise internal_error(e, "Error clearing print jobs") from e


def delete_job(job_id: str) -> Dict[str, Any]:
    """Delete a single job (queued or finished). 404 when it does not exist.

    A queued job is cancelled and removed; a finished job is removed. A job that
    is currently printing cannot be deleted and yields ``removed: false``.
    """
    job = print_queue.get(job_id)
    if job is None:
        logger.warning("Cannot delete: print job not found", job_id=job_id)
        raise ResourceNotFoundError(
            "Print job not found",
            resource_type="job",
            resource_id=job_id,
        )
    removed = print_queue.remove(job_id)
    logger.info("Delete print job requested", job_id=job_id, removed=removed)
    return {"removed": removed}


def get_queue_status() -> Dict[str, Any]:
    """Return the queue control status (paused flag + queued/printing counts)."""
    return print_queue.queue_status()


def pause_queue() -> Dict[str, Any]:
    """Pause queue processing (the printing job, if any, still finishes)."""
    print_queue.pause()
    return print_queue.queue_status()


def resume_queue() -> Dict[str, Any]:
    """Resume a paused queue."""
    print_queue.resume()
    return print_queue.queue_status()


def stop_queue() -> Dict[str, Any]:
    """Emergency stop: pause and cancel every queued job."""
    cancelled = print_queue.stop()
    status = print_queue.queue_status()
    status["cancelled"] = cancelled
    logger.info("Stop print queue requested", cancelled=cancelled)
    return status


def clear_all_jobs() -> Dict[str, Any]:
    """Cancel all queued jobs and remove every job except one printing."""
    cleared = print_queue.clear_all()
    logger.info("Cleared all print jobs", cleared=cleared)
    return {"cleared": cleared}
