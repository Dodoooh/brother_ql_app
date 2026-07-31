"""
Tests for die-cut label rendering, round media in particular.

A die-cut label is a fixed piece of paper and ``convert()`` refuses any image
that is not exactly its printable size, so "it looked right in the preview" is
worth nothing here. Round media adds a second trap: the printable area is
reported as a square but is really the inscribed *circle*, so content in the
corners is printed onto paper the user never gets -- it stays on the backing.

These tests therefore assert three separate things:

* the rendered image has the medium's exact pixel size (and the real
  ``convert()`` is handed the result, which is the only authority on that),
* on round media no ink lands outside the circle,
* continuous ("endless") tape still produces byte-identical printer
  instructions, because that is what people are printing on today.
"""

import math
import os

import pytest

from src.services.printer_service import (
    PrinterService,
    get_label_geometry,
    get_round_line_widths,
    get_round_safe_radius,
)

Image = pytest.importorskip("PIL.Image")

# Printable geometry of the media under test, per brother_ql.
WIDTH_12MM = 106  # DK-22214, continuous
WIDTH_62MM = 696  # DK-22205, continuous
SIZE_D12 = 94  # DK-11219, 12 mm round die-cut
SIZE_D24 = 236  # DK-11218, 24 mm round die-cut
SIZE_62X29 = (696, 271)  # DK-11209, rectangular die-cut

ROUND_LABELS = ("d12", "d24", "d58")


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


@pytest.fixture
def photo(tmp_path):
    """A wide, non-square source image (the shape that used to break die-cut)."""
    path = tmp_path / "photo.png"
    img = Image.new("RGB", (800, 300), "white")
    draw = pytest.importorskip("PIL.ImageDraw").Draw(img)
    draw.rectangle((20, 20, 780, 280), fill="black")
    img.save(path)
    return str(path)


@pytest.fixture
def drawn_lines(monkeypatch):
    """Collect the exact strings the renderer draws, in drawing order.

    Pixel dimensions cannot tell "Kalibriert" from "Kalibrier" + "t", so the
    line content has to be asserted on directly.
    """
    module = pytest.importorskip("PIL.ImageDraw")
    captured = []
    original = module.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        captured.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(module.ImageDraw, "text", spy)
    return captured


def _open(path):
    """Load a rendered label eagerly so the file can be removed afterwards."""
    with Image.open(path) as img:
        return img.copy()


def _accept(img, label_size):
    """Hand a rendered label to the real convert(); fails loudly if rejected."""
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    return convert(raster("QL-820NWB"), [img], label_size, rotate=0)


def _ink_outside_circle(img):
    """Return how many black pixels fall outside the label's printable circle."""
    gray = img.convert("L")
    radius = gray.width / 2.0
    centre = (gray.width - 1) / 2.0
    pixels = gray.load()
    outside = 0
    for y in range(gray.height):
        for x in range(gray.width):
            if pixels[x, y] < 128 and math.hypot(x - centre, y - centre) > radius:
                outside += 1
    return outside


def _ink_bbox(img):
    """Bounding box of the black pixels, or None when the label is blank."""
    gray = img.convert("L")
    return gray.point(lambda p: 255 if p < 128 else 0).getbbox()


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_round_media_is_reported_as_round():
    """Round and rectangular die-cut media must be distinguishable."""
    round_24 = get_label_geometry("d24")

    assert round_24.is_die_cut is True
    assert round_24.is_round is True
    assert (round_24.width, round_24.height) == (SIZE_D24, SIZE_D24)


def test_rectangular_die_cut_is_not_round():
    """62x29 is a fixed size, but its printable area really is the rectangle."""
    rectangular = get_label_geometry("62x29")

    assert rectangular.is_die_cut is True
    assert rectangular.is_round is False
    assert (rectangular.width, rectangular.height) == SIZE_62X29


