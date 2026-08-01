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

import io
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


# --------------------------------------------------------------------------- #
# Fitting a round label by its ink
#
# Fitting the image's *rectangle* into the circle costs 1/sqrt(2) of the
# diameter, and it costs it whether or not there is anything in the corners to
# pay for. Measured on paper: a 236x236 bitmap holding a ring drawn to its own
# edge -- 19.8 mm of artwork -- came off a DK-11218 13.3 mm across, a 5 mm ring
# of bare paper all round. The fit therefore follows the ink: the largest scale
# that keeps every dot the printer will actually blacken inside the safe
# ellipse. Artwork that does reach its corners is bound by the corner dot, which
# is the half-diagonal rule again, so photographs do not move at all.
#
# What is asserted here is rendered dots, not scale factors. This pipeline has
# been green while the paper was wrong before.
# --------------------------------------------------------------------------- #

# The reported artwork and what it measured, in dots.
RING_SOURCE_PX = 236
RING_INK_PX = 234        # drawn to within a dot of the bitmap's own edge
RING_BOX_RULE_PX = 157   # what came off the printer: 13.3 mm of a 24 mm label

# 2 * get_round_safe_radius(236): the full diameter less the registration
# sliver that keeps a misplaced die cut off the artwork.
D24_SAFE_DIAMETER_PX = 226


def _draw(img):
    return pytest.importorskip("PIL.ImageDraw").Draw(img)


def _ink_span(img):
    """Width and height of the printed ink, or None for a blank label."""
    box = _ink_bbox(img)
    return None if box is None else (box[2] - box[0], box[3] - box[1])


def _ink_outside_ellipse(img):
    """Black pixels outside the ellipse inscribed in the canvas.

    On a bled round label the canvas is no longer square, so the die cut is not
    a circle any more; the inscribed ellipse is the region that is both on the
    canvas and on the label.
    """
    gray = img.convert("L")
    semi_x = gray.width / 2.0
    semi_y = gray.height / 2.0
    pixels = gray.load()
    outside = 0
    for y in range(gray.height):
        for x in range(gray.width):
            if pixels[x, y] < 128 and math.hypot(
                    (x - (gray.width - 1) / 2.0) / semi_x,
                    (y - (gray.height - 1) / 2.0) / semi_y) > 1.0:
                outside += 1
    return outside


def _ring(size=RING_SOURCE_PX, inset=1, stroke=6):
    """The reported artwork: a ring drawn to the edge of its own bitmap."""
    img = Image.new("RGB", (size, size), "white")
    _draw(img).ellipse((inset, inset, size - 1 - inset, size - 1 - inset),
                       outline="black", width=stroke)
    return img


def _filled(size, colour="black"):
    img = Image.new("RGB", size, "white")
    _draw(img).rectangle((0, 0, size[0] - 1, size[1] - 1), fill=colour)
    return img


def _disc_with_a_corner_mark(mark):
    """A big black disc plus a pale mark in one corner, twice label size.

    Whether that mark is ink is a question only the threshold and dither
    settings can answer, which is the point: it decides the fit.
    """
    img = Image.new("L", (472, 472), 255)
    _draw(img).ellipse((36, 36, 435, 435), fill=0)
    _draw(img).rectangle((0, 0, 30, 30), fill=mark)
    return img


def _png_bytes(img):
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


# --- The regression itself ------------------------------------------------- #

def test_the_bounding_box_rule_is_what_shrank_the_ring(service):
    """
    The measurement that started this, reproduced exactly.

    Fitting the bitmap's rectangle leaves the ring at 157 dots of a 236-dot
    label -- 13.3 mm of 24 mm, the 5 mm of bare paper the user measured on
    every side.
    """
    boxed = service._fit_to_label(_ring(), "d24", {}, margin_is_content=True)

    assert _ink_span(boxed) == (RING_BOX_RULE_PX, RING_BOX_RULE_PX)


