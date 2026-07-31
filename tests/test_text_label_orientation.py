"""
Tests for text label orientation.

``orientation`` decides which way the text runs on the medium:

* ``across`` (the default) keeps the historical behaviour -- the text runs
  across the tape width and the label grows in length as lines are added.
* ``lengthwise`` runs the text along the tape instead, so the roll's printable
  width becomes the line height and the length grows with the message.

The load-bearing property is the rendered *geometry*: ``convert()`` only leaves
an image alone when its width matches the roll's printable width exactly, and
it rejects any die-cut image that is not the label's own size. These tests
therefore assert pixel dimensions and, where it matters, hand the result to the
real ``convert()`` so a regression cannot pass by looking plausible.
"""

import os

import pytest

from src.services.printer_service import (
    MAX_LENGTHWISE_LENGTH_PX,
    PrinterService,
    get_label_geometry,
)
from src.utils.exceptions import ValidationError

Image = pytest.importorskip("PIL.Image")

# Printable widths of the rolls under test, per brother_ql.
WIDTH_12MM = 106  # DK-22214, continuous
SIZE_D24 = 236  # DK-11218, round die-cut


def _find_font():
    """Return a usable TrueType font path, or None if the host has none."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    return next((path for path in candidates if os.path.exists(path)), None)


@pytest.fixture
def service(tmp_path):
    """A PrinterService writing into a temp dir, with a font that exists."""
    svc = PrinterService(upload_folder=str(tmp_path))
    font = _find_font()
    if not font:
        pytest.skip("no TrueType font available on this host")
    svc.font_path = font
    return svc


def _render(service, text, **settings):
    """Render a text label and return the resulting PIL image (loaded eagerly)."""
    settings.setdefault("label_size", "12")
    path = service._create_text_label(text, settings)
    with Image.open(path) as img:
        return img.copy()


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_geometry_of_the_rolls_under_test():
    """Guard the constants the rest of the file leans on."""
    assert get_label_geometry("12") == (WIDTH_12MM, 0, False)
    assert get_label_geometry("d24") == (SIZE_D24, SIZE_D24, True)


def test_lengthwise_renders_at_the_rolls_printable_width(service):
    """The tape width is fixed; only the length may grow with the message."""
    img = _render(service, "A reasonably long maintenance note", orientation="lengthwise")

    assert img.width == WIDTH_12MM
    # The message has to run *somewhere*, and across is the one axis it cannot
    # use -- so a sentence must come out longer than the tape is wide.
    assert img.height > img.width


def test_lengthwise_length_grows_with_the_message(service):
    """Longer text means more tape, not a smaller font on the same strip."""
    short = _render(service, "Rack A1", orientation="lengthwise")
    long = _render(service, "Rack A1 patch panel port 24 uplink", orientation="lengthwise")

    assert long.height > short.height
    assert short.width == long.width == WIDTH_12MM


def test_lengthwise_reads_bottom_to_top(service):
    """
    With the strip held upright the message must read bottom-to-top.

    "MMMM" is far denser than "llll", so the ink of the first word betrays where
    the text begins: after the rotation it has to sit in the bottom half.
    """
    img = _render(service, "MMMM llll", orientation="lengthwise").convert("L")

    def ink(box):
        histogram = img.crop(box).histogram()
        return sum((255 - value) * count for value, count in enumerate(histogram))

    midpoint = img.height // 2
    top_half = ink((0, 0, img.width, midpoint))
    bottom_half = ink((0, midpoint, img.width, img.height))

    assert bottom_half > top_half


def test_explicit_line_breaks_stack_across_the_tape(service):
    """<br> still starts a new line; the stack runs across the tape width."""
    one = _render(service, "Alpha", orientation="lengthwise")
    three = _render(service, "Alpha<br>Beta<br>Gamma", orientation="lengthwise")

    # Both fit the same tape...
    assert one.width == three.width == WIDTH_12MM
    # ...but three lines have to share that width, so auto_fit shrinks the font
    # and the (single-word) lines get shorter rather than the tape wider.
    assert three.height < one.height


# --------------------------------------------------------------------------- #
# Opt-in: the default must not change
# --------------------------------------------------------------------------- #

def test_default_is_across_and_unchanged(service):
    """Omitting orientation renders exactly as an explicit `across` does."""
    text = "Storage shelf B, second row"
    default = _render(service, text)
    across = _render(service, text, orientation="across")

    assert default.size == across.size
    assert default.tobytes() == across.tobytes()


def test_across_and_lengthwise_actually_differ(service):
    """Guard against the setting quietly doing nothing."""
    text = "Storage shelf B, second row"

    assert _render(service, text).size != _render(service, text, orientation="lengthwise").size


def test_unknown_orientation_falls_back_to_across(service):
    """A typo must not silently produce a lengthwise strip."""
    text = "Storage shelf B, second row"

    assert _render(service, text, orientation="sideways").size == _render(service, text).size


# --------------------------------------------------------------------------- #
# Media that has no spare axis
# --------------------------------------------------------------------------- #

def test_die_cut_ignores_lengthwise(service):
    """
    A die-cut label is a fixed size in both directions, so there is no length to
    grow into. The canvas stays pinned to the label or convert() rejects it.
    """
    img = _render(service, "Round label", label_size="d24", orientation="lengthwise")

    assert img.size == (SIZE_D24, SIZE_D24)


# --------------------------------------------------------------------------- #
# The length guard
# --------------------------------------------------------------------------- #

def test_absurdly_long_lengthwise_label_is_refused(service):
    """
    Nothing wraps lengthwise, so a long enough message would unspool metres of
    tape. That must fail loudly, and as a client error rather than a 500.
    """
    with pytest.raises(ValidationError):
        _render(service, "unspool " * 400, orientation="lengthwise", auto_fit=False, font_size=100)


def test_length_guard_leaves_ordinary_labels_alone(service):
    """The cap is a backstop, not something a real label should ever meet."""
    img = _render(service, "Patch panel port 24", orientation="lengthwise")

    assert img.height < MAX_LENGTHWISE_LENGTH_PX


# --------------------------------------------------------------------------- #
# End to end: the printer library has to accept what we render
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "label_size, orientation",
    [
        ("12", "lengthwise"),
        ("12", "across"),
        ("62", "lengthwise"),
        ("d24", "lengthwise"),
    ],
)
def test_rendered_label_is_accepted_by_convert(service, label_size, orientation):
    """
    The real proof: brother_ql resizes an endless image whose width does not
    match the roll (silently changing the effective font size) and refuses a
    die-cut one outright. Feeding convert() the actual render catches both.
    """
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster

    img = _render(service, "Cable run 12", label_size=label_size, orientation=orientation)
    width_before = img.width

    convert(raster("QL-820NWB"), [img], label_size, rotate=0)

    # convert() only leaves the width alone when it already matches the roll.
    assert img.width == width_before == get_label_geometry(label_size)[0]
