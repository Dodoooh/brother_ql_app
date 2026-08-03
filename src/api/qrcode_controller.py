"""
Controller for QR code-related API endpoints.
"""

import structlog
from typing import Dict, Any

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.exceptions import ValidationError, PrinterError, ConfirmationRequiredError
from src.utils.print_guard import enforce_large_batch_confirmation, is_confirmed
from src.utils.dry_run import is_dry_run, build_dry_run_response

logger = structlog.get_logger()


def _short_label(text: str, limit: int = 40) -> str:
    """Build a short, single-line human label for a queued job."""
    flattened = " ".join((text or "").split())
    if len(flattened) > limit:
        return flattened[:limit].rstrip() + "..."
    return flattened

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
        settings = settings_service.resolve_print_settings(body.get("settings"))
        
        # Get data from qr settings
        data = qr_settings.get("data")
        
        if not data:
            raise ValidationError("qr.data is required", "qr.data")

        # Large batches require explicit confirmation before enqueuing.
        enforce_large_batch_confirmation(
            settings.get("copies", 1), is_confirmed(body.get("confirm_large_batch"))
        )

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

            # Only the caption itself is conditional. Asking for a caption and
            # leaving the text empty means "label it with what it encodes", and
            # the render path already falls back to the QR data; gating the
            # whole block on text_content meant that request printed no caption
            # at all.
            if text_content:
                combined_settings["text"] = text_content
            combined_settings["show_text"] = text_position != "none"
            combined_settings["text_position"] = text_position
            combined_settings["text_font_size"] = text_settings.get("font_size", 30)
            combined_settings["text_alignment"] = text_settings.get("alignment", "center")
            combined_settings["text_wrap"] = text_settings.get("wrap", True)
        
        # Dry run: render + reachability check, but do not print or enqueue.
        if is_dry_run(body.get("dry_run")):
            data_url = printer_service.render_qrcode_preview(combined_settings)
            return build_dry_run_response(combined_settings, data_url)

        # Enqueue the print job; the actual print runs later in the worker.
        def job(data=data, settings=combined_settings):
            printer_service.print_qr_code(data, settings)

        # Parameters that allow the UI to restore the form for a reprint.
        params = {"type": "qrcode", "data": data, "settings": combined_settings}
        job_id = print_queue.submit("qrcode", "QR: " + _short_label(data), job, params=params)
        logger.info("QR code print job queued", job_id=job_id)

        return {"success": True, "job_id": job_id, "message": "Print job queued"}
    except ConfirmationRequiredError:
        raise
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error", error=str(e), exc_info=True)
        raise ValidationError(str(e), "settings")
    except Exception as e:
        logger.error("Error printing QR code", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing QR code: {str(e)}")
