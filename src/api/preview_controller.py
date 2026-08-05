"""
Controller for server-side "render-only" preview API endpoints.

These endpoints run the *exact* same render pipeline as the corresponding
print endpoints (same fonts, layout, rotation and 1-bit black/white
conversion) but return the rendered label as a base64 PNG data URL instead of
sending it to the printer. The settings parsing mirrors the matching print
controllers so the preview is faithful to the real print.
"""

import os
import json
import base64
import uuid
import structlog
from io import BytesIO
from typing import Any, Dict, Union

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from flask import request, current_app, Response
from PIL import Image, UnidentifiedImageError

from src.services.printer_service import printer_service
from src.services.settings_service import settings_service
from src.utils.exceptions import AppError, ValidationError, PrinterError, internal_error
from src.utils.print_settings_schema import parse_and_validate_settings

logger = structlog.get_logger()

# Guard against decompression-bomb DoS, consistent with the image print path.
Image.MAX_IMAGE_PIXELS = 50_000_000


def _preview_response(data_url: str) -> Union[Dict[str, Any], Response]:
    """Shape the rendered preview based on the request's ``Accept`` header.

    With ``Accept: image/png`` (and not ``application/json``) the raw PNG bytes
    are returned directly so a consumer can pipe them straight to an ``<img>``
    without parsing JSON or decoding base64; the pixel dimensions are exposed as
    ``X-Label-Width-Px`` / ``X-Label-Height-Px`` headers. Otherwise the default
    JSON wrapper ``{"image": "data:image/png;base64,..."}`` is returned (keeping
    existing clients, including the bundled UI, working unchanged).

    Content negotiation needs a live request, and there is not always one: these
    functions are plain callables that anything in-process may invoke directly.
    Without a request there is no ``Accept`` to honour, so the documented
    default -- the JSON wrapper -- is what comes back, rather than the render
    being lost to a context error. Same reasoning as :func:`_get_upload_folder`.
    """
    try:
        accept = (request.headers.get("Accept") or "").lower()
    except RuntimeError:
        accept = ""
    if "image/png" in accept and "application/json" not in accept:
        png = base64.b64decode(data_url.split(",", 1)[1])
        headers = {}
        try:
            with Image.open(BytesIO(png)) as im:
                headers["X-Label-Width-Px"] = str(im.width)
                headers["X-Label-Height-Px"] = str(im.height)
        except Exception:  # noqa: BLE001 - headers are best-effort metadata
            pass
        return Response(png, mimetype="image/png", headers=headers)

    # The JSON branch names its type as well. Declaring two content types on
    # the operation means Connexion no longer picks one on our behalf, and a
    # bare dict leaves it unable to tell which of the two was meant -- which it
    # answers with a 500 rather than a guess.
    return Response(json.dumps({"image": data_url}), mimetype="application/json")


