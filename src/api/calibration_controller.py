"""
Controller for print alignment calibration API endpoints.

Calibration corrects content that lands off-centre on the paper: the die cut is
punched with a tolerance, the media wanders on the roll and the models differ in
where they start the raster. The offsets themselves are ordinary settings, read
and written through ``/settings`` like everything else; these endpoints only
produce the target the user judges them by -- printed on the real medium
(``/calibration/test-print``) or rendered on screen (``/calibration/preview``).
"""

import structlog
from typing import Any, Dict, Union

from flask import Response

from src.api.preview_controller import _preview_response
from src.config.default_settings import (
    CALIBRATION_LIMIT_MM,
    CALIBRATION_SCALE_MAX,
    CALIBRATION_SCALE_MIN,
)
from src.services.printer_service import printer_service
from src.services.queue_service import print_queue
from src.services.settings_service import settings_service
from src.utils.dry_run import is_dry_run, build_dry_run_response
from src.utils.exceptions import ValidationError, PrinterError

logger = structlog.get_logger()


def _validated_offset(offset: Any) -> Dict[str, float]:
    """
    Validate an inline ``offset`` override from a request body.

    Args:
        offset: The raw ``offset`` object:
            ``{"x_mm": float, "y_mm": float, "scale": float}``.

    Returns:
        The offset with both axes present and coerced to float, and ``scale``
        only when the caller sent one -- an absent scale means "leave the
        stored size correction alone", not "print at 100 %".

    Raises:
        ValidationError: If the shape, field names or values are invalid.
    """
    if not isinstance(offset, dict):
        raise ValidationError("offset must be an object with x_mm and y_mm", "offset")

    unknown = set(offset) - {"x_mm", "y_mm", "scale"}
    if unknown:
        raise ValidationError(
            f"offset has unknown field(s) {sorted(unknown)}; only x_mm, y_mm and "
            f"scale are allowed",
            "offset",
        )

    validated: Dict[str, float] = {}
    for axis in ("x_mm", "y_mm"):
        value = offset.get(axis, 0)
        # bool is an int subclass, and "shift by True mm" is not a correction.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"offset.{axis} must be a number in millimetres",
                                  f"offset.{axis}")
        if not (-CALIBRATION_LIMIT_MM <= value <= CALIBRATION_LIMIT_MM):
            raise ValidationError(
                f"offset.{axis} must be between -{CALIBRATION_LIMIT_MM} and "
                f"{CALIBRATION_LIMIT_MM} mm",
                f"offset.{axis}",
            )
        validated[axis] = float(value)

    if "scale" in offset:
        scale = offset["scale"]
        if isinstance(scale, bool) or not isinstance(scale, (int, float)):
            raise ValidationError("offset.scale must be a multiplier such as 0.98",
                                  "offset.scale")
        if not (CALIBRATION_SCALE_MIN <= scale <= CALIBRATION_SCALE_MAX):
            raise ValidationError(
                f"offset.scale must be between {CALIBRATION_SCALE_MIN} and "
                f"{CALIBRATION_SCALE_MAX}",
                "offset.scale",
            )
        validated["scale"] = float(scale)
    return validated


