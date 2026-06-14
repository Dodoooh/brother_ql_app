"""
PDF rendering helpers for the Brother QL label app.

Turns PDF pages into PIL images (one per page) at a chosen DPI using
``pypdfium2`` -- a self-contained renderer whose PDFium binary ships inside the
wheel, so there are no system dependencies to install in the Docker image.

The two public helpers (:func:`parse_page_range` and :func:`render_pdf`) are the
contract consumed by ``PrinterService.print_pdf``.
"""

import base64
import io

import structlog

logger = structlog.get_logger()

# Import pypdfium2 defensively: if the wheel is somehow missing we still want the
# module to import (so the rest of the app keeps working) and only fail with a
# clear message at call time.
try:
    import pypdfium2 as pdfium
    PDFIUM_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dep is absent
    pdfium = None
    PDFIUM_AVAILABLE = False
    logger.warning("pypdfium2 not available, PDF printing will not work")


def parse_page_range(spec, total: int) -> list:
    """
    Parse a 1-based page-range string into a sorted, deduplicated list of page
    numbers within ``[1, total]``.

    Examples (with ``total`` large enough)::

        parse_page_range("1-3,5", 10) -> [1, 2, 3, 5]
        parse_page_range("", 4)       -> [1, 2, 3, 4]
        parse_page_range("all", 4)    -> [1, 2, 3, 4]

    Args:
        spec: Range string such as ``"1-3,5"``. An empty string, ``None`` or the
            literal ``"all"`` selects every page ``[1..total]``.
        total: Total number of pages in the document (must be >= 1).

    Returns:
        Sorted list of unique 1-based page numbers.

    Raises:
        ValueError: For non-positive ``total`` or any malformed/out-of-range
            token (non-numeric, ``0``, ``> total`` or a reversed ``b-a`` range).
    """
    if not isinstance(total, int) or total < 1:
        raise ValueError(f"total must be a positive integer, got {total!r}")

    # Empty / None / "all" -> every page.
    if spec is None:
        return list(range(1, total + 1))
    spec = str(spec).strip()
    if spec == "" or spec.lower() == "all":
        return list(range(1, total + 1))

    pages = set()
    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            # Tolerate stray/trailing commas (e.g. "1,,3" or "1,").
            continue

        if "-" in token:
            parts = token.split("-")
            if len(parts) != 2:
                raise ValueError(f"Invalid page range token: {raw_token!r}")
            start_str, end_str = parts[0].strip(), parts[1].strip()
            start = _parse_page_number(start_str, total, raw_token)
            end = _parse_page_number(end_str, total, raw_token)
            if start > end:
                raise ValueError(
                    f"Invalid page range {raw_token!r}: start {start} is "
                    f"greater than end {end}"
                )
            pages.update(range(start, end + 1))
        else:
            pages.add(_parse_page_number(token, total, raw_token))

    if not pages:
        # Only stray separators were supplied (e.g. "," or " ").
        raise ValueError(f"No valid pages found in range spec: {spec!r}")

    return sorted(pages)


