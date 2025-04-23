"""
Controller for QR code-related API endpoints.
"""

import structlog
from typing import Dict, Any

from src.services.printer_service import printer_service
from src.utils.exceptions import ValidationError, PrinterError

logger = structlog.get_logger()

def print_qr_code(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Print a QR code on a label.
    
    Args:
        body: Dict containing QR code data and print settings.
        
    Returns:
        Dict containing the result of the print operation.
    """
    try:
        logger.info("Processing QR code print request")
        
        # Extract and validate parameters
        qr_settings = body.get("qr", {})
        text_settings = body.get("text", {})
        settings = body.get("settings", {})
        
        # Get data from qr settings
        data = qr_settings.get("data")
        
        if not data:
            raise ValidationError("qr.data is required", "qr.data")
        
        # Prepare settings for the printer service
        combined_settings = settings.copy()
        
        # Add QR code settings
        if qr_settings:
            combined_settings["qr_version"] = qr_settings.get("version", 1)
            combined_settings["qr_size"] = qr_settings.get("size", 400)
            combined_settings["qr_box_size"] = qr_settings.get("box_size", 10)
            combined_settings["qr_border"] = qr_settings.get("border", 4)
            combined_settings["error_correction"] = qr_settings.get("error_correction", "M")
        
        # Add text settings
        if text_settings:
            text_content = text_settings.get("content")
            text_position = text_settings.get("position", "bottom")
            
            if text_content:
                combined_settings["text"] = text_content
                combined_settings["show_text"] = text_position != "none"
                combined_settings["text_position"] = text_position
                combined_settings["text_font_size"] = text_settings.get("font_size", 30)
                combined_settings["text_alignment"] = text_settings.get("alignment", "center")
        
        # Print QR code
        result = printer_service.print_qr_code(data, combined_settings)
        
        return result
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error printing QR code", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing QR code: {str(e)}")
