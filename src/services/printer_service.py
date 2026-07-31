"""
Printer service for managing Brother QL printer operations.
"""

import os
import sys
import io
import base64
import math
import uuid
import structlog
import threading
import time
import socket
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from PIL import Image, ImageDraw, ImageFont, ImageOps
import qrcode
from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends import backend_factory, guess_backend

# Import pysnmp for SNMP-based printer communication
try:
    from pysnmp.hlapi import (
        SnmpEngine, CommunityData, UdpTransportTarget, 
        ContextData, ObjectType, ObjectIdentity, getCmd
    )
    SNMP_AVAILABLE = True
except ImportError:
    SNMP_AVAILABLE = False
    logger = structlog.get_logger()
    logger.warning("pysnmp not available, SNMP-based keep-alive will not work")

from src.services.settings_service import settings_service
from src.services.ipp_client import get_printer_attributes
from src.services.pdf_renderer import render_pdf, parse_page_range
from src.utils.exceptions import PrinterError, ImageProcessingError, ValidationError
from src.utils.uri_validation import validate_printer_uri

logger = structlog.get_logger()

# Printable width of 62 mm tape. Used as the fallback for label identifiers
# brother_ql does not know, which is what the app assumed unconditionally
# before, so unknown media keeps behaving as it always did.
DEFAULT_LABEL_WIDTH_PX = 696

# Auto-fit never shrinks below this; past it the text is unreadable anyway and
# clipping is the more honest outcome.
MIN_AUTO_FIT_FONT_SIZE = 8

# Upper bound for a lengthwise label, in printer dots. The QL series prints at
# 300 dpi, so this is a little under a metre of tape. Lengthwise rendering has
# no width to wrap against, which means a long enough message would silently
# unspool a good part of the roll; refusing is the cheaper failure.
MAX_LENGTHWISE_LENGTH_PX = 11811

# A round label's printable area is a circle, but the media is fed and cut
# mechanically, so the die cut never lands on exactly the same dot twice. Keep a
# sliver of the rim clear so a rounding error or a slightly misregistered cut
# takes white paper rather than the edge of a glyph.
ROUND_LABEL_MARGIN_RATIO = 0.02
MIN_ROUND_LABEL_MARGIN_PX = 2

# Slack kept between a shifted text block and the chord it is measured against.
# Moving a block towards the rim stops where the chord is exactly as wide as the
# text, and "exactly" does not survive the trip through integer chord widths and
# rounded draw coordinates: the block would be pushed one dot too far, fail its
# own fit check and shrink the font for no visible reason. Two dots of slack
# costs nothing and makes the placement land inside every time.
ROUND_BLOCK_TRAVEL_SLACK_PX = 2

# Where a text block sits on the axis perpendicular to its reading direction.
# Only media with spare room on that axis can honour it: a die-cut label has a
# fixed height, and a continuous roll rendered lengthwise has the tape width to
# play with. Continuous tape rendered across grows to exactly fit the text, so
# there is no slack to distribute and the setting is a no-op there.
VERTICAL_ALIGNMENTS = ("top", "middle", "bottom")
DEFAULT_VERTICAL_ALIGNMENT = "middle"

# Clear space kept between a text block and the edge of a fixed-size label. The
# die cut and the cutter both have a tolerance of a dot or two, so a block flush
# against the very edge comes back trimmed however exact the render was.
LABEL_EDGE_MARGIN_PX = 10


class LabelGeometry(tuple):
    """The printable geometry of one label, as a tuple with named access.

    This *is* the historical ``(width, height, is_die_cut)`` 3-tuple -- it
    compares and unpacks exactly like one, so existing callers are unaffected --
    extended with the ``is_round`` flag. Round and rectangular die-cut media are
    both a fixed size, but only the round ones have a *circular* printable area:
    content laid out to the full square corners falls outside the die cut and is
    simply not on the label the user peels off.

    Attributes:
        width: Printable width in pixels.
        height: Printable height in pixels, 0 for continuous tape.
        is_die_cut: Whether the label has a fixed physical size.
        is_round: Whether the printable area is a circle of diameter ``width``.
    """

    def __new__(cls, width: int, height: int, is_die_cut: bool, is_round: bool = False):
        geometry = tuple.__new__(cls, (int(width), int(height), bool(is_die_cut)))
        geometry.is_round = bool(is_round)
        return geometry

    @property
    def width(self) -> int:
        return self[0]

    @property
    def height(self) -> int:
        return self[1]

    @property
    def is_die_cut(self) -> bool:
        return self[2]


def get_label_geometry(label_size: Optional[str]) -> LabelGeometry:
    """Return ``(printable_width_px, printable_height_px, is_die_cut)`` for a roll.

    brother_ql already knows the true printable area of every supported label,
    so look it up rather than assuming one. Continuous ("endless") rolls report
    a height of 0: their length is unbounded, so a label may grow downwards.
    Die-cut labels are a fixed physical size that the content has to fit inside
    -- ``convert()`` rejects any other height outright.

    Args:
        label_size: Label identifier, e.g. "62", "50", "62x29" or "d24".

    Returns:
        A :class:`LabelGeometry`: the printable width in pixels, the printable
        height in pixels (0 for continuous tape) and whether the label is
        die-cut, plus an ``is_round`` attribute for round die-cut media.
    """
    if label_size:
        try:
            from brother_ql.labels import ALL_LABELS, FormFactor

            for label in ALL_LABELS:
                if label.identifier == str(label_size):
                    is_round = label.form_factor == FormFactor.ROUND_DIE_CUT
                    die_cut = is_round or label.form_factor == FormFactor.DIE_CUT
                    width, height = label.dots_printable
                    return LabelGeometry(width, height, die_cut, is_round)
        except Exception:
            logger.warning(
                "Could not resolve label geometry, falling back to 62 mm",
                label_size=label_size,
                exc_info=True,
            )
    return LabelGeometry(DEFAULT_LABEL_WIDTH_PX, 0, False, False)


def get_label_width(label_size: Optional[str]) -> int:
    """Return the printable width in pixels for a label identifier."""
    return get_label_geometry(label_size)[0]


def get_round_safe_radius(diameter: int) -> float:
    """Return the radius content may occupy on a round label of ``diameter``.

    Args:
        diameter: The label's printable width in pixels (a round label's
            printable area is square, so width and height are its diameter).

    Returns:
        The radius in pixels, shrunk by the die-cut registration margin.
    """
    margin = max(MIN_ROUND_LABEL_MARGIN_PX, int(round(diameter * ROUND_LABEL_MARGIN_RATIO)))
    return max(1.0, diameter / 2.0 - margin)


def get_vertical_alignment(settings: Dict[str, Any]) -> str:
    """Return the requested vertical alignment, defaulting to ``middle``.

    An unrecognised value falls back to the default rather than raising: this is
    a layout hint, and a typo in it is not worth refusing a print over -- the
    same treatment ``orientation`` gets.

    Args:
        settings: Print settings, possibly carrying ``vertical_alignment``.

    Returns:
        One of ``top``, ``middle`` or ``bottom``.
    """
    value = str(settings.get("vertical_alignment", DEFAULT_VERTICAL_ALIGNMENT)).lower()
    return value if value in VERTICAL_ALIGNMENTS else DEFAULT_VERTICAL_ALIGNMENT