def test_a_ring_drawn_to_its_own_edge_now_uses_the_whole_label(service):
    """
    The fix: nothing is in the corners, so nothing is paid for them.

    The ring cannot come back at its full 234 dots -- the outer 5 belong to the
    registration margin that keeps a misplaced die cut off the artwork -- but it
    gets everything inside that, which is 18.9 mm of a 24 mm label instead of
    13.3 mm.
    """
    fitted = service._fit_to_label(_ring(), "d24", {})
    span = _ink_span(fitted)

    assert fitted.size == (SIZE_D24, SIZE_D24)
    assert span[0] >= D24_SAFE_DIAMETER_PX - 2
    assert span[0] <= D24_SAFE_DIAMETER_PX
    assert span[0] == span[1]  # still a ring, not an ellipse
    assert span[0] > RING_BOX_RULE_PX * 1.4
    assert _ink_outside_circle(fitted) == 0


def test_the_ring_survives_the_whole_image_print_path(service, tmp_path):
    """Not just the fit in isolation: the path a printed file actually takes."""
    source = tmp_path / "ring.png"
    _ring().save(source)

    img = _open(service._resize_image(str(source), "d24"))

    assert img.size == (SIZE_D24, SIZE_D24)
    assert _ink_span(img)[0] >= D24_SAFE_DIAMETER_PX - 2
    assert _ink_outside_circle(img) == 0
    _accept(img, "d24")


# --- Artwork that really does fill its rectangle must not move -------------- #

def test_a_fully_inked_rectangle_is_byte_identical_to_the_old_fit(service):
    """
    A photograph with ink in its corners is bound by the corner dot, and the
    corner dot is what the half-diagonal rule measures. Same rule, same
    arithmetic, same file.
    """
    for size in [(800, 300), (236, 236), (300, 800), (1000, 1000)]:
        source = _filled(size)
        assert _png_bytes(service._fit_to_label(source, "d24", {})) == \
            _png_bytes(service._fit_to_label(source, "d24", {}, margin_is_content=True))


@pytest.mark.parametrize("label_size", ROUND_LABELS)
def test_a_photo_that_reaches_its_corners_is_untouched(service, label_size):
    """The same claim on every round medium, and on the bled geometry too."""
    source = _filled((800, 600))
    bled = {"label_size": label_size, "bleed_mm": {label_size: 1.0}}

    for settings in ({}, bled):
        assert _png_bytes(service._fit_to_label(source, label_size, settings)) == \
            _png_bytes(service._fit_to_label(source, label_size, settings,
                                             margin_is_content=True))


# --- The guarantee that must not weaken ------------------------------------ #

@pytest.mark.parametrize("label_size", ROUND_LABELS)
@pytest.mark.parametrize("artwork", ["ring", "wide_bar", "disc", "corner_dot", "diagonal"])
def test_no_design_ever_puts_ink_outside_the_die_cut(service, label_size, artwork):
    """Ink outside the circle is ink on the backing paper, whatever the fit."""
    source = Image.new("RGB", (472, 472), "white")
    draw = _draw(source)
    if artwork == "ring":
        draw.ellipse((2, 2, 469, 469), outline="black", width=12)
    elif artwork == "wide_bar":
        draw.rectangle((0, 200, 471, 271), fill="black")
    elif artwork == "disc":
        draw.ellipse((60, 60, 411, 411), fill="black")
    elif artwork == "corner_dot":
        draw.rectangle((8, 8, 24, 24), fill="black")
    else:
        draw.line((0, 0, 471, 471), fill="black", width=10)

    fitted = service._fit_to_label(source, label_size, {})

    assert fitted.size == tuple(get_label_geometry(label_size)[:2])
    assert _ink_outside_circle(fitted) == 0
    _accept(fitted, label_size)


# --- Ink means what the printer will blacken ------------------------------- #

def test_the_fit_uses_the_threshold_the_job_will_print_with(service):
    """
    A mark too pale to print is not ink and must not hold the fit back; the same
    mark under a threshold that *does* print it must. Anything else and the fit
    and the printer disagree about the rim, which is the one place it shows.
    """
    source = _disc_with_a_corner_mark(mark=230)

    pale = _ink_span(service._fit_to_label(source, "d24", {"threshold": 70}))
    printed = _ink_span(service._fit_to_label(source, "d24", {"threshold": 5}))

    assert pale[0] > printed[0] * 1.2
    # And "printed" is the bounding-box answer, because the mark is in a corner.
    assert printed == _ink_span(
        service._fit_to_label(source, "d24", {"threshold": 5}, margin_is_content=True))