def test_geometry_still_unpacks_as_the_old_three_tuple():
    """Existing callers unpack three values; that contract has to keep working."""
    width, height, is_die_cut = get_label_geometry("d24")

    assert (width, height, is_die_cut) == (SIZE_D24, SIZE_D24, True)
    assert get_label_geometry("12") == (WIDTH_12MM, 0, False)
    assert get_label_geometry("62x29") == (696, 271, True)


def test_unknown_label_keeps_falling_back_to_62_mm():
    """Unknown media behaved as 62 mm continuous before and must continue to."""
    fallback = get_label_geometry("not-a-real-label")

    assert fallback == (WIDTH_62MM, 0, False)
    assert fallback.is_round is False


def test_a_single_line_gets_more_than_the_inscribed_square():
    """
    The reason the chord is computed per line at all.

    An inscribed square would hand every line ``diameter / sqrt(2)`` regardless
    of how tall it is -- about 70 % of the label. One centred line barely
    reaches away from the middle, where the circle is at its widest, so it gets
    to be substantially bigger than that.
    """
    radius = get_round_safe_radius(SIZE_D24)
    inscribed_square_side = 2 * radius / math.sqrt(2)

    single_line = get_round_line_widths(radius, 1, 40)[0]

    assert single_line > inscribed_square_side
    assert single_line <= 2 * radius


def test_line_widths_narrow_towards_the_rim():
    """The outer lines of a stack have to be shorter than the middle ones."""
    widths = get_round_line_widths(get_round_safe_radius(SIZE_D24), 5, 40)

    assert widths[0] == widths[-1]  # symmetric about the centre
    assert widths[0] < widths[1] < widths[2]
    assert len(widths) == 5


def test_lines_pushed_off_the_label_get_no_width():
    """A stack taller than the label reports 0 rather than a negative width."""
    widths = get_round_line_widths(get_round_safe_radius(SIZE_D12), 20, 30)

    assert widths[0] == 0
    assert min(widths) == 0


# --------------------------------------------------------------------------- #
# The shared fit
# --------------------------------------------------------------------------- #

def test_fit_scales_continuous_tape_to_the_printable_width(service):
    """Continuous tape fixes the width only; the length follows the content."""
    fitted = service._fit_to_label(Image.new("RGB", (400, 200), "white"), "62")

    assert fitted.size == (WIDTH_62MM, 348)


def test_fit_leaves_a_correctly_sized_continuous_label_alone(service):
    """No needless resample of an image that already matches the roll."""
    original = Image.new("RGB", (WIDTH_12MM, 40), "white")

    assert service._fit_to_label(original, "12") is original


def test_fit_pins_die_cut_media_to_the_label(service):
    """convert() rejects any other size, so the canvas is not negotiable."""
    for size in [(400, 400), (2000, 100), (30, 700), SIZE_62X29]:
        fitted = service._fit_to_label(Image.new("RGB", size, "white"), "62x29")
        assert fitted.size == SIZE_62X29


def test_fit_never_distorts_the_content(service):
    """Scaling is uniform: a circle must not come out as an ellipse."""
    source = Image.new("RGB", (200, 100), "white")
    pytest.importorskip("PIL.ImageDraw").Draw(source).rectangle((0, 0, 199, 99), fill="black")

    box = _ink_bbox(service._fit_to_label(source, "d24"))
    width = box[2] - box[0]
    height = box[3] - box[1]

    assert width / height == pytest.approx(2.0, abs=0.05)


def test_fit_keeps_content_inside_the_circle(service):
    """A rectangle fits a circle exactly when its half-diagonal fits the radius."""
    source = Image.new("RGB", (800, 300), "white")
    pytest.importorskip("PIL.ImageDraw").Draw(source).rectangle((0, 0, 799, 299), fill="black")

    fitted = service._fit_to_label(source, "d24")

    assert fitted.size == (SIZE_D24, SIZE_D24)
    assert _ink_outside_circle(fitted) == 0


