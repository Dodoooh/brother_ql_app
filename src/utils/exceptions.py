"""
Custom exceptions for the Brother QL Printer App.
"""

class AppError(Exception):
    """Base exception for all application errors."""
    
    def __init__(self, message: str, code: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            code: Error code.
            details: Additional error details.
        """
        self.message = message
        self.code = code or self.__class__.__name__.upper()
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> dict:
        """
        Convert the exception to a dictionary.
        
        Returns:
            Dict representation of the exception.
        """
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details
        }


class PrinterError(AppError):
    """Exception raised for printer-related errors."""
    pass


class ImageProcessingError(AppError):
    """Exception raised for image processing errors."""
    pass


class ConfigurationError(AppError):
    """Exception raised for configuration errors."""
    pass


class ValidationError(AppError):
    """Exception raised for validation errors."""
    
    def __init__(self, message: str, field: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            field: Field that failed validation.
            details: Additional error details.
        """
        details = details or {}
        if field:
            details["field"] = field
        super().__init__(message, "VALIDATION_ERROR", details)


class ResourceNotFoundError(AppError):
    """Exception raised when a requested resource is not found."""
    
    def __init__(self, message: str, resource_type: str = None, resource_id: str = None, details: dict = None):
        """
        Initialize the exception.
        
        Args:
            message: Error message.
            resource_type: Type of resource that was not found.
            resource_id: ID of the resource that was not found.
            details: Additional error details.
        """
        details = details or {}
        if resource_type:
            details["resource_type"] = resource_type
        if resource_id:
            details["resource_id"] = resource_id
        super().__init__(message, "RESOURCE_NOT_FOUND", details)
