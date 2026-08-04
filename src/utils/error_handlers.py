"""
Error handlers for the application.

Every error a client sees leaves the app through this module, and they all get
the same shape -- the ``Error`` schema the OpenAPI specification declares::

    {"code": "VALIDATION_ERROR", "message": "...", "details": {...}}

That was not true before. Four shapes were in circulation: our own errors used
the schema; Connexion's request validator answered with an empty ``message`` and
a *string* ``details``; 404/405/413 came out as RFC-7807 problem documents
because Connexion registers a handler per HTTP status code (see
``FlaskApp.set_errors_handlers``), which Flask prefers over a handler registered
on the ``HTTPException`` class; and the API-key check answered ``{"error":
"unauthorized"}``. Three of those four were undeclared, so a client had to
parse all of them to be safe. :func:`register_error_handlers` now claims every
status code Connexion claimed, which is why the per-code loop at the bottom is
not redundant with the class-based registration next to it.

The second job of this module is deciding how much of an error a client is
allowed to read. Two rules:

* An error the caller caused keeps its sentence. "label_size 'xy' is not
  supported", "copies exceeds the maximum", the printer's own refusal -- those
  are the difference between a client that can fix the request and one that
  cannot.
* An error from the inside of the app is answered with
  :class:`~src.utils.exceptions.InternalError`: a fixed sentence plus a
  correlation id. The real one goes to the log.

On top of that every outgoing message passes through :func:`_scrub`, which
removes absolute server paths. That is a backstop for messages we do not write
ourselves -- ``pdf_renderer`` puts the path of the temporary upload into the
``ValueError`` it raises for an unreadable PDF, and that ValueError legitimately
becomes a 400, so the path would otherwise be echoed to whoever uploaded a
broken file.
"""

import re
import structlog
from typing import Any, Dict, Tuple
from werkzeug.exceptions import HTTPException, default_exceptions
from connexion import ProblemException

from src.utils.exceptions import (
    AppError,
    InternalError,
    ValidationError,
    ResourceNotFoundError,
    PrinterError,
    ImageProcessingError,
    ConfigurationError,
    ConfirmationRequiredError,
    internal_error,
)

logger = structlog.get_logger()

# --- Message hygiene ---------------------------------------------------------

# Top-level directories that only ever name the server's own filesystem. A path
# under one of these tells a caller where the app is installed, where uploads
# are staged and which OS layout it runs on -- reconnaissance, never help.
#
# ``dev`` is deliberately absent: a printer URI like ``file:///dev/usb/lp0`` is
# the user's own configuration, and mangling it would break the one message that
# explains what a valid URI looks like.
_INTERNAL_PATH_ROOTS = (
    "app", "usr", "opt", "srv", "home", "root", "Users", "tmp", "var",
    "private", "etc", "data", "mnt", "media", "proc", "lib", "bin", "sbin",
)

