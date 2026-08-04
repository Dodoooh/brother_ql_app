"""
Controller for settings-related API endpoints.
"""

import structlog
from typing import Dict, Any
from flask import request

from src.services.settings_service import settings_service
from src.utils.exceptions import AppError, ValidationError, ConfigurationError, internal_error

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
        raise internal_error(e, "Error getting settings") from e

def update_settings() -> Dict[str, Any]:
    """
    Update settings.

    Validation is delegated entirely to the settings service (the single,
    canonical source of truth). Any ValueError raised there -- including
    printer URI / SSRF rejections -- is surfaced as an API ValidationError.

    Returns:
        Dict containing the result of the update operation.
    """
    try:
        logger.info("Updating settings")

        # Get settings from request body
        settings = request.get_json()
        if not settings:
            raise ValidationError("No settings provided", "settings")

        # Validate via the canonical service validator (raises ValueError).
        try:
            settings_service._validate_settings(settings)
        except ValueError as ve:
            logger.warning("Settings validation failed", error=str(ve))
            raise ValidationError(str(ve), "settings")

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
    except ConfigurationError:
        raise
    except ValueError as e:
        # Pure input/validation errors must map to HTTP 400, not 500.
        logger.warning("Settings validation failed", error=str(e), exc_info=True)
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
        raise internal_error(e, "Error updating settings") from e