def _resolve_calibration_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the print settings for a calibration request.

    The medium may be named at the top level (``label_size``), which is how a
    calibration UI thinks -- "calibrate the 24 mm round labels" -- and takes
    precedence over ``settings.label_size``. An inline ``offset`` overrides the
    stored calibration for this request only, so a candidate value can be tried
    on paper before it is saved.

    Args:
        body: The request body.

    Returns:
        Resolved print settings with ``label_size`` and ``calibration`` set.

    Raises:
        ValidationError: If no label size is available or the offset is invalid.
    """
    settings = settings_service.resolve_print_settings(body.get("settings"))

    label_size = body.get("label_size") or settings.get("label_size")
    if not label_size:
        raise ValidationError("label_size is required", "label_size")
    settings["label_size"] = str(label_size)

    if body.get("offset") is not None:
        calibration = dict(settings.get("calibration") or {})
        stored = calibration.get(str(label_size))
        override = _validated_offset(body["offset"])
        # A size correction the caller did not mention is inherited from the
        # stored entry rather than reset. The inline override exists to try an
        # *alignment* on paper before saving it; silently printing that trial
        # at a different size than everything else would make it a trial of the
        # wrong thing. The axes keep their long-standing behaviour of
        # defaulting to 0, which is what "offset" means when only one axis is
        # named.
        if "scale" not in override and isinstance(stored, dict) and "scale" in stored:
            override["scale"] = stored["scale"]
        calibration[str(label_size)] = override
        settings["calibration"] = calibration

    return settings


def test_print_calibration(body: Dict[str, Any]) -> Dict[str, Any]:
    """
    Print a calibration target on the configured medium.

    Args:
        body: Dict with the optional ``settings``, ``label_size``, ``offset``,
            ``sweep`` and ``dry_run`` fields.

    Returns:
        Dict with the queued job id and the offsets that will be printed --
        the ones the printer can actually deliver, alongside what was requested
        and the travel the medium allows, so a client can say "that is all this
        medium allows in that direction" rather than appearing to accept a
        value and quietly doing less with it.
    """
    try:
        logger.info("Processing calibration test print request")

        settings = _resolve_calibration_settings(body)
        label_size = settings["label_size"]
        sweep = body.get("sweep")

        # Planning validates the sweep parameters and is deterministic, so the
        # response can report the offsets before the worker has printed them --
        # including how much of each one the printer can actually deliver.
        described = printer_service.describe_calibration_run(settings, sweep)
        offsets = described["offsets_mm"]

        # Dry run: render + reachability check, but do not print or enqueue.
        if is_dry_run(body.get("dry_run")):
            data_url = printer_service.render_calibration_preview(settings)
            response = build_dry_run_response(settings, data_url)
            response["would_print"]["labels"] = len(offsets)
            response["would_print"].update(described)
            return response

        def job(settings=settings, sweep=sweep):
            printer_service.print_calibration_target(settings, sweep)

        job_label = (f"Calibration sweep {label_size} ({len(offsets)} labels)"
                     if len(offsets) > 1 else f"Calibration {label_size}")
        params = {"type": "calibration", "settings": settings, "sweep": sweep}
        job_id = print_queue.submit("calibration", job_label, job, params=params)
        logger.info("Calibration print job queued",
                    job_id=job_id, label_size=label_size, labels=len(offsets))

        return {
            "success": True,
            "job_id": job_id,
            "message": "Print job queued",
            "label_size": label_size,
            **described,
        }
    except ValidationError as e:
        logger.warning("Validation error for calibration test print", error=str(e))
        raise
    except PrinterError as e:
        logger.error("Printer error for calibration test print", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        # Pure input/validation errors from the service layer must map to
        # HTTP 400, not 500.
        logger.warning("Validation error for calibration test print", error=str(e))
        raise ValidationError(str(e), "settings")
    except Exception as e:
        logger.error("Error printing calibration target", error=str(e), exc_info=True)
        raise PrinterError(f"Error printing calibration target: {str(e)}")


def preview_calibration(body: Dict[str, Any]) -> Union[Dict[str, Any], Response]:
    """
    Render the calibration target without printing it.

    Unlike every other preview this one *does* show the calibration offset: the
    target's whole subject is where the ink lands, so a picture of it without
    the shift would be a picture of the wrong thing.

    Args:
        body: Dict with the optional ``settings``, ``label_size`` and ``offset``
            fields.

    Returns:
        The rendered target as a PNG data URL, or the raw PNG when the request
        asks for ``Accept: image/png``.
    """
    try:
        logger.info("Processing calibration preview request")

        settings = _resolve_calibration_settings(body)
        image = printer_service.render_calibration_preview(settings)
        return _preview_response(image)
    except ValidationError as e:
        logger.warning("Validation error rendering calibration preview", error=str(e))
        raise
    except PrinterError as e:
        logger.error("Printer error rendering calibration preview", error=str(e), exc_info=True)
        raise
    except ValueError as e:
        logger.warning("Validation error rendering calibration preview", error=str(e))
        raise ValidationError(str(e), "settings")
    except Exception as e:
        logger.error("Error rendering calibration preview", error=str(e), exc_info=True)
        raise PrinterError(f"Error rendering calibration preview: {str(e)}")
