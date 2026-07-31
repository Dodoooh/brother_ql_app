"""
Tests for vertical alignment of text labels.

``vertical_alignment`` positions the text block on the axis perpendicular to the
reading direction. Whether that axis has any slack to distribute is a property
of the loaded media, and the three cases are genuinely different:

* a die-cut label is a fixed piece of paper, so the block can sit anywhere
  between the two long edges;
* a round die-cut label is a fixed piece of paper whose printable area is the
  inscribed *circle*, so moving the block towards the rim costs width -- the
  chords the lines are wrapped against change with the placement, and computing
  them for one placement while drawing at another puts ink on the backing paper;
* continuous tape rendered ``across`` grows to exactly fit the text, so there is
  no slack at all and the setting is documented as a no-op there. Rendered
  ``lengthwise`` the tape width becomes the free axis and the setting applies.

The default is ``middle``, which is what every medium already did, so the tests
below insist on byte-identical output for it: this is a pure addition and a user
who never sets it must see no change whatsoever.
"""

import math
import os

import pytest

from src.services.printer_service import (
    MIN_AUTO_FIT_FONT_SIZE,
    PrinterService,
    get_label_geometry,
    get_round_block_top,
    get_round_line_widths,
    get_round_safe_radius,
    get_vertical_alignment,
    get_vertical_offset,
)

Image = pytest.importorskip("PIL.Image")

# Printable geometry of the media under test, per brother_ql.
WIDTH_12MM = 106  # DK-22214, continuous
WIDTH_62MM = 696  # DK-22205, continuous
SIZE_D12 = 94  # DK-11219, 12 mm round die-cut
SIZE_D24 = 236  # DK-11218, 24 mm round die-cut
SIZE_62X29 = (696, 271)  # DK-11209, rectangular die-cut

ROUND_LABELS = ("d12", "d24", "d58")
ALIGNMENTS = ("top", "middle", "bottom")

