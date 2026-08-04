"""
Controller for combined label layouts (text + uploaded image).
"""

import json
import structlog
from typing import Dict, Any

from flask import request
from werkzeug.utils import secure_filename
from PIL import Image, UnidentifiedImageError

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.exceptions import (
    AppError,
    ValidationError,
    PrinterError,
    ImageProcessingError,
    ConfirmationRequiredError,
    internal_error,
)
from src.utils.print_settings_schema import parse_and_validate_settings
from src.utils.print_guard import enforce_large_batch_confirmation, is_confirmed
from src.utils.dry_run import is_dry_run, build_dry_run_response

# Reuse the image controller's persistent-upload helpers to avoid duplication.
from src.api.image_controller import (
    _save_uploaded_file,
    _cleanup_uploaded_file,
)

logger = structlog.get_logger()

# Guard against decompression-bomb DoS: cap the number of pixels Pillow will
# decode from an uploaded image.
Image.MAX_IMAGE_PIXELS = 50_000_000


def _short_label(text: str, limit: int = 40) -> str:
    """Build a short, single-line human label for a queued job."""
    flattened = " ".join((text or "").split())
    if len(flattened) > limit:
        return flattened[:limit].rstrip() + "..."
    return flattened


def print_text_image() -> Dict[str, Any]:
    """
    Print a label with an uploaded image and a text block side by side.

    Reads the multipart/form-data payload (image + text + layout options +
    settings), validates and persists the image, then enqueues a print job
    that renders the image and text side by side.

    Returns:
        Dict containing the result of the print operation (PrintResponse).
    """
    try:
        logger.info("Processing text+image label print request")

        # Check if image file is present.
        if 'image' not in request.files:
            raise ValidationError("No image file provided", "image")

        image_file = request.files['image']
        if image_file.filename == '':
            raise ValidationError("No image file selected", "image")

        # Text content is required.
        text = request.form.get('text')
        if not text:
            raise ValidationError("text is required", "text")

        # Layout options.
        try:
            font_size = int(request.form.get('font_size', 30))
        except (TypeError, ValueError):
            raise ValidationError("font_size must be an integer", "font_size")

        alignment = request.form.get('alignment', 'left')
        if alignment not in ('left', 'center', 'right'):
            raise ValidationError("alignment must be one of: left, center, right", "alignment")

        position = request.form.get('position', 'right')
        if position not in ('left', 'right'):
            raise ValidationError("position must be one of: left, right", "position")

        # Parse settings.
        # Validated against the same PrintSettings schema the JSON endpoints
        # are held to. A file upload carries settings as a string, so the spec
        # cannot check inside it and this route used to accept anything.
        settings = settings_service.resolve_print_settings(
            parse_and_validate_settings(request.form.get('settings')))

        if not isinstance(settings, dict):
            raise ValidationError("settings must be a JSON object", "settings")

        # Validate required settings.
        required_settings = ["printer_uri", "printer_model", "label_size"]
        for setting in required_settings:
            if setting not in settings:
                raise ValidationError(f"{setting} is required", f"settings.{setting}")

        # Large batches require explicit confirmation before enqueuing.
        enforce_large_batch_confirmation(
            settings.get("copies", 1), is_confirmed(request.form.get("confirm_large_batch"))
        )

        # Dry run: validate settings + reachability, but do not save/print.
        if is_dry_run(request.form.get("dry_run")):
            return build_dry_run_response(settings, None)

        # Persist the uploaded image under uploads/jobs/ so it survives the
        # print and is available for reprint/open. TTL cleanup in the queue
        # service removes it later.
        stored_path = _save_uploaded_file(image_file)
        logger.info("Image saved", path=stored_path)

        # Verify the uploaded file is actually a decodable image before
        # enqueuing it. On rejection we clean up the just-saved file
        # immediately, since nothing was queued.
        try:
            with Image.open(stored_path) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            logger.warning("Rejected non-image or invalid upload", error=str(e))
            _cleanup_uploaded_file(stored_path)
            raise ValidationError("Uploaded file is not a valid image", "image")

        # Pass the layout options into settings the service layer reads.
        combined_settings = settings.copy()
        combined_settings["image_position"] = position
        combined_settings["text_alignment"] = alignment
        combined_settings["text_font_size"] = font_size

        # Enqueue the print job. The job prints from the persistent path and
        # does NOT delete it; TTL cleanup handles removal. Default args bind
        # the current path/settings to avoid late-binding in the closure.
        def job(path=stored_path, t=text, s=combined_settings):
            printer_service.print_text_image(path, t, s)

        original_name = image_file.filename or "Image"
        # Parameters that allow the UI to restore the form for a reprint.
        params = {
            "type": "text-image",
            "text": text,
            "filename": original_name,
            "settings": combined_settings,
            "font_size": font_size,
            "alignment": alignment,
            "position": position,
        }
        job_id = print_queue.submit(
            "label", "Text+Image: " + _short_label(text), job,
            params=params, file_path=stored_path
        )
        logger.info("Text+image label print job queued", job_id=job_id, path=stored_path)

        return {"success": True, "job_id": job_id, "message": "Print job queued"}
    except ConfirmationRequiredError:
        raise
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except ImageProcessingError as e:
        logger.error("Image processing error", error=str(e), exc_info=True)
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
        raise internal_error(e, "Error printing text+image label") from e