def test_fit_uses_more_than_the_inscribed_square_for_a_wide_block(service):
    """
    A short, wide block is not a square and must not be shrunk to one.

    This is the pay-off of fitting by the diagonal: a caption strip keeps
    roughly the full diameter instead of losing 30 % of it for nothing.
    """
    source = Image.new("RGB", (800, 100), "white")
    pytest.importorskip("PIL.ImageDraw").Draw(source).rectangle((0, 0, 799, 99), fill="black")

    box = _ink_bbox(service._fit_to_label(source, "d24"))

    assert (box[2] - box[0]) > 2 * get_round_safe_radius(SIZE_D24) / math.sqrt(2)


# --------------------------------------------------------------------------- #
# Text on round media
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size, diameter", [("d12", SIZE_D12), ("d24", SIZE_D24)])
def test_round_text_label_is_exactly_the_label(service, label_size, diameter):
    img = _open(service._create_text_label("Milk", {"label_size": label_size}))

    assert img.size == (diameter, diameter)


@pytest.mark.parametrize("text", [
    "Milk",
    "Bay<br>leaf",
    "Apfelsaft<br>naturtrueb<br>2026-08",
    "Sourdough starter fed on 3 August, keep refrigerated",
    "unbreakablewordthatismuchwiderthanthelabelitself",
])
def test_round_text_never_prints_outside_the_circle(service, text):
    """Ink in the corners is ink on the backing paper, not on the label."""
    img = _open(service._create_text_label(text, {"label_size": "d24"}))

    assert _ink_outside_circle(img) == 0


@pytest.mark.parametrize("alignment", ["left", "center", "right"])
def test_round_text_stays_inside_the_circle_for_every_alignment(service, alignment):
    """Alignment is relative to the line's chord, not to the bounding square."""
    img = _open(service._create_text_label(
        "Apfelsaft<br>naturtrueb<br>2026-08",
        {"label_size": "d24", "alignment": alignment},
    ))

    assert _ink_outside_circle(img) == 0


def test_round_text_is_centred_on_the_label(service):
    """
    A top-aligned block starts where the circle is narrowest, so the first line
    is exactly the one that gets cut off. The stack has to sit in the middle.
    """
    img = _open(service._create_text_label("Milk", {"label_size": "d24"}))
    box = _ink_bbox(img)

    assert box is not None
    assert (box[1] + box[3]) / 2 == pytest.approx(SIZE_D24 / 2, abs=6)


def test_round_text_uses_the_width_it_is_given(service):
    """
    Regression guard for the cheap fix: retreating to the inscribed square would
    make a single centred line noticeably smaller for no reason.
    """
    img = _open(service._create_text_label(
        "Bratapfel", {"label_size": "d24", "alignment": "center"}))
    box = _ink_bbox(img)

    assert (box[2] - box[0]) > 2 * get_round_safe_radius(SIZE_D24) / math.sqrt(2)
    assert _ink_outside_circle(img) == 0


def test_round_text_wraps_instead_of_overflowing(service):
    """Long text turns into several lines rather than one clipped one."""
    img = _open(service._create_text_label(
        "Sourdough starter fed on 3 August, keep refrigerated",
        {"label_size": "d24"},
    ))
    box = _ink_bbox(img)

    # Several lines means the block is tall, not a single wide strip.
    assert (box[3] - box[1]) > (SIZE_D24 / 4)
    assert _ink_outside_circle(img) == 0


def test_round_text_survives_wrapping_and_auto_fit_being_off(service):
    """Even with every fitting aid disabled the label must remain printable."""
    img = _open(service._create_text_label(
        "Refrigerate after opening",
        {"label_size": "d24", "text_wrap": False, "auto_fit": False, "font_size": 60},
    ))

    assert img.size == (SIZE_D24, SIZE_D24)
    _accept(img, "d24")


# --------------------------------------------------------------------------- #
# Auto-fit must shrink the font rather than chop a word in half
#
# Hard-breaking satisfies every width test trivially, so a fitting rule phrased
# only as "the lines are short enough" terminates at a large font with wreckage
# like "Kalibrier / t 2026" -- while the same text fits one unbroken line a
# couple of steps further down. This is the trade a narrow continuous roll
# already makes; fixed-size media has to make it too.
# --------------------------------------------------------------------------- #

