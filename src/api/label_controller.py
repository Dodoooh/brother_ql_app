"""
Controller for combined label layouts (text + QR code).
"""

import structlog
from typing import Dict, Any

from src.services.printer_service import printer_service
from src.utils.exceptions import ValidationError, PrinterError

logger = structlog.get_logger()

def print_text_qrcode_label(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Print a label with text on the left and QR code on the right.
    
    Args:
        body: Dict containing label data and print settings.
        
    Returns:
        Dict containing the result of the print operation.
    """
    try:
        logger.info("Processing text+QR code label print request")
        
        # Extract and validate parameters
        qr_settings = body.get("qr", {})
        text_settings = body.get("text", {})
        settings = body.get("settings", {})
        
        # Get QR data
        qr_data = qr_settings.get("data")
        if not qr_data:
            raise ValidationError("qr.data is required", "qr.data")
        
        # Get text content
        text_content = text_settings.get("content")
        if not text_content:
            raise ValidationError("text.content is required", "text.content")
        
        # Get layout options
        qr_position = qr_settings.get("position", "right")  # "left" or "right"
        text_alignment = text_settings.get("alignment", "left")  # "left", "center", or "right"
        text_font_size = text_settings.get("font_size", 30)
        
        # Add side-by-side settings
        combined_settings = settings.copy()
        combined_settings["side_by_side"] = True
        combined_settings["side_text"] = text_content
        combined_settings["qr_position"] = qr_position
        combined_settings["text_alignment"] = text_alignment
        combined_settings["text_font_size"] = text_font_size
        
        # Add QR code settings
        if qr_settings:
            combined_settings["qr_version"] = qr_settings.get("version", 1)
            combined_settings["qr_size"] = qr_settings.get("size", 400)
            combined_settings["qr_box_size"] = qr_settings.get("box_size", 10)
            combined_settings["qr_border"] = qr_settings.get("border", 4)
            combined_settings["error_correction"] = qr_settings.get("error_correction", "M")
        
        # Print QR code with side-by-side layout
        result = printer_service.print_qr_code(qr_data, combined_settings)
        
        return result
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error printing text+QR code label", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing text+QR code label: {str(e)}")
