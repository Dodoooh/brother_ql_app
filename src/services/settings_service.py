"""
Settings service for managing application settings.
Handles loading from and saving to a JSON file with atomic writes.
"""

import os
import json
import structlog
import copy  # For deepcopy
import threading
from typing import Dict, Any, Optional
from brother_ql.backends import guess_backend

from src.utils.uri_validation import validate_printer_uri

# Attempt to import default settings, handle potential import errors during startup phases
try:
    from src.config.default_settings import (
        DEFAULT_SETTINGS,
        BLEED_LIMIT_MM,
        CALIBRATION_LIMIT_MM,
        CALIBRATION_SCALE_MAX,
        CALIBRATION_SCALE_MIN,
    )
except ImportError:
    BLEED_LIMIT_MM = 5.0
    CALIBRATION_LIMIT_MM = 10.0
    CALIBRATION_SCALE_MIN = 0.95
    CALIBRATION_SCALE_MAX = 1.05
    logger = structlog.get_logger()
    logger.error("Failed to import DEFAULT_SETTINGS. Using fallback defaults.")
    # Define fallback defaults directly if import fails
    DEFAULT_SETTINGS = {
        "printer_uri": "tcp://192.168.1.100", "printer_model": "QL-800", "label_size": "62",
        "font_size": 50, "alignment": "left", "rotate": 0, "threshold": 70.0,
        "dither": False, "compress": False, "red": False,
        "keep_alive_enabled": False, "keep_alive_interval": 60, "calibration": {},
        "bleed_mm": {},
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

        # In-memory cache with mtime-based invalidation. Guarded by a lock so
        # the keep-alive thread can safely read while the API writes.
        self._cache_lock = threading.Lock()
        self._cached_settings: Optional[Dict[str, Any]] = None
        self._cached_mtime: Optional[float] = None

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
            "font_size": (int, float), "alignment": str, "orientation": str,
            "vertical_alignment": str,
            "rotate": (int, float),
            "threshold": (int, float), "dither": bool, "compress": bool, "red": bool,
            "keep_alive_enabled": bool, "keep_alive_interval": (int, float),
            "keep_alive_mode": str, "keep_alive_duration_seconds": int,
            "ipp_port": int,
            "copies": int, "cut_mode": str, "dpi_600": bool, "hq": bool,
            "calibration": dict,
            "bleed_mm": dict,
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

        # --- Printer URI safety check (scheme allowlist + SSRF guard) ---
        # Reuses the canonical validator so that e.g. file://, lpt:// or
        # link-local/metadata hosts are rejected. Private/LAN IPs and
        # hostnames (the normal case) pass through unchanged.
        if "printer_uri" in settings_to_validate:
            validate_printer_uri(settings_to_validate["printer_uri"])

        # --- Value Checks ---
        if "alignment" in settings_to_validate and settings_to_validate["alignment"] not in ["left", "center", "right"]:
            raise ValueError(f"Invalid alignment value: {settings_to_validate['alignment']}")

        if "vertical_alignment" in settings_to_validate and settings_to_validate["vertical_alignment"] not in ["top", "middle", "bottom"]:
            raise ValueError(f"Invalid vertical_alignment value: {settings_to_validate['vertical_alignment']}. Must be top, middle, or bottom.")

        if "orientation" in settings_to_validate and settings_to_validate["orientation"] not in ["across", "lengthwise"]:
            raise ValueError(f"Invalid orientation value: {settings_to_validate['orientation']}. Must be across or lengthwise.")

        if "rotate" in settings_to_validate and settings_to_validate["rotate"] not in [0, 90, 180, 270]:
             raise ValueError(f"Invalid rotate value: {settings_to_validate['rotate']}. Must be 0, 90, 180, or 270.")

        if "threshold" in settings_to_validate and not (0 <= settings_to_validate["threshold"] <= 100):
             raise ValueError(f"Invalid threshold value: {settings_to_validate['threshold']}. Must be between 0 and 100.")

        if "copies" in settings_to_validate and not (1 <= settings_to_validate["copies"] <= 100):
             raise ValueError(f"Invalid copies value: {settings_to_validate['copies']}. Must be between 1 and 100.")

        if "cut_mode" in settings_to_validate and settings_to_validate["cut_mode"] not in ["each", "end", "none"]:
             raise ValueError(f"Invalid cut_mode value: {settings_to_validate['cut_mode']}. Must be each, end, or none.")

        if "keep_alive_mode" in settings_to_validate and settings_to_validate["keep_alive_mode"] not in ["forever", "timed"]:
             raise ValueError(f"Invalid keep_alive_mode value: {settings_to_validate['keep_alive_mode']}. Must be forever or timed.")

        if "keep_alive_duration_seconds" in settings_to_validate and settings_to_validate["keep_alive_duration_seconds"] < 0:
             raise ValueError(f"Invalid keep_alive_duration_seconds value: {settings_to_validate['keep_alive_duration_seconds']}. Must be 0 or greater.")

        if "ipp_port" in settings_to_validate and not (1 <= settings_to_validate["ipp_port"] <= 65535):
             raise ValueError(f"Invalid ipp_port value: {settings_to_validate['ipp_port']}. Must be between 1 and 65535.")

        if settings_to_validate.get("keep_alive_enabled"):
            interval = settings_to_validate.get("keep_alive_interval")
            if interval is None:
                raise ValueError("keep_alive_interval is required when keep_alive_enabled is true.")
            # Type check already done, now value check
            if interval < 10:
                raise ValueError(f"keep_alive_interval must be at least 10 seconds, got {interval}")

            # Keep-alive for non-network backends is not useful
            if guess_backend(settings_to_validate["printer_uri"]) != "network":
                raise ValueError("Keep alive is not useful for non-network backends")

        # Validate the per-label print offsets (nested, so the shape matters as
        # much as the type).
        if "calibration" in settings_to_validate: # Type check confirmed it's a dict
            self._validate_calibration(settings_to_validate["calibration"])

        # Validate the per-label bleed (a separate map, on purpose -- see
        # _validate_bleed).
        if "bleed_mm" in settings_to_validate: # Type check confirmed it's a dict
            self._validate_bleed(settings_to_validate["bleed_mm"])

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
                # Validate each printer's URI with the same allowlist/SSRF rules.
                if "printer_uri" in printer:
                    try:
                        validate_printer_uri(printer["printer_uri"])
                    except ValueError as ve:
                        raise ValueError(f"Printer at index {i}: {ve}")
        logger.debug("Settings validation passed")

    @staticmethod
    def _validate_calibration(calibration: Dict[str, Any]) -> None:
        """
        Validate the ``calibration`` map: per-label print offsets in millimetres.

        The map is keyed by label identifier and each entry is an object with
        the optional keys ``x_mm``, ``y_mm`` and ``scale``::

            {"d24": {"x_mm": -0.5, "y_mm": 1.0, "scale": 0.98}}

        ``x_mm`` positive moves the printed content right on the tape, ``y_mm``
        positive moves it down (later in the feed direction). A missing key
        means no offset, so an absent map and an empty one behave identically.

        ``scale`` multiplies the size of the printed content about the centre
        of the label (0.98 prints it 2 % smaller), correcting a printer that
        lays ink down slightly large or small.

        The ranges accepted here are the ranges a *correction* can sensibly
        take, not a promise that every printer can travel that far: sideways
        travel is bounded by the width of the print head beside the loaded
        media, and a request beyond it is clamped at print time and logged.

        Unknown keys inside an entry are rejected rather than ignored: a
        misspelt ``x_min`` would otherwise be stored, silently do nothing, and
        leave the user turning a dial that is not connected to anything. The
        label identifier itself is *not* checked against the media catalogue --
        a settings file may legitimately carry offsets for media that is not
        loaded right now, or for a label a newer brother_ql knows about.

        Args:
            calibration: The ``calibration`` map to validate.

        Raises:
            ValueError: If the map's shape, key names or values are invalid.
        """
        for label_size, offset in calibration.items():
            if not isinstance(label_size, str) or not label_size.strip():
                raise ValueError("Calibration keys must be non-empty label identifiers.")
            if not isinstance(offset, dict):
                raise ValueError(
                    f"Invalid calibration entry for '{label_size}': expected an object "
                    f"with x_mm/y_mm, got {type(offset)}"
                )
            unknown = set(offset) - {"x_mm", "y_mm", "scale"}
            if unknown:
                raise ValueError(
                    f"Invalid calibration entry for '{label_size}': unknown field(s) "
                    f"{sorted(unknown)}. Only x_mm, y_mm and scale are allowed."
                )
            for axis in ("x_mm", "y_mm"):
                if axis not in offset:
                    continue
                value = offset[axis]
                # bool is an int subclass, and "shift the label True mm" is not
                # a correction anybody meant to make.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(
                        f"Invalid calibration {axis} for '{label_size}': expected a "
                        f"number in millimetres, got {type(value)}"
                    )
                if not (-CALIBRATION_LIMIT_MM <= value <= CALIBRATION_LIMIT_MM):
                    raise ValueError(
                        f"Invalid calibration {axis} for '{label_size}': {value} mm is "
                        f"outside the supported range of "
                        f"+/-{CALIBRATION_LIMIT_MM} mm."
                    )

            if "scale" in offset:
                scale = offset["scale"]
                if isinstance(scale, bool) or not isinstance(scale, (int, float)):
                    raise ValueError(
                        f"Invalid calibration scale for '{label_size}': expected a "
                        f"multiplier such as 0.98, got {type(scale)}"
                    )
                if not (CALIBRATION_SCALE_MIN <= scale <= CALIBRATION_SCALE_MAX):
                    raise ValueError(
                        f"Invalid calibration scale for '{label_size}': {scale} is "
                        f"outside the supported range of {CALIBRATION_SCALE_MIN} to "
                        f"{CALIBRATION_SCALE_MAX}. This corrects a printer that lays "
                        f"ink down slightly large or small, not the size of the "
                        f"design -- change the design or the label size for that."
                    )

    @staticmethod
    def _validate_bleed(bleed: Dict[str, Any]) -> None:
        """
        Validate the ``bleed_mm`` map: per-label bleed in millimetres per side.

        The map is keyed by label identifier and each value is a plain number::

            {"d24": 1.5}

        Bleed lets a design run out past the printable area brother_ql
        publishes, into the strip of label around it that Brother declares
        non-printable -- 2.03 mm all round a 24 mm round die cut, which is
        exactly the ring of bare paper a user measures on a finished label. 0
        and a missing key mean the same thing: print the published area, as the
        app always has.

        The value widens the label and never lengthens it. Extending the raster
        along the feed makes the media advance further per label and walks the
        cutter off the gap between labels; the feed margin is therefore not
        something a bleed can spend, however much of it the catalogue shows.

        This is deliberately a *separate* map from ``calibration`` rather than
        another field inside it, and the separation is load-bearing:
        calibration corrects a printer that puts ink in the wrong place, so it
        applies to prints and never to previews; bleed changes how large the
        label being designed is, so it applies to previews too. Folding them
        together would force one of those two rules to give.

        The bound checked here is a sanity bound, not the real limit. What a
        given medium can actually give is half the difference between its total
        and printable *width* in dots -- and no more than the print head is wide
        -- which depends on the loaded media and the printer model, so it is
        resolved and clamped at render time and logged with the reason. A settings file may
        also legitimately carry a bleed for media that is not loaded right now,
        which is why the identifier is not checked against the catalogue.

        Args:
            bleed: The ``bleed_mm`` map to validate.

        Raises:
            ValueError: If the map's shape, keys or values are invalid.
        """
        for label_size, value in bleed.items():
            if not isinstance(label_size, str) or not label_size.strip():
                raise ValueError("Bleed keys must be non-empty label identifiers.")
            # bool is an int subclass, and "bleed by True millimetres" is not a
            # measurement anybody meant to make.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"Invalid bleed for '{label_size}': expected a number of "
                    f"millimetres per side, got {type(value)}"
                )
            if value < 0:
                raise ValueError(
                    f"Invalid bleed for '{label_size}': {value} mm is negative. "
                    f"Bleed only ever adds printable area; to print a smaller "
                    f"design, change the design."
                )
            if value > BLEED_LIMIT_MM:
                raise ValueError(
                    f"Invalid bleed for '{label_size}': {value} mm is outside the "
                    f"supported range of 0 to {BLEED_LIMIT_MM} mm. The widest "
                    f"non-printable margin on any supported medium is under 3 mm, "
                    f"so a larger value is a wrong unit rather than a request."
                )

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

            # 5. Refresh the in-memory cache so freshly written values are
            #    immediately visible without another disk read. We re-derive the
            #    cached object via _load_settings (applying default-key merging)
            #    and record the new mtime. On any hiccup we simply invalidate.
            with self._cache_lock:
                try:
                    self._cached_settings = self._load_settings()
                    self._cached_mtime = os.path.getmtime(self.settings_file)
                    logger.debug("Settings cache refreshed after save", file=self.settings_file, mtime=self._cached_mtime)
                except OSError as cache_err:
                    self._cached_settings = None
                    self._cached_mtime = None
                    logger.debug("Could not refresh settings cache after save, invalidating", error=str(cache_err))

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
        Returns the current settings, served from an in-memory cache that is
        invalidated whenever the settings file's mtime changes.

        The file is only re-read from disk when nothing is cached yet or when
        the file has been modified since the last load. A deep copy is always
        returned so callers can never mutate the cached state.

        Thread-safe: the cache is guarded by a lock so the keep-alive thread
        can read while the API writes.
        """
        with self._cache_lock:
            try:
                current_mtime = os.path.getmtime(self.settings_file)
            except OSError:
                # File missing/unreadable -> fall back to default behaviour and
                # do not cache (so a later-created file is picked up).
                logger.debug("Settings file unavailable for mtime check, loading defaults", file=self.settings_file)
                self._cached_settings = None
                self._cached_mtime = None
                return self._load_settings()

            if self._cached_settings is None or self._cached_mtime != current_mtime:
                logger.debug("Settings cache miss, loading from file", file=self.settings_file, mtime=current_mtime)
                self._cached_settings = self._load_settings()
                self._cached_mtime = current_mtime
            else:
                logger.debug("Settings cache hit", file=self.settings_file, mtime=current_mtime)

            # Return a deep copy so callers cannot mutate the cached state.
            return copy.deepcopy(self._cached_settings)

    # Settings keys a print/preview request may inherit from the saved config
    # when omitted. keep_alive_*/ipp_port/printers are excluded (not per-print).
    _INHERITABLE_PRINT_KEYS = (
        "printer_uri", "printer_model", "label_size", "font_size", "alignment",
        "orientation", "vertical_alignment", "rotate", "threshold", "dither",
        "compress", "red",
        "copies", "cut_mode", "dpi_600", "hq",
        # The print path reads the calibration offsets straight off the resolved
        # settings, so they have to be inherited like any other render option --
        # otherwise a request that omits "settings" would print uncalibrated.
        "calibration",
        # Bleed decides how big the label being rendered is, so it has to be
        # inherited for the same reason label_size is: a preview or a print that
        # omitted "settings" would otherwise be built at a different size than
        # the one the user configured, and for die-cut media convert() would
        # reject it outright.
        "bleed_mm",
    )

    def resolve_print_settings(self, request_settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Merge a request's (possibly partial or missing) ``settings`` with the
        saved configuration, so callers no longer have to repeat the printer
        config on every request.

        The saved config supplies defaults for the inheritable print keys
        (printer URI/model/label size and rendering options); any field present
        in ``request_settings`` overrides the saved value. Extra keys carried by
        the request (e.g. side-by-side layout hints) are preserved as-is.

        Args:
            request_settings: The ``settings`` object from the request, or None.

        Returns:
            A new settings dict with inherited defaults filled in.
        """
        saved = self.get_settings()
        merged: Dict[str, Any] = {}
        for key in self._INHERITABLE_PRINT_KEYS:
            if saved.get(key) is not None:
                merged[key] = saved[key]
        if request_settings:
            merged.update(request_settings)
        return merged

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
