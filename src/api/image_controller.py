"""
Controller for image printing API endpoints.
"""

import os
import json
import uuid
import structlog
from typing import Dict, Any
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from flask import request, current_app
from PIL import Image, UnidentifiedImageError

from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.exceptions import ValidationError, PrinterError, ImageProcessingError, ResourceNotFoundError, ConfirmationRequiredError
from src.utils.print_settings_schema import parse_and_validate_settings
from src.utils.print_guard import enforce_large_batch_confirmation, is_confirmed
from src.utils.dry_run import is_dry_run, build_dry_run_response

logger = structlog.get_logger()

# Guard against decompression-bomb DoS: cap the number of pixels Pillow will
# decode from an uploaded image.
Image.MAX_IMAGE_PIXELS = 50_000_000

def print_image() -> Dict[str, Any]:
    """
    Print an image on a label.
    
    Returns:
        Dict containing the result of the print operation.
    """
    try:
        logger.info("Processing image print request")
        
        # Check if image file is present
        if 'image' not in request.files:
            raise ValidationError("No image file provided", "image")
        
        image_file = request.files['image']
        if image_file.filename == '':
            raise ValidationError("No image file selected", "image")
        
        # Parse settings
        # Validated against the same PrintSettings schema the JSON endpoints
        # are held to. A file upload carries settings as a string, so the spec
        # cannot check inside it and this route used to accept anything.
        settings = settings_service.resolve_print_settings(
            parse_and_validate_settings(request.form.get('settings')))
        
        # Validate required settings
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
        # service removes it later -- the job no longer deletes it.
        stored_path = _save_uploaded_file(image_file)
        logger.info("Image saved", path=stored_path)

        # Verify the uploaded file is actually a decodable image before
        # enqueuing it. Image.verify() consumes the file object, so we
        # re-open for each step. On rejection we clean up the just-saved file
        # immediately, since nothing was queued.
        try:
            with Image.open(stored_path) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            logger.warning("Rejected non-image or invalid upload", error=str(e))
            _cleanup_uploaded_file(stored_path)
            raise ValidationError("Uploaded file is not a valid image", "image")

        # Enqueue the print job. The job prints from the persistent path and
        # does NOT delete it; TTL cleanup handles removal. Default args bind
        # the current path/settings to avoid late-binding in the closure.
        def job(path=stored_path, s=settings):
            printer_service.print_image(path, s)

        original_name = image_file.filename or "Image"
        label = secure_filename(image_file.filename or "") or "Image"
        params = {"type": "image", "filename": original_name, "settings": settings}
        job_id = print_queue.submit(
            "image", label, job, params=params, file_path=stored_path
        )
        logger.info("Image print job queued", job_id=job_id, path=stored_path)

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
    except ResourceNotFoundError as e:
        logger.error("Resource not found", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error", error=str(e), exc_info=True)
        raise ValidationError(str(e), "settings")
    except Exception as e:
        logger.error("Error printing image", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing image: {str(e)}")

def _save_uploaded_file(file: FileStorage) -> str:
    """
    Save an uploaded file to the upload folder.
    
    Args:
        file: The uploaded file.
        
    Returns:
        Path to the saved file.
    """
    # Sanitize the original filename to prevent path traversal. secure_filename
    # may return an empty string (e.g. for names made up entirely of unsafe
    # characters), so we only keep its extension and always prefix a UUID.
    safe_name = secure_filename(file.filename or "")
    extension = os.path.splitext(safe_name)[1]
    filename = f"{uuid.uuid4().hex}{extension}"

    # Persist into the uploads/jobs/ subfolder so queued jobs keep their file
    # around for reprint/open until the queue service's TTL cleanup removes it.
    jobs_folder = os.path.join(_get_upload_folder(), "jobs")
    os.makedirs(jobs_folder, exist_ok=True)

    # Save the file
    file_path = os.path.join(jobs_folder, filename)
    file.save(file_path)

    return file_path


def _get_upload_folder() -> str:
    """Return the configured upload folder, falling back to the default.

    Resolves from app config first, then printer_service.upload_folder (the
    single source of truth honouring the UPLOAD_FOLDER env var), then the
    historical code-relative default as a last resort.
    """
    try:
        upload_folder = current_app.config.get('UPLOAD_FOLDER')
    except RuntimeError:
        upload_folder = None
    if not upload_folder:
        upload_folder = getattr(printer_service, "upload_folder", None)
    if not upload_folder:
        upload_folder = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads"
        )
    return upload_folder


def _cleanup_uploaded_file(file_path: str) -> None:
    """
    Remove the uploaded source file after processing.

    Args:
        file_path: Path to the saved upload to delete.
    """
    if not file_path:
        return
    try:
        os.remove(file_path)
        logger.info("Cleaned up uploaded file", path=file_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Cleanup failure must not break the print response.
        logger.warning("Failed to clean up uploaded file", path=file_path, error=str(e))
