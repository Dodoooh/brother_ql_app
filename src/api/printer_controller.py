"""
Controller for printer-related API endpoints.
"""

import structlog
from typing import Dict, Any, List

from src.services.printer_service import printer_service
from src.services.relay_service import relay_service
from src.services.settings_service import settings_service
from src.utils.exceptions import AppError, ValidationError, PrinterError, internal_error

logger = structlog.get_logger()

def get_printers() -> List[Dict[str, Any]]:
    """
    Get available printers.
    
    Returns:
        List of printer configurations.
    """
    try:
        logger.info("Getting available printers")
        printers = printer_service.get_printers()
        return printers
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error getting printers") from e

def check_printer_status(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Check printer status, including the media it is loaded with.

    Args:
        body: Dict containing printer information. An optional ``label_size``
            says which label the loaded media should be compared against;
            without it the configured one is used.

    Returns:
        Dict containing printer status. Its ``media`` section carries the loaded
        medium, the label identifiers it could be, which single one those come
        down to (``resolution``) and what automatic mode wants done about it
        (``auto_switch``).

        The switch itself is the client's to make. ``label_size`` already has one
        writer — PUT /settings, on every change — and a status check that wrote
        it too would be racing that from a poll the UI repeats every 30 seconds,
        besides turning a read into an edit of the user's configuration.
    """
    try:
        logger.info("Checking printer status")

        # Extract and validate parameters
        printer_uri = body.get("printer_uri")
        printer_model = body.get("printer_model")
        label_size = body.get("label_size")

        if not printer_uri:
            raise ValidationError("printer_uri is required", "printer_uri")
        if not printer_model:
            raise ValidationError("printer_model is required", "printer_model")
        if label_size is not None and not isinstance(label_size, str):
            raise ValidationError("label_size must be a string", "label_size")

        # Check printer status. An offline/unreachable printer is a normal,
        # queryable state (reachable=false), not an error, so we return the
        # status with HTTP 200 and let the client render it.
        #
        # The media/label comparison is done here rather than in the browser on
        # purpose: the rules for it — which identifiers are the same physical
        # medium, which sizes cannot be told apart — live with the label
        # catalogue, and a second copy of them in JavaScript would be a second
        # thing to keep right.
        status = printer_service.check_printer_status(printer_uri, printer_model,
                                                      label_size=label_size)
        return status
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error", error=str(e), exc_info=True)
        raise ValidationError(str(e), "printer")
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error checking printer status") from e

def get_keep_alive_status() -> Dict[str, Any]:
    """
    Get the current status of the keep alive feature.
    
    Returns:
        Dict containing the status information.
    """
    try:
        logger.info("Getting keep alive status")
        status = printer_service.get_keep_alive_status()
        return status
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error getting keep alive status") from e

def get_relay_power_status() -> Dict[str, Any]:
    """
    Get the current state of relay power control.

    Read-only and side-effect free: it reports the configuration, the timing
    chain derived from it, when the turn_off webhook is next due, and the
    outcome of the most recent one. It never sends a webhook and never contacts
    the printer.

    Returns:
        Dict containing the relay power-control status. It always carries a
        ``warning`` naming the one thing the app cannot check for the user, that
        the configured ``printer_auto_power_off_minutes`` matches the device, and
        a ``hardware_offset_applied`` flag saying whether that interval was
        actually subtracted from the keep-alive window, so a client never has to
        infer the subtraction from two numbers that can legitimately be equal.

        The timing chain comes as clock times as well as offsets: every moment
        is reported both absolutely (unix and ISO-8601) and as the seconds left
        until it, and ``next_step`` names the one being waited on. ``origin_source``
        distinguishes a real print from the process start time the window falls
        back to before anything has printed, and from the current time the chain
        is re-based to once a window has run out; ``last_print_at`` answers "when
        did it last print" on its own, and is null when it has not. That is the
        difference between a display saying "last print" truthfully and saying it
        about a print that never happened.
    """
    try:
        logger.info("Getting relay power status")
        return relay_service.status()
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error getting relay power status") from e

def send_relay_power_webhook(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send one relay power webhook immediately and report the outcome.

    Unlike ``get_relay_power_status`` this really switches the relay: it is a
    POST because it is an action, and the action is named in the body rather
    than in the path so that ``turn_on`` and ``turn_off`` cannot be reached by
    guessing at a URL. Sending ``turn_off`` cuts mains power to the printer.

    It needs relay power control to be switched on and a URL for the action, and
    nothing else. The schedule does not have to be armed, and it is not touched:
    see ``RelayPowerService.send_now``.

    Args:
        body: Dict with an ``action`` of ``turn_on`` or ``turn_off``.

    Returns:
        Dict reporting what was sent, where, what came back and what that means
        for the printer's power. A relay that refused the request is reported
        with ``success: false`` and HTTP 200: the endpoint's job is to say what
        happened, and a refusal is an outcome rather than an error in carrying
        the request out. Only a request that could not be attempted at all is an
        error status.

    Raises:
        ValidationError: When the action is missing or unknown, when relay power
            control is switched off, or when no URL is configured for the action.
            Nothing was sent in any of those cases.
    """
    try:
        action = body.get("action")
        if action is None:
            raise ValidationError("action is required", "action")
        if not isinstance(action, str):
            raise ValidationError("action must be a string", "action")

        logger.info("Relay webhook requested by hand", action=action)
        report = relay_service.send_now(action)
        if report["success"]:
            logger.info("Relay webhook sent on request", action=action,
                        response_status=report["response_status"],
                        mains_power=report["mains_power"])
        else:
            logger.warning("Relay webhook requested by hand could not be confirmed",
                           action=action, error=report["error"])
        return report
    except ValidationError as e:
        logger.warning("Validation error", error=str(e))
        raise
    except ValueError as e:
        # The service refuses a request it must not attempt (feature off, no URL,
        # unknown action) with a plain ValueError, exactly as the settings
        # validator does. Those are bad requests, not server faults.
        logger.warning("Refusing to send a relay webhook", error=str(e))
        raise ValidationError(str(e), "relay_power")
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error sending relay webhook") from e

def update_keep_alive(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update the keep alive settings and start/stop the keep alive thread.
    
    Args:
        body: Dict containing keep alive settings.
        
    Returns:
        Dict containing the result of the operation.
    """
    try:
        logger.info("Updating keep alive settings")
        
        # Extract and validate parameters
        enabled = body.get("enabled")
        interval = body.get("interval", 60)
        
        if enabled is None:
            raise ValidationError("enabled is required", "enabled")
        
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean", "enabled")
        
        if not isinstance(interval, (int, float)):
            raise ValidationError("interval must be a number", "interval")
        
        if interval < 10:
            raise ValidationError("interval must be at least 10 seconds", "interval")

        # Prepare the specific settings to update
        keep_alive_update = {
            "keep_alive_enabled": enabled,
            "keep_alive_interval": interval
        }

        # Use update_settings to merge changes correctly and save
        success = settings_service.update_settings(keep_alive_update)

        if not success:
             logger.error("Failed to save keep-alive settings via update_settings")
             raise PrinterError("Failed to save keep-alive settings")

        # Start or stop keep alive thread
        if enabled:
            # Use the updated start_keep_alive method without parameters
            # It will automatically use the settings from settings_service
            result = printer_service.start_keep_alive(interval=interval)
        else:
            result = printer_service.stop_keep_alive()
        
        return {
            "success": result["success"],
            "message": result["message"],
            "enabled": enabled,
            "interval": interval
        }
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error", error=str(e), exc_info=True)
        raise ValidationError(str(e), "keep_alive")
    except AppError as e:
        # Our own errors already say the right thing to the caller (see
        # utils/exceptions.py) and must not be recast as internal. Logged
        # with the stack because the clause below no longer does it for them.
        logger.warning("Request failed with a reported error", error=str(e),
                       error_type=e.__class__.__name__, exc_info=True)
        raise
    except Exception as e:
        # Unexpected failure: recorded in full by internal_error, answered
        # generically so no library or filesystem detail reaches the client.
        raise internal_error(e, "Error updating keep alive") from e
