"""
Controller for printer-related API endpoints.
"""

import structlog
from typing import Dict, Any, List

from src.services.printer_service import printer_service
from src.services.settings_service import settings_service
from src.utils.exceptions import ValidationError, PrinterError

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
    except Exception as e:
        logger.error("Error getting printers", error=str(e), exc_info=True)
        raise PrinterError(f"Error getting printers: {str(e)}")

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
        writer -- PUT /settings, on every change -- and a status check that wrote
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
        # purpose: the rules for it -- which identifiers are the same physical
        # medium, which sizes cannot be told apart -- live with the label
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
    except Exception as e:
        logger.error("Error checking printer status", error=str(e), exc_info=True)
        raise PrinterError(f"Error checking printer status: {str(e)}")

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
    except Exception as e:
        logger.error("Error getting keep alive status", error=str(e), exc_info=True)
        raise PrinterError(f"Error getting keep alive status: {str(e)}")

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
    except Exception as e:
        logger.error("Error updating keep alive", error=str(e), exc_info=True)
        raise PrinterError(f"Error updating keep alive: {str(e)}")