def _parse_page_number(value: str, total: int, original_token: str) -> int:
    """Parse and bounds-check a single 1-based page number."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid page number {value!r} in token {original_token!r} "
            f"(must be an integer)"
        )
    if number < 1:
        raise ValueError(
            f"Page number {number} in token {original_token!r} must be >= 1"
        )
    if number > total:
        raise ValueError(
            f"Page number {number} in token {original_token!r} exceeds the "
            f"document's {total} page(s)"
        )
    return number


def render_pdf(pdf_path: str, pages=None, dpi: int = 300) -> list:
    """
    Render selected pages of a PDF into PIL images.

    Args:
        pdf_path: Path to the PDF file.
        pages: 1-based page-range spec accepted by :func:`parse_page_range`
            (string like ``"1-3,5"``, empty string, ``None`` or ``"all"`` ->
            every page).
        dpi: Render resolution. 300 is the native Brother QL print resolution;
            pass 600 for the high-resolution (``dpi_600``) mode.

    Returns:
        List of ``PIL.Image.Image`` objects, one per selected page (in order).

    Raises:
        ValueError: If pypdfium2 is unavailable, the page spec is invalid, or
            the file cannot be opened/read as a PDF.
    """
    if not PDFIUM_AVAILABLE:
        raise ValueError(
            "PDF rendering is unavailable: the 'pypdfium2' package is not "
            "installed"
        )

    pdf = None
    try:
        try:
            pdf = pdfium.PdfDocument(pdf_path)
            total = len(pdf)
        except Exception as e:
            # Not a PDF, corrupt, password-protected or unreadable file.
            raise ValueError(f"Could not open PDF {pdf_path!r}: {e}") from e

        if total < 1:
            raise ValueError(f"PDF {pdf_path!r} contains no pages")

        # parse_page_range may raise ValueError for a bad spec -> propagate.
        page_numbers = parse_page_range(pages, total)

        scale = dpi / 72.0
        images = []
        for page_number in page_numbers:
            page = pdf[page_number - 1]  # parse_page_range is 1-based
            pil_image = page.render(scale=scale).to_pil()
            images.append(pil_image)

        logger.info("Rendered PDF pages",
                    pdf_path=pdf_path,
                    pages=page_numbers,
                    dpi=dpi,
                    rendered=len(images))
        return images
    finally:
        if pdf is not None:
            pdf.close()


def render_pdf_thumbnails(pdf_path: str, pages=None, dpi: int = 120,
                          max_pages: int = 12) -> dict:
    """
    Render selected pages of a PDF into small PNG thumbnails (data URLs).

    Intended for a fast, server-side *preview* (not printing): it renders at a
    low DPI and caps the number of rendered pages so previewing a large PDF
    stays cheap.

    Args:
        pdf_path: Path to the PDF file.
        pages: 1-based page-range spec accepted by :func:`parse_page_range`
            (string like ``"1-3,5"``, empty string, ``None`` or ``"all"`` ->
            every page).
        dpi: Render resolution for the thumbnails. Deliberately low (default
            120) to keep previews fast and small.
        max_pages: Maximum number of pages to render. If the selection holds
            more pages, only the first ``max_pages`` are rendered and
            ``truncated`` is set to ``True``.

    Returns:
        Dict with keys:
            - ``total_pages`` (int): total page count of the document.
            - ``rendered_pages`` (list[int]): the 1-based pages actually
              rendered (in order).
            - ``truncated`` (bool): whether the selection was capped to
              ``max_pages``.
            - ``previews`` (list[dict]): one entry per rendered page,
              ``{"page": <int>, "image": "data:image/png;base64,..."}``.

    Raises:
        ValueError: If pypdfium2 is unavailable, the page spec is invalid, or
            the file cannot be opened/read as a PDF.
    """
    if not PDFIUM_AVAILABLE:
        raise ValueError(
            "PDF rendering is unavailable: the 'pypdfium2' package is not "
            "installed"
        )

    pdf = None
    try:
        try:
            pdf = pdfium.PdfDocument(pdf_path)
            total = len(pdf)
        except Exception as e:
            # Not a PDF, corrupt, password-protected or unreadable file.
            raise ValueError(f"Could not open PDF {pdf_path!r}: {e}") from e

        if total < 1:
            raise ValueError(f"PDF {pdf_path!r} contains no pages")

        # parse_page_range may raise ValueError for a bad spec -> propagate.
        selected = parse_page_range(pages, total)

        truncated = len(selected) > max_pages
        rendered_pages = selected[:max_pages]

        scale = dpi / 72.0
        previews = []
        for page_number in rendered_pages:
            page = pdf[page_number - 1]  # parse_page_range is 1-based
            pil_image = page.render(scale=scale).to_pil()

            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            previews.append({
                "page": page_number,
                "image": f"data:image/png;base64,{encoded}",
            })

        logger.info("Rendered PDF thumbnails",
                    pdf_path=pdf_path,
                    total_pages=total,
                    rendered=len(previews),
                    dpi=dpi,
                    truncated=truncated)

        return {
            "total_pages": total,
            "rendered_pages": rendered_pages,
            "truncated": truncated,
            "previews": previews,
        }
    finally:
        if pdf is not None:
            pdf.close()