# A block short enough to leave real slack on every medium under test, so
# "did the ink move?" has a meaningful answer.
SHORT_TEXT = "Bay<br>leaf"


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
def drawn(monkeypatch):
    """Collect ``(y, text)`` for every line the renderer actually draws.

    The canvas size proves nothing about the layout: a label can be exactly the
    right number of pixels and still have every line stacked in the wrong place
    or chopped mid-word. The draw calls are the output that matters.
    """
    module = pytest.importorskip("PIL.ImageDraw")
    captured = []
    original = module.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        captured.append((xy[1], text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(module.ImageDraw, "text", spy)
    return captured


def _render(service, text, **settings):
    """Render a text label and return the resulting PIL image (loaded eagerly)."""
    settings.setdefault("label_size", "62x29")
    path = service._create_text_label(text, settings)
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


def _ink(img, box):
    """Total darkness inside ``box``; the measure of where the text landed."""
    histogram = img.convert("L").crop(box).histogram()
    return sum((255 - value) * count for value, count in enumerate(histogram))


def _ink_halves(img):
    """Return (upper, lower) ink mass, split across the middle of the canvas."""
    midpoint = img.height // 2
    return (_ink(img, (0, 0, img.width, midpoint)),
            _ink(img, (0, midpoint, img.width, img.height)))


def _as_read(img):
    """Undo the lengthwise transpose so the label is back in reading orientation.

    ``_create_text_label`` lays a lengthwise label out unrolled and rotates it
    counter-clockwise onto the tape, so the edge the stack was aligned against
    ends up on the left. Rotating back is what makes "top" mean top again.
    """
    return img.transpose(Image.Transpose.ROTATE_270)


# --------------------------------------------------------------------------- #
# The offset helpers
# --------------------------------------------------------------------------- #

def test_vertical_offset_places_the_block_against_each_edge():
    """The margin is respected in every direction, not only when centring."""
    assert get_vertical_offset(271, 100, "top") == 10
    assert get_vertical_offset(271, 100, "middle") == 85
    assert get_vertical_offset(271, 100, "bottom") == 161


def test_vertical_offset_keeps_the_margin_off_the_edge():
    """
    Flush against the very edge of a die-cut label is the part the cut tolerance
    eats, so neither extreme may reach pixel zero or the last row.
    """
    top = get_vertical_offset(271, 100, "top")
    bottom = get_vertical_offset(271, 100, "bottom")

    assert top >= 10
    assert bottom + 100 <= 271 - 10


def test_vertical_offset_falls_back_to_centring_when_there_is_no_room():
    """A block taller than the label overflows symmetrically, as it always did."""
    for alignment in ALIGNMENTS:
        assert get_vertical_offset(100, 400, alignment) == 10


def test_unknown_vertical_alignment_is_treated_as_middle():
    """A typo is a layout hint gone wrong, not a reason to refuse a print."""
    assert get_vertical_alignment({}) == "middle"
    assert get_vertical_alignment({"vertical_alignment": "centre"}) == "middle"
    assert get_vertical_alignment({"vertical_alignment": "TOP"}) == "top"


# --------------------------------------------------------------------------- #
# Round geometry: the chords have to follow the block
# --------------------------------------------------------------------------- #

def test_round_block_top_centres_by_default():
    """``middle`` is exactly the placement get_round_line_widths assumes."""
    radius = get_round_safe_radius(SIZE_D24)

    assert get_round_block_top(radius, 120, 80, "middle") == -60.0


def test_round_block_top_moves_the_block_towards_the_rim():
    """top and bottom are mirror images and really do leave the centre."""
    radius = get_round_safe_radius(SIZE_D24)

    top = get_round_block_top(radius, 60, 80, "top")
    bottom = get_round_block_top(radius, 60, 80, "bottom")

    assert top < -30.0  # further up than centred
    assert bottom > -30.0
    assert top == pytest.approx(-(bottom + 60))


def test_round_block_travel_is_bounded_by_the_width_the_block_needs():
    """
    The whole point of the bound: a wide block cannot go far up a circle,
    because the chord up there is narrower than the text.
    """
    radius = get_round_safe_radius(SIZE_D24)

    narrow = get_round_block_top(radius, 40, 40, "top")
    wide = get_round_block_top(radius, 40, 180, "top")

    assert narrow < wide < 0


def test_round_block_that_fills_the_circle_stays_centred():
    """Nothing left to shift means the overflow has to stay symmetric."""
    radius = get_round_safe_radius(SIZE_D24)

    for alignment in ALIGNMENTS:
        assert get_round_block_top(radius, 400, 200, alignment) == -200.0


def test_round_block_wider_than_the_label_stays_centred():
    """No real chord to aim at, so the geometry must not go imaginary."""
    radius = get_round_safe_radius(SIZE_D24)

    assert get_round_block_top(radius, 40, 10_000, "top") == -20.0


def test_round_chords_follow_the_block_that_is_handed_to_them():
    """
    The subtlety this feature lives or dies on: a moved block gets *different*
    chords. Widths computed for a centred stack and text drawn near the rim is
    exactly how ink lands outside the die cut.
    """
    radius = get_round_safe_radius(SIZE_D24)
    centred = get_round_line_widths(radius, 2, 40)
    raised = get_round_line_widths(radius, 2, 40, block_top=-90.0)

    assert centred == get_round_line_widths(radius, 2, 40, block_top=-40.0)
    assert raised[0] < centred[0]


def test_round_chord_at_the_travel_limit_still_fits_the_block():
    """
    The bound is self-consistent: at the extreme placement every line of the
    stack still has at least the block's own width behind it.
    """
    radius = get_round_safe_radius(SIZE_D24)
    block_width, block_height, line_height = 120.0, 60, 30

    block_top = get_round_block_top(radius, block_height, block_width, "top")
    widths = get_round_line_widths(radius, 2, line_height, block_top)

    assert min(widths) >= block_width


# --------------------------------------------------------------------------- #
# middle must be byte-for-byte what the app already produced
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size, extra", [
    ("62x29", {}),
    ("d24", {}),
    ("d12", {}),
    ("62", {"orientation": "lengthwise"}),
    ("12", {"orientation": "lengthwise"}),
    ("12", {}),
])
def test_middle_is_identical_to_not_setting_it_at_all(service, label_size, extra):
    """A pure addition: the setting absent and set to middle must agree exactly."""
    text = "Wartung faellig<br>Maerz 2027"
    absent = _render(service, text, label_size=label_size, **extra)
    middle = _render(service, text, label_size=label_size,
                     vertical_alignment="middle", **extra)

    assert absent.size == middle.size
    assert absent.tobytes() == middle.tobytes()


@pytest.mark.parametrize("label_size", ["62x29", "d24"])
def test_middle_draws_at_exactly_the_same_places(service, drawn, label_size):
    """Not just the same pixels -- the same draw calls at the same coordinates."""
    service._create_text_label(SHORT_TEXT, {"label_size": label_size})
    absent = list(drawn)
    drawn.clear()
    service._create_text_label(
        SHORT_TEXT, {"label_size": label_size, "vertical_alignment": "middle"})

    assert drawn == absent
    assert [text for _, text in drawn] == ["Bay", "leaf"]


# --------------------------------------------------------------------------- #
# top and bottom have to actually move the ink
# --------------------------------------------------------------------------- #

def test_die_cut_top_and_bottom_move_the_block(service):
    """The fixed height of 62x29 is slack, and the setting has to spend it."""
    upper_of_top, lower_of_top = _ink_halves(
        _render(service, SHORT_TEXT, vertical_alignment="top"))
    upper_of_bottom, lower_of_bottom = _ink_halves(
        _render(service, SHORT_TEXT, vertical_alignment="bottom"))

    assert upper_of_top > lower_of_top
    assert lower_of_bottom > upper_of_bottom


