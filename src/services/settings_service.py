"""
Settings service for managing application settings.
Handles loading from and saving to a JSON file with atomic writes.
"""

import os
import json
import structlog
import copy  # For deepcopy
from typing import Dict, Any, Optional

# Attempt to import default settings, handle potential import errors during startup phases
try:
    from src.config.default_settings import DEFAULT_SETTINGS
except ImportError:
    logger = structlog.get_logger()
    logger.error("Failed to import DEFAULT_SETTINGS. Using fallback defaults.")
    # Define fallback defaults directly if import fails
    DEFAULT_SETTINGS = {
        "printer_uri": "tcp://192.168.1.100", "printer_model": "QL-800", "label_size": "62",
        "font_size": 50, "alignment": "left", "rotate": 0, "threshold": 70.0,
        "dither": False, "compress": False, "red": False,
        "keep_alive_enabled": False, "keep_alive_interval": 60,
        "printers": [{"id": "default", "name": "Default Printer", "printer_uri": "tcp://192.168.1.100", "printer_model": "QL-800", "label_size": "62"}]
    }

logger = structlog.get_logger()

class SettingsService:
    """
    Manages application settings, loading from and saving to a JSON file.
    Uses atomic writes for safer saving operations.
    """

    def __init__(self, settings_file: Optional[str] = None):
        """
        Initializes the settings service.

        Args:
            settings_file: Path to the settings file. Defaults to /app/data/settings.json.
        """
        if settings_file is None:
            data_dir = "/app/data"
            self.settings_file = os.path.join(data_dir, "settings.json")
        else:
            self.settings_file = settings_file

        self.settings: Dict[str, Any] = self._load_settings()
        logger.info("SettingsService initialized", initial_settings_source=self.settings_file)

    def _load_settings(self) -> Dict[str, Any]:
        """
        Loads settings from the JSON file.
        If the file doesn't exist or is invalid, returns default settings.
        """
        try:
            if os.path.exists(self.settings_file):
                logger.debug("Attempting to load settings from file", file=self.settings_file)
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded_settings = json.load(f)
                # Basic check if it's a dictionary
                if not isinstance(loaded_settings, dict):
                     raise ValueError("Loaded settings are not a dictionary.")
                logger.info("Successfully loaded settings from file", file=self.settings_file)
                # Ensure all default keys are present (add missing ones)
                # This prevents errors if new settings are added to defaults later
                updated_settings = copy.deepcopy(DEFAULT_SETTINGS)
                updated_settings.update(loaded_settings) # Overwrite defaults with loaded values
                return updated_settings
            else:
                logger.warning("Settings file not found, using default settings", file=self.settings_file)
                return copy.deepcopy(DEFAULT_SETTINGS)
        except (json.JSONDecodeError, ValueError, IOError) as e:
            logger.error("Error loading or parsing settings file, using defaults", file=self.settings_file, error=str(e), exc_info=True)
            return copy.deepcopy(DEFAULT_SETTINGS)
        except Exception as e:
            logger.error("Unexpected error loading settings, using defaults", file=self.settings_file, error=str(e), exc_info=True)
            return copy.deepcopy(DEFAULT_SETTINGS)

    def _validate_settings(self, settings_to_validate: Dict[str, Any]) -> None:
        """
        Validates the structure and values of a settings dictionary.
        Raises ValueError if validation fails.
        """
        logger.debug("Validating settings structure and values")
        # --- Type Checks First ---
        if not isinstance(settings_to_validate, dict):
             raise ValueError("Settings must be a dictionary.")

        type_checks = {
            "printer_uri": str, "printer_model": str, "label_size": str,
            "font_size": (int, float), "alignment": str, "rotate": (int, float),
            "threshold": (int, float), "dither": bool, "compress": bool, "red": bool,
            "keep_alive_enabled": bool, "keep_alive_interval": (int, float),
            "printers": list
        }
        for field, expected_type in type_checks.items():
            # Check type only if the field exists in the dictionary being validated
            if field in settings_to_validate and not isinstance(settings_to_validate[field], expected_type):
                 raise ValueError(f"Invalid type for setting '{field}': Expected {expected_type}, got {type(settings_to_validate[field])}")

        # --- Required Fields ---
        required_fields = ["printer_uri", "printer_model", "label_size"]
        for field in required_fields:
            if field not in settings_to_validate:
                raise ValueError(f"Missing required setting: {field}")
            # Ensure required fields are not empty strings
            if isinstance(settings_to_validate[field], str) and not settings_to_validate[field].strip():
                raise ValueError(f"Required setting '{field}' cannot be empty.")

        # --- Value Checks ---
        if "alignment" in settings_to_validate and settings_to_validate["alignment"] not in ["left", "center", "right"]:
            raise ValueError(f"Invalid alignment value: {settings_to_validate['alignment']}")

        if "rotate" in settings_to_validate and settings_to_validate["rotate"] not in [0, 90, 180, 270]:
             raise ValueError(f"Invalid rotate value: {settings_to_validate['rotate']}. Must be 0, 90, 180, or 270.")

        if "threshold" in settings_to_validate and not (0 <= settings_to_validate["threshold"] <= 100):
             raise ValueError(f"Invalid threshold value: {settings_to_validate['threshold']}. Must be between 0 and 100.")

        if settings_to_validate.get("keep_alive_enabled"):
             interval = settings_to_validate.get("keep_alive_interval")
             if interval is None:
                 raise ValueError("keep_alive_interval is required when keep_alive_enabled is true.")
             # Type check already done, now value check
             if interval < 10:
                 raise ValueError(f"keep_alive_interval must be at least 10 seconds, got {interval}")

        # Validate printers list structure
        if "printers" in settings_to_validate: # Type check confirmed it's a list
            if not settings_to_validate["printers"]: # Ensure printers list is not empty if present
                 raise ValueError("The 'printers' list cannot be empty if provided.")
            for i, printer in enumerate(settings_to_validate["printers"]):
                if not isinstance(printer, dict):
                    raise ValueError(f"Item at index {i} in 'printers' list must be a dictionary.")
                printer_required_fields = ["id", "printer_uri", "printer_model", "label_size"]
                for field in printer_required_fields:
                    if field not in printer:
                        raise ValueError(f"Printer at index {i} missing required field: {field}")
                    if isinstance(printer[field], str) and not printer[field].strip():
                         raise ValueError(f"Required field '{field}' in printer at index {i} cannot be empty.")
        logger.debug("Settings validation passed")


    def save_settings(self, settings_to_save: Dict[str, Any]) -> bool:
        """
        Atomically saves the provided settings dictionary to the JSON file.
        Validates the settings before attempting to save.

        Args:
            settings_to_save: The complete dictionary of settings to save.

        Returns:
            True if saving was successful, False otherwise.
        """
        temp_file_path = self.settings_file + ".tmp"
        try:
            logger.info("Attempting to save settings", file=self.settings_file)

            # 1. Validate the complete settings object before saving
            try:
                self._validate_settings(settings_to_save)
            except ValueError as ve:
                logger.error("Settings validation failed before save", error=str(ve), invalid_settings=settings_to_save)
                return False

            # 2. Ensure the target directory exists
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)

            # 3. Write to temporary file
            logger.debug("Writing settings to temporary file", temp_file=temp_file_path)
            with open(temp_file_path, 'w', encoding='utf-8') as f:
                json.dump(settings_to_save, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno()) # Force write to disk
            logger.debug("Successfully wrote and synced temporary file", temp_file=temp_file_path)

            # 4. Atomically replace the original file
            logger.debug("Attempting to replace original file with temporary file", source=temp_file_path, dest=self.settings_file)
            os.replace(temp_file_path, self.settings_file)
            logger.debug("Successfully replaced original file", file=self.settings_file)

            # 5. In-memory state update is removed. State will be re-read from file on next access.
            logger.info("Settings saved successfully to file", file=self.settings_file)
            return True

        except (IOError, OSError) as e:
            logger.error("File system error during settings save", error=str(e), temp_file=temp_file_path, final_file=self.settings_file, exc_info=True)
            # Clean up temp file if it still exists after an error
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug("Removed temporary file after error", temp_file=temp_file_path)
                except Exception as rm_err:
                    logger.error("Failed to remove temporary settings file after error", temp_file=temp_file_path, remove_error=str(rm_err))
            return False
        except Exception as e:
            logger.error("Unexpected error during save_settings", error=str(e), exc_info=True)
            # Clean up temp file if it still exists
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug("Removed temporary file after unexpected error", temp_file=temp_file_path)
                except Exception as rm_err:
                    logger.error("Failed to remove temporary settings file after unexpected error", temp_file=temp_file_path, remove_error=str(rm_err))
            return False

    def get_settings(self) -> Dict[str, Any]:
        """
        Loads and returns the current settings directly from the file.
        Ensures the freshest state is always returned.
        """
        # Always load from file to get the most current state
        logger.debug("get_settings called, loading from file", file=self.settings_file)
        # Use _load_settings which handles defaults if file is missing/invalid
        # Return a deep copy to prevent external modification if needed, though _load_settings already returns a copy
        return self._load_settings()

    def update_settings(self, settings_update: Dict[str, Any]) -> bool:
        """
        Merges partial updates with current settings and saves the result.

        Args:
            settings_update: A dictionary containing the settings keys/values to update.

        Returns:
            True if the update and save were successful, False otherwise.
        """
        try:
            if not isinstance(settings_update, dict):
                 logger.warning("Invalid settings update data type provided", data_type=type(settings_update))
                 return False

            logger.debug("Received settings update request", raw_update_data=settings_update)

            # Load the *absolute latest* settings from the file system
            current_settings_from_file = self._load_settings()
            logger.debug("Loaded current settings from file before update", loaded_settings=current_settings_from_file)

            # Create a deep copy to modify
            merged_settings = copy.deepcopy(current_settings_from_file)
            # Merge the updates
            merged_settings.update(settings_update)
            logger.debug("Merged settings prepared for saving", merged_settings=merged_settings)

            # Attempt to save the fully merged and validated settings object
            return self.save_settings(merged_settings)

        except Exception as e:
            logger.error("Error during settings update process", error=str(e), exc_info=True)
            return False

# Create a singleton instance of the service for the application to use
settings_service = SettingsService()
