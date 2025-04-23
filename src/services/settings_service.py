"""
Settings service for managing application settings.
"""

import os
import json
import structlog
from typing import Dict, Any, Optional
from pathlib import Path

from src.config.default_settings import DEFAULT_SETTINGS

logger = structlog.get_logger()

class SettingsService:
    """Service for managing application settings."""
    
    def __init__(self, settings_file: Optional[str] = None):
        """
        Initialize the settings service.
        
        Args:
            settings_file: Path to the settings file. If None, uses the default path.
        """
        if settings_file is None:
            self.settings_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config",
                "settings.json"
            )
        else:
            self.settings_file = settings_file
        
        self.settings = self._load_settings()
    
    def _load_settings(self) -> Dict[str, Any]:
        """
        Load settings from the settings file.
        
        Returns:
            Dict containing the settings.
        """
        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r') as f:
                    settings = json.load(f)
                
                # Merge with default settings to ensure all required fields exist
                merged_settings = DEFAULT_SETTINGS.copy()
                merged_settings.update(settings)
                
                logger.info("Settings loaded successfully", file=self.settings_file)
                return merged_settings
            else:
                logger.warning("Settings file not found, using defaults", file=self.settings_file)
                return DEFAULT_SETTINGS.copy()
        except Exception as e:
            logger.error("Error loading settings", error=str(e), exc_info=True)
            return DEFAULT_SETTINGS.copy()
    
    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """
        Save settings to the settings file.
        
        Args:
            settings: Dict containing the settings to save.
            
        Returns:
            True if successful, False otherwise.
        """
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            
            # Validate settings
            self._validate_settings(settings)
            
            # Save settings
            with open(self.settings_file, 'w') as f:
                json.dump(settings, f, indent=4)
            
            # Update current settings
            self.settings = settings
            
            logger.info("Settings saved successfully", file=self.settings_file)
            return True
        except Exception as e:
            logger.error("Error saving settings", error=str(e), exc_info=True)
            return False
    
    def get_settings(self) -> Dict[str, Any]:
        """
        Get the current settings.
        
        Returns:
            Dict containing the current settings.
        """
        return self.settings
    
    def update_settings(self, new_settings: Dict[str, Any]) -> bool:
        """
        Update the current settings with new values.
        
        Args:
            new_settings: Dict containing the new settings.
            
        Returns:
            True if successful, False otherwise.
        """
        try:
            # Merge with current settings
            updated_settings = self.settings.copy()
            updated_settings.update(new_settings)
            
            # Save merged settings
            return self.save_settings(updated_settings)
        except Exception as e:
            logger.error("Error updating settings", error=str(e), exc_info=True)
            return False
    
    def _validate_settings(self, settings: Dict[str, Any]) -> None:
        """
        Validate settings to ensure they contain required fields and valid values.
        
        Args:
            settings: Dict containing the settings to validate.
            
        Raises:
            ValueError: If settings are invalid.
        """
        required_fields = ["printer_uri", "printer_model", "label_size"]
        for field in required_fields:
            if field not in settings:
                raise ValueError(f"Missing required setting: {field}")
        
        # Validate specific fields
        if "alignment" in settings and settings["alignment"] not in ["left", "center", "right"]:
            raise ValueError(f"Invalid alignment value: {settings['alignment']}")
        
        if "rotate" in settings and not isinstance(settings["rotate"], (int, float)):
            raise ValueError(f"Invalid rotate value: {settings['rotate']}")
        
        if "threshold" in settings and not isinstance(settings["threshold"], (int, float)):
            raise ValueError(f"Invalid threshold value: {settings['threshold']}")
        
        # Validate keep alive settings
        if "keep_alive_enabled" in settings and not isinstance(settings["keep_alive_enabled"], bool):
            raise ValueError(f"Invalid keep_alive_enabled value: {settings['keep_alive_enabled']}")
        
        if "keep_alive_interval" in settings:
            if not isinstance(settings["keep_alive_interval"], (int, float)):
                raise ValueError(f"Invalid keep_alive_interval value: {settings['keep_alive_interval']}")
            if settings["keep_alive_interval"] < 10:
                raise ValueError(f"keep_alive_interval must be at least 10 seconds")
        
        # Validate printers if present
        if "printers" in settings and isinstance(settings["printers"], list):
            for printer in settings["printers"]:
                if not isinstance(printer, dict):
                    raise ValueError("Printer must be a dictionary")
                
                for field in ["id", "printer_uri", "printer_model", "label_size"]:
                    if field not in printer:
                        raise ValueError(f"Printer missing required field: {field}")

# Create a singleton instance
settings_service = SettingsService()
