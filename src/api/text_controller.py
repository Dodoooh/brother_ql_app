"""
Controller for text printing API endpoints.
"""

import structlog
from typing import Dict, Any

from services.printer_service import printer_service
from utils.exceptions import ValidationError, PrinterError, ResourceNotFoundError

logger = structlog.get_logger()

def print_text(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Print text on a label.
    
    Args:
        body: Dict containing text and print settings.
        
    Returns:
        Dict containing the result of the print operation.
    """
    try:
        logger.info("Processing text print request")
        
        # Extract and validate parameters
        text = body.get("text")
        settings = body.get("settings", {})
        
        if not text:
            raise ValidationError("text is required", "text")
        if not settings:
            raise ValidationError("settings is required", "settings")
        
        # Validate required settings
        required_settings = ["printer_uri", "printer_model", "label_size"]
        for setting in required_settings:
            if setting not in settings:
                raise ValidationError(f"{setting} is required", f"settings.{setting}")
        
        # Print text
        result = printer_service.print_text(text, settings)
        
        return result
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except ResourceNotFoundError as e:
        logger.error("Resource not found", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error printing text", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing text: {str(e)}")
