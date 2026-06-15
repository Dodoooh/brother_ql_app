"""
Printer service for managing Brother QL printer operations.
"""

import os
import sys
import io
import base64
import uuid
import structlog
import threading
import time
import socket
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
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

            # Resize image to fit label width
            resized_path = self._resize_image(image_path)
            temp_files.append(resized_path)
            logger.info("Image resized", job_id=job_id, resized_path=resized_path)

            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                resized_path = self._apply_rotation(resized_path, rotate)
                temp_files.append(resized_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

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

            # Layout geometry: fixed 696px label, image 1/3, text 2/3.
            width = 696
            padding = 20
            image_area_width = int(width * 1 / 3) - padding * 2
            text_area_width = width - image_area_width - padding * 3

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

                # Fit the page to the label width (same path as image printing).
                resized_path = self._resize_image(temp_png)
                temp_files.append(resized_path)

                # Apply rotation if requested.
                if rotate != 0:
                    resized_path = self._apply_rotation(resized_path, rotate)
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

            resized_path = self._resize_image(image_path)
            temp_files.append(resized_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                resized_path = self._apply_rotation(resized_path, rotate)
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
            
            # Create image
            width = 696  # Fixed label width
            font_size = int(settings.get("font_size", 50))
            alignment = settings.get("alignment", "left")
            
            # Create a dummy image to calculate text dimensions
            dummy_image = Image.new("RGB", (width, 10), "white")
            dummy_draw = ImageDraw.Draw(dummy_image)
            
            # Calculate total height and line metrics
            total_height = 10
            line_spacing = 5
            line_metrics = []
            
            for line in lines:
                font = ImageFont.truetype(self.font_path, font_size)
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                max_ascent, max_descent = font.getmetrics()
                total_height += line_height + line_spacing
                line_metrics.append((line, max_ascent, max_descent, line_height, line_width))
            
            # Create the actual image
            total_height += 10
            image = Image.new("RGB", (width, total_height), "white")
            draw = ImageDraw.Draw(image)
            
            # Draw text
            y = 10
            for line_text, max_ascent, max_descent, line_height, line_width in line_metrics:
                if alignment == "center":
                    x = (width - line_width) // 2
                elif alignment == "right":
                    x = width - line_width - 10
                else:
                    x = 10
                draw.text((x, y), line_text, font=font, fill="black")
                y += line_height + line_spacing
            
            # Save image
            image_path = os.path.join(self.upload_folder, f"text_label_{uuid.uuid4().hex[:8]}.png")
            image.save(image_path)
            
            return image_path
        except Exception as e:
            logger.error("Error creating text label", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating text label: {str(e)}")
    
    def _resize_image(self, image_path: str) -> str:
        """
        Resize an image to fit the label width.
        
        Args:
            image_path: Path to the image file.
            
        Returns:
            Path to the resized image file.
            
        Raises:
            ImageProcessingError: If there's an error resizing the image.
        """
        try:
            max_width = 696  # Fixed label width
            
            with Image.open(image_path) as img:
                # Calculate new dimensions
                aspect_ratio = img.height / img.width
                new_height = int(max_width * aspect_ratio)
                
                # Resize image
                img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
                
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
        rotate = settings.get("rotate", 0)
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

            # Convert image(s) to printer instructions
            instructions = convert(
                qlr=qlr,
                images=images,
                label=label_size,
                rotate=rotate,
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

            # 3. Persist the result.
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

        # Parse side_text into lines
        side_text_lines = side_text.split('\n')

        # Calculate text dimensions for each line
        text_metrics = []
        max_text_width = 0
        total_text_height = 0
        line_spacing = 10

        dummy_draw = ImageDraw.Draw(qr_img)
        for line in side_text_lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            max_text_width = max(max_text_width, line_width)
            text_metrics.append((line, line_width, line_height))
            total_text_height += line_height + line_spacing

        # Remove extra line spacing from the last line
        total_text_height -= line_spacing

        # Calculate dimensions for the combined image
        # Text takes 2/3, QR code takes 1/3
        padding = 20
        total_width = max(qr_width + max_text_width + padding * 3, 696)  # Ensure minimum width
        text_area_width = int(total_width * 2/3) - padding * 2
        qr_area_width = total_width - text_area_width - padding * 3

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

        # Calculate text dimensions
        dummy_draw = ImageDraw.Draw(qr_img)
        bbox = dummy_draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Create a new image with space for text
        padding = 20  # Padding between QR code and text

        # Determine layout based on text position
        if text_position == "top":
            # Text above QR code
            new_height = qr_height + text_height + padding
            new_img = Image.new("RGB", (qr_width, new_height), "white")

            # Draw text at the top
            draw = ImageDraw.Draw(new_img)

            # Calculate text position based on alignment
            if text_alignment == "center":
                x = (qr_width - text_width) // 2
            elif text_alignment == "right":
                x = qr_width - text_width - 10
            else:  # left alignment
                x = 10

            y = padding // 2
            draw.text((x, y), text, font=font, fill="black")

            # Paste QR code below text
            new_img.paste(qr_img, (0, text_height + padding))
        else:
            # Text below QR code (default)
            new_height = qr_height + text_height + padding
            new_img = Image.new("RGB", (qr_width, new_height), "white")

            # Paste QR code at the top
            new_img.paste(qr_img, (0, 0))

            # Draw text below QR code
            draw = ImageDraw.Draw(new_img)

            # Calculate text position based on alignment
            if text_alignment == "center":
                x = (qr_width - text_width) // 2
            elif text_alignment == "right":
                x = qr_width - text_width - 10
            else:  # left alignment
                x = 10

            y = qr_height + padding // 2
            draw.text((x, y), text, font=font, fill="black")

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
