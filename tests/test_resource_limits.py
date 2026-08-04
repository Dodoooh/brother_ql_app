"""
Tests for the two upload resource limits.

Both bound how much memory a single request can make the process allocate:

* ``MAX_PDF_PAGES``            -- how many PDF pages one job may rasterise
  (``src/services/pdf_renderer.py``).
* ``MAX_UPLOAD_IMAGE_PIXELS``  -- how many pixels an uploaded image may hold
  (``src/services/printer_service.py``).

The interesting property of both is *when* they act, not just that they act: a
limit that trips after the pages are rendered or after the bitmap is decoded has
already paid the cost it exists to avoid. The tests therefore make the expensive
step blow up if it is ever reached, and assert the rejection happens first.

Both limits are read from the environment on every call, so ``monkeypatch``
setting/deleting the variable is enough; nothing is cached between cases.
"""

import pytest

from src.config.default_settings import (
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_MAX_UPLOAD_IMAGE_PIXELS,
)
from src.services import pdf_renderer
from src.services.pdf_renderer import max_pdf_pages, render_pdf
from src.services.printer_service import (
    guard_image_pixels,
    max_upload_image_pixels,
    printer_service,
)
from src.utils.exceptions import ValidationError

pdfium = pytest.importorskip(
    "pypdfium2", reason="PDF limits need the real renderer")
PIL_Image = pytest.importorskip(
    "PIL.Image", reason="image limits need real Pillow")

# conftest installs a bare PIL stand-in when Pillow is genuinely missing; that
# stub can be imported but cannot make an image, so there is nothing to measure.
if not hasattr(PIL_Image, "new"):  # pragma: no cover - only in a bare env
    pytest.skip("image limits need real Pillow", allow_module_level=True)


# --- Helpers -----------------------------------------------------------------

def _make_pdf(tmp_path, page_count: int, name: str = "doc.pdf") -> str:
    """Write a PDF with ``page_count`` small, empty pages and return its path."""
    document = pdfium.PdfDocument.new()
    for _ in range(page_count):
        # Small pages (points): the tests care about the page *count*, and a
        # small page keeps the allowed cases cheap to actually render.
        document.new_page(72, 72)
    path = str(tmp_path / name)
    document.save(path)
    document.close()
    return path


def _make_image(tmp_path, width: int, height: int, name: str = "img.png") -> str:
    """Write a 1-bit PNG of the given size and return its path.

    Mode "1" keeps even the deliberately huge case cheap to *create* (8000x8000
    is 8 MB here, not 190 MB), which is the whole point: the file on disk is
    small and harmless, and only decoding it is not.
    """
    path = str(tmp_path / name)
    image = PIL_Image.new("1", (width, height), 1)
    image.save(path)
    image.close()
    return path


# --- MAX_PDF_PAGES: reading the setting --------------------------------------

def test_pdf_page_limit_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("MAX_PDF_PAGES", raising=False)
    assert max_pdf_pages() == DEFAULT_MAX_PDF_PAGES


def test_pdf_page_limit_reads_environment(monkeypatch):
    monkeypatch.setenv("MAX_PDF_PAGES", "3")
    assert max_pdf_pages() == 3


def test_pdf_page_limit_tolerates_whitespace(monkeypatch):
    monkeypatch.setenv("MAX_PDF_PAGES", "  7  ")
    assert max_pdf_pages() == 7


@pytest.mark.parametrize("raw", ["abc", "", "   ", "12.5", "-1", "None"])
def test_pdf_page_limit_falls_back_on_nonsense(monkeypatch, raw):
    # A value the app cannot make sense of must never remove the protection --
    # and must never raise on the way to that decision either.
    monkeypatch.setenv("MAX_PDF_PAGES", raw)
    assert max_pdf_pages() == DEFAULT_MAX_PDF_PAGES


def test_pdf_page_limit_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("MAX_PDF_PAGES", "0")
    assert max_pdf_pages() == 0


# --- MAX_PDF_PAGES: what it does to a job ------------------------------------

