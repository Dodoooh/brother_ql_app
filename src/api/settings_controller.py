"""
Controller for settings-related API endpoints.
"""

import structlog
from typing import Dict, Any
from flask import request

from services.settings_service import settings_service
from utils.exceptions import ValidationError, ConfigurationError

logger = structlog.get_logger()

def get_settings() -> Dict[str, Any]:
    """
    Get current settings.
    
    Returns:
        Dict containing the current settings.
    """
    try:
        logger.info("Getting current settings")
        settings = settings_service.get_settings()
        return settings
    except Exception as e:
        logger.error("Error getting settings", error=str(e), exc_info=True)
        raise ConfigurationError(f"Error getting settings: {str(e)}")

def update_settings() -> Dict[str, Any]:
    """
    Update settings.
    
    Returns:
        Dict containing the result of the update operation.
    """
    try:
        logger.info("Updating settings")
        
        # Get settings from request body
        settings = request.get_json()
        if not settings:
            raise ValidationError("No settings provided", "settings")
        
        # Validate settings
        _validate_settings(settings)
        
        # Update settings
        success = settings_service.update_settings(settings)
        
        if success:
            logger.info("Settings updated successfully")
            return {
                "success": True,
                "message": "Settings updated successfully"
            }
        else:
            logger.error("Failed to update settings")
            raise ConfigurationError("Failed to update settings")
    except ValidationError as e:
        logger.error("Validation error", error=str(e), exc_info=True)
        raise
    except Exception as e:
        logger.error("Error updating settings", error=str(e), exc_info=True)
        raise ConfigurationError(f"Error updating settings: {str(e)}")

def _validate_settings(settings: Dict[str, Any]) -> None:
    """
    Validate settings.
    
    Args:
        settings: Dict containing the settings to validate.
        
    Raises:
        ValidationError: If settings are invalid.
    """
    # Check required fields
    required_fields = ["printer_uri", "printer_model", "label_size"]
    for field in required_fields:
        if field not in settings:
            raise ValidationError(f"Missing required setting: {field}", field)
    
    # Validate alignment
    if "alignment" in settings and settings["alignment"] not in ["left", "center", "right"]:
        raise ValidationError(f"Invalid alignment value: {settings['alignment']}", "alignment")
    
    # Validate rotate
    if "rotate" in settings:
        try:
            rotate = int(settings["rotate"])
            if rotate not in [0, 90, 180, 270]:
                raise ValidationError(f"Invalid rotate value: {rotate}", "rotate")
        except (ValueError, TypeError):
            raise ValidationError(f"Invalid rotate value: {settings['rotate']}", "rotate")
    
    # Validate threshold
    if "threshold" in settings:
        try:
            threshold = float(settings["threshold"])
            if threshold < 0 or threshold > 100:
                raise ValidationError(f"Invalid threshold value: {threshold}", "threshold")
        except (ValueError, TypeError):
            raise ValidationError(f"Invalid threshold value: {settings['threshold']}", "threshold")
    
    # Validate printers
    if "printers" in settings and isinstance(settings["printers"], list):
        for i, printer in enumerate(settings["printers"]):
            if not isinstance(printer, dict):
                raise ValidationError(f"Printer must be a dictionary", f"printers[{i}]")
            
            for field in ["id", "printer_uri", "printer_model", "label_size"]:
                if field not in printer:
                    raise ValidationError(f"Printer missing required field: {field}", f"printers[{i}].{field}")
