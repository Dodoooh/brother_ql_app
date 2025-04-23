"""
Error handlers for the application.
"""

import structlog
from typing import Dict, Any, Tuple
from werkzeug.exceptions import HTTPException
from connexion import ProblemException

from utils.exceptions import (
    AppError,
    ValidationError,
    ResourceNotFoundError,
    PrinterError,
    ImageProcessingError,
    ConfigurationError
)

logger = structlog.get_logger()

def register_error_handlers(app):
    """
    Register error handlers for the application.
    
    Args:
        app: Flask application.
    """
    @app.errorhandler(ValidationError)
    def handle_validation_error(error: ValidationError) -> Tuple[Dict[str, Any], int]:
        """
        Handle validation errors.
        
        Args:
            error: ValidationError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Validation error", error=str(error), details=error.details)
        return error.to_dict(), 400
    
    @app.errorhandler(ResourceNotFoundError)
    def handle_resource_not_found_error(error: ResourceNotFoundError) -> Tuple[Dict[str, Any], int]:
        """
        Handle resource not found errors.
        
        Args:
            error: ResourceNotFoundError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Resource not found", error=str(error), details=error.details)
        return error.to_dict(), 404
    
    @app.errorhandler(PrinterError)
    def handle_printer_error(error: PrinterError) -> Tuple[Dict[str, Any], int]:
        """
        Handle printer errors.
        
        Args:
            error: PrinterError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.error("Printer error", error=str(error), details=error.details)
        return error.to_dict(), 500
    
    @app.errorhandler(ImageProcessingError)
    def handle_image_processing_error(error: ImageProcessingError) -> Tuple[Dict[str, Any], int]:
        """
        Handle image processing errors.
        
        Args:
            error: ImageProcessingError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.error("Image processing error", error=str(error), details=error.details)
        return error.to_dict(), 500
    
    @app.errorhandler(ConfigurationError)
    def handle_configuration_error(error: ConfigurationError) -> Tuple[Dict[str, Any], int]:
        """
        Handle configuration errors.
        
        Args:
            error: ConfigurationError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.error("Configuration error", error=str(error), details=error.details)
        return error.to_dict(), 500
    
    @app.errorhandler(AppError)
    def handle_app_error(error: AppError) -> Tuple[Dict[str, Any], int]:
        """
        Handle generic application errors.
        
        Args:
            error: AppError instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.error("Application error", error=str(error), details=error.details)
        return error.to_dict(), 500
    
    @app.errorhandler(ProblemException)
    def handle_connexion_problem(error: ProblemException) -> Tuple[Dict[str, Any], int]:
        """
        Handle Connexion problem exceptions.
        
        Args:
            error: ProblemException instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.warning("Connexion problem", error=str(error), status=error.status)
        return {
            "code": f"CONNEXION_{error.status}",
            "message": str(error),
            "details": error.detail if hasattr(error, 'detail') else {}
        }, error.status
    
    @app.errorhandler(HTTPException)
    def handle_http_exception(error: HTTPException) -> Tuple[Dict[str, Any], int]:
        """
        Handle HTTP exceptions.
        
        Args:
            error: HTTPException instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.warning("HTTP exception", error=str(error), code=error.code)
        return {
            "code": f"HTTP_{error.code}",
            "message": error.description,
            "details": {}
        }, error.code
    
    @app.errorhandler(Exception)
    def handle_generic_exception(error: Exception) -> Tuple[Dict[str, Any], int]:
        """
        Handle generic exceptions.
        
        Args:
            error: Exception instance.
            
        Returns:
            Tuple of error response and status code.
        """
        logger.error("Unhandled exception", error=str(error), exc_info=True)
        return {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred",
            "details": {
                "error_type": error.__class__.__name__,
                "error_message": str(error)
            }
        }, 500