def test_pdf_over_limit_is_rejected_naming_limit_and_actual(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_PDF_PAGES", "3")
    path = _make_pdf(tmp_path, 5)

    with pytest.raises(ValidationError) as exc:
        render_pdf(path, None, dpi=300)

    message = str(exc.value)
    assert "5" in message and "3" in message
    assert exc.value.details["requested_pages"] == 5
    assert exc.value.details["limit"] == 3
    assert exc.value.details["field"] == "pages"


def test_pdf_over_limit_is_rejected_before_any_page_is_rendered(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_PDF_PAGES", "2")
    path = _make_pdf(tmp_path, 6)

    # A page whose render() is reached at all fails the test loudly: the limit
    # exists precisely so that rasterising never starts.
    def _explode(*_args, **_kwargs):  # pragma: no cover - only on regression
        raise AssertionError("a page was rendered despite the limit")

    monkeypatch.setattr(pdfium.PdfPage, "render", _explode)

    with pytest.raises(ValidationError):
        render_pdf(path, None, dpi=300)


def test_pdf_within_limit_is_rendered(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_PDF_PAGES", "3")
    path = _make_pdf(tmp_path, 3)

    images = render_pdf(path, None, dpi=72)
    assert len(images) == 3


def test_page_selection_is_what_counts_not_the_document(monkeypatch, tmp_path):
    # A long document stays printable through a range: the cost follows the
    # selection, so that is what the limit measures.
    monkeypatch.setenv("MAX_PDF_PAGES", "2")
    path = _make_pdf(tmp_path, 10)

    images = render_pdf(path, "2-3", dpi=72)
    assert len(images) == 2


def test_pdf_default_limit_rejects_the_audited_page_count(monkeypatch, tmp_path):
    # The pentest had a 40-page document accepted; with no environment override
    # at all it must now be turned away.
    monkeypatch.delenv("MAX_PDF_PAGES", raising=False)
    path = _make_pdf(tmp_path, DEFAULT_MAX_PDF_PAGES * 2)

    with pytest.raises(ValidationError) as exc:
        render_pdf(path, None, dpi=72)
    assert str(DEFAULT_MAX_PDF_PAGES) in str(exc.value)


def test_pdf_limit_disabled_renders_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_PDF_PAGES", "0")
    path = _make_pdf(tmp_path, DEFAULT_MAX_PDF_PAGES + 2)

    images = render_pdf(path, None, dpi=72)
    assert len(images) == DEFAULT_MAX_PDF_PAGES + 2


def test_print_pdf_surfaces_the_limit_as_a_validation_error(monkeypatch, tmp_path):
    """The print path must reject (400), not fail (500), and print nothing."""
    monkeypatch.setenv("MAX_PDF_PAGES", "2")
    path = _make_pdf(tmp_path, 5)

    def _never(*_args, **_kwargs):  # pragma: no cover - reached only on regression
        raise AssertionError("_send_to_printer must not be reached")

    monkeypatch.setattr(printer_service, "_send_to_printer", _never)

    with pytest.raises(ValidationError) as exc:
        printer_service.print_pdf(
            path,
            {"printer_uri": "tcp://192.168.1.100",
             "printer_model": "QL-800",
             "label_size": "62"},
        )
    assert "2" in str(exc.value) and "5" in str(exc.value)


# --- MAX_UPLOAD_IMAGE_PIXELS: reading the setting ----------------------------

def test_image_pixel_limit_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    assert max_upload_image_pixels() == DEFAULT_MAX_UPLOAD_IMAGE_PIXELS


def test_image_pixel_limit_reads_environment(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1000")
    assert max_upload_image_pixels() == 1000


@pytest.mark.parametrize("raw", ["abc", "", "   ", "1e6", "-1"])
def test_image_pixel_limit_falls_back_on_nonsense(monkeypatch, raw):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", raw)
    assert max_upload_image_pixels() == DEFAULT_MAX_UPLOAD_IMAGE_PIXELS


def test_image_pixel_limit_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "0")
    assert max_upload_image_pixels() == 0


def test_image_pixel_limit_does_not_read_pillows_name(monkeypatch):
    # Pillow's Image.MAX_IMAGE_PIXELS is a *different* setting with different
    # semantics (a soft warning at the value, a hard error only at twice it).
    # The app's variable deliberately does not share that name, and nothing here
    # may quietly start honouring it either -- one name, one meaning.
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    monkeypatch.setenv("MAX_IMAGE_PIXELS", "7")
    assert max_upload_image_pixels() == DEFAULT_MAX_UPLOAD_IMAGE_PIXELS


# --- MAX_UPLOAD_IMAGE_PIXELS: what it does to an upload ----------------------

def test_small_image_passes(monkeypatch, tmp_path):
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    assert guard_image_pixels(_make_image(tmp_path, 64, 64)) is None


def test_image_over_limit_is_rejected_naming_limit_and_actual(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1000")
    path = _make_image(tmp_path, 64, 64)  # 4096 pixels

    with pytest.raises(ValidationError) as exc:
        guard_image_pixels(path)

    message = str(exc.value)
    assert "4096" in message and "1000" in message and "64x64" in message
    assert exc.value.details["pixels"] == 4096
    assert exc.value.details["limit"] == 1000
    assert exc.value.details["field"] == "image"


def test_image_exactly_at_limit_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "4096")
    assert guard_image_pixels(_make_image(tmp_path, 64, 64)) is None


def test_image_limit_disabled_passes_anything(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "0")
    assert guard_image_pixels(_make_image(tmp_path, 64, 64)) is None


def test_default_limit_rejects_the_audited_image(monkeypatch, tmp_path):
    # The pentest's 8000x8000 PNG (64 MP) sat in Pillow's blind spot between
    # MAX_IMAGE_PIXELS and twice MAX_IMAGE_PIXELS and was decoded in full.
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    path = _make_image(tmp_path, 8000, 8000, name="bomb.png")

    with pytest.raises(ValidationError) as exc:
        guard_image_pixels(path)
    assert "64000000" in str(exc.value)
    assert str(DEFAULT_MAX_UPLOAD_IMAGE_PIXELS) in str(exc.value)


def test_non_image_is_left_to_the_normal_pipeline(monkeypatch, tmp_path):
    # The guard only speaks about size; "this is not an image" is reported by
    # the code that tries to use it, in its own words.
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    path = tmp_path / "not-an-image.png"
    path.write_bytes(b"definitely not a PNG")
    assert guard_image_pixels(str(path)) is None


def test_missing_file_is_left_to_the_normal_pipeline(monkeypatch, tmp_path):
    monkeypatch.delenv("MAX_UPLOAD_IMAGE_PIXELS", raising=False)
    assert guard_image_pixels(str(tmp_path / "gone.png")) is None


def test_print_image_rejects_before_anything_is_decoded(monkeypatch, tmp_path):
    """The print path must reject (400) before the resize walks the bitmap."""
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1000")
    path = _make_image(tmp_path, 64, 64)

    def _never(*_args, **_kwargs):  # pragma: no cover - reached only on regression
        raise AssertionError("the image must not be decoded past the limit")

    monkeypatch.setattr(printer_service, "_resize_image", _never)
    monkeypatch.setattr(printer_service, "_apply_rotation", _never)
    monkeypatch.setattr(printer_service, "_send_to_printer", _never)

    with pytest.raises(ValidationError) as exc:
        printer_service.print_image(
            path,
            {"printer_uri": "tcp://192.168.1.100",
             "printer_model": "QL-800",
             "label_size": "62",
             "rotate": 90},
        )
    assert "1000" in str(exc.value)


def test_image_preview_rejects_before_anything_is_decoded(monkeypatch, tmp_path):
    """The synchronous preview endpoint answers 400 for the same file."""
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1000")
    path = _make_image(tmp_path, 64, 64)

    def _never(*_args, **_kwargs):  # pragma: no cover - reached only on regression
        raise AssertionError("the image must not be decoded past the limit")

    monkeypatch.setattr(printer_service, "_resize_image", _never)

    with pytest.raises(ValidationError):
        printer_service.render_image_preview(
            path, {"label_size": "62", "printer_model": "QL-800"})


def test_text_image_print_rejects_before_anything_is_decoded(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1000")
    path = _make_image(tmp_path, 64, 64)

    def _never(*_args, **_kwargs):  # pragma: no cover - reached only on regression
        raise AssertionError("the image must not be decoded past the limit")

    monkeypatch.setattr(printer_service, "_create_text_image_label", _never)
    monkeypatch.setattr(printer_service, "_send_to_printer", _never)

    with pytest.raises(ValidationError):
        printer_service.print_text_image(
            path, "hello",
            {"printer_uri": "tcp://192.168.1.100",
             "printer_model": "QL-800",
             "label_size": "62"})


def test_pdf_pages_are_not_subject_to_the_upload_pixel_limit(monkeypatch, tmp_path):
    # The pixel limit is about what a *caller uploads*. A rendered PDF page is
    # produced by this app at a DPI it chose, so it is not measured against it
    # -- otherwise a low limit would silently make PDF printing impossible.
    monkeypatch.setenv("MAX_UPLOAD_IMAGE_PIXELS", "1")
    monkeypatch.delenv("MAX_PDF_PAGES", raising=False)
    path = _make_pdf(tmp_path, 1)

    images = render_pdf(path, None, dpi=72)
    assert len(images) == 1


def test_renderer_module_exposes_the_limit_helper():
    # The helper is part of the module's contract (imported by tests and read by
    # operators looking for the knob), so a rename should be a visible change.
    assert callable(pdf_renderer.max_pdf_pages)