def test_wrapping_broke_a_word_recognises_a_split_word():
    """The predicate the fitting rule is built on."""
    assert PrinterService._wrapping_broke_a_word(["Kalibriert 2026"], ["Kalibrier", "t 2026"])
    assert not PrinterService._wrapping_broke_a_word(["Kalibriert 2026"], ["Kalibriert", "2026"])
    assert not PrinterService._wrapping_broke_a_word(["Milk"], ["Milk"])


def test_round_label_shrinks_rather_than_breaking_a_word(service, drawn_lines):
    """The reported repro: "Kalibriert" must not come out as "Kalibrier" + "t"."""
    service._create_text_label(
        "Kalibriert 2026", {"label_size": "d24", "font_size": 50, "alignment": "center"})

    assert " ".join(drawn_lines).split() == ["Kalibriert", "2026"]


def test_round_label_keeps_words_whole_across_explicit_breaks(service, drawn_lines):
    """<br> starts a new line; it must not license breaking the words either."""
    service._create_text_label(
        "Wartung faellig<br>Maerz 2027", {"label_size": "d24", "alignment": "center"})

    assert " ".join(drawn_lines).split() == ["Wartung", "faellig", "Maerz", "2027"]


@pytest.mark.parametrize("label_size", ROUND_LABELS)
def test_round_labels_of_every_size_keep_words_whole(service, drawn_lines, label_size):
    service._create_text_label(
        "Kalibriert 2026", {"label_size": label_size, "alignment": "center"})

    assert " ".join(drawn_lines).split() == ["Kalibriert", "2026"]


@pytest.mark.parametrize("font_size", [150, 90, 50])
def test_rectangular_die_cut_shrinks_rather_than_breaking_a_word(
        service, drawn_lines, font_size):
    """
    62x29 had the same defect: its auto-fit asked only whether the stack fit the
    label's height, which chopping a word satisfies just as well.
    """
    service._create_text_label(
        "Kalibrierungsetikett 2026", {"label_size": "62x29", "font_size": font_size})

    assert " ".join(drawn_lines).split() == ["Kalibrierungsetikett", "2026"]


@pytest.mark.parametrize("label_size", ["12", "62"])
def test_continuous_tape_still_keeps_words_whole(service, drawn_lines, label_size):
    """Guard the rule this borrows from; endless tape already got it right."""
    service._create_text_label(
        "Kalibrierungsetikett 2026", {"label_size": label_size, "font_size": 50})

    assert " ".join(drawn_lines).split() == ["Kalibrierungsetikett", "2026"]


@pytest.mark.parametrize("label_size", ["d24", "d12", "62x29"])
def test_a_word_that_cannot_fit_at_all_still_terminates(service, label_size):
    """
    Below the minimum font size a single word wider than any available line has
    to break or clip -- the same floor the continuous path has. What must not
    happen is an endless search or an unprintable label.
    """
    img = _open(service._create_text_label(
        "Unbreakablesupercalifragilisticexpialidocious", {"label_size": label_size}))

    assert img.size == tuple(get_label_geometry(label_size)[:2])
    _accept(img, label_size)


def test_auto_fit_off_still_honours_the_requested_font(service, drawn_lines):
    """The opt-out stays an opt-out: no shrinking, wrapping only."""
    service._create_text_label(
        "Kalibriert 2026",
        {"label_size": "d24", "font_size": 50, "auto_fit": False, "alignment": "center"},
    )

    # At 50 px this text cannot fit unbroken, and nothing is allowed to rescue
    # it -- the caller asked for that size explicitly.
    assert " ".join(drawn_lines).split() != ["Kalibriert", "2026"]


def test_wrapping_off_draws_exactly_the_input_lines(service, drawn_lines):
    """text_wrap = false means the lines are the caller's, breaks and all."""
    service._create_text_label(
        "Kalibriert 2026<br>Maerz", {"label_size": "d24", "text_wrap": False})

    assert drawn_lines == ["Kalibriert 2026", "Maerz"]


