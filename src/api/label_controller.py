"""
Controller for combined label layouts (text + QR code).
"""

import structlog
from typing import Dict, Any

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.exceptions import (AppError, ValidationError, PrinterError, ConfirmationRequiredError,
                                  internal_error)
from src.utils.print_guard import enforce_large_batch_confirmation, is_confirmed
from src.utils.dry_run import is_dry_run, build_dry_run_response

logger = structlog.get_logger()


def _short_label(text: str, limit: int = 40) -> str:
    """Build a short, single-line human label for a queued job."""
    flattened = " ".join((text or "").split())
    if len(flattened) > limit:
        return flattened[:limit].rstrip() + "..."
    return flattened

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
        settings = settings_service.resolve_print_settings(body.get("settings"))
        
        # Get QR data
        qr_data = qr_settings.get("data")
        if not qr_data:
            raise ValidationError("qr.data is required", "qr.data")
        
        # Get text content
        text_content = text_settings.get("content")
        if not text_content:
            raise ValidationError("text.content is required", "text.content")

        # Large batches require explicit confirmation before enqueuing.
        enforce_large_batch_confirmation(
            settings.get("copies", 1), is_confirmed(body.get("confirm_large_batch"))
        )

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
        combined_settings["text_wrap"] = text_settings.get("wrap", True)

        # Add QR code settings
        if qr_settings:
            combined_settings["qr_version"] = qr_settings.get("version", 1)
            combined_settings["qr_size"] = qr_settings.get("size", 400)
            combined_settings["qr_box_size"] = qr_settings.get("box_size", 10)
            combined_settings["qr_border"] = qr_settings.get("border", 4)
            combined_settings["error_correction"] = qr_settings.get("error_correction", "M")
        
        # Dry run: render + reachability check, but do not print or enqueue.
        if is_dry_run(body.get("dry_run")):
            data_url = printer_service.render_label_preview(combined_settings)
            return build_dry_run_response(combined_settings, data_url)

        # Enqueue the combined text+QR print job; printed later in the worker.
        def job(data=qr_data, settings=combined_settings):
            printer_service.print_qr_code(data, settings)

        # Parameters that allow the UI to restore the form for a reprint.
        params = {
            "type": "label",
            "text": text_content,
            "data": qr_data,
            "settings": combined_settings,
        }
        job_id = print_queue.submit(
            "label", "Text+QR: " + _short_label(text_content), job, params=params
        )
        logger.info("Text+QR code label print job queued", job_id=job_id)

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
        raise internal_error(e, "Error printing text+QR code label") from e
