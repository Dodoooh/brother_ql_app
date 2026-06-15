"""
Shared helpers guarding large print batches behind an explicit confirmation.

Any print request that would produce a large number of copies must carry an
explicit confirmation flag; otherwise the request is rejected with a
machine-readable ``ConfirmationRequiredError`` (HTTP 400). This keeps a slip of
the keyboard from accidentally driving the printer through dozens of labels.
"""

from src.utils.exceptions import ConfirmationRequiredError

# Copy count at/above which a print request needs explicit confirmation.
LARGE_BATCH_THRESHOLD = 10


def is_confirmed(value) -> bool:
    """Interpret a confirmation flag from JSON or form data as a boolean.

    Accepts native booleans as well as the common truthy string spellings
    ("true", "yes", "1"), case-insensitively. Everything else is treated as
    not confirmed.

    Args:
        value: The raw confirmation value from the request body/form.

    Returns:
        True when the value expresses confirmation, False otherwise.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def enforce_large_batch_confirmation(copies, confirmed: bool) -> None:
    """Reject large batches that are not explicitly confirmed.

    Args:
        copies: Requested number of copies (parsed leniently, defaulting to 1).
        confirmed: Whether the request carries a valid confirmation flag.

    Raises:
        ConfirmationRequiredError: When ``copies`` is at/above
            ``LARGE_BATCH_THRESHOLD`` and ``confirmed`` is falsey.
    """
    try:
        n = int(copies or 1)
    except (TypeError, ValueError):
        n = 1
    if n >= LARGE_BATCH_THRESHOLD and not confirmed:
        raise ConfirmationRequiredError(n, LARGE_BATCH_THRESHOLD)
