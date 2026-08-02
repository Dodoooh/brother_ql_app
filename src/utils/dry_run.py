"""
Dry-run support for the print endpoints.

A dry run validates a print request end-to-end (settings resolved, large-batch
guard, and — where a render is available — the actual label rendering) and
reports whether the printer is reachable, but never sends anything to the
printer. This lets a client check "would this print cleanly?" without consuming
a label, which is valuable for endless (62 mm) media and for CI.
"""

import base64
from io import BytesIO
from typing import Any, Dict, Optional

from PIL import Image

from src.services.printer_service import printer_service


def is_dry_run(value: Any) -> bool:
    """Truthy check for a ``dry_run`` flag (accepts bool or string form)."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def build_dry_run_response(settings: Dict[str, Any], data_url: Optional[str] = None) -> Dict[str, Any]:
    """Build the response for a successful dry run.

    Args:
        settings: The fully-resolved print settings for the request.
        data_url: Optional rendered-preview data URL; when given, its pixel
            dimensions are reported under ``would_print``.

    Returns:
        ``{ok, dry_run, printer_reachable, would_print: {...}}``.
    """
    width = height = None
    if data_url:
        try:
            png = base64.b64decode(data_url.split(",", 1)[1])
            with Image.open(BytesIO(png)) as im:
                width, height = im.width, im.height
        except Exception:  # noqa: BLE001 - dimensions are best-effort metadata
            pass

    reachable = False
    media = None
    try:
        status = printer_service.check_printer_status(
            settings.get("printer_uri", ""), settings.get("printer_model", ""),
            label_size=settings.get("label_size"),
        )
        # Reachability comes from its own field. It used to be read off
        # ``available``, which has since been narrowed to mean "ready to print"
        # -- an open cover would otherwise report the printer as unreachable.
        reachable = bool(status.get("reachable"))
        media = status.get("media")
    except Exception:  # noqa: BLE001 - reachability is best-effort, never fatal
        reachable = False

    response = {
        "ok": True,
        "dry_run": True,
        "printer_reachable": reachable,
        "would_print": {
            "label_size": settings.get("label_size"),
            "copies": settings.get("copies", 1),
            "width_px": width,
            "height_px": height,
        },
    }
    if media is not None:
        # A dry run is exactly where "the roll in the printer is not the roll
        # you are printing for" is worth knowing, so carry the media check.
        response["media"] = media
    return response
