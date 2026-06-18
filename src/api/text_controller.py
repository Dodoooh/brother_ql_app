"""
Controller for text printing API endpoints.
"""

import structlog
from typing import Dict, Any

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.exceptions import ValidationError, PrinterError, ResourceNotFoundError, ConfirmationRequiredError
from src.utils.print_guard import enforce_large_batch_confirmation, is_confirmed
from src.utils.dry_run import is_dry_run, build_dry_run_response

logger = structlog.get_logger()


def _short_label(text: str, limit: int = 40) -> str:
    """Build a short, single-line human label for a queued job."""
    flattened = " ".join((text or "").split())
    if len(flattened) > limit:
        return flattened[:limit].rstrip() + "..."
    return flattened

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
        settings = settings_service.resolve_print_settings(body.get("settings"))
        
        if not text:
            raise ValidationError("text is required", "text")
        if not settings:
            raise ValidationError("settings is required", "settings")
        
        # Validate required settings
        required_settings = ["printer_uri", "printer_model", "label_size"]
        for setting in required_settings:
            if setting not in settings:
                raise ValidationError(f"{setting} is required", f"settings.{setting}")

        # Large batches require explicit confirmation before enqueuing.
        enforce_large_batch_confirmation(
            settings.get("copies", 1), is_confirmed(body.get("confirm_large_batch"))
        )

        # Dry run: render + reachability check, but do not print or enqueue.
        if is_dry_run(body.get("dry_run")):
            data_url = printer_service.render_text_preview(text, settings)
            return build_dry_run_response(settings, data_url)

        # Enqueue the print job; the actual print runs later in the worker.
        def job(text=text, settings=settings):
            printer_service.print_text(text, settings)

        # Parameters that allow the UI to restore the form for a reprint.
        params = {"type": "text", "text": text, "settings": settings}
        job_id = print_queue.submit("text", _short_label(text), job, params=params)
        logger.info("Text print job queued", job_id=job_id)

        return {"success": True, "job_id": job_id, "message": "Print job queued"}
    except ConfirmationRequiredError:
        raise
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except ResourceNotFoundError as e:
        logger.error("Resource not found", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error", error=str(e), exc_info=True)
        raise ValidationError(str(e), "settings")
    except Exception as e:
        logger.error("Error printing text", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing text: {str(e)}")
