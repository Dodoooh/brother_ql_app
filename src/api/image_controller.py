"""
Controller for image printing API endpoints.
"""

import os
import json
import uuid
import structlog
from typing import Dict, Any
from werkzeug.datastructures import FileStorage

from flask import request, current_app
from services.printer_service import printer_service
from utils.exceptions import ValidationError, PrinterError, ImageProcessingError, ResourceNotFoundError

logger = structlog.get_logger()

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
        
        # Print image
        result = printer_service.print_image(image_path, settings)
        
        return result
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
    # Generate a unique filename
    filename = f"{uuid.uuid4().hex}_{file.filename}"
    
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