# --------------------------------------------------------------------------- #
# Rectangular die-cut
# --------------------------------------------------------------------------- #

def test_rectangular_die_cut_text_is_centred_vertically(service):
    """Same argument as round media: the label is a fixed piece of paper."""
    img = _open(service._create_text_label("Milk", {"label_size": "62x29"}))
    box = _ink_bbox(img)

    assert img.size == SIZE_62X29
    assert (box[1] + box[3]) / 2 == pytest.approx(SIZE_62X29[1] / 2, abs=8)


# --------------------------------------------------------------------------- #
# End to end: the printer library has to accept every render path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ["d24", "d12", "62x29", "12", "62"])
def test_every_render_path_is_accepted_by_convert(service, label_size, photo):
    """
    The load-bearing test. A path that only gets the pixel count right can still
    be refused by the printer library, and a path that is never fed to it can be
    broken for months without a test noticing.
    """
    renders = {
        "text": service._create_text_label("Sourdough 2026-08", {"label_size": label_size}),
        "image": service._resize_image(photo, label_size),
        "qrcode": service._create_qr_code("https://example.org/abc", {"label_size": label_size}),
        "qrcode_caption": service._create_qr_code(
            "https://example.org/abc",
            {"label_size": label_size, "show_text": True, "text": "Shelf B2"},
        ),
        "qrcode_side_by_side": service._create_qr_code(
            "https://example.org/abc",
            {"label_size": label_size, "side_by_side": True, "side_text": "Shelf B2"},
        ),
        "text_image": service._create_text_image_label(
            photo, "Shelf B2\nrow 3", {"label_size": label_size}),
    }

    expected = get_label_geometry(label_size)
    for path, rendered in renders.items():
        img = _open(rendered)
        assert img.width == expected.width, path
        if expected.is_die_cut:
            assert img.height == expected.height, path
        _accept(img, label_size)


@pytest.mark.parametrize("label_size", ROUND_LABELS)
def test_every_render_path_stays_inside_the_circle(service, label_size, photo):
    for rendered in (
        service._create_text_label("Sourdough", {"label_size": label_size}),
        service._resize_image(photo, label_size),
        service._create_qr_code("https://example.org/abc", {"label_size": label_size}),
        service._create_text_image_label(photo, "Shelf B2", {"label_size": label_size}),
    ):
        assert _ink_outside_circle(_open(rendered)) == 0


# --------------------------------------------------------------------------- #
# Continuous media must not move
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ["12", "62"])
def test_continuous_qr_code_prints_exactly_as_before(service, label_size):
    """
    Fitting the QR to the roll before ``convert()`` has to be a no-op in effect:
    convert() would have scaled it to the same printable width itself. The proof
    is the instruction stream, not the image.
    """
    settings = {"label_size": label_size, "show_text": True, "text": "Shelf B2"}

    unfitted = service._compose_qr_with_text(
        service._generate_qr_image("https://example.org/abc", settings),
        "https://example.org/abc",
        settings,
    )
    fitted = _open(service._create_qr_code("https://example.org/abc", settings))

    assert fitted.width == get_label_geometry(label_size).width
    assert _accept(fitted, label_size) == _accept(unfitted, label_size)


@pytest.mark.parametrize("label_size", ["12", "62"])
def test_continuous_image_keeps_growing_with_its_aspect_ratio(service, label_size, photo):
    """A photo on endless tape is fitted to the width and keeps its shape."""
    img = _open(service._resize_image(photo, label_size))
    width = get_label_geometry(label_size).width

    assert img.width == width
    assert img.height == int(width * 300 / 800)


def test_continuous_text_label_is_not_padded_to_a_fixed_height(service):
    """Endless tape has no fixed length, so short text stays a short label."""
    img = _open(service._create_text_label("Milk", {"label_size": "12"}))

    assert img.width == WIDTH_12MM
    assert img.height < WIDTH_12MM
