"""
Printer service for managing Brother QL printer operations.
"""

import os
import uuid
import structlog
import threading
import time
import socket
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageOps
import qrcode
from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends import backend_factory

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
from src.utils.exceptions import PrinterError, ImageProcessingError

logger = structlog.get_logger()

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
        if upload_folder is None:
            self.upload_folder = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "uploads"
            )
        else:
            self.upload_folder = upload_folder
        
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
        try:
            # Create a test connection to the printer
            backend = backend_factory("network")["backend_class"](printer_uri)
            
            # Try to get printer status (implementation depends on printer capabilities)
            # For now, we just check if we can establish a connection
            is_available = True
            status_message = "Printer is ready"
            
            # Close the connection
            backend.dispose()
            
            return {
                "available": is_available,
                "status": status_message,
                "details": {
                    "printer_uri": printer_uri,
                    "printer_model": printer_model
                }
            }
        except Exception as e:
            logger.error("Error checking printer status", 
                        printer_uri=printer_uri, 
                        printer_model=printer_model,
                        error=str(e),
                        exc_info=True)
            
            return {
                "available": False,
                "status": f"Printer error: {str(e)}",
                "details": {
                    "printer_uri": printer_uri,
                    "printer_model": printer_model,
                    "error": str(e)
                }
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
        try:
            # Generate a unique job ID
            job_id = f"text_{uuid.uuid4().hex[:8]}"
            
            logger.info("Processing text print request", job_id=job_id, text_length=len(text))
            
            # Create label image
            image_path = self._create_text_label(text, settings)
            logger.info("Text label created", job_id=job_id, image_path=image_path)
            
            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)
            
            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)
            
            return {
                "success": True,
                "job_id": job_id,
                "message": "Text printed successfully"
            }
        except Exception as e:
            logger.error("Error printing text", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing text: {str(e)}")
    
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
        try:
            # Generate a unique job ID
            job_id = f"image_{uuid.uuid4().hex[:8]}"
            
            logger.info("Processing image print request", job_id=job_id, image_path=image_path)
            
            # Resize image to fit label width
            resized_path = self._resize_image(image_path)
            logger.info("Image resized", job_id=job_id, resized_path=resized_path)
            
            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                resized_path = self._apply_rotation(resized_path, rotate)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)
            
            # Send to printer
            self._send_to_printer(resized_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)
            
            return {
                "success": True,
                "job_id": job_id,
                "message": "Image printed successfully"
            }
        except Exception as e:
            logger.error("Error printing image", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing image: {str(e)}")
    
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
        try:
            # Extract settings
            printer_uri = settings.get("printer_uri")
            printer_model = settings.get("printer_model")
            label_size = settings.get("label_size")
            rotate = settings.get("rotate", 0)
            threshold = float(settings.get("threshold", 70.0))
            dither = settings.get("dither", False)
            compress = settings.get("compress", False)
            red = settings.get("red", False)
            
            # Validate required settings
            if not printer_uri:
                raise ValueError("printer_uri is required")
            if not printer_model:
                raise ValueError("printer_model is required")
            if not label_size:
                raise ValueError("label_size is required")
            
            # Create rasterizer
            qlr = BrotherQLRaster(printer_model)
            qlr.exception_on_warning = True
            
            # Convert image to printer instructions
            instructions = convert(
                qlr=qlr,
                images=[image_path],
                label=label_size,
                rotate=rotate,
                threshold=threshold,
                dither=dither,
                compress=compress,
                red=red,
            )
            
            # Send to printer
            backend = backend_factory("network")["backend_class"](printer_uri)
            backend.write(instructions)
            backend.dispose()
            
            logger.info("Print job sent to printer", 
                       printer_uri=printer_uri, 
                       printer_model=printer_model,
                       label_size=label_size)
        except Exception as e:
            logger.error("Error sending to printer", error=str(e), exc_info=True)
            raise PrinterError(f"Error sending to printer: {str(e)}")

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
        try:
            # Generate a unique job ID
            job_id = f"qrcode_{uuid.uuid4().hex[:8]}"
            
            logger.info("Processing QR code print request", job_id=job_id, data_length=len(data))
            
            # Create QR code image
            image_path = self._create_qr_code(data, settings)
            logger.info("QR code created", job_id=job_id, image_path=image_path)
            
            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)
            
            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)
            
            return {
                "success": True,
                "job_id": job_id,
                "message": "QR code printed successfully"
            }
        except Exception as e:
            logger.error("Error printing QR code", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing QR code: {str(e)}")
    
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
            # Extract QR code settings
            show_text = settings.get("show_text", False)
            text = settings.get("text", data)  # Use data as default text if not provided
            qr_version = settings.get("qr_version", 1)  # QR code version (1-40)
            qr_box_size = settings.get("qr_box_size", 10)  # Size of each box in pixels
            qr_border = settings.get("qr_border", 4)  # Border size in boxes
            error_correction = settings.get("error_correction", "M")  # L, M, Q, H
            qr_size = settings.get("qr_size", 400)  # Overall QR code size in pixels
            
            # Text settings
            text_position = settings.get("text_position", "bottom")  # Position of text: "top", "bottom", or "none"
            text_alignment = settings.get("text_alignment", "center")  # Text alignment: "left", "center", or "right"
            
            # Layout settings
            side_by_side = settings.get("side_by_side", False)  # Whether to show text and QR code side by side
            side_text = settings.get("side_text", "")  # Text to show on the side
            qr_position = settings.get("qr_position", "right")  # Position of QR code: "left" or "right"
            
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
            
            # Check if we should use side-by-side layout
            if side_by_side and side_text:
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
                
                qr_img = new_img
                
            # If text should be shown with the QR code (only if not using side-by-side)
            elif show_text and text:
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
                
                qr_img = new_img
            
            # Save QR code image
            image_path = os.path.join(self.upload_folder, f"qrcode_{uuid.uuid4().hex[:8]}.png")
            qr_img.save(image_path)
            
            return image_path
        except Exception as e:
            logger.error("Error creating QR code", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating QR code: {str(e)}")
    
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
                
                logger.debug("Sending keep alive ping", ip_address=ip_address)
                
                # Try SNMP ping first (for all addresses including host.docker.internal)
                if self._snmp_ping(ip_address):
                    logger.debug("SNMP keep alive ping successful", ip_address=ip_address)
                    consecutive_failures = 0
                # Fall back to TCP ping if SNMP fails
                elif self._tcp_ping(ip_address):
                    logger.debug("TCP keep alive ping successful", ip_address=ip_address)
                    consecutive_failures = 0
                # If both fail, try the original method with the full printer_uri
                else:
                    logger.debug("Falling back to original keep alive method", printer_uri=printer_uri)
                    try:
                        # Create a connection to the printer using the original URI
                        # This is important for Docker environments where host.docker.internal
                        # might be used to access the host network
                        backend = backend_factory("network")["backend_class"](printer_uri)
                        
                        # Just establishing and closing a connection might be enough
                        logger.debug("Connection established as keep-alive ping", printer_uri=printer_uri)
                        
                        # Close the connection
                        backend.dispose()
                        consecutive_failures = 0
                    except Exception as backend_error:
                        logger.warning("Original keep alive method failed", 
                                   printer_uri=printer_uri,
                                   error=str(backend_error))
                        # Don't raise the error, just increment the failure counter
                        consecutive_failures += 1
                        continue
                
                logger.debug("Keep alive ping successful", printer_uri=printer_uri)
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
