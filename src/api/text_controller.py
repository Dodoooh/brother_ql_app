"""
Controller for text printing API endpoints.
"""

import structlog
from typing import Dict, Any

from flask import send_file

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


def preview_text(body: Dict[str, Any]) -> Any:
    """
    Render a text label and return the PNG without printing.

    Uses the same code path as print_text, so the preview is the exact bitmap
    that would be sent to the printer -- including wrapping, font metrics and
    label width. A client-side approximation cannot stay in sync with Pillow's
    rendering; this cannot drift because it IS the render.

    Args:
        body: Dict containing text and render settings.

    Returns:
        Flask response containing the rendered PNG.
    """
    try:
        logger.info("Processing text preview request")

        text = body.get("text")
        settings = body.get("settings", {})

        if not text:
            raise ValidationError("text is required", "text")

        # label_size drives the canvas width; the printer settings are not
        # needed to render, so preview works before a printer is configured.
        settings.setdefault("label_size", "62")

        image_path = printer_service._create_text_label(text, settings)

        rotate = settings.get("rotate", 0)
        if rotate:
            image_path = printer_service._apply_rotation(image_path, rotate)

        return send_file(image_path, mimetype="image/png")
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error generating preview", error=str(e), exc_info=True)
        raise