def test_die_cut_alignments_are_ordered(service, drawn):
    """top is above middle is above bottom -- and the lines are still intact."""
    first_line_y = {}
    for alignment in ALIGNMENTS:
        drawn.clear()
        service._create_text_label(
            SHORT_TEXT, {"label_size": "62x29", "vertical_alignment": alignment})
        assert [text for _, text in drawn] == ["Bay", "leaf"]
        first_line_y[alignment] = drawn[0][0]

    assert first_line_y["top"] < first_line_y["middle"] < first_line_y["bottom"]


def test_round_top_and_bottom_move_the_block(service):
    """A short block has room to travel up and down a 24 mm circle."""
    upper_of_top, lower_of_top = _ink_halves(
        _render(service, SHORT_TEXT, label_size="d24", vertical_alignment="top"))
    upper_of_bottom, lower_of_bottom = _ink_halves(
        _render(service, SHORT_TEXT, label_size="d24", vertical_alignment="bottom"))

    assert upper_of_top > lower_of_top
    assert lower_of_bottom > upper_of_bottom


def test_lengthwise_top_and_bottom_move_the_block_across_the_tape(service):
    """
    Lengthwise the free axis is the tape width. The label is drawn unrolled and
    rotated onto the tape, so the comparison is made in reading orientation.
    """
    top = _as_read(_render(service, SHORT_TEXT, label_size="62",
                           orientation="lengthwise", vertical_alignment="top"))
    bottom = _as_read(_render(service, SHORT_TEXT, label_size="62",
                              orientation="lengthwise", vertical_alignment="bottom"))

    upper_of_top, lower_of_top = _ink_halves(top)
    upper_of_bottom, lower_of_bottom = _ink_halves(bottom)

    assert upper_of_top > lower_of_top
    assert lower_of_bottom > upper_of_bottom


def test_unknown_value_renders_exactly_like_middle(service):
    """A typo must not quietly shove the text against an edge."""
    fallback = _render(service, SHORT_TEXT, vertical_alignment="centre")
    middle = _render(service, SHORT_TEXT, vertical_alignment="middle")

    assert fallback.tobytes() == middle.tobytes()


# --------------------------------------------------------------------------- #
# Continuous tape rendered across has no spare axis
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ["12", "62"])
def test_continuous_across_is_unaffected_by_every_value(service, label_size):
    """
    The label grows in length to exactly fit the text, so there is nothing to
    distribute. Padding it to fake an effect would waste tape on every print.
    """
    text = "Storage shelf B, second row"
    baseline = _render(service, text, label_size=label_size)

    for alignment in ALIGNMENTS:
        rendered = _render(service, text, label_size=label_size,
                           vertical_alignment=alignment)
        assert rendered.size == baseline.size, alignment
        assert rendered.tobytes() == baseline.tobytes(), alignment


def test_continuous_across_label_is_still_not_padded(service):
    """Guard the reason it is a no-op: short text keeps producing a short label."""
    img = _render(service, "Milk", label_size="12", vertical_alignment="bottom")

    assert img.width == WIDTH_12MM
    assert img.height < WIDTH_12MM


# --------------------------------------------------------------------------- #
# The failure mode round media exists to avoid
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ROUND_LABELS)
@pytest.mark.parametrize("alignment", ALIGNMENTS)
@pytest.mark.parametrize("text", [
    "Milk",
    "Bay<br>leaf",
    "Apfelsaft<br>naturtrueb<br>2026-08",
    "Sourdough starter fed on 3 August, keep refrigerated",
    "unbreakablewordthatismuchwiderthanthelabelitself",
])
def test_round_text_never_leaves_the_circle_at_any_alignment(
        service, label_size, alignment, text):
    """
    Ink in the corners is ink on the backing paper. Moving the stack towards the
    rim narrows every chord, so this is the check most likely to catch a chord
    computation that was left describing a centred block.
    """
    img = _render(service, text, label_size=label_size, vertical_alignment=alignment)

    assert img.size == (get_label_geometry(label_size).width,) * 2
    assert _ink_outside_circle(img) == 0


@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_round_text_stays_inside_the_circle_for_every_alignment_pair(
        service, alignment):
    """Horizontal and vertical alignment are combined on every print."""
    for horizontal in ("left", "center", "right"):
        img = _render(service, "Apfelsaft<br>naturtrueb<br>2026-08",
                      label_size="d24", alignment=horizontal,
                      vertical_alignment=alignment)
        assert _ink_outside_circle(img) == 0, horizontal


