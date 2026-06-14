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
from src.utils.exceptions import ValidationError, PrinterError, ImageProcessingError, ResourceNotFoundError

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
        settings_json = request.form.get('settings', '{}')
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError:
            raise ValidationError("Invalid settings JSON", "settings")
        
        # Validate required settings
        required_settings = ["printer_uri", "printer_model", "label_size"]
        for setting in required_settings:
            if setting not in settings:
                raise ValidationError(f"{setting} is required", f"settings.{setting}")
        
        # Save uploaded image
        image_path = _save_uploaded_file(image_file)
        logger.info("Image saved", path=image_path)

        try:
            # Verify the uploaded file is actually a decodable image before
            # handing it to the printer service. Image.verify() consumes the
            # file object, so we re-open for each step.
            try:
                with Image.open(image_path) as img:
                    img.verify()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
                logger.warning("Rejected non-image or invalid upload", error=str(e))
                raise ValidationError("Uploaded file is not a valid image", "image")

            # Print image
            result = printer_service.print_image(image_path, settings)

            return result
        finally:
            # Clean up only the upload we saved in this controller. Render
            # artifacts produced by printer_service are handled elsewhere.
            _cleanup_uploaded_file(image_path)
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

    # Get upload folder from app config
    upload_folder = current_app.config.get('UPLOAD_FOLDER')
    if not upload_folder:
        upload_folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")

    # Ensure upload folder exists
    os.makedirs(upload_folder, exist_ok=True)

    # Save the file
    file_path = os.path.join(upload_folder, filename)
    file.save(file_path)

    return file_path


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
