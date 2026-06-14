"""
Controller for PDF printing API endpoints.
"""

import os
import json
import uuid
import structlog
from typing import Dict, Any, Optional
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from flask import request, current_app

from src.services.printer_service import printer_service
from src.services.pdf_renderer import render_pdf_thumbnails
from src.utils.exceptions import ValidationError, PrinterError

logger = structlog.get_logger()

# Leading magic bytes that mark a PDF document.
_PDF_MAGIC = b"%PDF"


def print_pdf() -> Dict[str, Any]:
    """
    Print a PDF document on labels.

    Reads the multipart/form-data payload (file + settings + optional pages
    and scale_mode), validates and stores the PDF, then hands it to the
    printer service. The uploaded file is always cleaned up afterwards.

    Returns:
        Dict containing the result of the print operation (PrintResponse).
    """
    try:
        logger.info("Processing PDF print request")

        # Check if PDF file is present
        if 'file' not in request.files:
            raise ValidationError("No PDF file provided", "file")

        pdf_file = request.files['file']
        if pdf_file.filename == '':
            raise ValidationError("No PDF file selected", "file")

        # Validate that the upload is actually a PDF (extension or magic bytes).
        if not _is_pdf(pdf_file):
            raise ValidationError("Uploaded file is not a PDF", "file")

        # Parse settings JSON
        settings_json = request.form.get('settings', '{}')
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError:
            raise ValidationError("Invalid settings JSON", "settings")

        if not isinstance(settings, dict):
            raise ValidationError("settings must be a JSON object", "settings")

        # Validate required settings
        required_settings = ["printer_uri", "printer_model", "label_size"]
        for setting in required_settings:
            if setting not in settings:
                raise ValidationError(f"{setting} is required", f"settings.{setting}")

        # Optional page selection (e.g. "1-3,5"); empty/missing means all pages.
        pages = request.form.get('pages', '').strip() or None

        # Optional scaling mode, defaults to "fit".
        scale_mode = request.form.get('scale_mode', 'fit').strip() or 'fit'
        if scale_mode not in ('fit', 'fill'):
            raise ValidationError("scale_mode must be 'fit' or 'fill'", "scale_mode")

        # Save uploaded PDF
        pdf_path = _save_uploaded_file(pdf_file)
        logger.info("PDF saved", path=pdf_path)

        try:
            result = printer_service.print_pdf(pdf_path, settings, pages, scale_mode)
            return result
        finally:
            # Only remove the upload we saved here; render artifacts are
            # handled by the printer service.
            _cleanup_uploaded_file(pdf_path)
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error printing PDF", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "settings")
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error printing PDF", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing PDF: {str(e)}")


def preview_pdf() -> Dict[str, Any]:
    """
    Render a server-side preview (thumbnails) of selected PDF pages.

    Reads the multipart/form-data payload (file + optional pages), validates
    and stores the PDF temporarily, renders the selected pages as small PNG
    thumbnails and returns them as data URLs. The uploaded file is always
    cleaned up afterwards -- the preview does not need it kept around.

    Returns:
        Dict containing the preview result (PdfPreview).
    """
    pdf_path = None
    try:
        logger.info("Processing PDF preview request")

        # Check if PDF file is present
        if 'file' not in request.files:
            raise ValidationError("No PDF file provided", "file")

        pdf_file = request.files['file']
        if pdf_file.filename == '':
            raise ValidationError("No PDF file selected", "file")

        # Validate that the upload is actually a PDF (extension or magic bytes).
        if not _is_pdf(pdf_file):
            raise ValidationError("Uploaded file is not a PDF", "file")

        # Optional page selection (e.g. "1-3,5"); empty/missing means all pages.
        pages = request.form.get('pages', '').strip() or None

        # Save uploaded PDF temporarily.
        pdf_path = _save_uploaded_file(pdf_file)
        logger.info("PDF saved for preview", path=pdf_path)

        result = render_pdf_thumbnails(pdf_path, pages)
        return result
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error previewing PDF", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "pages")
    except PrinterError as e:
        logger.error("Printer error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error previewing PDF", error=str(e), exc_info=True)
        raise PrinterError(f"Error previewing PDF: {str(e)}")
    finally:
        # The preview never needs to keep the upload around.
        _cleanup_uploaded_file(pdf_path)


def _is_pdf(file: FileStorage) -> bool:
    """
    Determine whether an uploaded file is a PDF.

    Accepts the file if its filename ends in .pdf or if it begins with the
    %PDF magic bytes. The stream position is restored afterwards so the file
    can still be saved.

    Args:
        file: The uploaded file.

    Returns:
        True if the upload looks like a PDF.
    """
    name = (file.filename or "").lower()
    if name.endswith(".pdf"):
        return True

    try:
        head = file.stream.read(len(_PDF_MAGIC))
        file.stream.seek(0)
    except (OSError, ValueError):
        return False

    return head == _PDF_MAGIC


def _save_uploaded_file(file: FileStorage) -> str:
    """
    Save an uploaded file to the upload folder under a UUID-based name.

    Args:
        file: The uploaded file.

    Returns:
        Path to the saved file.
    """
    # Sanitize the original filename to prevent path traversal. secure_filename
    # may return an empty string, so we only keep its extension and always
    # prefix a UUID.
    safe_name = secure_filename(file.filename or "")
    extension = os.path.splitext(safe_name)[1] or ".pdf"
    filename = f"{uuid.uuid4().hex}{extension}"

    upload_folder = _get_upload_folder()
    os.makedirs(upload_folder, exist_ok=True)

    file_path = os.path.join(upload_folder, filename)
    file.save(file_path)

    return file_path


def _get_upload_folder() -> str:
    """Return the configured upload folder, falling back to the default."""
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


def _cleanup_uploaded_file(file_path: Optional[str]) -> None:
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
        logger.warning("Failed to clean up uploaded file", path=file_path, error=str(e))