def test_dithering_turns_grey_into_ink_and_the_fit_follows(service):
    """
    Mid grey prints as nothing under a hard threshold and as a field of dots
    under error diffusion. The fit has to see the dots.
    """
    source = _disc_with_a_corner_mark(mark=128)

    hard = _ink_span(service._fit_to_label(source, "d24", {"dither": False}))
    dithered = _ink_span(service._fit_to_label(source, "d24", {"dither": True}))

    assert dithered[0] < hard[0]
    assert _ink_outside_circle(service._fit_to_label(source, "d24", {"dither": True})) == 0


def test_a_transparent_corner_is_empty_not_black(service):
    """
    Dropping the alpha channel would leave the hidden colour behind, and a
    transparent black corner would then hold the fit back for ink that never
    prints.
    """
    source = Image.new("RGBA", (472, 472), (255, 255, 255, 0))
    _draw(source).ellipse((36, 36, 435, 435), fill=(0, 0, 0, 255))
    opaque = Image.new("RGBA", (472, 472), (255, 255, 255, 0))
    _draw(opaque).ellipse((36, 36, 435, 435), fill=(0, 0, 0, 255))
    _draw(source).rectangle((0, 0, 30, 30), fill=(0, 0, 0, 0))

    assert _ink_span(service._fit_to_label(source, "d24", {})) == \
        _ink_span(service._fit_to_label(opaque, "d24", {}))


# --- Nothing to fit, and nearly nothing to fit ----------------------------- #

def test_a_blank_image_is_not_scaled_to_infinity(service):
    """No ink means no constraint; the rectangle rule decides, as it always did."""
    blank = Image.new("RGB", (400, 400), "white")

    fitted = service._fit_to_label(blank, "d24", {})

    assert fitted.size == (SIZE_D24, SIZE_D24)
    assert _ink_bbox(fitted) is None
    assert _png_bytes(fitted) == _png_bytes(
        service._fit_to_label(blank, "d24", {}, margin_is_content=True))


def test_a_lone_dot_is_never_enlarged_past_its_own_size(service):
    """
    A single dot near the middle would otherwise be blown up 40-fold to fill the
    label. Upsampling a bitmap invents no detail, so 1:1 is the ceiling.
    """
    source = Image.new("RGB", (472, 472), "white")
    _draw(source).rectangle((234, 234, 237, 237), fill="black")

    span = _ink_span(service._fit_to_label(source, "d24", {}))

    assert span[0] <= 6


def test_a_small_design_keeps_the_enlargement_the_old_rule_gave_it(service):
    """
    Where the rectangle rule was already enlarging, it stays the ceiling. This
    fit exists to stop giving diameter away, not to start magnifying more.
    """
    source = _ring(size=60, stroke=3)

    assert _png_bytes(service._fit_to_label(source, "d24", {})) == \
        _png_bytes(service._fit_to_label(source, "d24", {}, margin_is_content=True))


# --- The QR quiet zone ----------------------------------------------------- #

def _qr_geometry(service, img, data="https://example.org/abc"):
    """Module pitch and black-module box of a rendered QR label."""
    encoder = pytest.importorskip("qrcode").QRCode(version=1, box_size=10, border=4)
    encoder.add_data(data)
    encoder.make(fit=True)
    box = _ink_bbox(img)
    return box, (box[2] - box[0]) / encoder.modules_count, encoder.border


def _quiet_zone_corners(box, pitch, border):
    margin = border * pitch
    return [(box[0] - margin, box[1] - margin), (box[2] + margin, box[1] - margin),
            (box[0] - margin, box[3] + margin), (box[2] + margin, box[3] + margin)]


def _inside_circle(point, diameter):
    radius = diameter / 2.0
    return math.hypot(point[0] - radius, point[1] - radius) <= radius


def test_a_qr_code_keeps_its_quiet_zone_on_the_label(service):
    """
    A QR's white border is not waste, it is the margin the scanner needs, and it
    has to land on the paper the user peels off rather than on the backing.
    """
    img = _open(service._create_qr_code("https://example.org/abc", {"label_size": "d24"}))
    box, pitch, border = _qr_geometry(service, img)

    assert border == 4
    assert pitch > 1  # a module is more than a single dot, or nothing scans
    for corner in _quiet_zone_corners(box, pitch, border):
        assert _inside_circle(corner, SIZE_D24)


