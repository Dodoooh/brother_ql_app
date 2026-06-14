"""
Controller for the generic /share upload endpoint.

This lets a phone "share" a file (PDF or image) into the app via Apple
Shortcuts / Android HTTP Shortcuts. The uploaded file is staged under a
random token, and the user is redirected to the web UI which then loads the
file into the print dialog via the ?share= query parameter.
"""

import os
import re
import time
import uuid
import structlog
from typing import Optional

from flask import request, current_app, redirect, send_from_directory, abort

from src.services.printer_service import printer_service
from src.utils.exceptions import ValidationError

logger = structlog.get_logger()

# Leading magic bytes used to classify uploads.
_PDF_MAGIC = b"%PDF"
_IMAGE_MAGICS = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"GIF87a",                # GIF
    b"GIF89a",                # GIF
    b"BM",                    # BMP
)

# A staged token is a hex string (uuid4().hex), optionally followed by a file
# extension. Strictly hex-only to prevent path traversal.
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]+(\.[0-9a-zA-Z]+)?$")

# Default time-to-live for staged share files (1 hour).
_DEFAULT_SHARE_TTL_SECONDS = 3600


def share():
    """
    Accept a shared file and stage it for the web UI.

    Returns:
        A 302 redirect to /?share=<token>&type=<pdf|image>.
    """
    logger.info("Processing share upload")

    # Opportunistically clean up expired staged files on every share activity.
    _sweep_expired_shares()

    if 'file' not in request.files:
        raise ValidationError("No file provided", "file")

    shared_file = request.files['file']
    if shared_file.filename == '':
        raise ValidationError("No file selected", "file")

    file_type = _detect_type(shared_file)
    if file_type is None:
        raise ValidationError("Unsupported file type (expected PDF or image)", "file")

    # Stage the file under a random token name (hex only, no traversal risk).
    extension = ".pdf" if file_type == "pdf" else _image_extension(shared_file)
    token = f"{uuid.uuid4().hex}{extension}"

    share_folder = _get_share_folder()
    os.makedirs(share_folder, exist_ok=True)
    shared_file.save(os.path.join(share_folder, token))

    logger.info("Staged shared file", token=token, type=file_type)

    return redirect(f"/?share={token}&type={file_type}", code=302)


def get_shared(token: str):
    """
    Serve a previously staged file by its token.

    Args:
        token: The staging token (hex, optionally with a file extension).

    Returns:
        The staged file as a binary download, or a 404 if it does not exist.
    """
    # Opportunistically clean up expired staged files on every share activity.
    _sweep_expired_shares()

    # Strictly validate the token to prevent path traversal. Only hex
    # characters and a simple alphanumeric extension are allowed.
    if not token or not _TOKEN_RE.match(token):
        logger.warning("Rejected invalid share token", token=token)
        abort(404)

    share_folder = _get_share_folder()
    file_path = os.path.join(share_folder, token)

    # Defence in depth: ensure the resolved path stays inside the share folder.
    real_folder = os.path.realpath(share_folder)
    real_path = os.path.realpath(file_path)
    if os.path.commonpath([real_folder, real_path]) != real_folder:
        logger.warning("Rejected share token escaping share folder", token=token)
        abort(404)

    if not os.path.isfile(real_path):
        abort(404)

    mimetype = "application/pdf" if token.lower().endswith(".pdf") else "application/octet-stream"
    return send_from_directory(
        share_folder, token, mimetype=mimetype, as_attachment=False
    )


def _detect_type(file) -> Optional[str]:
    """
    Classify an upload as "pdf" or "image" via content-type, magic bytes or
    extension. Returns None when it is neither.
    """
    name = (file.filename or "").lower()
    content_type = (file.content_type or "").lower()

    # Magic-byte sniffing is the most reliable signal.
    try:
        head = file.stream.read(16)
        file.stream.seek(0)
    except (OSError, ValueError):
        head = b""

    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if any(head.startswith(magic) for magic in _IMAGE_MAGICS):
        return "image"

    if content_type == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if content_type.startswith("image/"):
        return "image"
    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif")):
        return "image"

    return None


def _image_extension(file) -> str:
    """Pick a safe image extension based on the original filename."""
    name = (file.filename or "").lower()
    ext = os.path.splitext(name)[1]
    # Only allow a short, purely alphanumeric extension; otherwise fall back.
    if ext and re.match(r"^\.[0-9a-z]{1,5}$", ext):
        return ext
    return ".png"


def _get_upload_folder() -> str:
    """Return the configured upload folder, falling back to the default."""
    try:
        upload_folder = current_app.config.get('UPLOAD_FOLDER')
    except RuntimeError:
        upload_folder = None
    if not upload_folder:
        upload_folder = getattr(printer_service, "upload_folder", None)
    if not upload_folder:
        upload_folder = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads"
        )
    return upload_folder


def _get_share_folder() -> str:
    """
    Return the dedicated subfolder for staged share files.

    Share files live in ``uploads/shared/`` so they are clearly separated from
    regular print uploads and can be safely swept by TTL.
    """
    return os.path.join(_get_upload_folder(), "shared")


def _share_ttl_seconds() -> int:
    """Return the staged-file TTL from SHARE_TTL_SECONDS (default 3600)."""
    raw = os.environ.get("SHARE_TTL_SECONDS", str(_DEFAULT_SHARE_TTL_SECONDS))
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SHARE_TTL_SECONDS
    return ttl if ttl > 0 else _DEFAULT_SHARE_TTL_SECONDS


def _sweep_expired_shares() -> None:
    """
    Delete staged share files older than the configured TTL.

    Best-effort cleanup invoked on every share activity (staging and serving).
    Per-file errors are logged and swallowed so a failed cleanup can never break
    the request.
    """
    share_folder = _get_share_folder()
    ttl = _share_ttl_seconds()
    cutoff = time.time() - ttl

    try:
        entries = os.listdir(share_folder)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not list share folder for cleanup", error=str(exc))
        return

    for name in entries:
        path = os.path.join(share_folder, name)
        try:
            if not os.path.isfile(path):
                continue
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info("Removed expired shared file", name=name)
        except OSError as exc:
            logger.warning("Failed to remove expired shared file", name=name, error=str(exc))