def get_vertical_offset(available_height: int, block_height: int,
                        vertical_alignment: str = DEFAULT_VERTICAL_ALIGNMENT,
                        margin: int = LABEL_EDGE_MARGIN_PX) -> int:
    """Return the y of a text block's first line inside ``available_height``.

    The margin is respected in every direction: a block pinned flush against the
    edge of a die-cut label is the part the cut tolerance eats, so ``top`` means
    "as high as the label safely allows", not "at pixel zero". When the block is
    taller than the space it has, the margin wins and the overflow is clipped
    symmetrically, exactly as a centred block always was.

    Args:
        available_height: Height of the canvas the block is placed on.
        block_height: Total height of the stack of lines.
        vertical_alignment: One of ``top``, ``middle`` or ``bottom``.
        margin: Clear space to keep against the top and bottom edges.

    Returns:
        The y coordinate the first line is drawn at.
    """
    if vertical_alignment == "top":
        return margin
    if vertical_alignment == "bottom":
        return max(margin, available_height - block_height - margin)
    return max(margin, (available_height - block_height) // 2)


def get_round_block_top(radius: float, block_height: float, block_width: float,
                        vertical_alignment: str = DEFAULT_VERTICAL_ALIGNMENT) -> float:
    """Return the top of a text block, measured from a round label's centre.

    Moving a block up or down a circle costs width, because the chord narrows
    towards the rim. So the travel is bounded by the block's own width: a block
    ``block_width`` wide only has the circle behind it while it stays within
    ``sqrt(radius^2 - (block_width / 2)^2)`` of the centre line. Pushing past
    that would print the ends of the lines onto the backing paper, which is the
    whole failure mode round media exists to avoid.

    A block that already fills that span has nothing left to shift, so it stays
    centred -- and if it overflows, centring is what keeps the overflow
    symmetric instead of dumping all of it on one edge.

    Args:
        radius: Usable radius in pixels (see :func:`get_round_safe_radius`).
        block_height: Total height of the stack of lines, in pixels.
        block_width: Width of the widest line in the stack, in pixels.
        vertical_alignment: One of ``top``, ``middle`` or ``bottom``.

    Returns:
        The block's top edge as an offset from the label's centre (negative is
        above the centre).
    """
    centred = -block_height / 2.0
    if vertical_alignment not in ("top", "bottom"):
        return centred
    needed = block_width + ROUND_BLOCK_TRAVEL_SLACK_PX
    half_span = math.sqrt(max(0.0, radius * radius - (needed / 2.0) ** 2))
    if block_height >= 2 * half_span:
        return centred
    return -half_span if vertical_alignment == "top" else half_span - block_height


def get_round_line_widths(radius: float, line_count: int, line_height: int,
                          block_top: Optional[float] = None) -> List[int]:
    """Return the usable width of each line of a stack on a round label.

    A circle is only as wide as its chord at a given height, so a stack of lines
    gets a different budget per line: the lines nearest the centre may use
    nearly the full diameter while those towards the rim are pinched. This is
    what keeps a single centred line big -- an inscribed square would hand it
    only ``diameter / sqrt(2)`` (about 70 %) whatever the line height.

    Because the budget depends on *where* the stack sits, the caller has to pass
    the same ``block_top`` it will draw at (see :func:`get_round_block_top`).
    Computing centred chords and then drawing the text somewhere else is how ink
    ends up outside the die cut.

    Args:
        radius: Usable radius in pixels (see :func:`get_round_safe_radius`).
        line_count: Number of lines in the stack.
        line_height: Height of one line box in pixels.
        block_top: Top of the stack as an offset from the label's centre.
            Defaults to a stack centred on the label.

    Returns:
        One width per line, top to bottom. A line whose box falls entirely
        outside the circle gets 0.
    """
    widths: List[int] = []
    # Vertical offsets are measured from the centre of the label.
    if block_top is None:
        block_top = -(line_count * line_height) / 2.0
    for index in range(line_count):
        top = block_top + index * line_height
        bottom = top + line_height
        # The chord narrows towards the rim, so a line only fits if its *worst*
        # edge fits -- the one furthest from the centre line.
        offset = max(abs(top), abs(bottom))
        if offset >= radius:
            widths.append(0)
        else:
            widths.append(int(2 * math.sqrt(radius * radius - offset * offset)))
    return widths


class _CutAtEndRaster(BrotherQLRaster):
    """BrotherQLRaster that forces "auto-cut every N labels".

    brother_ql's ``convert(cut=True)`` always calls ``add_cut_every(1)`` (cut
    after every label). To cut only once at the end of a multi-label job we
    override ``add_cut_every`` so it uses a fixed N (the total label count),
    making the printer cut a single time after the last label.
    """

    def __init__(self, model, cut_every_n):
        super().__init__(model)
        self._cut_every_n = max(1, int(cut_every_n))

    def add_cut_every(self, n=1):
        return super().add_cut_every(self._cut_every_n)


class PrinterService:
    """Service for managing Brother QL printer operations."""
    
    def __init__(self, upload_folder: Optional[str] = None):
        """
        Initialize the printer service.
        
        Args:
            upload_folder: Path to the upload folder. If None, uses the default path.
        """
        # Keep alive thread
        self.keep_alive_thread = None
        self.keep_alive_stop_event = threading.Event()
        # Serializes raw access to the printer's port 9100 so the keep-alive
        # heartbeat never collides with an in-progress print job (the printer
        # accepts only one 9100 connection at a time).
        self._io_lock = threading.Lock()
        # Timestamp of the last print attempt. The "timed" keep-alive mode keeps
        # the printer awake for a configurable window after this moment, then
        # pauses until the next print. Initialised to now so enabling keep-alive
        # gives one window straight away.
        self._last_print_at = time.time()
        # Single source of truth for the upload folder. Precedence:
        #   1. explicit constructor argument
        #   2. UPLOAD_FOLDER environment variable (lets operators relocate or
        #      persist the otherwise-ephemeral scratch directory)
        #   3. the historical code-relative default (unchanged behaviour when no
        #      env var is set, so there is no regression).
        self.upload_folder = (
            upload_folder
            or os.environ.get("UPLOAD_FOLDER")
            or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "uploads",
            )
        )
        
        # Ensure upload folder exists
        os.makedirs(self.upload_folder, exist_ok=True)
        
        # Font path for text rendering
        self.font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not os.path.exists(self.font_path):
            # Try to find a suitable font on the system
            try:
                import matplotlib.font_manager as fm
                self.font_path = fm.findfont(fm.FontProperties(family='DejaVu Sans'))
                logger.info("Using font", font_path=self.font_path)
            except ImportError:
                logger.warning("Matplotlib not available, using default font")
                self.font_path = None
    
    def _cleanup_temp_files(self, paths: List[str]) -> None:
        """
        Remove intermediate render/resize artifacts produced by this service
        (e.g. ``text_label_*``, ``resized_*``, ``rotated_*``, ``qrcode_*``).

        This plugs a disk leak: those PNGs accumulated in the uploads folder
        forever. We only ever pass paths generated *internally* here -- the
        original uploaded image is never included, so we don't double-delete a
        file the image controller may own.

        Failures are logged and swallowed so cleanup can never break a print.
        """
        for path in paths:
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug("Removed temporary print artifact", path=path)
            except OSError as e:
                logger.warning("Failed to remove temporary print artifact",
                               path=path, error=str(e))

    def get_printers(self) -> List[Dict[str, Any]]:
        """
        Get list of configured printers.
        
        Returns:
            List of printer configurations.
        """
        settings = settings_service.get_settings()
        return settings.get("printers", [])
    
    def check_printer_status(self, printer_uri: str, printer_model: str) -> Dict[str, Any]:
        """
        Check if a printer is available and ready.
        
        Args:
            printer_uri: URI of the printer to check.
            printer_model: Model of the printer.
            
        Returns:
            Dict containing status information.
            
        Raises:
            PrinterError: If there's an error checking the printer status.
        """
        # Defense in depth: never probe an unvetted URI. This guards against
        # SSRF (e.g. tcp://169.254.169.254) and disallowed schemes even if a
        # bad value somehow bypassed settings validation.
        try:
            validate_printer_uri(printer_uri)
        except ValueError as ve:
            logger.warning("Rejected printer URI before status check",
                           printer_uri=printer_uri, error=str(ve))
            return {
                "available": False,
                "status": f"Invalid printer URI: {str(ve)}",
                "details": {
                    "printer_uri": printer_uri,
                    "printer_model": printer_model,
                    "error": str(ve),
                },
            }

        backend_type = guess_backend(printer_uri)
        details: Dict[str, Any] = {
            "printer_uri": printer_uri,
            "printer_model": printer_model,
            "backend": backend_type,
        }

        # Network printers: query the real device state via IPP (TCP 631).
        # This works on Brother QL models where SNMP is disabled and the raw
        # 9100 port offers no status read-back. A plain TCP connect is used as
        # a reachability fallback when IPP does not answer.
        if backend_type == "network":
            ip_address = self._extract_ip_from_uri(printer_uri)
            ipp = get_printer_attributes(ip_address, port=self._get_ipp_port())
            if ipp.get("reachable"):
                details.update({
                    "printer_state": ipp.get("printer_state"),
                    "printer_state_reasons": ipp.get("printer_state_reasons"),
                    "reported_model": ipp.get("make_and_model"),
                    "source": "ipp",
                    "clock": self._build_clock_info(ipp.get("current_time")),
                })
                state = ipp.get("printer_state") or "unknown"
                return {
                    "available": True,
                    "status": f"Printer is {state}",
                    "details": details,
                }
            if self._tcp_reachable(ip_address):
                details["source"] = "tcp"
                return {
                    "available": True,
                    "status": "Printer reachable (no IPP status)",
                    "details": details,
                }
            details["source"] = "tcp"
            if ipp.get("error"):
                details["error"] = ipp["error"]
            return {
                "available": False,
                "status": "Printer not reachable",
                "details": details,
            }

        # Non-network backends (usb://, file://): constructing the backend is
        # the available reachability check.
        try:
            backend = backend_factory(backend_type)["backend_class"](printer_uri)
            backend.dispose()
            return {
                "available": True,
                "status": "Printer is ready",
                "details": details,
            }
        except Exception as e:
            logger.error("Error checking printer status",
                        printer_uri=printer_uri,
                        printer_model=printer_model,
                        error=str(e),
                        exc_info=True)
            details["error"] = str(e)
            return {
                "available": False,
                "status": f"Printer error: {str(e)}",
                "details": details,
            }
    
    def print_text(self, text: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print text on a label.
        
        Args:
            text: Text to print (can include HTML formatting).
            settings: Dict containing print settings.
            
        Returns:
            Dict containing the result of the print operation.
            
        Raises:
            PrinterError: If there's an error printing the text.
            ValueError: If settings are invalid.
        """
        # Track intermediate artifacts so they can be cleaned up afterwards.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"text_{uuid.uuid4().hex[:8]}"

            logger.info("Processing text print request", job_id=job_id, text_length=len(text))

            # Create label image
            image_path = self._create_text_label(text, settings)
            temp_files.append(image_path)
            logger.info("Text label created", job_id=job_id, image_path=image_path)

            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Text printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for text print", error=str(e))
            raise ValidationError(f"Error printing text: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing text", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing text: {str(e)}") from e
        finally:
            # All tracked files are generated by this service (no original upload).
            self._cleanup_temp_files(temp_files)
    
    def print_image(self, image_path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print an image on a label.
        
        Args:
            image_path: Path to the image file.
            settings: Dict containing print settings.
            
        Returns:
            Dict containing the result of the print operation.
            
        Raises:
            PrinterError: If there's an error printing the image.
            ImageProcessingError: If there's an error processing the image.
            ValueError: If settings are invalid.
        """
        # Track only the artifacts generated *here* (resized_/rotated_). The
        # original uploaded ``image_path`` is intentionally NOT tracked so we
        # don't double-delete a file owned by the image controller.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"image_{uuid.uuid4().hex[:8]}"

            logger.info("Processing image print request", job_id=job_id, image_path=image_path)

            # Rotate before resizing. Resizing fits the image to the label
            # width, so rotating afterwards swaps the axes and leaves the image
            # narrower than the label -- convert() then scales it back up to
            # fill the tape, which visually undoes the rotation. Rotating first
            # means the resize fits whichever edge really runs across the tape.
            rotate = settings.get("rotate", 0)
            source_path = image_path
            if rotate != 0:
                source_path = self._apply_rotation(image_path, rotate)
                temp_files.append(source_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Resize image to fit label width
            resized_path = self._resize_image(source_path, settings.get("label_size"))
            temp_files.append(resized_path)
            logger.info("Image resized", job_id=job_id, resized_path=resized_path)

            # Send to printer
            self._send_to_printer(resized_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Image printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for image print", error=str(e))
            raise ValidationError(f"Error printing image: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing image", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing image: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def print_text_image(self, image_path: str, text: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print a label with an uploaded image and a text block side by side.

        Mirrors the text+QR layout but uses an uploaded image instead of a
        generated QR code. The image is placed on ``settings['image_position']``
        ("left" or "right") and the text block on the opposite side. The
        composed label then runs through the standard rotation/convert/send
        pipeline so it honours all standard settings (label_size, rotate,
        threshold, dither, red, copies, cut_mode).

        Args:
            image_path: Path to the uploaded image file.
            text: Text content to render alongside the image.
            settings: Dict containing layout and print settings. Recognised
                layout keys: ``image_position`` ("left"/"right", default
                "right"), ``text_alignment`` ("left"/"center"/"right", default
                "left") and ``text_font_size`` (default 30).

        Returns:
            Dict containing the result of the print operation.

        Raises:
            PrinterError: If there's an error printing the label.
            ImageProcessingError: If there's an error composing the label.
            ValueError: If settings are invalid.
        """
        # Track only the artifacts generated *here* (composed label and its
        # rotated derivative). The original uploaded ``image_path`` is owned by
        # the controller and intentionally not tracked.
        temp_files: List[str] = []
        try:
            job_id = f"textimage_{uuid.uuid4().hex[:8]}"

            logger.info("Processing text+image print request", job_id=job_id, image_path=image_path)

            # Compose the side-by-side label (image + text).
            composed_path = self._create_text_image_label(image_path, text, settings)
            temp_files.append(composed_path)
            logger.info("Text+image label created", job_id=job_id, image_path=composed_path)

            # Apply rotation if specified.
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                composed_path = self._apply_rotation(composed_path, rotate)
                temp_files.append(composed_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer.
            self._send_to_printer(composed_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Text+image label printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for text+image print", error=str(e))
            raise ValidationError(f"Error printing text+image label: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing text+image label", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing text+image label: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def _create_text_image_label(self, image_path: str, text: str, settings: Dict[str, Any]) -> str:
        """
        Compose an uploaded image and a multi-line text block side by side.

        Lays the image and text out on a 696px-wide label (image 1/3, text 2/3),
        with the image on ``image_position`` ("left"/"right") and the text on the
        opposite side. The uploaded image is scaled to the image area while
        preserving its aspect ratio. The result is saved as a PNG.

        Args:
            image_path: Path to the uploaded image file.
            text: Text content to render alongside the image.
            settings: Dict containing layout settings.

        Returns:
            Path to the created label image file.

        Raises:
            ImageProcessingError: If there's an error composing the label.
        """
        try:
            text_alignment = settings.get("text_alignment", "left")
            image_position = settings.get("image_position", "right")
            text_font_size = int(settings.get("text_font_size", settings.get("font_size", 30)))
            font = ImageFont.truetype(self.font_path, text_font_size)

            # Layout geometry: the roll's printable width, image 1/3, text 2/3.
            # The 20 px padding is kept for anything from 24 mm up, but scales
            # down on narrow media: three gutters of 20 px are wider than a
            # 12 mm roll's printable area, which left the image column with a
            # negative width and crashed the resize.
            width = get_label_width(settings.get("label_size"))
            padding = min(20, max(2, width // 20))
            image_area_width = max(1, int(width * 1 / 3) - padding * 2)
            text_area_width = max(1, width - image_area_width - padding * 3)

            # Load the uploaded image, flattening transparency onto white, and
            # scale it into the image area while preserving aspect ratio.
            with Image.open(image_path) as src:
                src.load()
                if src.mode in ("RGBA", "LA", "P"):
                    src = src.convert("RGBA")
                    background = Image.new("RGB", src.size, "white")
                    background.paste(src, mask=src.split()[-1])
                    user_img = background
                else:
                    user_img = src.convert("RGB")

                img_w, img_h = user_img.size
                scale = image_area_width / img_w
                scaled_size = (image_area_width, max(1, int(img_h * scale)))
                user_img = user_img.resize(scaled_size, Image.Resampling.LANCZOS)

            img_w, img_h = user_img.size

            # Parse text into lines and measure each.
            text_lines = text.split("\n")
            line_spacing = 10
            text_metrics = []
            total_text_height = 0

            dummy_img = Image.new("RGB", (width, 10), "white")
            dummy_draw = ImageDraw.Draw(dummy_img)
            for line in text_lines:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                text_metrics.append((line, line_width, line_height))
                total_text_height += line_height + line_spacing
            total_text_height -= line_spacing  # No trailing spacing.

            # Combined canvas: tall enough for the larger of image/text.
            total_height = max(img_h, total_text_height) + padding * 2
            new_img = Image.new("RGB", (width, total_height), "white")

            # Determine horizontal positions based on image_position.
            if image_position == "left":
                img_x = padding
                text_area_x = image_area_width + padding * 2
            else:  # image on the right (default)
                img_x = text_area_width + padding * 2
                text_area_x = padding

            # Paste image, vertically centered.
            img_y = (total_height - img_h) // 2
            new_img.paste(user_img, (img_x, img_y))

            # Draw text, vertically centered, honouring alignment.
            draw = ImageDraw.Draw(new_img)
            text_y = (total_height - total_text_height) // 2
            for line, line_width, line_height in text_metrics:
                if text_alignment == "center":
                    text_x = text_area_x + (text_area_width - line_width) // 2
                elif text_alignment == "right":
                    text_x = text_area_x + text_area_width - line_width
                else:  # left alignment (default)
                    text_x = text_area_x
                draw.text((text_x, text_y), line, font=font, fill="black")
                text_y += line_height + line_spacing

            # The composition is built at the roll's printable width and grows
            # downwards, which only a continuous roll allows. Fit it to the
            # medium so a die-cut label gets its exact canvas (and a round one
            # keeps the block inside the circle) instead of a "Bad image
            # dimensions" rejection.
            new_img = self._fit_to_label(new_img, settings.get("label_size"))

            label_path = os.path.join(self.upload_folder, f"text_image_{uuid.uuid4().hex[:8]}.png")
            new_img.save(label_path)

            return label_path
        except Exception as e:
            logger.error("Error creating text+image label", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating text+image label: {str(e)}")

    def print_pdf(self, pdf_path: str, settings: Dict[str, Any],
                  pages=None, scale_mode: str = "fit") -> Dict[str, Any]:
        """
        Render a PDF and print each selected page on its own label.

        Each page is rasterised to a PIL image (300 DPI by default, 600 DPI when
        ``settings['dpi_600']`` is set), saved as a temporary grayscale PNG and
        then fed through the *existing* image print pipeline
        (``_resize_image`` -> optional rotation -> ``_send_to_printer``). As a
        result every page automatically inherits the standard print settings:
        copies, cut_mode, dpi_600, red, rotate, threshold and dither.

        Args:
            pdf_path: Path to the PDF file to print.
            settings: Dict of print settings (same shape as ``print_image``).
            pages: 1-based page-range spec (e.g. ``"1-3,5"``); empty/``None``/
                ``"all"`` prints every page. Validated via ``parse_page_range``.
            scale_mode: ``"fit"`` (default) or ``"fill"``. NOTE: scaling is
                currently delegated entirely to ``_resize_image`` (fit-to-width),
                so ``"fill"`` is accepted and validated but behaves like
                ``"fit"`` for now -- it is a pass-through parameter until a
                die-cut-aware fill path is implemented.

        Returns:
            Dict with ``success``, ``job_id``, ``message`` and ``pages_printed``.

        Raises:
            ValidationError: For invalid ``scale_mode`` / page spec (-> 400).
            PrinterError: For render/IO/printer failures (-> 500).
        """
        # Only artifacts generated *here* (temporary PNGs and their
        # resized_/rotated_ derivatives) are tracked for cleanup. The original
        # ``pdf_path`` is owned by the caller and intentionally not deleted.
        temp_files: List[str] = []
        job_id = f"pdf_{uuid.uuid4().hex[:8]}"
        try:
            # --- Input/validation phase (-> ValidationError -> 400) ---
            try:
                if scale_mode not in ("fit", "fill"):
                    raise ValueError("scale_mode must be one of: fit, fill")

                dpi = 600 if settings.get("dpi_600") else 300

                # Render the requested pages. parse_page_range (invoked inside
                # render_pdf) raises ValueError for a bad page spec; an unreadable
                # / non-PDF file also raises ValueError -- both are caller input
                # problems and surface as a 400.
                images = render_pdf(pdf_path, pages, dpi=dpi)
            except ValueError as e:
                logger.warning("Invalid input for PDF print", job_id=job_id, error=str(e))
                raise ValidationError(f"Error printing PDF: {str(e)}") from e

            logger.info("Processing PDF print request",
                        job_id=job_id,
                        pdf_path=pdf_path,
                        pages=pages,
                        dpi=dpi,
                        scale_mode=scale_mode,
                        page_count=len(images))

            # --- Render/IO/printer phase (-> PrinterError -> 500) ---
            rotate = settings.get("rotate", 0)
            pages_printed = 0
            for index, page_image in enumerate(images, start=1):
                # Persist each rendered page as a grayscale PNG so it can flow
                # through the standard image pipeline.
                temp_png = os.path.join(
                    self.upload_folder, f"pdf_page_{uuid.uuid4().hex[:8]}.png"
                )
                page_image.convert("L").save(temp_png)
                temp_files.append(temp_png)

                # Rotate first, then fit: resizing to the label width before
                # rotating leaves the page narrower than the tape and convert()
                # scales it back up, undoing the rotation.
                page_source = temp_png
                if rotate != 0:
                    page_source = self._apply_rotation(temp_png, rotate)
                    temp_files.append(page_source)

                # Fit the page to the label width (same path as image printing).
                resized_path = self._resize_image(page_source, settings.get("label_size"))
                temp_files.append(resized_path)

                # Send to the printer (inherits copies/cut_mode/dpi/red/etc.).
                self._send_to_printer(resized_path, settings)
                pages_printed += 1
                logger.info("PDF page sent to printer",
                            job_id=job_id, page=index, total=len(images))

            logger.info("PDF print job completed successfully",
                        job_id=job_id, pages_printed=pages_printed)

            return {
                "success": True,
                "job_id": job_id,
                "message": f"PDF printed ({pages_printed} page(s))",
                "pages_printed": pages_printed,
            }
        except ValidationError:
            # Already classified as a client (400) error above.
            raise
        except (ValueError, ImageProcessingError) as e:
            # Render/processing problems surfacing here are real failures.
            logger.error("Error printing PDF", job_id=job_id, error=str(e), exc_info=True)
            raise PrinterError(f"Error printing PDF: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing PDF", job_id=job_id, error=str(e), exc_info=True)
            raise PrinterError(f"Error printing PDF: {str(e)}") from e
        finally:
            # All tracked files are generated by this service (never the original PDF).
            self._cleanup_temp_files(temp_files)

    # ------------------------------------------------------------------ #
    # Server-side "render-only" previews.
    #
    # These run the *exact* same render pipeline as printing (same fonts,
    # layout, rotation and 1-bit black/white conversion) but return the
    # rendered label as a base64 PNG data URL instead of sending it to the
    # printer. They never call ``_send_to_printer``.
    # ------------------------------------------------------------------ #

    def _to_print_appearance(self, img: "Image.Image", settings: Dict[str, Any]) -> "Image.Image":
        """
        Transform a finished label image into how it will actually look when
        printed: a 1-bit black/white rendering.

        This mirrors what ``brother_ql.conversion.convert`` does internally so
        the preview is truthful:

        * Convert to grayscale ("L") first.
        * If ``dither`` is set, use Floyd-Steinberg dithering via
          ``img.convert("1")`` (Pillow's default error-diffusion).
        * Otherwise apply a hard threshold. convert maps the 0-100 ``threshold``
          setting to a 0-255 pixel cutoff as ``(100 - threshold) * 255 / 100``;
          we replicate that exactly so the preview matches the print.

        The result is returned as RGB for a clean, broadly-compatible PNG.
        """
        # Grayscale baseline (same starting point as convert()).
        gray = img.convert("L")

        if settings.get("dither"):
            # Floyd-Steinberg error diffusion (Pillow default for mode "1").
            bw = gray.convert("1")
        else:
            # Replicate convert()'s threshold mapping: a 0-100 setting becomes a
            # 0-255 cutoff via (100 - threshold) * 255 / 100. Pixels at or above
            # the cutoff stay white, below it become black.
            try:
                threshold = float(settings.get("threshold", 70))
            except (TypeError, ValueError):
                threshold = 70.0
            cutoff = (100.0 - threshold) * 255.0 / 100.0
            bw = gray.point(lambda p, c=cutoff: 255 if p >= c else 0, mode="1")

        return bw.convert("RGB")

    def _encode_png_data_url(self, img: "Image.Image") -> str:
        """Encode a PIL image as a ``data:image/png;base64,...`` data URL."""
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def render_text_preview(self, text: str, settings: Dict[str, Any]) -> str:
        """
        Render a text label exactly as it would be printed and return it as a
        base64 PNG data URL (no printing).

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_text_{uuid.uuid4().hex[:8]}"
            logger.info("Rendering text preview", job_id=job_id, text_length=len(text))

            image_path = self._create_text_label(text, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Text preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for text preview", error=str(e))
            raise ValidationError(f"Error rendering text preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering text preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering text preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_qrcode_preview(self, settings: Dict[str, Any]) -> str:
        """
        Render a QR code label exactly as it would be printed and return it as a
        base64 PNG data URL (no printing).

        The QR ``data`` is taken from ``settings`` (the controller injects it,
        same as the print path which derives text from data when absent).

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_qrcode_{uuid.uuid4().hex[:8]}"
            data = settings.get("data", "")
            logger.info("Rendering QR code preview", job_id=job_id, data_length=len(data))

            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("QR code preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for QR code preview", error=str(e))
            raise ValidationError(f"Error rendering QR code preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering QR code preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering QR code preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_label_preview(self, settings: Dict[str, Any]) -> str:
        """
        Render a combined text + QR code label exactly as it would be printed and
        return it as a base64 PNG data URL (no printing).

        Uses the same side-by-side composition path as the combined label print
        (``_create_qr_code`` with side-by-side settings). The QR ``data`` is
        taken from ``settings``.

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_label_{uuid.uuid4().hex[:8]}"
            data = settings.get("data", "")
            logger.info("Rendering label preview", job_id=job_id, data_length=len(data))

            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Label preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for label preview", error=str(e))
            raise ValidationError(f"Error rendering label preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering label preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering label preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_image_preview(self, image_path: str, settings: Dict[str, Any]) -> str:
        """
        Render an uploaded image exactly as it would be printed (resized to the
        label width, optional rotation, 1-bit black/white) and return it as a
        base64 PNG data URL (no printing).

        The original ``image_path`` is owned by the caller and is NOT cleaned up
        here -- only the resized/rotated derivatives generated internally are.

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_image_{uuid.uuid4().hex[:8]}"
            logger.info("Rendering image preview", job_id=job_id, image_path=image_path)

            # Same order as printing (rotate, then fit) so the preview matches.
            rotate = settings.get("rotate", 0)
            source_path = image_path
            if rotate != 0:
                source_path = self._apply_rotation(image_path, rotate)
                temp_files.append(source_path)

            resized_path = self._resize_image(source_path, settings.get("label_size"))
            temp_files.append(resized_path)

            with Image.open(resized_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Image preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for image preview", error=str(e))
            raise ValidationError(f"Error rendering image preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering image preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering image preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    @staticmethod
    def _wrap_text_to_width(text: str, font: "ImageFont.FreeTypeFont", max_width: int) -> List[str]:
        """Word-wrap ``text`` so each line fits within ``max_width`` pixels.

        Splits on existing newlines (blank lines preserved for visual spacing),
        wraps at word boundaries, and hard-breaks any single word that is itself
        wider than ``max_width``. Returns the list of lines to render. The app
        owns the font/metrics, so this produces an exact fit the client cannot.
        """
        if max_width <= 0:
            return text.split("\n")
        lines: List[str] = []
        for paragraph in text.split("\n"):
            if paragraph == "":
                lines.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                candidate = word if not current else current + " " + word
                if font.getlength(candidate) <= max_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    current = ""
                if font.getlength(word) <= max_width:
                    current = word
                else:
                    # Hard-break a word wider than the whole line.
                    chunk = ""
                    for ch in word:
                        if font.getlength(chunk + ch) <= max_width:
                            chunk += ch
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    current = chunk
            lines.append(current)
        return lines

    @staticmethod
    def _wrap_text_to_widths(lines: List[str], font: "ImageFont.FreeTypeFont",
                             widths: List[int]) -> List[str]:
        """Word-wrap ``lines`` where every output line has its own width budget.

        The sibling of :meth:`_wrap_text_to_width` for media whose usable width
        is not constant: on a round label each line of the stack gets the
        circle's chord at its own height, so the limit changes as the wrap
        progresses. ``widths`` is indexed by output line; once it runs out (the
        text wrapped into more lines than were budgeted for) the last entry is
        reused, and the caller re-runs the wrap with the new line count.

        Args:
            lines: Input lines (existing breaks are preserved).
            font: Font the text will be measured and drawn with.
            widths: Usable width per output line, top to bottom.

        Returns:
            The wrapped lines.
        """
        if not widths:
            return list(lines)

        wrapped: List[str] = []

        def limit_for_next_line() -> int:
            return widths[min(len(wrapped), len(widths) - 1)]

        for paragraph in lines:
            if paragraph == "":
                wrapped.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                limit = limit_for_next_line()
                candidate = word if not current else current + " " + word
                # A non-positive budget means this line is off the label
                # entirely; wrapping harder cannot save it, and hard-breaking
                # against it would loop forever, so take the text as-is and let
                # auto-fit shrink the font.
                if limit <= 0 or font.getlength(candidate) <= limit:
                    current = candidate
                    continue
                if current:
                    wrapped.append(current)
                    current = ""
                    limit = limit_for_next_line()
                if limit <= 0 or font.getlength(word) <= limit:
                    current = word
                else:
                    # Hard-break a word that is wider than the line it starts on.
                    chunk = ""
                    for character in word:
                        if font.getlength(chunk + character) <= limit:
                            chunk += character
                            continue
                        if chunk:
                            wrapped.append(chunk)
                            limit = limit_for_next_line()
                        chunk = character
                    current = chunk
            wrapped.append(current)
        return wrapped

    @staticmethod
    def _wrapping_broke_a_word(lines: List[str], wrapped: List[str]) -> bool:
        """Whether wrapping had to split a word instead of breaking between words.

        Hard-breaking always "fits", so any fitting rule that only asks whether
        the lines are short enough is trivially satisfiable by chopping a word in
        half. Comparing the word sequence before and after the wrap is what tells
        the two apart: a break between words leaves the sequence untouched, a
        break inside one does not.

        Args:
            lines: The input lines, before wrapping.
            wrapped: The lines produced by the wrap.

        Returns:
            True if at least one word was split.
        """
        return " ".join(lines).split() != " ".join(wrapped).split()

    @staticmethod
    def _widest_word(lines: List[str], font: "ImageFont.FreeTypeFont") -> float:
        """Width in pixels of the widest single word across ``lines``.

        Used to decide whether wrapping would have to hard-break a word, which
        is the point where a narrow roll needs a smaller font rather than more
        line breaks.
        """
        widest = 0.0
        for line in lines:
            for word in line.replace("\n", " ").split(" "):
                if word:
                    widest = max(widest, font.getlength(word))
        return widest

    def _render_round_text(self, lines: List[str], settings: Dict[str, Any],
                           diameter: int) -> "Image.Image":
        """
        Lay a block of text out inside the circle of a round die-cut label.

        A round label reports a square printable area, but only the inscribed
        circle is actually on the paper. Rather than retreat to the inscribed
        square -- which would throw away 36 % of the label and make a single
        centred line needlessly small -- each line is measured against the
        circle's chord at its own height (see :func:`get_round_line_widths`), so
        the middle of the label is used at nearly full width and only the top
        and bottom lines are pinched.

        ``vertical_alignment`` moves the stack up or down the circle, but only as
        far as the chord it needs still exists (see :func:`get_round_block_top`).
        The default stays ``middle``, because the circle is narrowest exactly
        where a top-aligned block starts and centring is what keeps the first
        line from being the one that gets cut off.

        Args:
            lines: The text lines to render (explicit breaks already applied).
            settings: Print settings; ``font_size``, ``alignment``,
                ``vertical_alignment``, ``text_wrap`` and ``auto_fit`` are
                honoured.
            diameter: The label's printable width in pixels.

        Returns:
            The rendered label, exactly ``diameter`` x ``diameter`` pixels.
        """
        font_size = int(settings.get("font_size", 50))
        alignment = settings.get("alignment", "left")
        vertical_alignment = get_vertical_alignment(settings)
        wrap = settings.get("text_wrap", True)
        auto_fit = settings.get("auto_fit", True)
        radius = get_round_safe_radius(diameter)

        def block_top_for(current_lines, current_font, line_height):
            """Where this exact stack may sit, given the width it needs."""
            block_width = max(
                (current_font.getlength(line) for line in current_lines), default=0.0)
            return get_round_block_top(
                radius,
                len(current_lines) * line_height,
                # A block wider than the label has no room to travel at all; cap
                # it so the geometry stays real instead of going imaginary.
                min(block_width, 2 * radius),
                vertical_alignment,
            )

        def layout(current_font):
            """Wrap ``lines`` to the circle and return (lines, widths, height, top)."""
            ascent, descent = current_font.getmetrics()
            line_height = ascent + descent
            rendered = list(lines)
            if wrap:
                # Chord widths depend on how many lines there are *and* on where
                # the stack sits; the line count depends on the widths, and the
                # position depends on the line count and the widest line. All
                # three are settled in the same iteration -- resolving them in
                # separate passes is what leaves the widths describing one
                # placement while the text is drawn at another. It converges in
                # a step or two; the cap is only there so a pathological input
                # cannot spin.
                block_top = block_top_for(rendered, current_font, line_height)
                for _ in range(4):
                    widths = get_round_line_widths(
                        radius, max(1, len(rendered)), line_height, block_top)
                    rewrapped = self._wrap_text_to_widths(lines, current_font, widths)
                    rewrapped_top = block_top_for(rewrapped, current_font, line_height)
                    settled = len(rewrapped) == len(rendered) and rewrapped_top == block_top
                    rendered, block_top = rewrapped, rewrapped_top
                    if settled:
                        break
            rendered = rendered or [""]
            # Whatever the iteration settled on, the widths handed back describe
            # the placement the caller is about to draw at, never another one.
            block_top = block_top_for(rendered, current_font, line_height)
            widths = get_round_line_widths(radius, len(rendered), line_height, block_top)
            return rendered, widths, line_height, block_top

        font = ImageFont.truetype(self.font_path, font_size)
        rendered, widths, line_height, block_top = layout(font)

        def fits(current_font, current_lines, current_widths, current_line_height):
            # The stack has to fit the diameter, and every line has to fit the
            # chord it sits on. Wrapping already enforces the second for all but
            # unwrappable input, so this mostly guards text_wrap = false.
            if len(current_lines) * current_line_height > 2 * radius:
                return False
            # ...and no word may have been cut in half to get there. A narrow
            # chord needs a smaller font, not more line breaks: hard-breaking
            # satisfies every width test trivially, so without this the search
            # stops at a large font and prints "Kalibrier / t 2026" when the very
            # same text fits one unbroken line two steps further down.
            if self._wrapping_broke_a_word(lines, current_lines):
                return False
            return all(
                current_font.getlength(line) <= width
                for line, width in zip(current_lines, current_widths)
            )

        if auto_fit:
            while (font_size > MIN_AUTO_FIT_FONT_SIZE
                   and not fits(font, rendered, widths, line_height)):
                font_size -= 2
                font = ImageFont.truetype(self.font_path, font_size)
                rendered, widths, line_height, block_top = layout(font)

        logger.debug("Laid out round label text",
                     diameter=diameter,
                     font_size=font_size,
                     line_count=len(rendered),
                     line_height=line_height,
                     vertical_alignment=vertical_alignment)

        image = Image.new("RGB", (diameter, diameter), "white")
        draw = ImageDraw.Draw(image)

        centre = diameter / 2.0
        # The very same offset the chords above were measured at.
        y = centre + block_top
        for line, width in zip(rendered, widths):
            line_width = font.getlength(line)
            # Alignment is relative to the chord this line may use, not to the
            # square, so "left" still lands on paper near the top and bottom.
            chord_left = centre - width / 2.0
            if alignment == "center":
                x = centre - line_width / 2.0
            elif alignment == "right":
                x = chord_left + width - line_width
            else:
                x = chord_left
            draw.text((int(round(x)), int(round(y))), line, font=font, fill="black")
            y += line_height

        return image

    def _create_text_label(self, html_text: str, settings: Dict[str, Any]) -> str:
        """
        Create a label image from HTML text.
        
        Args:
            html_text: Text to print (can include HTML formatting).
            settings: Dict containing print settings.
            
        Returns:
            Path to the created image file.
            
        Raises:
            ImageProcessingError: If there's an error creating the label.
        """
        try:
            # Parse HTML formatting (simplified for now)
            from html.parser import HTMLParser
            
            class TextParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                
                def handle_starttag(self, tag, attrs):
                    if tag == "br":
                        self.parts.append("<br>")
                
                def handle_data(self, data):
                    self.parts.append(data)
                
                def handle_endtag(self, tag):
                    pass
            
            parser = TextParser()
            parser.feed(html_text)
            
            # Process text parts
            lines = []
            current_line = []
            for part in parser.parts:
                if part == "<br>":
                    lines.append("".join(current_line))
                    current_line = []
                else:
                    current_line.append(part)
            if current_line:
                lines.append("".join(current_line))
            
            # Render at the loaded roll's true printable width. Anything else is
            # rescaled by convert() on the way to the printer, which silently
            # changes the effective font size and softens the result.
            geometry = get_label_geometry(settings.get("label_size"))
            width, label_height, is_die_cut = geometry

            # A round label needs its own layout: the usable width is the
            # circle's chord, which changes from line to line, so there is no
            # single text area for the rectangular path below to work with.
            if geometry.is_round and label_height:
                image = self._render_round_text(lines, settings, width)
                image_path = os.path.join(
                    self.upload_folder, f"text_label_{uuid.uuid4().hex[:8]}.png"
                )
                image.save(image_path)
                return image_path

            font_size = int(settings.get("font_size", 50))
            alignment = settings.get("alignment", "left")
            vertical_alignment = get_vertical_alignment(settings)
            text_area = width - 20  # 10 px margin on either side

            # Continuous tape has a spare axis: its length is unbounded. Running
            # the text along it instead of across it turns the printable width
            # into the line height and lets the label grow with the message, so
            # a long text on a narrow roll comes out as one readable strip
            # rather than a column of hard-wrapped fragments in a tiny font.
            # A die-cut label is a fixed size in both directions and has no such
            # axis to spend, so it always renders across.
            lengthwise = (
                str(settings.get("orientation", "across")).lower() == "lengthwise"
                and not (is_die_cut and label_height)
            )

            wrap = settings.get("text_wrap", True)
            auto_fit = settings.get("auto_fit", True)
            font = ImageFont.truetype(self.font_path, font_size)

            def wrap_all(current_font):
                # Auto-wrap long lines to the label width (default on) so text is
                # never silently truncated. Disable with settings.text_wrap = false.
                # Lengthwise there is no width to wrap against -- the tape grows
                # with the message -- so lines break only where the input said.
                if not wrap or lengthwise:
                    return list(lines)
                return [
                    wrapped
                    for line in lines
                    for wrapped in self._wrap_text_to_width(line, current_font, text_area)
                ]

            wrapped = wrap_all(font)

            # auto_fit shrinks the font until the text fits the medium. What
            # "fits" means depends on the medium, so the cases differ.
            if auto_fit and lengthwise:
                # The lines stack across the tape, so the printable width is the
                # height budget. Length is free, so nothing else constrains it.
                while font_size > MIN_AUTO_FIT_FONT_SIZE:
                    ascent, descent = font.getmetrics()
                    if 20 + len(wrapped) * (ascent + descent) <= width:
                        break
                    font_size -= 2
                    font = ImageFont.truetype(self.font_path, font_size)
            elif auto_fit and wrap:
                if is_die_cut and label_height:
                    # Fixed physical height: shrink until the wrapped text fits
                    # inside it *and* no word had to be hard-broken to get there.
                    # Height alone is satisfiable by chopping a word in half, so
                    # on its own it stops at a large font and prints
                    # "Kalibrierungs / etikett" where a smaller one would have
                    # kept the word whole -- the same trade a narrow continuous
                    # roll makes below.
                    while font_size > MIN_AUTO_FIT_FONT_SIZE:
                        ascent, descent = font.getmetrics()
                        if (20 + len(wrapped) * (ascent + descent) <= label_height
                                and self._widest_word(lines, font) <= text_area):
                            break
                        font_size -= 2
                        font = ImageFont.truetype(self.font_path, font_size)
                        wrapped = wrap_all(font)
                else:
                    # Continuous tape grows downwards, so height is never the
                    # constraint -- width is. On a narrow roll a single word can
                    # be wider than the whole label, and hard-breaking it turns a
                    # sentence into a column of letters metres long. Shrink until
                    # every word fits a line of its own instead.
                    while (font_size > MIN_AUTO_FIT_FONT_SIZE
                           and self._widest_word(lines, font) > text_area):
                        font_size -= 2
                        font = ImageFont.truetype(self.font_path, font_size)
                    wrapped = wrap_all(font)

            lines = wrapped

            # Create a dummy image to calculate text dimensions
            dummy_image = Image.new("RGB", (width, 10), "white")
            dummy_draw = ImageDraw.Draw(dummy_image)

            # Measure with the font's own line box instead of the ink bounding
            # box. The bbox covers only the glyphs actually present, so a line
            # without descenders measures short -- but draw.text() still
            # positions from the ascent line and reserves descent space, which
            # pushed the final line's descenders off the bottom of the canvas.
            # ascent+descent already includes the gap below the baseline, so no
            # extra leading is added on top.
            ascent, descent = font.getmetrics()
            line_height = ascent + descent

            total_height = 10
            line_metrics = []
            for line in lines:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                total_height += line_height
                line_metrics.append((line, bbox[2] - bbox[0]))

            total_height += 10

            if lengthwise:
                # Lay the label out unrolled: the text reads left to right on a
                # canvas as tall as the tape is wide, and the whole thing is
                # rotated onto the tape once it is drawn. So here the canvas
                # width is the tape *length* and grows with the longest line.
                line_area = max((w for _, w in line_metrics), default=0) + 20
                if line_area > MAX_LENGTHWISE_LENGTH_PX:
                    raise ValidationError(
                        f"Lengthwise label would need {line_area} px of tape "
                        f"(limit {MAX_LENGTHWISE_LENGTH_PX} px, about 1 m). "
                        "Shorten the text or reduce the font size."
                    )
                canvas_height = width
                # The tape width is the axis with room to spare here, so it is
                # the one vertical_alignment acts on: the stack may sit against
                # either edge of the tape or, by default, centred across it --
                # on a narrow roll the difference is the whole margin. The
                # canvas is still in reading orientation at this point, so "top"
                # is the edge above the first line as the label reads; the
                # transpose below carries it onto the tape. alignment continues
                # to position the lines along the tape.
                y = get_vertical_offset(
                    canvas_height, len(line_metrics) * line_height, vertical_alignment)
            else:
                # A die-cut label is a fixed physical size, so pin the canvas to
                # it: convert() raises "Bad image dimensions" for anything else.
                # With auto_fit on the font was already stepped down to fit; with
                # it off the requested size is honoured and the overflow is
                # clipped, rather than inventing a label the printer cannot cut.
                if is_die_cut and label_height:
                    total_height = label_height
                line_area = width
                canvas_height = total_height
                if is_die_cut and label_height:
                    # A die-cut label is a fixed piece of paper, so there is
                    # slack between the stack and the two long edges to hand to
                    # vertical_alignment. It stays centred by default: on media
                    # where the printable area narrows towards the edge the
                    # first line is the one that gets cut off.
                    y = get_vertical_offset(
                        canvas_height, len(line_metrics) * line_height, vertical_alignment)
                else:
                    # Continuous tape rendered across grows to exactly fit the
                    # text, so there is no spare length for the stack to move
                    # into -- only the 10 px lead-in, which every value keeps.
                    y = 10

            image = Image.new("RGB", (line_area, canvas_height), "white")
            draw = ImageDraw.Draw(image)

            # Draw text
            for line_text, line_width in line_metrics:
                if alignment == "center":
                    x = (line_area - line_width) // 2
                elif alignment == "right":
                    x = line_area - line_width - 10
                else:
                    x = 10
                draw.text((x, y), line_text, font=font, fill="black")
                y += line_height

            if lengthwise:
                # Rotate counter-clockwise so the message reads bottom-to-top
                # with the strip held upright; pair with rotate=180 for the other
                # direction. transpose() is a lossless pixel move, where rotate()
                # would resample. The result is exactly the roll's printable
                # width, so convert() passes it through without rescaling.
                image = image.transpose(Image.Transpose.ROTATE_90)

            # Save image
            image_path = os.path.join(self.upload_folder, f"text_label_{uuid.uuid4().hex[:8]}.png")
            image.save(image_path)
            
            return image_path
        except ValidationError:
            # Already a precise client error (the lengthwise length cap), so let
            # it through; wrapping it would turn a 400 into a 500.
            raise
        except Exception as e:
            logger.error("Error creating text label", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating text label: {str(e)}")
    
    def _fit_to_label(self, img: "Image.Image", label_size: Optional[str] = None) -> "Image.Image":
        """
        Centre-fit a finished label image onto the medium's exact canvas.

        Every render path ends here, because what "the right size" means is a
        property of the loaded media, not of the content:

        * Continuous tape only fixes the width; the length grows with the
          content. The image is scaled to the printable width and returned --
          exactly what ``convert()`` would otherwise do on the way out, done
          here so the pipeline and the preview agree on the pixels.
        * A rectangular die-cut label is a fixed size in both directions and
          ``convert()`` raises "Bad image dimensions" for anything else, so the
          content is scaled to fit inside it and centred on a white canvas of
          the label's own size.
        * A round die-cut label reports the same square size, but its printable
          area is the inscribed *circle*. A centred rectangle fits in a circle
          exactly when its half-diagonal fits the radius, so that is the scale
          used: a square (a QR code) ends up at the inscribed square, while a
          wide, short block gets to use nearly the full diameter instead of
          being needlessly shrunk to the same square.

        The aspect ratio is never distorted; the leftover area is white padding.

        Args:
            img: The finished label image.
            label_size: Label identifier the image is destined for. Defaults to
                62 mm tape when not given.

        Returns:
            The image fitted to the medium (the original object when it already
            fits and nothing has to change).

        Raises:
            ImageProcessingError: If the image has no usable dimensions.
        """
        geometry = get_label_geometry(label_size)
        source_width, source_height = img.size
        if source_width <= 0 or source_height <= 0:
            raise ImageProcessingError(f"Cannot fit an empty image to label {label_size}")

        if not (geometry.is_die_cut and geometry.height):
            # Continuous tape: only the width is fixed, the label may be as long
            # as it needs to be.
            if source_width == geometry.width:
                return img
            aspect_ratio = source_height / source_width
            new_height = max(1, int(geometry.width * aspect_ratio))
            return img.resize((geometry.width, new_height), Image.Resampling.LANCZOS)

        # Die-cut media from here on: the canvas is the label itself.
        # Paletted/alpha images are flattened first so the white padding really
        # is white and LANCZOS has continuous tone to work with.
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")

        if geometry.is_round:
            radius = get_round_safe_radius(geometry.width)
            scale = radius / (math.hypot(source_width, source_height) / 2.0)
        else:
            scale = min(geometry.width / source_width, geometry.height / source_height)

        new_size = (
            max(1, min(geometry.width, int(source_width * scale))),
            max(1, min(geometry.height, int(source_height * scale))),
        )
        if new_size != img.size:
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        # White canvas, so the untouched area of the label stays blank rather
        # than picking up whatever the source image had in its corners.
        canvas = Image.new(img.mode, (geometry.width, geometry.height), (255,) * len(img.mode))
        canvas.paste(img, ((geometry.width - new_size[0]) // 2, (geometry.height - new_size[1]) // 2))

        logger.debug("Fitted image to die-cut label",
                     label_size=label_size,
                     is_round=geometry.is_round,
                     source_size=(source_width, source_height),
                     content_size=new_size,
                     canvas_size=(geometry.width, geometry.height))
        return canvas

    def _resize_image(self, image_path: str, label_size: Optional[str] = None) -> str:
        """
        Resize an image to fit the label.

        Args:
            image_path: Path to the image file.
            label_size: Label identifier the image is destined for. Defaults to
                62 mm tape when not given.

        Returns:
            Path to the resized image file.

        Raises:
            ImageProcessingError: If there's an error resizing the image.
        """
        try:
            with Image.open(image_path) as img:
                # Fit to the medium: the printable width on continuous tape, the
                # label's exact canvas on die-cut media (which convert() insists
                # on) with the content centred inside it.
                img = self._fit_to_label(img, label_size)

                # Save resized image
                filename = os.path.basename(image_path)
                resized_path = os.path.join(self.upload_folder, f"resized_{filename}")
                img.save(resized_path)

                return resized_path
        except Exception as e:
            logger.error("Error resizing image", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error resizing image: {str(e)}")
    
    def _apply_rotation(self, image_path: str, angle: int) -> str:
        """
        Apply rotation to an image.
        
        Args:
            image_path: Path to the image file.
            angle: Rotation angle in degrees.
            
        Returns:
            Path to the rotated image file.
            
        Raises:
            ImageProcessingError: If there's an error rotating the image.
        """
        try:
            with Image.open(image_path) as img:
                # Apply rotation
                rotated_img = img.rotate(-angle, resample=Image.Resampling.LANCZOS, expand=True)
                
                # Save rotated image
                filename = os.path.basename(image_path)
                rotated_path = os.path.join(self.upload_folder, f"rotated_{filename}")
                rotated_img.save(rotated_path)
                
                return rotated_path
        except Exception as e:
            logger.error("Error rotating image", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error rotating image: {str(e)}")
    
    def _send_to_printer(self, image_path: str, settings: Dict[str, Any]) -> None:
        """
        Send an image to the printer.
        
        Args:
            image_path: Path to the image file.
            settings: Dict containing print settings.
            
        Raises:
            PrinterError: If there's an error sending to the printer.
        """
        # Extract settings
        printer_uri = settings.get("printer_uri")
        printer_model = settings.get("printer_model")
        label_size = settings.get("label_size")
        # settings["rotate"] is deliberately not read here: callers apply the
        # rotation to the image themselves before handing it over.
        dither = settings.get("dither", False)
        compress = settings.get("compress", False)
        red = settings.get("red", False)
        copies = settings.get("copies", 1)
        cut_mode = settings.get("cut_mode", "each")
        dpi_600 = settings.get("dpi_600", False)
        hq = settings.get("hq", True)

        # --- Input/validation phase (-> ValidationError -> 400) ---
        # Bad/missing settings and disallowed/SSRF URIs are caller mistakes,
        # not printer faults, so they are classified as validation errors.
        try:
            threshold = float(settings.get("threshold", 70.0))

            # Validate required settings
            if not printer_uri:
                raise ValueError("printer_uri is required")
            if not printer_model:
                raise ValueError("printer_model is required")
            if not label_size:
                raise ValueError("label_size is required")

            # Defense in depth: validate the destination URI immediately before
            # handing it to the backend. Rejects disallowed schemes (file://,
            # lpt://, ...) and SSRF/metadata targets even if settings validation
            # was bypassed. Private/LAN IPs and hostnames stay valid.
            validate_printer_uri(printer_uri)

            # Copies and cut mode.
            try:
                copies = int(copies)
            except (TypeError, ValueError):
                raise ValueError("copies must be an integer")
            if copies < 1 or copies > 100:
                raise ValueError("copies must be between 1 and 100")
            if cut_mode not in ("each", "end", "none"):
                raise ValueError("cut_mode must be one of: each, end, none")
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid print settings", error=str(e))
            raise ValidationError(f"Invalid print settings: {str(e)}") from e

        # --- Printer/IO phase (-> PrinterError -> 500) ---
        try:
            # One image per copy; the cut mode decides how the rasterizer cuts.
            images = [image_path] * copies
            if cut_mode == "none":
                qlr = BrotherQLRaster(printer_model)
                cut = False
            elif cut_mode == "end" and copies > 1:
                # Cut a single time after the last of the N labels.
                qlr = _CutAtEndRaster(printer_model, copies)
                cut = True
            else:  # "each" (and "end" with a single label, where they coincide)
                qlr = BrotherQLRaster(printer_model)
                cut = True
            qlr.exception_on_warning = True

            # Convert image(s) to printer instructions.
            #
            # rotate=0, NOT the caller's value: every path into this method has
            # already rotated the image with _apply_rotation. Passing the angle
            # on makes convert() rotate a second time, returning the image to
            # its original orientation before fitting it to the tape -- so the
            # logs report "Rotation applied" while the label comes out
            # unrotated.
            instructions = convert(
                qlr=qlr,
                images=images,
                label=label_size,
                rotate=0,
                threshold=threshold,
                dither=dither,
                compress=compress,
                red=red,
                cut=cut,
                dpi_600=dpi_600,
                hq=hq,
            )

            # Send to printer (serialized against the keep-alive heartbeat).
            with self._io_lock:
                backend = backend_factory(guess_backend(printer_uri))["backend_class"](printer_uri)
                backend.write(instructions)
                backend.dispose()

            # Record print activity so the "timed" keep-alive mode extends its
            # awake window from this moment.
            self._last_print_at = time.time()

            logger.info("Print job sent to printer",
                       printer_uri=printer_uri,
                       printer_model=printer_model,
                       label_size=label_size,
                       copies=copies,
                       cut_mode=cut_mode)
        except Exception as e:
            logger.error("Error sending to printer", error=str(e), exc_info=True)
            raise PrinterError(f"Error sending to printer: {str(e)}") from e

    def start_keep_alive(self, printer_uri: Optional[str] = None, printer_model: Optional[str] = None, interval: int = 60) -> Dict[str, Any]:
        """
        Start a background thread that periodically pings the printer to keep it from shutting down.
        
        Args:
            printer_uri: URI of the printer to keep alive. If None, uses the default printer from settings.
            printer_model: Model of the printer. If None, uses the default printer model from settings.
            interval: Time interval between pings in seconds.
            
        Returns:
            Dict containing the result of the operation.
        """
        try:
            # Get settings if printer_uri or printer_model is not provided
            settings = settings_service.get_settings()
            
            # Use provided values or defaults from settings
            if printer_uri is None:
                printer_uri = settings.get("printer_uri", "")
                if not printer_uri:
                    # Try to get the first printer from the printers list
                    printers = settings.get("printers", [])
                    if printers:
                        printer_uri = printers[0].get("printer_uri", "")
            
            if printer_model is None:
                printer_model = settings.get("printer_model", "")
                if not printer_model:
                    # Try to get the first printer from the printers list
                    printers = settings.get("printers", [])
                    if printers:
                        printer_model = printers[0].get("printer_model", "")
            
            # Validate required parameters
            if not printer_uri:
                return {
                    "success": False,
                    "message": "Printer URI is required but not provided and not found in settings"
                }
            
            if not printer_model:
                return {
                    "success": False,
                    "message": "Printer model is required but not provided and not found in settings"
                }

            # Disable keep alive if backend is not 'network'
            if guess_backend(printer_uri) != "network":
                logger.info("Keep alive disabled: backend is not 'network'", printer_uri=printer_uri, backend=guess_backend(printer_uri))
                return {
                    "success": False,
                    "message": f"Keep alive is not useful for non-network backends"
                }
            
            # Stop any existing keep alive thread
            self.stop_keep_alive()
            
            # Create a new stop event
            self.keep_alive_stop_event = threading.Event()
            
            # Start a new thread
            self.keep_alive_thread = threading.Thread(
                target=self._keep_alive_worker,
                args=(printer_uri, printer_model, interval, self.keep_alive_stop_event),
                daemon=True
            )
            self.keep_alive_thread.start()
            
            logger.info("Keep alive started", 
                       printer_uri=printer_uri, 
                       printer_model=printer_model,
                       interval=interval)
            
            # Save the keep alive settings
            settings["keep_alive_enabled"] = True
            settings["keep_alive_interval"] = interval
            settings_service.update_settings(settings)
            
            return {
                "success": True,
                "message": f"Keep alive started for {printer_uri} with interval {interval} seconds"
            }
        except Exception as e:
            logger.error("Error starting keep alive", error=str(e), exc_info=True)
            return {
                "success": False,
                "message": f"Error starting keep alive: {str(e)}"
            }
    
    def stop_keep_alive(self) -> Dict[str, Any]:
        """
        Stop the keep alive thread if it's running.
        
        Returns:
            Dict containing the result of the operation.
        """
        try:
            if self.keep_alive_thread and self.keep_alive_thread.is_alive():
                self.keep_alive_stop_event.set()
                self.keep_alive_thread.join(timeout=5)
                self.keep_alive_thread = None
                
                logger.info("Keep alive stopped")
                
                return {
                    "success": True,
                    "message": "Keep alive stopped"
                }
            else:
                return {
                    "success": True,
                    "message": "Keep alive was not running"
                }
        except Exception as e:
            logger.error("Error stopping keep alive", error=str(e), exc_info=True)
            return {
                "success": False,
                "message": f"Error stopping keep alive: {str(e)}"
            }
    
    def print_qr_code(self, data: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate and print a QR code.
        
        Args:
            data: The data to encode in the QR code.
            settings: Dict containing print settings.
            
        Returns:
            Dict containing the result of the print operation.
            
        Raises:
            PrinterError: If there's an error printing the QR code.
            ImageProcessingError: If there's an error generating the QR code.
            ValueError: If settings are invalid.
        """
        # Track intermediate artifacts so they can be cleaned up afterwards.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"qrcode_{uuid.uuid4().hex[:8]}"

            logger.info("Processing QR code print request", job_id=job_id, data_length=len(data))

            # Create QR code image
            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)
            logger.info("QR code created", job_id=job_id, image_path=image_path)

            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "QR code printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for QR code print", error=str(e))
            raise ValidationError(f"Error printing QR code: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing QR code", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing QR code: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)
    
    def _create_qr_code(self, data: str, settings: Dict[str, Any]) -> str:
        """
        Create a QR code image.
        
        Args:
            data: The data to encode in the QR code.
            settings: Dict containing QR code settings.
            
        Returns:
            Path to the created QR code image file.
            
        Raises:
            ImageProcessingError: If there's an error creating the QR code.
        """
        try:
            # 1. Render the bare QR code (encoding + optional resize).
            qr_img = self._generate_qr_image(data, settings)

            # 2. Compose with text according to the requested layout
            #    (side-by-side, or text above/below). No-op if no text.
            qr_img = self._compose_qr_with_text(qr_img, data, settings)

            # 3. Fit the composition to the loaded media. The QR code is
            #    rendered at whatever qr_size asks for, which is a size the
            #    printer knows nothing about: on continuous tape it has to be
            #    scaled to the printable width, and on a die-cut label it has to
            #    land on the label's exact canvas -- convert() refuses anything
            #    else outright, so without this a round label could never print.
            qr_img = self._fit_to_label(qr_img, settings.get("label_size"))

            # 4. Persist the result.
            image_path = os.path.join(self.upload_folder, f"qrcode_{uuid.uuid4().hex[:8]}.png")
            qr_img.save(image_path)

            return image_path
        except Exception as e:
            logger.error("Error creating QR code", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating QR code: {str(e)}")

    def _generate_qr_image(self, data: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Encode ``data`` into a QR code image and resize it to the configured
        overall size (maintaining aspect ratio).

        Returns:
            The rendered QR code as an RGB ``PIL.Image``.
        """
        # Extract QR code settings
        qr_version = settings.get("qr_version", 1)  # QR code version (1-40)
        qr_box_size = settings.get("qr_box_size", 10)  # Size of each box in pixels
        qr_border = settings.get("qr_border", 4)  # Border size in boxes
        error_correction = settings.get("error_correction", "M")  # L, M, Q, H

        # Map error correction string to qrcode constants
        error_correction_map = {
            "L": qrcode.constants.ERROR_CORRECT_L,  # 7% error correction
            "M": qrcode.constants.ERROR_CORRECT_M,  # 15% error correction
            "Q": qrcode.constants.ERROR_CORRECT_Q,  # 25% error correction
            "H": qrcode.constants.ERROR_CORRECT_H,  # 30% error correction
        }
        ec_level = error_correction_map.get(error_correction, qrcode.constants.ERROR_CORRECT_M)

        # Create QR code
        qr = qrcode.QRCode(
            version=qr_version,
            error_correction=ec_level,
            box_size=qr_box_size,
            border=qr_border,
        )
        qr.add_data(data)
        qr.make(fit=True)

        # Create image
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img = qr_img.convert("RGB")

        # Resize QR code to desired size if specified
        qr_size = settings.get("qr_size", 400)  # Default to 400px for better visibility
        if qr_size:
            # Get current size
            current_width, current_height = qr_img.size

            # Calculate new size while maintaining aspect ratio
            if current_width != qr_size:
                ratio = qr_size / current_width
                new_size = (qr_size, int(current_height * ratio))
                qr_img = qr_img.resize(new_size, Image.Resampling.LANCZOS)
                logger.debug("Resized QR code",
                           original_size=(current_width, current_height),
                           new_size=new_size)

        return qr_img

    def _compose_qr_with_text(self, qr_img: Image.Image, data: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Combine the rendered QR code with any configured text.

        Dispatches to the side-by-side layout when requested (and side text is
        present), otherwise to the text-above/below layout when text display is
        enabled. If neither applies the QR image is returned unchanged.
        """
        show_text = settings.get("show_text", False)
        text = settings.get("text", data)  # Use data as default text if not provided

        # Layout settings
        side_by_side = settings.get("side_by_side", False)  # Whether to show text and QR code side by side
        side_text = settings.get("side_text", "")  # Text to show on the side

        # Check if we should use side-by-side layout
        if side_by_side and side_text:
            return self._layout_side_by_side(qr_img, side_text, settings)
        # If text should be shown with the QR code (only if not using side-by-side)
        elif show_text and text:
            return self._layout_text_above_below(qr_img, text, settings)

        return qr_img

    def _layout_side_by_side(self, qr_img: Image.Image, side_text: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Place the QR code and multi-line text side by side (text 2/3, QR 1/3).

        ``qr_position`` selects whether the QR sits on the left or the right;
        ``text_alignment`` controls horizontal alignment of the text block.
        """
        text_alignment = settings.get("text_alignment", "center")  # Text alignment: "left", "center", or "right"
        qr_position = settings.get("qr_position", "right")  # Position of QR code: "left" or "right"

        # Get QR code dimensions
        qr_width, qr_height = qr_img.size

        # Use text_font_size if provided, otherwise fall back to font_size or default
        text_font_size = settings.get("text_font_size", settings.get("font_size", 30))
        font = ImageFont.truetype(self.font_path, text_font_size)

        # Fix the label to the loaded roll's printable width, split into a text
        # column (2/3) and a QR column (1/3), then wrap the text to its column
        # (default on) so long names stay readable instead of being truncated or
        # ballooning the label. Disable with settings.text_wrap = false.
        padding = 20
        total_width = get_label_width(settings.get("label_size"))
        text_area_width = int(total_width * 2 / 3) - padding * 2
        qr_area_width = total_width - text_area_width - padding * 3

        if settings.get("text_wrap", True):
            side_text_lines = self._wrap_text_to_width(side_text, font, text_area_width)
        else:
            side_text_lines = side_text.split('\n')

        # Measure each (wrapped) line.
        text_metrics = []
        total_text_height = 0
        line_spacing = 10
        dummy_draw = ImageDraw.Draw(qr_img)
        for line in side_text_lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            text_metrics.append((line, line_width, line_height))
            total_text_height += line_height + line_spacing
        # Remove extra line spacing after the last line.
        total_text_height -= line_spacing

        # Resize QR code to fit in the 1/3 area while keeping it square
        # Use the width as the limiting factor for both dimensions
        qr_img = qr_img.resize((qr_area_width, qr_area_width), Image.Resampling.LANCZOS)
        qr_width, qr_height = qr_img.size

        # Create a new image with the combined layout
        total_height = max(qr_height, total_text_height) + padding * 2
        new_img = Image.new("RGB", (total_width, total_height), "white")

        # Determine positions based on qr_position
        if qr_position == "left":
            # QR code on the left, text on the right
            qr_x = padding
            text_area_x = qr_area_width + padding * 2
        else:
            # QR code on the right, text on the left (default)
            qr_x = text_area_width + padding * 2
            text_area_x = padding

        # Paste QR code
        qr_y = (total_height - qr_height) // 2  # Center vertically
        new_img.paste(qr_img, (qr_x, qr_y))

        # Draw text with specified alignment
        draw = ImageDraw.Draw(new_img)
        text_y = (total_height - total_text_height) // 2  # Center vertically

        for line, line_width, line_height in text_metrics:
            # Calculate text position based on alignment
            if text_alignment == "center":
                text_x = text_area_x + (text_area_width - line_width) // 2
            elif text_alignment == "right":
                text_x = text_area_x + text_area_width - line_width
            else:  # left alignment (default)
                text_x = text_area_x

            draw.text((text_x, text_y), line, font=font, fill="black")
            text_y += line_height + line_spacing

        return new_img

    def _layout_text_above_below(self, qr_img: Image.Image, text: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Stack a single line of text above or below the QR code.

        ``text_position`` ("top"/"bottom") selects placement; ``text_alignment``
        controls horizontal alignment.
        """
        text_position = settings.get("text_position", "bottom")  # Position of text: "top", "bottom", or "none"
        text_alignment = settings.get("text_alignment", "center")  # Text alignment: "left", "center", or "right"

        # Get QR code dimensions
        qr_width, qr_height = qr_img.size

        # Create a new image with space for text
        # Use text_font_size if provided, otherwise fall back to font_size or default
        text_font_size = settings.get("text_font_size", settings.get("font_size", 30))
        font = ImageFont.truetype(self.font_path, text_font_size)

        # Wrap the caption to the QR width (default on) so long captions are
        # never truncated; disable with settings.text_wrap = false.
        margin = 10
        if settings.get("text_wrap", True):
            text_lines = self._wrap_text_to_width(text, font, qr_width - margin * 2)
        else:
            text_lines = text.split('\n')

        # Measure each line of the (possibly wrapped) caption.
        dummy_draw = ImageDraw.Draw(qr_img)
        line_spacing = 6
        line_metrics = []
        text_block_height = 0
        for line in text_lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            if not line:
                line_height = sum(font.getmetrics())  # give blank lines a real gap
            line_metrics.append((line, line_width, line_height))
            text_block_height += line_height + line_spacing
        text_block_height = max(0, text_block_height - line_spacing)

        padding = 20  # Padding between QR code and text
        new_height = qr_height + text_block_height + padding
        new_img = Image.new("RGB", (qr_width, new_height), "white")
        draw = ImageDraw.Draw(new_img)

        if text_position == "top":
            text_top = padding // 2
            qr_top = text_block_height + padding
        else:  # bottom (default)
            qr_top = 0
            text_top = qr_height + padding // 2

        new_img.paste(qr_img, (0, qr_top))

        # Draw each caption line with the requested horizontal alignment.
        y = text_top
        for line, line_width, line_height in line_metrics:
            if text_alignment == "center":
                x = (qr_width - line_width) // 2
            elif text_alignment == "right":
                x = qr_width - line_width - margin
            else:  # left alignment
                x = margin
            draw.text((x, y), line, font=font, fill="black")
            y += line_height + line_spacing

        return new_img
    
    def get_keep_alive_status(self) -> Dict[str, Any]:
        """
        Get the current status of the keep alive feature.
        
        Returns:
            Dict containing the status information.
        """
        try:
            settings = settings_service.get_settings()
            is_running = self.keep_alive_thread is not None and self.keep_alive_thread.is_alive()
            
            return {
                "enabled": settings.get("keep_alive_enabled", False),
                "interval": settings.get("keep_alive_interval", 60),
                "running": is_running
            }
        except Exception as e:
            logger.error("Error getting keep alive status", error=str(e), exc_info=True)
            return {
                "enabled": False,
                "interval": 60,
                "running": False,
                "error": str(e)
            }
    
    def _get_ipp_port(self) -> int:
        """IPP port used for status/keep-alive. Defaults to the IANA-standard
        631 (the same on virtually all Brother network printers) but can be
        overridden via the ``ipp_port`` setting for non-standard setups."""
        try:
            return int(settings_service.get_settings().get("ipp_port", 631) or 631)
        except (TypeError, ValueError):
            return 631

    def _build_clock_info(self, printer_time: Optional[datetime]) -> Dict[str, Any]:
        """Compare the printer's reported clock against the server clock (UTC)."""
        now = datetime.now(timezone.utc)
        info: Dict[str, Any] = {
            "server_time": now.isoformat(timespec="seconds"),
            "printer_time": None,
            "drift_seconds": None,
            "in_sync": None,
            "note": None,
        }
        if printer_time is None:
            info["note"] = "Printer did not report a clock"
            return info
        info["printer_time"] = printer_time.isoformat(timespec="seconds")
        drift = (printer_time.astimezone(timezone.utc) - now).total_seconds()
        info["drift_seconds"] = round(drift, 1)
        info["in_sync"] = abs(drift) <= 120  # within 2 minutes (UTC compare)
        if not info["in_sync"]:
            info["note"] = (
                "Printer clock differs from server time (UTC). It cannot be set "
                "remotely; adjust it on the device LCD / Brother Printer Setting "
                "Tool, check the CR2032 backup battery, and verify the timezone "
                "in the printer web UI."
            )
        return info

    def get_printer_clock(self, printer_uri: Optional[str] = None) -> Dict[str, Any]:
        """Read the printer's real-time clock via IPP and compare to the server.

        Read-only: the Brother QL clock cannot be set programmatically (no IPP
        Set-Printer-Attributes and no documented protocol command), so this only
        surfaces a drift warning.
        """
        if printer_uri is None:
            printer_uri = settings_service.get_settings().get("printer_uri", "")
        if not printer_uri or guess_backend(printer_uri) != "network":
            return {"available": False, "note": "Clock readout requires a network (tcp://) printer"}
        ip_address = self._extract_ip_from_uri(printer_uri)
        ipp = get_printer_attributes(ip_address, port=self._get_ipp_port())
        if not ipp.get("reachable"):
            return {"available": False, "note": "Printer not reachable via IPP", "error": ipp.get("error")}
        clock = self._build_clock_info(ipp.get("current_time"))
        clock["available"] = ipp.get("current_time") is not None
        return clock

    def _ipp_ping(self, ip_address: str) -> bool:
        """Keep-alive/reachability probe via IPP Get-Printer-Attributes (TCP 631).

        Unlike a bare TCP connect this is a real request/response round-trip and
        is the only working status channel on Brother QL models with SNMP off.
        """
        try:
            return bool(get_printer_attributes(ip_address, port=self._get_ipp_port()).get("reachable"))
        except Exception as e:
            logger.debug("IPP ping failed", ip_address=ip_address, error=str(e))
            return False

    def _write_keepalive(self, ip_address: str, port: int = 9100, timeout: float = 3.0) -> bool:
        """Active keep-alive heartbeat: send a harmless ``ESC @`` (initialize) to
        the raster/print port (TCP 9100).

        Rationale: a status *read* (IPP/SNMP/TCP-connect) does NOT reset a Brother
        QL's auto-power-off / sleep timer — per Brother's docs only *received
        print data* does. ``ESC @`` (0x1B 0x40) is the printer-reset command; it
        prints nothing and feeds nothing, but it is real data on the print
        channel, so it is the best app-side attempt to keep the device awake.

        Serialized via ``self._io_lock`` so it never interleaves with a real
        print. If a print is already in progress the lock is held — that print is
        itself activity, so we skip this heartbeat and report success.
        """
        host = ip_address.split(":")[0] if ":" in ip_address else ip_address
        if not self._io_lock.acquire(blocking=False):
            # A print job holds the port right now -> that already keeps the
            # printer awake; treat this cycle as a successful heartbeat.
            logger.debug("Keep alive skipped: print in progress", ip_address=host)
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                if sock.connect_ex((host, port)) != 0:
                    return False
                sock.sendall(b"\x1b\x40")  # ESC @ = initialize (no print, no feed)
                return True
            finally:
                sock.close()
        except Exception as e:
            logger.debug("Keep alive write failed", ip_address=host, error=str(e))
            return False
        finally:
            self._io_lock.release()

    def _tcp_reachable(self, ip_address: str, port: int = 9100, timeout: float = 1.5) -> bool:
        """Single quick TCP connect probe (reachability only).

        Used as the status-check fallback after IPP. Unlike ``_tcp_ping`` it does
        not sweep several ports, so an offline printer fails in ~1.5s instead of
        summing multiple timeouts.
        """
        host = ip_address.split(":")[0] if ":" in ip_address else ip_address
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _extract_ip_from_uri(self, printer_uri: str) -> str:
        """
        Extract IP address from printer URI.
        
        Args:
            printer_uri: URI of the printer (e.g., tcp://192.168.1.100).
            
        Returns:
            IP address as a string.
        """
        # Handle tcp:// format
        if printer_uri.startswith("tcp://"):
            ip_address = printer_uri[6:]
            # Remove port if present
            if ":" in ip_address:
                ip_address = ip_address.split(":")[0]
            return ip_address
        
        # Handle other formats or return as is if not recognized
        return printer_uri
    
    # Class variable to track if we've already logged the SNMP warning
    _snmp_warning_logged = False
    
    def _snmp_ping(self, ip_address: str) -> bool:
        """
        Send an SNMP ping to the printer.
        
        Args:
            ip_address: IP address of the printer.
            
        Returns:
            True if successful, False otherwise.
        """
        if not SNMP_AVAILABLE:
            # Only log the warning once per application run
            if not PrinterService._snmp_warning_logged:
                logger.warning("SNMP not available, falling back to TCP ping")
                PrinterService._snmp_warning_logged = True
            return False
            
        try:
            # Standard printer MIB - System Description
            system_description_oid = '1.3.6.1.2.1.1.1.0'
            
            # Create an SNMP GET request
            error_indication, error_status, error_index, var_binds = next(
                getCmd(
                    SnmpEngine(),
                    CommunityData('public'),  # SNMP community string, 'public' is common default
                    UdpTransportTarget((ip_address, 161), timeout=2.0, retries=1),
                    ContextData(),
                    ObjectType(ObjectIdentity(system_description_oid))
                )
            )
            
            if error_indication:
                logger.warning("SNMP error", ip_address=ip_address, error=str(error_indication))
                return False
            elif error_status:
                logger.warning("SNMP error status", 
                              ip_address=ip_address, 
                              error_status=error_status,
                              error_index=error_index,
                              var_binds=var_binds)
                return False
            else:
                # Successfully received SNMP response
                for var_bind in var_binds:
                    logger.debug("SNMP response", 
                                ip_address=ip_address, 
                                oid=var_bind[0].prettyPrint(), 
                                value=var_bind[1].prettyPrint())
                return True
                
        except Exception as e:
            logger.warning("SNMP ping failed", ip_address=ip_address, error=str(e))
            return False
    
    def _tcp_ping(self, ip_address: str) -> bool:
        """
        Send a TCP ping to the printer.
        
        Args:
            ip_address: IP address of the printer.
            
        Returns:
            True if successful, False otherwise.
        """
        try:
            # Extract port if present in the IP address
            port = 9100  # Default printer port
            if ":" in ip_address:
                ip_parts = ip_address.split(":")
                ip_address = ip_parts[0]
                try:
                    port = int(ip_parts[1])
                except (ValueError, IndexError):
                    pass
            
            # Try to connect to the specific port first
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                result = sock.connect_ex((ip_address, port))
                sock.close()
                
                if result == 0:
                    logger.debug("TCP ping successful on specific port", ip_address=ip_address, port=port)
                    return True
            except Exception as specific_error:
                logger.debug("TCP ping failed on specific port", 
                           ip_address=ip_address, 
                           port=port, 
                           error=str(specific_error))
            
            # If specific port fails, try common printer ports
            printer_ports = [9100, 515, 631]  # Standard printer ports (RAW, LPR, IPP)
            
            # Remove the specific port we already tried
            if port in printer_ports:
                printer_ports.remove(port)
            
            for alt_port in printer_ports:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    result = sock.connect_ex((ip_address, alt_port))
                    sock.close()
                    
                    if result == 0:
                        logger.debug("TCP ping successful on alternative port", 
                                   ip_address=ip_address, 
                                   port=alt_port)
                        return True
                except Exception:
                    continue
            
            logger.warning("TCP ping failed on all ports", ip_address=ip_address)
            return False
        except Exception as e:
            logger.warning("TCP ping error", ip_address=ip_address, error=str(e))
            return False
    
    def _is_docker_host_internal(self, ip_address: str) -> bool:
        """
        Check if the IP address is a Docker host.internal address.
        
        Args:
            ip_address: IP address to check.
            
        Returns:
            True if it's a Docker host.internal address, False otherwise.
        """
        return "host.docker.internal" in ip_address or "docker.host.internal" in ip_address
    
    def _keep_alive_worker(self, printer_uri: str, printer_model: str, interval: int, stop_event: threading.Event) -> None:
        """
        Worker function for the keep alive thread.
        
        Args:
            printer_uri: URI of the printer to keep alive.
            printer_model: Model of the printer.
            interval: Time interval between pings in seconds.
            stop_event: Event to signal the thread to stop.
        """
        backend_type = guess_backend(printer_uri)
        if backend_type != "network":
            logger.info("Keep alive worker exiting: backend is not 'network'", printer_uri=printer_uri, backend=backend_type)
            return
        
        logger.info("Keep alive worker started", 
                   printer_uri=printer_uri, 
                   printer_model=printer_model,
                   interval=interval)
        
        # Extract IP address from printer URI
        ip_address = self._extract_ip_from_uri(printer_uri)
        logger.info("Extracted IP address for keep alive", 
                   printer_uri=printer_uri, 
                   ip_address=ip_address)
        
        # Track consecutive failures to implement exponential backoff
        consecutive_failures = 0
        max_backoff = 300  # Maximum backoff in seconds (5 minutes)
        last_ok: Optional[bool] = None  # for state-change (INFO/WARN) logging
        last_active: Optional[bool] = None  # for timed-window pause/resume logging
        
        while not stop_event.is_set():
            try:
                # Calculate backoff time based on consecutive failures
                if consecutive_failures > 0:
                    # Exponential backoff with a maximum
                    backoff_time = min(interval * (2 ** consecutive_failures), max_backoff)
                    logger.warning("Using backoff due to consecutive failures", 
                                  consecutive_failures=consecutive_failures,
                                  backoff_time=backoff_time)
                    # Wait for the backoff time or until stopped
                    if stop_event.wait(backoff_time - interval):  # Subtract interval because we'll wait again at the end
                        break
                
                # Dynamic keep-alive window. In "timed" mode we only keep the
                # printer awake for `keep_alive_duration_seconds` after the last
                # print; outside that window we pause the heartbeat (letting the
                # printer sleep) until the next print resets the timer. In
                # "forever" mode (or duration<=0) we always ping. Settings are
                # re-read each tick so changes take effect live.
                ka = settings_service.get_settings()
                ka_mode = ka.get("keep_alive_mode", "forever")
                try:
                    ka_duration = int(ka.get("keep_alive_duration_seconds", 0) or 0)
                except (TypeError, ValueError):
                    ka_duration = 0
                if ka_mode == "timed" and ka_duration > 0 and (time.time() - self._last_print_at) > ka_duration:
                    if last_active is not False:
                        logger.info("Keep alive paused: no print within the configured window",
                                    printer_uri=printer_uri, window_seconds=ka_duration)
                        last_active = False
                    stop_event.wait(interval)
                    continue
                if last_active is False:
                    logger.info("Keep alive resumed after print activity", printer_uri=printer_uri)
                last_active = True

                logger.debug("Sending keep alive ping", ip_address=ip_address)

                # PRIMARY: write a harmless ESC @ to port 9100. A status *read*
                # (IPP/SNMP/TCP-connect) does not reset the printer's
                # auto-power-off timer — only *received print data* does — so the
                # write is the only app-side attempt that can actually keep the
                # device awake. Fall back to IPP/TCP purely for reachability
                # reporting if the write channel is unavailable.
                method = None
                if self._write_keepalive(ip_address):
                    method = "raw9100"
                elif self._ipp_ping(ip_address):
                    method = "ipp"
                elif self._tcp_ping(ip_address):
                    method = "tcp"

                if method is not None:
                    consecutive_failures = 0
                    if last_ok is not True:
                        logger.info("Keep alive: printer reachable",
                                    printer_uri=printer_uri, ip_address=ip_address, method=method)
                    else:
                        logger.debug("Keep alive ping successful",
                                     printer_uri=printer_uri, ip_address=ip_address, method=method)
                    last_ok = True
                else:
                    consecutive_failures += 1
                    if last_ok is not False:
                        logger.warning("Keep alive: printer not reachable",
                                       printer_uri=printer_uri, ip_address=ip_address,
                                       consecutive_failures=consecutive_failures)
                    else:
                        logger.debug("Keep alive ping failed (repeated)",
                                     printer_uri=printer_uri, ip_address=ip_address,
                                     consecutive_failures=consecutive_failures)
                    last_ok = False
            except Exception as e:
                # Increment consecutive failures for backoff
                consecutive_failures += 1
                
                # Log at warning level instead of error to reduce log noise
                log_level = "error" if consecutive_failures <= 3 else "warning"
                if log_level == "error":
                    logger.error("Error in keep alive ping", 
                               printer_uri=printer_uri,
                               ip_address=ip_address,
                               error=str(e),
                               consecutive_failures=consecutive_failures,
                               exc_info=True)
                else:
                    logger.warning("Error in keep alive ping (repeated)", 
                                 printer_uri=printer_uri,
                                 ip_address=ip_address,
                                 error=str(e),
                                 consecutive_failures=consecutive_failures)
            
            # Wait for the next interval or until stopped
            stop_event.wait(interval)
        
        logger.info("Keep alive worker stopped", printer_uri=printer_uri)

# Create a singleton instance
printer_service = PrinterService()
