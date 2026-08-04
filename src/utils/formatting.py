"""Small formatters shared by more than one module.

Both functions here produce text a *client* reads -- the label a queued job is
listed under, and the timestamps on a job or on the relay's state file. Each of
them used to be copied into every module that needed it: ``short_label`` four
times across the controllers, ``now_iso`` twice across the services. Identical
copies, which is exactly the problem: the next change to the truncation width or
to the timestamp format would have been made in one copy, and the endpoints
would have started disagreeing with each other about how a job is named or how a
time is written without anything failing. One definition per format, so there is
only one place for that decision to live.
"""

from datetime import datetime, timezone


def short_label(text: str, limit: int = 40) -> str:
    """Build a short, single-line human label for a queued job.

    Args:
        text: The job's own content (typed text, QR payload, ...).
        limit: Maximum number of characters kept before the ellipsis.

    Returns:
        The flattened text, truncated with a trailing ``...`` when too long.
    """
    flattened = " ".join((text or "").split())
    if len(flattened) > limit:
        return flattened[:limit].rstrip() + "..."
    return flattened


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