def test_fitting_a_qr_code_by_its_ink_would_push_the_quiet_zone_off_the_label(service):
    """
    Why the QR path opts out, stated as a measurement rather than an opinion:
    grow the modules to the rim and four modules of quiet zone no longer fit on
    the label at all.
    """
    raw = service._generate_qr_image("https://example.org/abc", {"label_size": "d24"})

    boxed = service._fit_to_label(raw, "d24", {}, margin_is_content=True)
    inked = service._fit_to_label(raw, "d24", {})

    box_quiet = _quiet_zone_corners(*_qr_geometry(service, boxed))
    ink_quiet = _quiet_zone_corners(*_qr_geometry(service, inked))

    assert all(_inside_circle(corner, SIZE_D24) for corner in box_quiet)
    assert not any(_inside_circle(corner, SIZE_D24) for corner in ink_quiet)


@pytest.mark.parametrize("settings", [
    {},
    {"show_text": True, "text": "Shelf B2"},
    {"side_by_side": True, "side_text": "Shelf B2"},
])
@pytest.mark.parametrize("label_size", ROUND_LABELS)
def test_every_qr_layout_is_still_fitted_by_its_rectangle(service, label_size, settings):
    """The opt-out covers the composed layouts too, not only the bare symbol."""
    settings = dict(settings, label_size=label_size)

    img = _open(service._create_qr_code("https://example.org/abc", settings))

    assert _ink_outside_circle(img) == 0
    assert _png_bytes(img) == _png_bytes(_open(
        service._create_qr_code("https://example.org/abc", settings)))


# --- Media the change must not touch --------------------------------------- #

def test_rectangular_die_cut_still_fits_by_the_bounding_box(service):
    """
    On 62x29 the constraint really is the rectangle: the corners print. A design
    floating in white must therefore stay exactly where the old rule put it.
    """
    source = Image.new("RGB", (300, 300), "white")
    _draw(source).rectangle((120, 120, 179, 179), fill="black")

    fitted = service._fit_to_label(source, "62x29", {})

    assert fitted.size == SIZE_62X29
    # 271 / 300 of a 60-dot block, and nothing like the 271 dots an ink fit
    # would have grown it to.
    assert _ink_span(fitted) == (55, 55)


def test_continuous_tape_still_fits_by_the_width(service):
    """Endless tape has no circle to fit into and no corners to give up."""
    source = Image.new("RGB", (300, 300), "white")
    _draw(source).rectangle((120, 120, 179, 179), fill="black")

    fitted = service._fit_to_label(source, "62", {})

    assert fitted.size == (WIDTH_62MM, WIDTH_62MM)
    assert _ink_span(fitted) == (140, 140)


# --- Composition with the other geometry features -------------------------- #

def test_the_ink_fit_follows_the_bleed_onto_the_ellipse(service):
    """
    Bleed makes a d24 284 x 236 and its drawable area an ellipse. The ink fit
    reads the ellipse, so a wide design gains across the tape and nothing leaves
    the die cut.
    """
    settings = {"label_size": "d24", "bleed_mm": {"d24": 2.03}}
    bled = get_label_geometry("d24", settings)
    source = Image.new("RGB", (472, 236), "white")
    _draw(source).rectangle((0, 100, 471, 135), fill="black")

    fitted = service._fit_to_label(source, "d24", settings)
    unbled = service._fit_to_label(source, "d24", {"label_size": "d24"})

    assert fitted.size == (bled.width, bled.height)
    assert _ink_span(fitted)[0] > _ink_span(unbled)[0]
    assert _ink_outside_ellipse(fitted) == 0


def test_the_calibration_offset_still_moves_an_ink_fitted_label(service):
    """The fit hands the funnel a normal label; the offset works on it as before."""
    fitted = service._fit_to_label(_ring(), "d24", {})

    shifted = service._shift_within_canvas(fitted, 0, 6, "d24")

    assert shifted.size == (SIZE_D24, SIZE_D24)
    assert _ink_bbox(shifted)[1] == _ink_bbox(fitted)[1] + 6


def test_the_size_correction_still_applies_to_an_ink_fitted_label(service):
    """Same for the size half of a calibration entry."""
    fitted = service._fit_to_label(_ring(), "d24", {})

    corrected = service._scale_within_canvas(fitted, 0.9, "d24")

    assert corrected.size == (SIZE_D24, SIZE_D24)
    assert _ink_span(corrected)[0] < _ink_span(fitted)[0]
    assert _ink_outside_circle(corrected) == 0