def preview_text(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render a server-side preview of a text label (no printing).

    Args:
        body: Dict containing text and print settings (same shape as
            /text/print).

    Returns:
        Dict with the rendered label as a PNG data URL ({"image": <dataurl>}).
    """
    try:
        logger.info("Processing text preview request")

        # Extract and validate parameters (same as /text/print).
        text = body.get("text")
        settings = settings_service.resolve_print_settings(body.get("settings"))

        if not text:
            raise ValidationError("text is required", "text")
        if not settings:
            raise ValidationError("settings is required", "settings")

        image = printer_service.render_text_preview(text, settings)
        return _preview_response(image)
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error rendering text preview", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "settings")
    except PrinterError as e:
        logger.error("Printer error rendering text preview", error=str(e), exc_info=True)
        raise
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
        raise internal_error(e, "Error rendering text preview") from e


def preview_qrcode(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render a server-side preview of a QR code label (no printing).

    Args:
        body: Dict containing QR code data and print settings (same shape as
            /qrcode/print).

    Returns:
        Dict with the rendered label as a PNG data URL ({"image": <dataurl>}).
    """
    try:
        logger.info("Processing QR code preview request")

        # Extract and validate parameters (same as /qrcode/print).
        qr_settings = body.get("qr", {})
        text_settings = body.get("text", {})
        settings = settings_service.resolve_print_settings(body.get("settings"))

        data = qr_settings.get("data")
        if not data:
            raise ValidationError("qr.data is required", "qr.data")

        # Prepare settings for the printer service (mirrors qrcode_controller).
        combined_settings = settings.copy()
        combined_settings["data"] = data

        if qr_settings:
            combined_settings["qr_version"] = qr_settings.get("version", 1)
            combined_settings["qr_size"] = qr_settings.get("size", 400)
            combined_settings["qr_box_size"] = qr_settings.get("box_size", 10)
            combined_settings["qr_border"] = qr_settings.get("border", 4)
            combined_settings["error_correction"] = qr_settings.get("error_correction", "M")

        if text_settings:
            text_content = text_settings.get("content")
            text_position = text_settings.get("position", "bottom")

            # Only the caption itself is conditional; see qrcode_controller.
            # An empty caption with a position means "label it with what it
            # encodes", which the render path supplies from the QR data.
            if text_content:
                combined_settings["text"] = text_content
            combined_settings["show_text"] = text_position != "none"
            combined_settings["text_position"] = text_position
            combined_settings["text_font_size"] = text_settings.get("font_size", 30)
            combined_settings["text_alignment"] = text_settings.get("alignment", "center")
            combined_settings["text_wrap"] = text_settings.get("wrap", True)

        image = printer_service.render_qrcode_preview(combined_settings)
        return _preview_response(image)
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error rendering QR code preview", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "settings")
    except PrinterError as e:
        logger.error("Printer error rendering QR code preview", error=str(e), exc_info=True)
        raise
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
        raise internal_error(e, "Error rendering QR code preview") from e


def preview_label(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render a server-side preview of a combined text + QR code label (no
    printing).

    Args:
        body: Dict containing label data and print settings (same shape as
            /label/text-qrcode).

    Returns:
        Dict with the rendered label as a PNG data URL ({"image": <dataurl>}).
    """
    try:
        logger.info("Processing text+QR code label preview request")

        # Extract and validate parameters (same as /label/text-qrcode).
        qr_settings = body.get("qr", {})
        text_settings = body.get("text", {})
        settings = settings_service.resolve_print_settings(body.get("settings"))

        qr_data = qr_settings.get("data")
        if not qr_data:
            raise ValidationError("qr.data is required", "qr.data")

        text_content = text_settings.get("content")
        if not text_content:
            raise ValidationError("text.content is required", "text.content")

        qr_position = qr_settings.get("position", "right")
        text_alignment = text_settings.get("alignment", "left")
        text_font_size = text_settings.get("font_size", 30)

        # Add side-by-side settings (mirrors label_controller).
        combined_settings = settings.copy()
        combined_settings["data"] = qr_data
        combined_settings["side_by_side"] = True
        combined_settings["side_text"] = text_content
        combined_settings["qr_position"] = qr_position
        combined_settings["text_alignment"] = text_alignment
        combined_settings["text_font_size"] = text_font_size
        combined_settings["text_wrap"] = text_settings.get("wrap", True)

        if qr_settings:
            combined_settings["qr_version"] = qr_settings.get("version", 1)
            combined_settings["qr_size"] = qr_settings.get("size", 400)
            combined_settings["qr_box_size"] = qr_settings.get("box_size", 10)
            combined_settings["qr_border"] = qr_settings.get("border", 4)
            combined_settings["error_correction"] = qr_settings.get("error_correction", "M")

        image = printer_service.render_label_preview(combined_settings)
        return _preview_response(image)
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error rendering label preview", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "settings")
    except PrinterError as e:
        logger.error("Printer error rendering label preview", error=str(e), exc_info=True)
        raise
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
        raise internal_error(e, "Error rendering label preview") from e


def preview_image() -> Dict[str, Any]:
    """
    Render a server-side preview of an uploaded image label (no printing).

    Reads the multipart/form-data payload (image + settings JSON string, same
    shape as /image/print), stores the upload temporarily, renders it through
    the print pipeline and always cleans up the temporary file.

    Returns:
        Dict with the rendered label as a PNG data URL ({"image": <dataurl>}).
    """
    image_path = None
    try:
        logger.info("Processing image preview request")

        # Check if image file is present (same as /image/print).
        if 'image' not in request.files:
            raise ValidationError("No image file provided", "image")

        image_file = request.files['image']
        if image_file.filename == '':
            raise ValidationError("No image file selected", "image")

        # Parse settings JSON.
        # Validated against the same PrintSettings schema the JSON endpoints
        # are held to. A file upload carries settings as a string, so the spec
        # cannot check inside it and this route used to accept anything.
        settings = settings_service.resolve_print_settings(
            parse_and_validate_settings(request.form.get('settings')))

        # Save uploaded image temporarily.
        image_path = _save_uploaded_file(image_file)
        logger.info("Image saved for preview", path=image_path)

        # Verify the uploaded file is actually a decodable image before
        # handing it to the printer service. Image.verify() consumes the file
        # object, so it is opened in its own context.
        try:
            with Image.open(image_path) as img:
                img.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            logger.warning("Rejected non-image or invalid upload", error=str(e))
            raise ValidationError("Uploaded file is not a valid image", "image")

        image = printer_service.render_image_preview(image_path, settings)
        return _preview_response(image)
    except (ValidationError, ValueError) as e:
        logger.warning("Validation error rendering image preview", error=str(e))
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(str(e), "settings")
    except PrinterError as e:
        logger.error("Printer error rendering image preview", error=str(e), exc_info=True)
        raise
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
        raise internal_error(e, "Error rendering image preview") from e
    finally:
        # The preview never needs to keep the upload around.
        _cleanup_uploaded_file(image_path)


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
    extension = os.path.splitext(safe_name)[1]
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


def _cleanup_uploaded_file(file_path) -> None:
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