# Matches an absolute path rooted at one of the directories above. The
# lookbehind keeps it off URLs (``http://example.com/app/x`` -- the slash there
# follows a word character) and off ``file:///app/...`` URIs, whose slash
# follows another slash; a path quoted or spaced inside a sentence still
# matches. The run stops at whitespace or a closing quote/bracket so the rest of
# the sentence survives.
_SERVER_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:" + "|".join(_INTERNAL_PATH_ROOTS) + r")(?:/[^\s'\"`),;]*)+"
)

_REDACTED_PATH = "<path>"

# HTTP status -> the ``code`` clients switch on. 400 maps onto the same
# VALIDATION_ERROR our own controllers raise on purpose: the request was wrong
# either way, and which layer noticed is our business, not the caller's.
_STATUS_ERROR_CODES = {
    400: "VALIDATION_ERROR",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    409: "CONFLICT",
    410: "GONE",
    413: "PAYLOAD_TOO_LARGE",
    414: "URI_TOO_LONG",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "UNPROCESSABLE_ENTITY",
    429: "TOO_MANY_REQUESTS",
    # 5xx keeps its status and its code -- a 503 is not a 500 and an orchestrator
    # reads the difference -- but never its message; see the handlers below.
    500: "INTERNAL_SERVER_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}


def _status_code_name(status: int) -> str:
    """Return the ``code`` for an HTTP status, falling back to ``HTTP_<n>``."""
    return _STATUS_ERROR_CODES.get(status, f"HTTP_{status}")


# How much of a message is worth sending. A schema violation quotes the value
# that broke the rule, and for a maxLength on a text field that value is the
# whole payload: refusing 10000 characters used to answer with 10000 characters.
# The part that identifies the problem sits at the front, so the tail is cut.
_MAX_MESSAGE_CHARS = 400
_TRUNCATION_MARK = "... [truncated]"


def _shorten(value: str) -> str:
    """Cap a message so a rejected payload is not echoed back in full."""
    if len(value) <= _MAX_MESSAGE_CHARS:
        return value
    keep = _MAX_MESSAGE_CHARS - len(_TRUNCATION_MARK)
    return value[:keep].rstrip() + _TRUNCATION_MARK


def _scrub(value: Any) -> Any:
    """
    Remove absolute server paths from a message on its way out, and cap length.

    Only strings are touched; anything else is returned as-is so numeric and
    boolean details (``copies``, ``threshold``) keep their type.
    """
    if not isinstance(value, str):
        return value
    return _shorten(_SERVER_PATH_RE.sub(_REDACTED_PATH, value))


def _scrub_details(details: Any) -> Any:
    """Apply :func:`_scrub` to every string inside an error's ``details``."""
    if isinstance(details, dict):
        return {key: _scrub_details(val) for key, val in details.items()}
    if isinstance(details, (list, tuple)):
        return [_scrub_details(item) for item in details]
    return _scrub(details)


def build_error_body(code: str, message: str, details: Any = None) -> Dict[str, Any]:
    """
    Build the one response body this app answers errors with.

    Args:
        code: Machine-readable error code.
        message: Human-readable sentence, scrubbed before it is sent.
        details: Additional details; always an object in the response, because
            the declared schema says object and a client should not have to
            check whether it got a string this time.

    Returns:
        Dict matching the ``Error`` schema in ``openapi.yaml``.
    """
    if not isinstance(details, dict):
        details = {}
    return {
        "code": code,
        "message": _scrub(message or ""),
        "details": _scrub_details(details),
    }


def _app_error_body(error: AppError) -> Dict[str, Any]:
    """Shape one of our own exceptions into the response body."""
    return build_error_body(error.code, error.message, error.details)


def register_error_handlers(app):
    """
    Register error handlers for the application.

    Args:
        app: Flask application.
    """
    @app.errorhandler(ValidationError)
    def handle_validation_error(error: ValidationError) -> Tuple[Dict[str, Any], int]:
        """
        Handle validation errors.

        Args:
            error: ValidationError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Validation error", error=str(error), details=error.details)
        return _app_error_body(error), 400

    @app.errorhandler(ResourceNotFoundError)
    def handle_resource_not_found_error(error: ResourceNotFoundError) -> Tuple[Dict[str, Any], int]:
        """
        Handle resource not found errors.

        Args:
            error: ResourceNotFoundError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Resource not found", error=str(error), details=error.details)
        return _app_error_body(error), 404

    @app.errorhandler(PrinterError)
    def handle_printer_error(error: PrinterError) -> Tuple[Dict[str, Any], int]:
        """
        Handle printer errors.

        The message survives to the client on purpose: "the printer refused the
        job", "connection refused" and "no media" are what the person standing
        next to the device needs to read.

        Args:
            error: PrinterError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.error("Printer error", error=str(error), details=error.details)
        return _app_error_body(error), 500

    @app.errorhandler(ImageProcessingError)
    def handle_image_processing_error(error: ImageProcessingError) -> Tuple[Dict[str, Any], int]:
        """
        Handle image processing errors.

        Args:
            error: ImageProcessingError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.error("Image processing error", error=str(error), details=error.details)
        return _app_error_body(error), 500

    @app.errorhandler(ConfigurationError)
    def handle_configuration_error(error: ConfigurationError) -> Tuple[Dict[str, Any], int]:
        """
        Handle configuration errors.

        Args:
            error: ConfigurationError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.error("Configuration error", error=str(error), details=error.details)
        return _app_error_body(error), 500

    @app.errorhandler(ConfirmationRequiredError)
    def handle_confirmation_required_error(error: ConfirmationRequiredError) -> Tuple[Dict[str, Any], int]:
        """
        Handle large-batch confirmation errors.

        Args:
            error: ConfirmationRequiredError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Confirmation required", error=str(error), details=error.details)
        return _app_error_body(error), 400

    @app.errorhandler(InternalError)
    def handle_internal_error(error: InternalError) -> Tuple[Dict[str, Any], int]:
        """
        Handle an error that was already recorded and deliberately made generic.

        The full record (message, type, stack trace) was written where the
        exception was caught, by ``exceptions.internal_error``. Repeating it
        here would only double the noise, so this logs the correlation id alone
        -- enough to tie the response a user quotes back to that record.

        Args:
            error: InternalError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Answering with a generic internal error",
                       error_id=error.error_id)
        return _app_error_body(error), 500

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError) -> Tuple[Dict[str, Any], int]:
        """
        Handle generic application errors.

        Args:
            error: AppError instance.

        Returns:
            Tuple of error response and status code.
        """
        logger.error("Application error", error=str(error), details=error.details)
        return _app_error_body(error), 500

    @app.errorhandler(ProblemException)
    def handle_connexion_problem(error: ProblemException) -> Tuple[Dict[str, Any], int]:
        """
        Handle Connexion problem exceptions.

        These are what the specification-driven request validation raises, so
        they are the most common 400 the API produces -- "'printer_uri' is a
        required property" and friends. Connexion builds them without passing a
        message to ``Exception``, which is why the old handler's ``str(error)``
        produced an empty ``message`` while the useful text sat in ``detail``.
        The text is now where clients (including the bundled UI) look for it.

        Args:
            error: ProblemException instance.

        Returns:
            Tuple of error response and status code.
        """
        status = error.status or 500

        # A 5xx problem is an internal fault regardless of who raised it, and
        # its detail is not ours to vouch for. Note that we *return* the generic
        # body rather than raising it: an exception raised inside an error
        # handler escapes Flask's dispatch entirely and the client would get a
        # bare, bodyless 500 from the WSGI layer.
        if status >= 500:
            generic = internal_error(error, "Connexion problem",
                                     status=status, title=error.title)
            return build_error_body(_status_code_name(status), generic.message,
                                    generic.details), status

        message = error.detail or error.title or _status_code_name(status).replace("_", " ").capitalize()
        # ``ext`` is Connexion's own place for structured extras and is already
        # a dict; the plain string detail belongs in `message`, not in `details`.
        details = error.ext if isinstance(error.ext, dict) else {}

        logger.warning("Connexion problem", error=str(error), detail=error.detail,
                       title=error.title, status=status)
        return build_error_body(_status_code_name(status), message, details), status

    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException) -> Tuple[Dict[str, Any], int]:
        """
        Handle HTTP exceptions.

        Covers everything the framework raises before or around a handler: an
        unknown path (404), a wrong method (405), an upload over
        ``MAX_CONTENT_LENGTH`` (413). Werkzeug's ``description`` is a fixed,
        human sentence written for exactly this purpose, so it is kept -- but
        only below 500, where it describes the request rather than the server.

        Args:
            error: HTTPException instance.

        Returns:
            Tuple of error response and status code.
        """
        status = error.code or 500

        # See the note in the Connexion handler: return, never raise, from here.
        if status >= 500:
            generic = internal_error(error, "HTTP exception", status=status)
            return build_error_body(_status_code_name(status), generic.message,
                                    generic.details), status

        logger.warning("HTTP exception", error=str(error), code=status)
        return build_error_body(_status_code_name(status), error.description), status

    @app.errorhandler(Exception)
    def handle_generic_exception(error: Exception) -> Tuple[Dict[str, Any], int]:
        """
        Handle generic exceptions.

        The last line of defence: anything that reached here is a bug or an
        unanticipated library failure. It is logged in full and answered with
        nothing but a correlation id.

        Args:
            error: Exception instance.

        Returns:
            Tuple of error response and status code.
        """
        # Log the full details (type, message, stack trace) for operators, but
        # never leak internal exception strings or stack traces to the client.
        generic = internal_error(error, "Unhandled exception")
        return _app_error_body(generic), 500

    # Claim every status code Connexion claimed for its RFC-7807 responses.
    # ``register_error_handler(404, ...)`` is stored under the concrete
    # exception class *and* the code, and Flask's lookup tries the code before
    # walking the class hierarchy -- so without this loop the handler above is
    # never reached for any of these and the response is a problem document.
    for status_code in default_exceptions:
        app.register_error_handler(status_code, handle_http_exception)