# --------------------------------------------------------------------------- #
# Auto-fit still terminates, and does not pay for the alignment in font size
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ROUND_LABELS)
@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_auto_fit_terminates_on_text_that_cannot_fit(service, label_size, alignment):
    """
    A shifted stack has a smaller budget, so the font may have to shrink further.
    What must not happen is an endless search or an unprintable label.
    """
    img = _render(service, "Unbreakablesupercalifragilisticexpialidocious",
                  label_size=label_size, vertical_alignment=alignment)

    assert img.size == tuple(get_label_geometry(label_size)[:2])
    _accept(img, label_size)


@pytest.mark.parametrize("label_size", ROUND_LABELS)
@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_round_auto_fit_keeps_words_whole_at_every_alignment(
        service, drawn, label_size, alignment):
    """
    The rule the round layout already had: shrink the font rather than chop a
    word in half. Aligning the block must not buy its way out of it.
    """
    service._create_text_label(
        "Kalibriert 2026",
        {"label_size": label_size, "alignment": "center",
         "vertical_alignment": alignment},
    )

    assert " ".join(text for _, text in drawn).split() == ["Kalibriert", "2026"]


def test_aligning_a_round_block_does_not_cost_font_size(service):
    """
    Moving a block that has room to move must not shrink it. The travel stops
    where the chord still fits the text, so the same font has to survive it.
    """
    heights = {}
    for alignment in ALIGNMENTS:
        img = _render(service, SHORT_TEXT, label_size="d24",
                      vertical_alignment=alignment).convert("L")
        box = img.point(lambda p: 255 if p < 128 else 0).getbbox()
        heights[alignment] = box[3] - box[1]

    assert heights["top"] == heights["middle"] == heights["bottom"]


def test_auto_fit_off_is_still_honoured_at_every_alignment(service):
    """The opt-out stays an opt-out: no shrinking, whatever the placement."""
    for alignment in ALIGNMENTS:
        img = _render(service, "Refrigerate after opening", label_size="d24",
                      text_wrap=False, auto_fit=False, font_size=60,
                      vertical_alignment=alignment)
        assert img.size == (SIZE_D24, SIZE_D24)
        _accept(img, "d24")


def test_the_auto_fit_floor_is_unchanged():
    """The alignment work must not have moved the readability floor."""
    assert MIN_AUTO_FIT_FONT_SIZE == 8


# --------------------------------------------------------------------------- #
# End to end: the printer library has to accept every placement
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ["d24", "62x29", "12", "62"])
@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_every_alignment_is_accepted_by_convert(service, label_size, alignment):
    """
    A layout that only gets the pixel count right can still be refused by the
    printer library, and convert() silently rescales a continuous image whose
    width does not match the roll.
    """
    expected = get_label_geometry(label_size)
    img = _render(service, "Sourdough 2026-08", label_size=label_size,
                  vertical_alignment=alignment)

    assert img.width == expected.width
    if expected.is_die_cut:
        assert img.height == expected.height
    _accept(img, label_size)


@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_lengthwise_still_renders_at_the_rolls_printable_width(service, alignment):
    """Aligning across the tape must not change how wide the tape is."""
    img = _render(service, "Cable run 12", label_size="12",
                  orientation="lengthwise", vertical_alignment=alignment)

    assert img.width == WIDTH_12MM
    _accept(img, "12")


# --------------------------------------------------------------------------- #
# Settings wiring
# --------------------------------------------------------------------------- #

def test_default_settings_carry_the_new_key():
    """New settings have to reach old settings files through the default merge."""
    from src.config.default_settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["vertical_alignment"] == "middle"


def test_the_setting_is_inheritable_by_a_print_request():
    """Saved once, applied to every print that does not override it."""
    from src.services.settings_service import SettingsService

    assert "vertical_alignment" in SettingsService._INHERITABLE_PRINT_KEYS


def _service_with_settings(tmp_path):
    from src.services.settings_service import SettingsService

    return SettingsService(settings_file=str(tmp_path / "settings.json"))


def _base_settings(**overrides):
    base = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("alignment", ALIGNMENTS)
def test_validation_accepts_every_allowed_value(tmp_path, alignment):
    service = _service_with_settings(tmp_path)

    assert service.save_settings(_base_settings(vertical_alignment=alignment)) is True


@pytest.mark.parametrize("value", ["centre", "up", "", 5, None])
def test_validation_rejects_anything_else(tmp_path, value):
    """A stored value the renderer would silently ignore is a stored mistake."""
    service = _service_with_settings(tmp_path)

    assert service.save_settings(_base_settings(vertical_alignment=value)) is False


def test_a_saved_value_is_inherited_by_a_print_request(tmp_path):
    service = _service_with_settings(tmp_path)
    service.save_settings(_base_settings(vertical_alignment="bottom"))

    assert service.resolve_print_settings(None)["vertical_alignment"] == "bottom"
    assert service.resolve_print_settings(
        {"vertical_alignment": "top"})["vertical_alignment"] == "top"
