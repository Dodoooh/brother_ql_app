"""
Custom exceptions for the Brother QL Printer App.

The types here split into two families, and the split is the whole point:

* Errors the *caller* caused -- a missing field, a label size that does not
  exist, more copies than the guard allows, a printer that refused the job.
  Those carry a sentence written for the caller, and that sentence is part of
  the API: without it a client is left guessing what to change.
* Errors that came out of the *inside* of the app -- a library raising
  something nobody anticipated, a bug. Nothing in those helps the caller, and
  their text (paths, stack frames, third-party wording) is exactly what an
  attacker maps the server with. :class:`InternalError` is the one answer given
  for all of them; :func:`internal_error` writes the real one to the log.
"""

import uuid

import structlog

# Module-level logger, matching the rest of the codebase (structlog, JSON
# output, context passed as keyword arguments).
_logger = structlog.get_logger()


class AppError(Exception):
    """Base exception for all application errors."""
    
    def __init__(self, message: str, code: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            code: Error code.
            details: Additional error details.
        """
        self.message = message
        self.code = code or self.__class__.__name__.upper()
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> dict:
        """
        Convert the exception to a dictionary.
        
        Returns:
            Dict representation of the exception.
        """
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details
        }


class PrinterError(AppError):
    """Exception raised for printer-related errors."""
    pass


class RelayWebhookError(PrinterError):
    """Exception raised when a relay power-control webhook cannot be delivered.

    Deliberately a subclass of :class:`PrinterError`: a relay that will not
    answer means the printer cannot be powered, so every caller that already
    knows how to handle "the printer is not usable" handles this correctly
    without being taught a new type, while code that cares specifically about
    the relay can still tell the two apart.
    """
    pass


class ImageProcessingError(AppError):
    """Exception raised for image processing errors."""
    pass


class ConfigurationError(AppError):
    """Exception raised for configuration errors."""
    pass


class ValidationError(AppError):
    """Exception raised for validation errors."""
    
    def __init__(self, message: str, field: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            field: Field that failed validation.
            details: Additional error details.
        """
        details = details or {}
        if field:
            details["field"] = field
        super().__init__(message, "VALIDATION_ERROR", details)


class ConfirmationRequiredError(AppError):
    """Exception raised when a large print batch needs explicit confirmation."""

    def __init__(self, copies, threshold: int = 10, details: dict = None):
        """
        Initialize the exception.

        Args:
            copies: Number of copies the request would print.
            threshold: Copy count at/above which confirmation is required.
            details: Additional error details.
        """
        details = details or {}
        details.update({"copies": copies, "threshold": threshold, "field": "confirm_large_batch"})
        super().__init__(
            f"Printing {copies} copies requires confirmation. Resend with confirm_large_batch=true.",
            "CONFIRMATION_REQUIRED", details)


class InternalError(AppError):
    """Exception raised in place of an error the caller neither caused nor can act on.

    Its message is a fixed sentence and never derived from the exception it
    replaces, because that exception's text is the one thing that must not
    travel: it is where absolute paths, module names and library wording come
    from. What does travel is an ``error_id`` -- the same short token the log
    record carries -- so an operator can be handed "internal error, id abc123"
    and find the complete story in the log within seconds. That is the trade
    this class makes: the response says less, and the two halves still fit
    together.

    Raise it through :func:`internal_error`, never directly, so no code path can
    answer generically without first recording what actually happened.
    """

    #: The only message this error ever shows a client.
    GENERIC_MESSAGE = "An internal error occurred"

    def __init__(self, error_id: str, details: dict = None):
        """
        Initialize the exception.

        Args:
            error_id: Correlation token that also appears in the log record.
            details: Additional error details. Must not contain anything derived
                from the original exception.
        """
        details = dict(details or {})
        details["error_id"] = error_id
        self.error_id = error_id
        super().__init__(self.GENERIC_MESSAGE, "INTERNAL_SERVER_ERROR", details)


def internal_error(exc: Exception, event: str, **context) -> InternalError:
    """
    Record an unexpected exception in full and return the error to answer with.

    Used at the ``except Exception`` boundary of a controller::

        except Exception as e:
            raise internal_error(e, "Error printing text") from e

    Controllers used to wrap those in ``PrinterError(f"...: {str(e)}")``, which
    reported a bug in the app as a fault of the printer *and* copied the raw
    exception text into the HTTP response. This does the opposite of both: the
    log keeps everything it kept before (the same event name, the message, the
    stack trace) plus the exception's type and the correlation id, and the
    response keeps nothing.

    Args:
        exc: The exception that was caught.
        event: Log event name, phrased as before ("Error printing text").
        **context: Extra structured log context (job ids, label sizes, ...).

    Returns:
        The :class:`InternalError` to raise in place of ``exc``.
    """
    error_id = uuid.uuid4().hex[:12]
    _logger.error(
        event,
        error=str(exc),
        error_type=exc.__class__.__name__,
        error_id=error_id,
        exc_info=True,
        **context,
    )
    return InternalError(error_id)


class ResourceNotFoundError(AppError):
    """Exception raised when a requested resource is not found."""
    
    def __init__(self, message: str, resource_type: str = None, resource_id: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            resource_type: Type of resource that was not found.
            resource_id: ID of the resource that was not found.
            details: Additional error details.
        """
        details = details or {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = resource_id
        super().__init__(message, "RESOURCE_NOT_FOUND", details)
