"""
Tests for print alignment calibration.

Calibration moves ink on paper, so "the label came out the right size" proves
nothing here -- the same canvas with the content in the wrong place passes that
check perfectly. These tests therefore measure the ink itself, and on the print
path they measure it in the instruction stream the printer receives rather than
in an image, because that is the last thing that can still be wrong.

The two axes are corrected by different mechanisms, and the tests are split the
same way:

* sideways, the label's raster is placed further along the printer's device
  row, so the offset relocates every dot of the label together and *never*
  clips -- the invariant a real d24 label disproved for the old design, and the
  one asserted here by counting the ink dots in the stream. Travel is bounded
  by the head, so an offset larger than the printer can reach is clamped and
  warned about, with what was actually applied;
* along the feed, there is no such lever: the raster starts where the feed
  starts, so the content moves inside a canvas that may not grow, and an offset
  large enough still clips and still warns.

Also asserted: zero offset produces byte-identical printer instructions to an
uncalibrated app on every medium, so nobody's existing setup moves; brother_ql's
global media table is left exactly as it was found, including when the
conversion raises; the previews are untouched by calibration, because they stand
for the label the user means to have and calibration exists to make the paper
match them; and the target itself is a usable instrument -- the right size, no
ink outside the die cut on round media, a scale that really is a millimetre
apart, and a caption naming the offset it was printed with.
"""

import math
import os
import threading

import pytest

from src.config.default_settings import (
    CALIBRATION_LIMIT_MM,
    CALIBRATION_SCALE_MAX,
    CALIBRATION_SCALE_MIN,
    DEFAULT_SETTINGS,
)
from src.services import printer_service as printer_module
from src.services.printer_service import (
    CALIBRATION_TARGET_LENGTH_MM,
    DOTS_PER_MM,
    MIN_CALIBRATION_FONT_PX,
    PrinterService,
    applied_calibration_offset,
    calibration_offset_px,
    format_calibration_offset,
    get_calibration_offset,
    get_calibration_scale,
    get_label_geometry,
    placed_raster,
    plan_raster_placement,
)
from src.services.settings_service import SettingsService
from src.utils.exceptions import PrinterError, ValidationError

label_type_specs = pytest.importorskip("brother_ql.devicedependent").label_type_specs

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

# Printable geometry of the media under test, per brother_ql.
SIZE_D12 = (94, 94)
SIZE_D24 = (236, 236)
SIZE_62X29 = (696, 271)
WIDTH_12MM = 106
WIDTH_62MM = 696

# Every medium a calibrated print has to survive: round die-cut (two sizes,
# including the smallest), rectangular die-cut and continuous.
ALL_MEDIA = ["d24", "d12", "62x29", "12", "62"]
ROUND_MEDIA = ["d12", "d24", "d58"]

# The printer every test prints to, and the width of its device row in dots.
# The row is wider than most media, which is exactly the room the sideways
# correction moves the label around in.
MODEL = "QL-820NWB"
DEVICE_WIDTH = 720

# Where a d24 label's raster sits in that row with no calibration, and how far
# it can travel before the head runs out: 720 - 236 - 42 = 442, leaving 484 - 442
# = 42 dots (3.5 mm) of rightward travel and 442 of leftward.
D24_BASE_X_OFFSET = 442
D24_TRAVEL_RIGHT = 42
# The same travel in millimetres, which is what a label, a caption and an API
# response speak in.
D24_TRAVEL_RIGHT_MM = 3.56
D24_TRAVEL_LEFT_MM = -37.42


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
def sent(monkeypatch):
    """Capture the instruction streams handed to the printer backend."""
    captured = []

    class _Backend:
        def __init__(self, _uri):
            pass

        def write(self, instructions):
            captured.append(instructions)

        def dispose(self):
            pass

    monkeypatch.setattr(printer_module, "guess_backend", lambda _uri: "network")
    monkeypatch.setattr(printer_module, "backend_factory",
                        lambda _kind: {"backend_class": _Backend})
    return captured


@pytest.fixture
def warnings(monkeypatch):
    """Collect the structured warnings the printer service emits."""
    collected = []
    real_logger = printer_module.logger

    class _Spy:
        def warning(self, event, **context):
            collected.append((event, context))

        def __getattr__(self, name):
            return getattr(real_logger, name)

    monkeypatch.setattr(printer_module, "logger", _Spy())
    return collected


@pytest.fixture
def drawn_lines(monkeypatch):
    """Collect the exact strings the renderer draws, in drawing order."""
    captured = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        captured.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return captured


def _print_settings(label_size, **overrides):
    """Minimal settings for a print, with no calibration configured at all."""
    settings = {
        "printer_uri": "tcp://192.0.2.10",
        "printer_model": "QL-820NWB",
        "label_size": label_size,
    }
    settings.update(overrides)
    return settings


def _calibrated(label_size, x_mm=0.0, y_mm=0.0, scale=None, **overrides):
    """Settings carrying a calibration entry for ``label_size``."""
    entry = {"x_mm": x_mm, "y_mm": y_mm}
    if scale is not None:
        entry["scale"] = scale
    return _print_settings(label_size, calibration={label_size: entry}, **overrides)


def _open(path):
    """Load a rendered label eagerly so the file can be removed afterwards."""
    with Image.open(path) as img:
        return img.copy()


def _accept(img, label_size):
    """Hand a rendered label to the real convert(); fails loudly if rejected."""
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    return convert(raster("QL-820NWB"), [img], label_size, rotate=0)


def _ink_bbox(img):
    """Bounding box of the black pixels, or None when the label is blank."""
    gray = img.convert("L")
    return gray.point(lambda p: 255 if p < 128 else 0).getbbox()


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


class _Ink:
    """The ink in one instruction stream: how much of it, and where it sits.

    ``dots`` is the load-bearing number. It is the count of black dots the
    printer is told to lay down, so it cannot be fooled by a shifted canvas: a
    correction that pushes content off the edge lowers it, one that relocates
    the raster does not. ``first``/``last`` are the outermost columns of the
    device row carrying ink, i.e. where on the tape the label ended up.
    """

    def __init__(self, dots, first, last):
        self.dots = dots
        self.first = first
        self.last = last

    def __repr__(self):
        return f"_Ink(dots={self.dots}, first={self.first}, last={self.last})"


def _raster_rows(instructions, device_width=DEVICE_WIDTH):
    """Pull the uncompressed raster rows out of a QL instruction stream.

    Each row is ``0x67 0x00 <length> <length bytes>``; anything else is a
    command and is stepped over. A candidate whose length is not exactly one
    device row is not a row header but a byte pattern inside some other
    payload, so it is skipped too.
    """
    row_len = device_width // 8
    rows = []
    i = 0
    while i < len(instructions):
        if instructions[i:i + 2] == b"\x67\x00" and instructions[i + 2:i + 3]:
            declared = instructions[i + 2]
            if declared == row_len and len(instructions) >= i + 3 + row_len:
                rows.append(instructions[i + 3:i + 3 + row_len])
                i += 3 + row_len
                continue
        i += 1
    return rows


def _printed_ink(instructions, device_width=DEVICE_WIDTH):
    """Return the :class:`_Ink` the printer is instructed to lay down.

    The raster rows are transmitted mirrored (``add_raster_data`` flips them
    before packing, which is the wire convention, not a change of direction),
    so a bit's position is unflipped here to get back to the device column the
    dot belongs to. Column order therefore matches the label's own pixel order:
    a label's right-hand end is the high end of the row.
    """
    rows = _raster_rows(instructions, device_width)
    assert rows, "no raster rows in the instruction stream"
    dots = 0
    first, last = None, None
    for row in rows:
        for byte_index, byte in enumerate(row):
            if not byte:
                continue
            for bit in range(8):
                if byte & (1 << (7 - bit)):
                    dots += 1
                    column = device_width - 1 - (byte_index * 8 + bit)
                    first = column if first is None else min(first, column)
                    last = column if last is None else max(last, column)
    return _Ink(dots, first, last)


def _band_label(service, name, left, right, size=(236, 236)):
    """A label of the given size carrying one vertical band of ink."""
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).rectangle((left, 90, right, 140), fill="black")
    path = os.path.join(service.upload_folder, f"{name}.png")
    img.save(path)
    return path


def _block_label(service, label_size, block=(60, 40)):
    """A label of the medium's exact size with a small black block on it.

    A compact block makes the shift unambiguous: its bounding box is the
    content's position, so a translation shows up as an exact pixel delta with
    nothing else moving.
    """
    geometry = get_label_geometry(label_size)
    height = geometry.height or 200
    img = Image.new("RGB", (geometry.width, height), "white")
    left = (geometry.width - block[0]) // 2
    top = (height - block[1]) // 2
    ImageDraw.Draw(img).rectangle(
        (left, top, left + block[0] - 1, top + block[1] - 1), fill="black")
    path = os.path.join(service.upload_folder, f"block_{label_size.replace('+', '')}.png")
    img.save(path)
    return path


# --------------------------------------------------------------------------- #
# Storage: shape, defaults and validation
# --------------------------------------------------------------------------- #

def test_defaults_ship_an_empty_calibration_map():
    """Absent offsets are the default, so nothing existing moves."""
    assert DEFAULT_SETTINGS["calibration"] == {}


@pytest.fixture
def settings_service(tmp_path):
    """A settings service backed by a throwaway file."""
    return SettingsService(settings_file=str(tmp_path / "settings.json"))


def test_valid_calibration_round_trips_through_the_settings_file(settings_service):
    saved = dict(DEFAULT_SETTINGS)
    saved["calibration"] = {"d24": {"x_mm": -0.5, "y_mm": 1.0, "scale": 0.98},
                            "62x29": {"x_mm": 0, "y_mm": 0}}

    assert settings_service.save_settings(saved) is True
    assert settings_service.get_settings()["calibration"] == {
        "d24": {"x_mm": -0.5, "y_mm": 1.0, "scale": 0.98},
        "62x29": {"x_mm": 0, "y_mm": 0},
    }


def test_the_rejected_scale_message_says_what_the_field_is_for(settings_service):
    """
    A range this narrow will be hit by someone trying to resize a design, so
    the message has to redirect rather than just refuse.
    """
    settings = dict(DEFAULT_SETTINGS, calibration={"d24": {"scale": 1.5}})

    with pytest.raises(ValueError) as excinfo:
        settings_service._validate_settings(settings)

    message = str(excinfo.value)
    assert str(CALIBRATION_SCALE_MIN) in message and str(CALIBRATION_SCALE_MAX) in message
    assert "label size" in message


def test_settings_without_calibration_still_validate(settings_service):
    """Every settings file written before this feature existed has to load."""
    legacy = {k: v for k, v in DEFAULT_SETTINGS.items() if k != "calibration"}

    settings_service._validate_settings(legacy)
    assert settings_service.save_settings(legacy) is True
    # The default-key merge fills the map in, so readers never special-case it.
    assert settings_service.get_settings()["calibration"] == {}


@pytest.mark.parametrize("calibration", [
    {"d24": {"x_mm": 0.5}},                      # one axis only
    {"d24": {}},                                 # entry present, no offsets
    {},                                          # explicit empty map
    {"d24": {"x_mm": -CALIBRATION_LIMIT_MM, "y_mm": CALIBRATION_LIMIT_MM}},
    {"not-a-known-label": {"x_mm": 1}},          # media the catalogue may gain
    {"d24": {"scale": 0.98}},                    # size correction on its own
    {"d24": {"x_mm": 0.5, "y_mm": 1.0, "scale": 1.0}},
    {"d24": {"scale": CALIBRATION_SCALE_MIN}},
    {"d24": {"scale": CALIBRATION_SCALE_MAX}},
    {"d24": {"scale": 1}},                       # an int is a fine multiplier
])
def test_accepted_calibration_shapes(settings_service, calibration):
    settings = dict(DEFAULT_SETTINGS, calibration=calibration)

    settings_service._validate_settings(settings)


@pytest.mark.parametrize("calibration, reason", [
    ("d24", "map is not an object"),
    ({"d24": 0.5}, "entry is not an object"),
    ({"d24": {"x": 0.5}}, "misspelt field would silently do nothing"),
    ({"d24": {"x_mm": "0.5"}}, "string instead of a number"),
    ({"d24": {"x_mm": True}}, "bool is not a distance"),
    ({"d24": {"y_mm": CALIBRATION_LIMIT_MM + 0.1}}, "beyond the supported range"),
    ({"d24": {"y_mm": -CALIBRATION_LIMIT_MM - 0.1}}, "beyond the supported range"),
    ({"": {"x_mm": 1}}, "empty label identifier"),
    ({"d24": {"scale": "0.98"}}, "string instead of a multiplier"),
    ({"d24": {"scale": True}}, "bool is not a multiplier"),
    ({"d24": {"scale": 0}}, "a label printed at nothing is not a correction"),
    ({"d24": {"scale": -1.0}}, "a negative multiplier has no meaning"),
    ({"d24": {"scale": CALIBRATION_SCALE_MAX + 0.01}}, "a zoom, not a correction"),
    ({"d24": {"scale": CALIBRATION_SCALE_MIN - 0.01}}, "a zoom, not a correction"),
    ({"d24": {"scale": 2}}, "twice the size is a design change"),
    ({"d24": {"skale": 0.98}}, "misspelt field would silently do nothing"),
])
def test_rejected_calibration_shapes(settings_service, calibration, reason):
    settings = dict(DEFAULT_SETTINGS, calibration=calibration)

    with pytest.raises(ValueError):
        settings_service._validate_settings(settings)
    # A rejected value must never reach the file either.
    assert settings_service.save_settings(settings) is False, reason


def test_calibration_is_inherited_by_requests_that_omit_settings(settings_service):
    """
    The print path reads the offsets off the resolved settings, so a request
    that sends no settings at all still has to print calibrated.
    """
    settings_service.save_settings(
        dict(DEFAULT_SETTINGS, calibration={"d24": {"x_mm": 1.5, "y_mm": -0.5}}))

    resolved = settings_service.resolve_print_settings(None)

    assert resolved["calibration"] == {"d24": {"x_mm": 1.5, "y_mm": -0.5}}


# --------------------------------------------------------------------------- #
# Reading an offset out of the settings
# --------------------------------------------------------------------------- #

def test_offset_lookup_is_keyed_by_label():
    settings = _print_settings("d24", calibration={"d24": {"x_mm": -0.5, "y_mm": 1.0}})

    assert get_calibration_offset(settings) == (-0.5, 1.0)
    # A different roll in the same install is unaffected.
    assert get_calibration_offset(settings, "62x29") == (0.0, 0.0)


@pytest.mark.parametrize("settings", [
    {"label_size": "d24"},                                   # no map at all
    {"label_size": "d24", "calibration": {}},                # empty map
    {"label_size": "d24", "calibration": {"d12": {"x_mm": 3}}},  # other media
    {"label_size": "d24", "calibration": {"d24": {}}},       # entry without axes
])
def test_absent_offsets_read_as_zero(settings):
    assert get_calibration_offset(settings) == (0.0, 0.0)


@pytest.mark.parametrize("calibration", [
    {"d24": "left a bit"},
    {"d24": {"x_mm": "0.5"}},
    {"d24": {"x_mm": True}},
    "not-a-map",
])
def test_malformed_offsets_never_stop_a_print(calibration):
    """
    A junk offset is a correction nobody can honour, but the label itself is
    ready to go -- printing it uncalibrated beats losing the job.
    """
    settings = _print_settings("d24", calibration=calibration)

    assert get_calibration_offset(settings) == (0.0, 0.0)


def test_out_of_range_offsets_are_clamped_not_dropped():
    settings = _print_settings("d24", calibration={
        "d24": {"x_mm": CALIBRATION_LIMIT_MM + 5, "y_mm": -CALIBRATION_LIMIT_MM - 5}})

    assert get_calibration_offset(settings) == (CALIBRATION_LIMIT_MM, -CALIBRATION_LIMIT_MM)


def test_millimetres_convert_to_printer_dots_at_300_dpi():
    """11.811 dots per millimetre; a wrong constant here is a wrong label."""
    assert DOTS_PER_MM == pytest.approx(11.811, abs=0.001)
    assert calibration_offset_px(1.0, -1.0) == (12, -12)
    assert calibration_offset_px(2.0, 0.0) == (24, 0)
    assert calibration_offset_px(0.5, -2.5) == (6, -30)
    assert calibration_offset_px(0.0, 0.0) == (0, 0)


# --------------------------------------------------------------------------- #
# The feed axis: a translation inside the canvas
#
# The vertical half of a calibration offset has no lever to pull. The raster
# starts where the feed starts, and the canvas may not grow (convert() refuses
# a die-cut image that is not exactly the label's printable size), so the
# content is moved inside the canvas it has and an offset large enough loses
# what it pushes over the edge. Deliberate, documented, and asserted here so it
# stays a decision rather than turning into a surprise.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("y_mm", [1.5, -1.5, 0.5, -3.0])
def test_the_feed_axis_moves_content_by_exactly_the_requested_distance(service, y_mm):
    """Millimetres in, that many dots of travel down the tape."""
    source = _open(_block_label(service, "62x29"))
    before = _ink_bbox(source)
    _, dy = calibration_offset_px(0.0, y_mm)

    shifted = service._shift_within_canvas(source, 0, dy, "62x29")
    after = _ink_bbox(shifted)

    assert (after[1] - before[1], after[3] - before[3]) == (dy, dy)
    # And nothing moved sideways: that axis is not this mechanism's business.
    assert (after[0], after[2]) == (before[0], before[2])


@pytest.mark.parametrize("label_size", ALL_MEDIA)
@pytest.mark.parametrize("x_mm, y_mm", [(0.5, 0.0), (-1.0, 1.0), (3.0, -3.0)])
def test_the_canvas_is_never_resized_by_an_offset(service, label_size, x_mm, y_mm):
    """A canvas one dot off its printable size is refused outright."""
    source = _open(_block_label(service, label_size))

    shifted = service._shift_within_canvas(
        source, *calibration_offset_px(x_mm, y_mm), label_size)

    assert shifted.size == source.size
    _accept(shifted, label_size)


def test_positive_y_moves_content_down_the_canvas(service):
    """The documented sign convention for the feed, asserted not assumed."""
    source = _open(_block_label(service, "62x29"))
    before = _ink_bbox(source)

    down = _ink_bbox(service._shift_within_canvas(
        source, 0, calibration_offset_px(0.0, 2.0)[1], "62x29"))

    assert down[1] > before[1] and down[0] == before[0]


# --------------------------------------------------------------------------- #
# The sideways axis: placing the raster in the printer's device row
#
# convert() pastes the label into a device row wider than the label itself, at
#
#     x_offset = device_pixel_width - label_width - right_margin_dots
#
# and that paste position is the whole sideways mechanism. It is arithmetic on
# integers, so it is worth pinning down on its own before watching it act on
# real instruction streams further down.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("dx_dots", [12, -12, 42, -442, 0])
def test_the_plan_asks_for_the_paste_position_the_offset_implies(dx_dots):
    """A right margin N dots smaller puts the raster N dots further right."""
    placement = plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", dx_dots)

    assert placement.base_x_offset == D24_BASE_X_OFFSET
    assert placement.applied_dots == dx_dots
    assert placement.right_margin_dots == label_type_specs["d24"]["right_margin_dots"] - dx_dots
    assert not placement.was_clamped


def test_the_plan_never_places_the_raster_off_the_head():
    """
    The paste position has to stay inside the device row. Beyond it, convert()
    would clip the raster against the row -- the very loss this mechanism
    exists to avoid -- so travel stops at the head and says so.
    """
    for dx_dots in (D24_TRAVEL_RIGHT + 1, 200, 118):
        placement = plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", dx_dots)
        assert placement.applied_dots == D24_TRAVEL_RIGHT
        assert placement.base_x_offset + placement.applied_dots == placement.max_x_offset
        assert placement.was_clamped

    far_left = plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", -D24_BASE_X_OFFSET - 1)
    assert far_left.applied_dots == -D24_BASE_X_OFFSET
    assert far_left.base_x_offset + far_left.applied_dots == 0
    assert far_left.was_clamped


def test_a_clamped_plan_reports_what_it_actually_applied(warnings):
    """"Did less than you asked" has to be legible, not inferred from a label."""
    plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", 118)

    clamped = [context for event, context in warnings
               if event == "Calibration offset exceeds the printer's sideways travel"]
    assert len(clamped) == 1
    assert clamped[0]["requested_px"] == 118
    assert clamped[0]["applied_px"] == D24_TRAVEL_RIGHT
    assert clamped[0]["applied_mm"] == pytest.approx(3.56, abs=0.01)
    assert clamped[0]["travel_px"] == {"left": -D24_BASE_X_OFFSET, "right": D24_TRAVEL_RIGHT}


def test_media_with_no_room_beside_it_offers_no_travel(warnings):
    """
    Tape as wide as the head has nowhere to go, and an identifier brother_ql
    does not know has no geometry to reason about. Both print uncalibrated
    sideways rather than guessing.
    """
    assert plan_raster_placement(696, MODEL, "62", 12) is None
    assert plan_raster_placement(DEVICE_WIDTH, MODEL, "not-a-label", 12) is None
    assert len(warnings) == 2


def test_the_applied_offset_is_the_one_the_printer_can_reach(warnings):
    """
    What a user reads back -- on the label, in a response, in a preview -- has
    to be what the printer will do, not what was asked of it.
    """
    applied = applied_calibration_offset(_calibrated("d24", x_mm=4.0, y_mm=1.0))

    assert applied.requested_x_mm == 4.0
    assert applied.x_mm == D24_TRAVEL_RIGHT_MM
    assert applied.y_mm == 1.0
    assert applied.was_clamped
    assert applied.travel_mm == (D24_TRAVEL_LEFT_MM, D24_TRAVEL_RIGHT_MM)
    # Reporting is not applying: the print path logs the shortfall once, where
    # it happens, so asking what would happen must not add a second warning.
    assert not warnings


def test_the_applied_offset_round_trips_back_to_the_same_dots():
    """
    The reported value is rounded for humans, so it has to survive being fed
    back in: a UI that stores what it was told must not lose a dot.
    """
    applied = applied_calibration_offset(_calibrated("d24", x_mm=CALIBRATION_LIMIT_MM))

    assert calibration_offset_px(applied.x_mm, 0)[0] == D24_TRAVEL_RIGHT


def test_an_offset_within_the_travel_is_reported_unchanged():
    applied = applied_calibration_offset(_calibrated("d24", x_mm=-2.0, y_mm=0.5))

    assert (applied.x_mm, applied.y_mm) == (-2.0, 0.5)
    assert not applied.was_clamped
    assert applied.travel_mm == (D24_TRAVEL_LEFT_MM, D24_TRAVEL_RIGHT_MM)


@pytest.mark.parametrize("settings", [
    _calibrated("d24", x_mm=4.0, printer_model="No-Such-Printer"),
    _calibrated("not-a-label", x_mm=4.0),
])
def test_an_unknown_printer_or_medium_reports_the_offset_as_requested(settings):
    """Not knowing the limit is no reason to invent one."""
    applied = applied_calibration_offset(settings)

    assert applied.x_mm == applied.requested_x_mm == 4.0
    assert not applied.was_clamped
    assert applied.travel_mm is None


def test_a_clamped_target_captions_what_it_actually_printed(service, drawn_lines):
    """
    The defect this closes: a label stating a correction the printer could not
    make is read as evidence that the correction did not work.
    """
    service._render_calibration_target(_calibrated("d24", x_mm=4.0))

    caption = " ".join(drawn_lines)
    assert "R3.6" in caption
    assert "R4.0" not in caption


def test_the_preview_shows_the_applied_offset_too(service):
    """
    The preview carries the same caption as the target, so a picture drawn at
    the requested offset would disagree with its own caption.
    """
    clamped = service.render_calibration_preview(_calibrated("d24", x_mm=4.0))
    reachable = service.render_calibration_preview(
        _calibrated("d24", x_mm=D24_TRAVEL_RIGHT_MM))

    assert clamped == reachable
    assert clamped != service.render_calibration_preview(_print_settings("d24"))


def test_a_calibration_run_describes_what_the_printer_will_do(service):
    described = service.describe_calibration_run(_calibrated("d24", x_mm=4.0, y_mm=1.0))

    assert described["offsets_mm"] == [{"x_mm": D24_TRAVEL_RIGHT_MM, "y_mm": 1.0}]
    assert described["requested_offsets_mm"] == [{"x_mm": 4.0, "y_mm": 1.0}]
    assert described["clamped"] is True
    assert described["sideways_travel_mm"] == {"min": D24_TRAVEL_LEFT_MM,
                                               "max": D24_TRAVEL_RIGHT_MM}
    assert described["scale"] == 1.0


def test_a_run_within_the_travel_reports_no_clamp(service):
    described = service.describe_calibration_run(_calibrated("d24", x_mm=1.0))

    assert described["offsets_mm"] == described["requested_offsets_mm"]
    assert described["clamped"] is False


def test_a_sweep_reports_the_clamp_of_every_step(service):
    """One step over the edge is enough to have to say so."""
    described = service.describe_calibration_run(
        _calibrated("d24", x_mm=3.0), {"axis": "x", "count": 3, "step_mm": 1.0})

    assert described["requested_offsets_mm"] == [
        {"x_mm": 2.0, "y_mm": 0.0}, {"x_mm": 3.0, "y_mm": 0.0},
        {"x_mm": 4.0, "y_mm": 0.0},
    ]
    assert described["offsets_mm"] == [
        {"x_mm": 2.0, "y_mm": 0.0}, {"x_mm": 3.0, "y_mm": 0.0},
        {"x_mm": D24_TRAVEL_RIGHT_MM, "y_mm": 0.0},
    ]
    assert described["clamped"] is True


def test_the_printed_run_reports_the_applied_offsets(service, sent):
    result = service.print_calibration_target(_calibrated("d24", x_mm=4.0))

    assert len(sent) == 1
    assert result["offsets_mm"] == [{"x_mm": D24_TRAVEL_RIGHT_MM, "y_mm": 0.0}]
    assert result["requested_offsets_mm"] == [{"x_mm": 4.0, "y_mm": 0.0}]
    assert result["clamped"] is True


def test_the_model_margin_addition_is_left_for_convert_to_add():
    """
    convert() adds the model's own margin addition on top of the label's, so
    only the label's share may be rewritten -- rewriting the sum would move
    every QL-1100 print by 44 dots.
    """
    placement = plan_raster_placement(1296, "QL-1100", "103", 12)

    assert placement.base_x_offset == 1296 - 1200 - (12 + 44)
    assert placement.right_margin_dots == 12 - 12


# --------------------------------------------------------------------------- #
# The print path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_zero_offset_is_byte_identical_to_no_calibration(service, sent, label_size):
    """
    Every existing installation has to keep printing exactly what it printed
    yesterday, so the proof is the instruction stream, not the image.
    """
    image_path = _block_label(service, label_size)

    service._send_to_printer(image_path, _print_settings(label_size))
    service._send_to_printer(image_path, _calibrated(label_size, 0.0, 0.0))
    # An offset far below a whole dot cannot be printed either.
    service._send_to_printer(image_path, _calibrated(label_size, 0.01, -0.01))

    assert sent[0] == sent[1] == sent[2]


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_the_feed_offset_reaches_the_printer_as_a_shifted_canvas(service, sent, label_size):
    """
    End to end for the axis that has no lever: what reaches the backend must
    equal the instructions for an image translated by hand by the same number
    of dots, with no sideways component involved at all.
    """
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    image_path = _block_label(service, label_size)

    service._send_to_printer(image_path, _calibrated(label_size, 0.0, -1.0))

    source = _open(image_path)
    expected_canvas = Image.new("RGB", source.size, (255, 255, 255))
    expected_canvas.paste(source, calibration_offset_px(0.0, -1.0))
    expected = convert(raster(MODEL), [expected_canvas], label_size, rotate=0)

    assert sent[-1] == expected


# --------------------------------------------------------------------------- #
# The sideways offset on the print path: ink is moved, never lost
#
# This is the section the physical labels wrote. A stored x_mm of 4.0 on d24
# used to translate the content inside a 236 x 236 canvas, which threw a sixth
# of the label away before the data ever reached the printer: the target came
# out missing the right-hand arc of its ring, and "test1234" printed as
# "test123" with the 3 cut in half. Counting the ink dots in the instruction
# stream is what catches that, and what proves the new mechanism does not.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("x_mm", [4.0, 2.0, -4.0, CALIBRATION_LIMIT_MM])
def test_a_sideways_offset_loses_no_ink(service, sent, x_mm):
    """The property the old design violated, on the label that disproved it."""
    image_path = service._create_calibration_label(_print_settings("d24"))

    service._send_to_printer(image_path, _print_settings("d24"))
    service._send_to_printer(image_path, _calibrated("d24", x_mm=x_mm))

    plain, calibrated = (_printed_ink(stream) for stream in sent)
    assert calibrated.dots == plain.dots
    # ...and it really did move, so "lost nothing" is not "did nothing".
    assert calibrated.first != plain.first


def test_the_users_text_label_keeps_its_last_character(service, sent):
    """
    The reported symptom, as a test: 4 mm of correction on a 24 mm label is a
    sixth of its width, and the old design spent that sixth on the last glyph.
    """
    settings = _print_settings("d24")
    image_path = service._create_text_label("test1234", settings)

    service._send_to_printer(image_path, settings)
    service._send_to_printer(image_path, _calibrated("d24", x_mm=4.0))

    plain, calibrated = (_printed_ink(stream) for stream in sent)
    assert calibrated.dots == plain.dots


@pytest.mark.parametrize("x_mm, expected_dots", [
    (1.0, 12), (-1.0, -12), (3.0, 35), (-3.0, -35), (0.5, 6),
])
def test_the_raster_lands_the_requested_number_of_dots_away(service, sent, x_mm,
                                                            expected_dots):
    """Millimetres in, that many dots of travel along the printer's row."""
    image_path = _block_label(service, "d24")

    service._send_to_printer(image_path, _print_settings("d24"))
    service._send_to_printer(image_path, _calibrated("d24", x_mm=x_mm))

    plain, moved = (_printed_ink(stream) for stream in sent)
    assert moved.first - plain.first == expected_dots
    assert moved.last - plain.last == expected_dots
    assert moved.dots == plain.dots


def test_right_on_the_label_is_still_right_on_the_tape(service, sent):
    """
    The direction the user sees must not quietly invert. Two facts close the
    loop, both read off real instruction streams: a band near the label's
    right-hand edge is printed at the high end of the device row, and a
    positive x_mm is what sends a band that way.
    """
    left_band = _band_label(service, "left_band", 10, 30)
    right_band = _band_label(service, "right_band", 205, 225)
    settings = _print_settings("d24")

    service._send_to_printer(left_band, settings)
    service._send_to_printer(right_band, settings)
    left, right = (_printed_ink(stream) for stream in sent)
    assert left.last < right.first, "the label's own right is the high end of the row"

    sent.clear()
    service._send_to_printer(left_band, settings)
    service._send_to_printer(left_band, _calibrated("d24", x_mm=1.0))
    plain, nudged = (_printed_ink(stream) for stream in sent)
    assert nudged.first > plain.first, "R must send the ink towards that same end"


def test_travel_beyond_the_head_is_clamped_and_announced(service, sent, warnings):
    """
    A d24 label already sits 42 dots from the right-hand end of a QL-820NWB's
    row, so a 10 mm correction that way is not something the printer can do.
    It does what it can, keeps every dot, and reports what it actually applied
    instead of silently doing less.
    """
    image_path = _block_label(service, "d24")

    service._send_to_printer(image_path, _print_settings("d24"))
    service._send_to_printer(image_path, _calibrated("d24", x_mm=CALIBRATION_LIMIT_MM))

    plain, moved = (_printed_ink(stream) for stream in sent)
    assert moved.first - plain.first == D24_TRAVEL_RIGHT
    assert moved.dots == plain.dots
    clamped = [context for event, context in warnings
               if event == "Calibration offset exceeds the printer's sideways travel"]
    assert len(clamped) == 1
    assert clamped[0]["applied_px"] == D24_TRAVEL_RIGHT
    assert clamped[0]["requested_px"] == calibration_offset_px(CALIBRATION_LIMIT_MM, 0)[0]


def test_a_sideways_offset_never_reports_clipping(service, sent, warnings):
    """
    The old contract was "shift, clip and warn"; the new one is "x never
    clips". The clipping warning belongs to the feed axis alone now, so a
    sideways offset at the very limit must not raise it on any medium.
    """
    for label_size in ALL_MEDIA:
        image_path = _block_label(service, label_size)
        service._send_to_printer(image_path, _calibrated(label_size, CALIBRATION_LIMIT_MM))
        service._send_to_printer(image_path, _calibrated(label_size, -CALIBRATION_LIMIT_MM))

    assert not [event for event, _ in warnings
                if event == "Calibration offset clips part of the label"]


# --------------------------------------------------------------------------- #
# Borrowing brother_ql's media table without breaking it
#
# The paste position comes out of a module-global dict, so applying an offset
# means editing that dict for the length of one conversion. The edit therefore
# has to be undone whatever happens -- a table left edited would silently
# re-align every later print in the process -- and it has to be serialized, so
# that two conversions cannot see each other's edit.
# --------------------------------------------------------------------------- #

def test_the_media_table_is_left_exactly_as_it_was_found(service, sent):
    original = label_type_specs["d24"]
    snapshot = dict(original)

    service._send_to_printer(_block_label(service, "d24"), _calibrated("d24", 2.0, -1.0))

    assert label_type_specs["d24"] is original
    assert label_type_specs["d24"] == snapshot


def test_the_media_table_is_restored_when_the_conversion_raises(service, monkeypatch):
    """A failed print must not leave every later one 24 dots off."""
    original = label_type_specs["d24"]
    snapshot = dict(original)

    def explode(**_kwargs):
        raise RuntimeError("printer model rejected the raster")

    monkeypatch.setattr(printer_module, "convert", explode)

    with pytest.raises(PrinterError):
        service._send_to_printer(_block_label(service, "d24"), _calibrated("d24", 2.0))

    assert label_type_specs["d24"] is original
    assert label_type_specs["d24"] == snapshot
    assert not printer_module._LABEL_SPEC_LOCK.locked()


def test_the_edited_table_is_only_visible_under_the_lock(service, sent, monkeypatch):
    """
    What the conversion reads has to be the edited entry, and nothing else may
    be reading the table while it does.
    """
    observed = {}
    real_convert = printer_module.convert

    def spy(**kwargs):
        observed["locked"] = printer_module._LABEL_SPEC_LOCK.locked()
        observed["right_margin_dots"] = label_type_specs["d24"]["right_margin_dots"]
        return real_convert(**kwargs)

    monkeypatch.setattr(printer_module, "convert", spy)

    service._send_to_printer(_block_label(service, "d24"), _calibrated("d24", 2.0))

    assert observed["locked"] is True
    assert observed["right_margin_dots"] == 42 - 24


def test_an_uncalibrated_print_never_touches_the_table_or_the_lock(service, sent,
                                                                   monkeypatch):
    """The zero short-circuit, at the level below byte-identical output."""
    observed = {}
    real_convert = printer_module.convert

    def spy(**kwargs):
        observed["locked"] = printer_module._LABEL_SPEC_LOCK.locked()
        observed["right_margin_dots"] = label_type_specs["d24"]["right_margin_dots"]
        return real_convert(**kwargs)

    monkeypatch.setattr(printer_module, "convert", spy)

    # Feed-only calibration counts as uncalibrated for the sideways table.
    service._send_to_printer(_block_label(service, "d24"), _calibrated("d24", 0.0, 1.0))

    assert observed["locked"] is False
    assert observed["right_margin_dots"] == 42


def test_the_placement_holds_the_lock_against_a_second_thread():
    """
    Two threads with different offsets must not overlap inside the table, and
    the second one has to plan against the *restored* entry -- planning against
    whatever the first published would correct its label by the difference
    between the two offsets instead of by its own.
    """
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster(MODEL)
    seen = []
    entered = threading.Event()
    release = threading.Event()

    def second():
        entered.wait(5)
        # The table still reads as edited by the first thread, and the second
        # thread is made to wait for it rather than interleaving.
        seen.append(printer_module._LABEL_SPEC_LOCK.locked())
        release.set()
        with placed_raster(raster, "d24", -24):
            seen.append(label_type_specs["d24"]["right_margin_dots"])

    worker = threading.Thread(target=second)
    worker.start()
    with placed_raster(raster, "d24", 24):
        assert label_type_specs["d24"]["right_margin_dots"] == 42 - 24
        entered.set()
        release.wait(5)
    worker.join(5)

    assert seen == [True, 42 + 24]
    assert label_type_specs["d24"]["right_margin_dots"] == 42


def test_every_content_type_goes_through_the_same_funnel(service, sent):
    """
    Text, images and QR codes all reach the printer through _send_to_printer,
    which is why the offset is applied there and nowhere else.
    """
    settings = _calibrated("62x29", x_mm=2.0)
    uncalibrated = _print_settings("62x29")
    photo = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(photo).rectangle((10, 10, 200, 100), fill="black")
    photo_path = os.path.join(service.upload_folder, "photo.png")
    photo.save(photo_path)

    for renderer in (
        lambda s: service._create_text_label("Shelf B2", s),
        lambda s: service._create_qr_code("https://example.org/abc", s),
        lambda s: service._resize_image(photo_path, s.get("label_size")),
    ):
        sent.clear()
        service._send_to_printer(renderer(uncalibrated), uncalibrated)
        service._send_to_printer(renderer(settings), settings)
        assert sent[0] != sent[1]


def test_a_feed_offset_that_clips_warns_instead_of_crashing(service, sent, warnings):
    """
    A feed offset large enough to push content off the label still prints --
    with the amount that was lost in the log, because a label that quietly
    comes back with a corner missing is worse than one that was announced.
    Unchanged from the original design, and unchangeable: the raster starts
    where the feed starts.
    """
    image_path = _block_label(service, "d12", block=(60, 60))

    service._send_to_printer(image_path, _calibrated("d12", 0.0, -CALIBRATION_LIMIT_MM))

    clipped = [context for event, context in warnings
               if event == "Calibration offset clips part of the label"]
    assert len(clipped) == 1
    assert clipped[0]["clipped_px"]["top"] > 0
    assert clipped[0]["clipped_px"]["left"] == 0
    assert clipped[0]["clipped_px"]["right"] == 0
    # And the label was still sent, at the label's exact size.
    assert len(sent) == 1


def test_a_feed_offset_that_loses_nothing_does_not_warn(service, warnings):
    """The warning has to mean something, so it must not cry wolf."""
    source = _open(_block_label(service, "62x29", block=(60, 40)))

    service._shift_within_canvas(source, 0, 12, "62x29")

    assert not [event for event, _ in warnings
                if event == "Calibration offset clips part of the label"]


def test_alpha_images_are_flattened_onto_white_not_black(service):
    """
    A transparent PNG pasted straight onto a canvas turns its background black.
    convert() flattens alpha onto white on its way out, and so must the shift.
    """
    source = Image.new("RGBA", (696, 271), (255, 255, 255, 0))
    ImageDraw.Draw(source).rectangle((100, 100, 200, 150), fill=(0, 0, 0, 255))

    shifted = service._shift_within_canvas(source, 0, 12, "62x29")

    assert _ink_bbox(shifted) == (100, 112, 201, 163)


# --------------------------------------------------------------------------- #
# The size correction
#
# A printer can lay ink down slightly larger or smaller than it was asked to.
# The correction is a multiplier stored beside the offsets and applied at the
# same funnel: content resized about the centre of a canvas that may not grow.
# Like the offsets it is a *printer* correction, so it never touches a preview
# -- the preview is the design the user means to have, and calibration exists
# to make the paper match it.
# --------------------------------------------------------------------------- #

def _ink_centre(img):
    """Centre of the ink, as (x, y), for measuring what a transform did."""
    box = _ink_bbox(img)
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _corner_blob_label(service, label_size="d24", inset=3, block=12):
    """A round label with a blob sitting just inside the die cut, at 45 deg.

    Where the circle is closest to the canvas, so growing the content takes it
    off the *label* without taking it off the canvas -- the loss a canvas-only
    check cannot see.
    """
    geometry = get_label_geometry(label_size)
    size = geometry.width
    centre = (size - 1) / 2.0
    reach = (size / 2.0 - inset) / math.sqrt(2)
    corner = int(round(centre - reach))
    img = Image.new("RGB", (size, size), "white")
    ImageDraw.Draw(img).rectangle((corner, corner, corner + block, corner + block),
                                  fill="black")
    path = os.path.join(service.upload_folder, f"blob_{label_size}.png")
    img.save(path)
    return path


def test_the_scale_defaults_to_one_and_is_read_per_label():
    settings = _print_settings("d24", calibration={"d24": {"scale": 0.98}})

    assert get_calibration_scale(settings) == 0.98
    # A different roll in the same install is unaffected.
    assert get_calibration_scale(settings, "62x29") == 1.0
    assert get_calibration_scale(_print_settings("d24")) == 1.0
    assert get_calibration_scale(_calibrated("d24", 1.0, 1.0)) == 1.0


@pytest.mark.parametrize("scale", ["0.98", True, None, [0.98]])
def test_a_malformed_scale_never_stops_a_print(scale):
    """Same bargain as a malformed offset: print it as rendered, and say so."""
    settings = _print_settings("d24", calibration={"d24": {"scale": scale}})

    assert get_calibration_scale(settings) == 1.0


def test_an_out_of_range_scale_is_clamped_not_dropped():
    assert get_calibration_scale(
        _print_settings("d24", calibration={"d24": {"scale": 2.0}})) == CALIBRATION_SCALE_MAX
    assert get_calibration_scale(
        _print_settings("d24", calibration={"d24": {"scale": 0.1}})) == CALIBRATION_SCALE_MIN


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_a_scale_of_one_is_byte_identical_to_no_scale(service, sent, label_size):
    """Nobody's existing setup moves, and nobody's resamples either."""
    image_path = _block_label(service, label_size)

    service._send_to_printer(image_path, _print_settings(label_size))
    service._send_to_printer(image_path, _calibrated(label_size, 0.0, 0.0, scale=1.0))

    assert sent[0] == sent[1]


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_scaling_never_resizes_the_canvas(service, label_size):
    source = _open(_block_label(service, label_size))

    for scale in (CALIBRATION_SCALE_MIN, CALIBRATION_SCALE_MAX):
        scaled = service._scale_within_canvas(source, scale, label_size)
        assert scaled.size == source.size
        _accept(scaled, label_size)


def test_scaling_down_shrinks_the_content_about_the_centre(service):
    source = _open(_block_label(service, "62x29"))
    before = _ink_bbox(source)

    scaled = service._scale_within_canvas(source, 0.95, "62x29")
    after = _ink_bbox(scaled)

    assert (after[2] - after[0]) == pytest.approx((before[2] - before[0]) * 0.95, abs=2)
    assert (after[3] - after[1]) == pytest.approx((before[3] - before[1]) * 0.95, abs=2)
    # Centred, not nudged: the offsets are the dial that moves a label.
    assert _ink_centre(scaled) == pytest.approx(_ink_centre(source), abs=1)


def test_scaling_up_clips_at_the_rim_and_warns(service, warnings):
    """Same bargain as the feed axis: the canvas may not grow, so say so."""
    target = service._render_calibration_target(_print_settings("62x29"))

    scaled = service._scale_within_canvas(target, 1.05, "62x29")

    clipped = [context for event, context in warnings
               if event == "Calibration scale clips part of the label"]
    assert len(clipped) == 1
    assert any(clipped[0]["clipped_px"][side] for side in ("left", "right", "top", "bottom"))
    assert scaled.size == target.size


def test_scaling_down_does_not_cry_wolf(service, warnings):
    service._scale_within_canvas(
        _open(_block_label(service, "62x29")), 0.95, "62x29")

    assert not [event for event, _ in warnings
                if event == "Calibration scale clips part of the label"]


def test_scaling_up_on_round_media_reports_ink_pushed_off_the_die_cut(service, warnings):
    """
    A round label is die-cut to the circle inside its square canvas, so ink can
    leave the label while every dot is still on the canvas. It lands on the
    backing paper, which is no better than losing it.
    """
    source = _open(_corner_blob_label(service, "d24"))
    assert _ink_outside_circle(source) == 0

    scaled = service._scale_within_canvas(source, 1.05, "d24")

    assert _ink_outside_circle(scaled) > 0
    clipped = [context for event, context in warnings
               if event == "Calibration scale clips part of the label"]
    assert len(clipped) == 1
    assert clipped[0]["outside_die_cut_px"] > 0
    # ...and the canvas itself lost nothing, which is exactly the case a
    # canvas-only check would have called clean.
    assert not any(clipped[0]["clipped_px"].values())


def test_a_square_label_is_not_mistaken_for_a_round_one(service, warnings):
    """
    23x23 is a square *rectangular* die-cut label whose corners print perfectly
    well. Inferring roundness from a square canvas would report a quarter of it
    as lost on every upscale.
    """
    source = _open(_corner_blob_label(service, "23x23"))

    service._scale_within_canvas(source, 1.05, "23x23")

    reported = [context for event, context in warnings
                if event == "Calibration scale clips part of the label"]
    assert not reported


def test_the_scale_reaches_the_printer_through_the_same_funnel(service, sent):
    image_path = _block_label(service, "62x29")

    service._send_to_printer(image_path, _print_settings("62x29"))
    service._send_to_printer(image_path, _calibrated("62x29", scale=0.95))

    assert sent[0] != sent[1]


def test_the_size_correction_does_not_move_the_alignment(service, sent):
    """
    Order of operations, and the reason for it: scaling about the centre after
    an offset multiplies that offset too, so correcting the size would silently
    un-correct the alignment the user had just measured. Scale first, then
    offset, and the two dials stay independent.
    """
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    image_path = _block_label(service, "62x29")
    source = _open(image_path)
    dy = calibration_offset_px(0.0, 8.0)[1]
    canvas_centre_y = (source.height - 1) / 2.0

    ours = service._shift_within_canvas(
        service._scale_within_canvas(source, 0.95, "62x29"), 0, dy, "62x29")
    reversed_order = service._scale_within_canvas(
        service._shift_within_canvas(source, 0, dy, "62x29"), 0.95, "62x29")

    # The offset is honoured in full...
    assert _ink_centre(ours)[1] - canvas_centre_y == pytest.approx(dy, abs=1)
    # ...where the other order would have delivered 95 % of it, silently.
    assert (_ink_centre(reversed_order)[1] - canvas_centre_y
            == pytest.approx(dy * 0.95, abs=1))

    # And the print path really is the first of the two.
    service._send_to_printer(image_path, _calibrated("62x29", 0.0, 8.0, scale=0.95))
    assert sent[-1] == convert(raster(MODEL), [ours], "62x29", rotate=0)


def test_the_sideways_offset_is_unaffected_by_the_scale(service, sent):
    """
    x is a placement, not a translation, so the scale cannot multiply it even
    in principle -- asserted so that a future refactor cannot make it.
    """
    image_path = _block_label(service, "d24")

    service._send_to_printer(image_path, _calibrated("d24", scale=0.95))
    service._send_to_printer(image_path, _calibrated("d24", x_mm=2.0, scale=0.95))

    plain, moved = (_printed_ink(stream) for stream in sent)
    assert moved.first - plain.first == calibration_offset_px(2.0, 0)[0]
    assert moved.dots == plain.dots


def test_the_target_captions_the_size_it_was_printed_at(service, drawn_lines):
    """Two targets printed at different sizes are otherwise identical twins."""
    service._render_calibration_target(_calibrated("d24", -0.5, 1.0, scale=0.98))

    caption = " ".join(drawn_lines)
    assert "98%" in caption
    assert "L0.5" in caption


def test_an_uncorrected_target_says_nothing_about_size(service, drawn_lines):
    service._render_calibration_target(_calibrated("d24", -0.5, 1.0))

    assert "%" not in " ".join(drawn_lines)


@pytest.mark.parametrize("x_mm, y_mm, scale, expected", [
    (0.5, -1.5, 1.0, "R0.5 U1.5"),
    (0.5, -1.5, 0.98, "R0.5 U1.5 98%"),
    (0.0, 0.0, 0.98, "98%"),
    (0.0, 0.0, 1.0, "centred"),
    (-2.0, 0.0, 1.02, "L2.0 102%"),
])
def test_the_caption_names_the_size_only_when_there_is_one(x_mm, y_mm, scale, expected):
    assert format_calibration_offset(x_mm, y_mm, scale) == expected


def test_a_printed_target_carries_the_size_correction(service, sent):
    """
    The target is printed through the same correction as everything else: one
    printed at a different size from the labels it is calibrating would be
    measuring the wrong thing.
    """
    service.print_calibration_target(_calibrated("d24", 0.5, 0.0))
    service.print_calibration_target(_calibrated("d24", 0.5, 0.0, scale=0.95))

    assert len(sent) == 2
    assert sent[0] != sent[1]


def test_every_sweep_step_keeps_the_size_correction(service, sent):
    service.print_calibration_target(
        _calibrated("d24", 0.0, 0.0, scale=0.95), {"axis": "x", "count": 3, "step_mm": 0.5})
    scaled = list(sent)
    sent.clear()
    service.print_calibration_target(
        _calibrated("d24", 0.0, 0.0), {"axis": "x", "count": 3, "step_mm": 0.5})

    assert len(scaled) == len(sent) == 3
    assert all(a != b for a, b in zip(scaled, sent))


# --------------------------------------------------------------------------- #
# Previews must not move
#
# The design decision most likely to be broken later, so it is asserted
# explicitly: a preview answers "is my design right?" and stands for the label
# the user means to have. Calibration exists to make the paper match it, so a
# preview that moved too would leave the user chasing a moving target.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_text_preview_ignores_calibration(service, label_size):
    plain = service.render_text_preview("Shelf B2", _print_settings(label_size))
    calibrated = service.render_text_preview(
        "Shelf B2", _calibrated(label_size, 3.0, -2.0))

    assert plain == calibrated


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_previews_ignore_the_size_correction_too(service, label_size):
    """
    The user chose this deliberately: scale is a printer correction, so the
    preview keeps standing for the design and the correction makes the paper
    match it. A preview that resized with it would be a design tool wearing a
    calibration label.
    """
    photo = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(photo).rectangle((10, 10, 200, 100), fill="black")
    path = os.path.join(service.upload_folder, "scaled_preview.png")
    photo.save(path)
    scaled = _calibrated(label_size, scale=0.95)

    assert (service.render_text_preview("Shelf B2", _print_settings(label_size))
            == service.render_text_preview("Shelf B2", scaled))
    assert (service.render_image_preview(path, _print_settings(label_size))
            == service.render_image_preview(path, scaled))


def test_qrcode_and_label_previews_ignore_calibration(service):
    settings = _print_settings("62x29", data="https://example.org/abc")
    calibrated = dict(settings, calibration={"62x29": {"x_mm": 3.0, "y_mm": -2.0}})

    assert (service.render_qrcode_preview(settings)
            == service.render_qrcode_preview(calibrated))

    side_by_side = dict(settings, side_by_side=True, side_text="Shelf B2")
    calibrated_side = dict(calibrated, side_by_side=True, side_text="Shelf B2")
    assert (service.render_label_preview(side_by_side)
            == service.render_label_preview(calibrated_side))


def test_image_preview_ignores_calibration(service):
    photo = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(photo).rectangle((10, 10, 200, 100), fill="black")
    path = os.path.join(service.upload_folder, "preview.png")
    photo.save(path)

    assert (service.render_image_preview(path, _print_settings("62x29"))
            == service.render_image_preview(path, _calibrated("62x29", 3.0, -2.0)))


def test_the_calibration_preview_is_the_one_that_does_move(service):
    """
    The documented exception: the target's subject is where the ink lands, so
    a picture of it without the shift would be a picture of the wrong thing.
    """
    plain = service.render_calibration_preview(_print_settings("d24"))
    calibrated = service.render_calibration_preview(_calibrated("d24", 2.0, 0.0))

    assert plain != calibrated


# --------------------------------------------------------------------------- #
# The target
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size, expected", [
    ("d24", SIZE_D24), ("d12", SIZE_D12), ("62x29", SIZE_62X29)])
def test_the_target_is_exactly_the_die_cut_label(service, label_size, expected):
    target = service._render_calibration_target(_print_settings(label_size))

    assert target.size == expected
    _accept(target, label_size)


@pytest.mark.parametrize("label_size, width", [("12", WIDTH_12MM), ("62", WIDTH_62MM)])
def test_continuous_targets_get_a_fixed_length(service, label_size, width):
    """Endless tape has no length of its own, so the target picks one."""
    target = service._render_calibration_target(_print_settings(label_size))

    assert target.size == (width, round(CALIBRATION_TARGET_LENGTH_MM * DOTS_PER_MM))
    _accept(target, label_size)


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
def test_round_targets_keep_all_their_ink_inside_the_circle(service, label_size):
    """Ink in the corners is ink on the backing paper, not on the label."""
    target = service._render_calibration_target(_print_settings(label_size))

    assert _ink_outside_circle(target) == 0


@pytest.mark.parametrize("label_size", ALL_MEDIA + ["d58"])
def test_the_target_scale_really_is_a_millimetre_apart(service, label_size):
    """
    The property the whole design rests on: the user counts marks instead of
    reaching for a ruler, so the marks have to be a millimetre apart to within
    a dot.
    """
    target = service._render_calibration_target(_print_settings(label_size))
    geometry = get_label_geometry(label_size)
    centre_y = (target.height - 1) / 2.0
    # A row just above the axis line the marks hang from: inside even the
    # shortest of them, and clear of the other axis's marks (the nearest is a
    # whole millimetre away).
    stroke = 1 if min(target.size) < 150 else 2
    row = int(centre_y) - stroke - 1
    pixels = target.convert("L").load()

    runs = []
    start = None
    for x in range(target.width):
        dark = pixels[x, row] < 128
        if dark and start is None:
            start = x
        elif not dark and start is not None:
            runs.append((start + x - 1) / 2.0)
            start = None
    if start is not None:
        runs.append((start + target.width - 1) / 2.0)

    # Drop the frame/ring, which is not part of the scale.
    edge = 6
    marks = [x for x in runs if edge <= x <= target.width - 1 - edge]
    # Every millimetre from the centre to the edge is marked (bar the outermost
    # one or two, which the frame or the narrowing circle swallows).
    assert len(marks) >= 2 * int((geometry.width / 2.0) / DOTS_PER_MM) - 4

    centre_x = (target.width - 1) / 2.0
    for mark in marks:
        steps = (mark - centre_x) / DOTS_PER_MM
        drift_px = abs(steps - round(steps)) * DOTS_PER_MM
        assert drift_px <= 1.5, (mark, steps)
    gaps = [round(b - a) for a, b in zip(marks, marks[1:])]
    assert set(gaps) <= {11, 12}, gaps


@pytest.mark.parametrize("label_size", ["d24", "62x29", "62"])
def test_every_fifth_mark_is_longer(service, label_size):
    """Counting single millimetres to five is slow; the long mark is the anchor."""
    target = service._render_calibration_target(_print_settings(label_size))
    pixels = target.convert("L").load()
    centre_x = (target.width - 1) / 2.0
    centre_y = (target.height - 1) / 2.0

    def mark_length(step):
        """How far the mark ``step`` mm right of centre reaches up the label."""
        column = int(round(centre_x + step * DOTS_PER_MM))
        reach = 0
        for y in range(int(centre_y) - 1, -1, -1):
            if pixels[column, y] >= 128:
                break
            reach += 1
        return reach

    assert mark_length(5) > mark_length(4)
    assert mark_length(5) > mark_length(6)


@pytest.mark.parametrize("label_size", ["d24", "62x29", "62", "12"])
def test_the_target_prints_the_offset_it_was_printed_with(service, drawn_lines, label_size):
    """Three iterations on a desk are indistinguishable without this."""
    service._render_calibration_target(_calibrated(label_size, -0.5, 1.0))

    caption = " ".join(drawn_lines)
    assert "L0.5" in caption
    assert "D1.0" in caption


def test_larger_media_also_names_the_label(service, drawn_lines):
    service._render_calibration_target(_calibrated("d24", -0.5, 1.0))

    assert "d24" in " ".join(drawn_lines)


def test_the_smallest_round_label_still_gets_a_readable_caption(service, drawn_lines):
    """
    d12 has under 8 mm of printable circle: the caption is shortened rather
    than shrunk into a smudge, and never below the legibility floor.
    """
    target = service._render_calibration_target(_calibrated("d12", -0.5, 1.0))

    assert target.size == SIZE_D12
    assert drawn_lines == ["L0.5 D1.0"]
    assert _ink_outside_circle(target) == 0


# --------------------------------------------------------------------------- #
# The caption states a direction, and it has to survive being printed
#
# The label carries its own offset in type barely a millimetre tall, converted
# to 1 bit on the way to the head. A sign is a hairline: at 14 px Arial's "+"
# loses its upright and prints as a bare bar, so a target that was shifted
# right tells the user it was shifted left and they calibrate the wrong way.
# Comparing caption *strings* cannot see that at all -- the string was always
# right -- so these tests look at the ink.
# --------------------------------------------------------------------------- #

DIRECTION_MARKERS = ("R", "L", "U", "D")


def _available_fonts():
    """Every TrueType font on this host the caption might end up using."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    return [path for path in candidates if os.path.exists(path)]


@pytest.mark.parametrize("marker", DIRECTION_MARKERS)
@pytest.mark.parametrize("font_path", _available_fonts() or [None])
def test_direction_markers_survive_the_one_bit_conversion(service, font_path, marker):
    """
    A marker reduced to a single row of ink is a marker that can be misread as
    another one. Checked at the smallest size the caption is ever printed at,
    against every font this host could hand the renderer.
    """
    if font_path is None:
        pytest.skip("no TrueType font available on this host")
    service.font_path = font_path
    font = service._calibration_font(MIN_CALIBRATION_FONT_PX)
    assert font is not None

    canvas = Image.new("RGB", (60, 60), "white")
    ImageDraw.Draw(canvas).text((8, 8), marker, font=font, fill="black")
    printed = service._to_print_appearance(canvas, {"threshold": 70.0})

    box = _ink_bbox(printed)
    assert box is not None, f"{marker} vanished entirely at {MIN_CALIBRATION_FONT_PX}px"
    # A letterform, not a bar: this is exactly the check a "+" fails.
    assert box[3] - box[1] >= 4, f"{marker} printed flat: {box}"
    assert box[2] - box[0] >= 2, f"{marker} printed as a hairline: {box}"


@pytest.mark.parametrize("x_mm, y_mm, expected", [
    (0.5, -1.5, "R0.5 U1.5"),
    (-2.0, 0.0, "L2.0"),
    (0.0, 1.0, "D1.0"),
    (0.0, 0.0, "centred"),     # zero has no direction
    (-0.0, 0.0, "centred"),    # and no "-0.0" either
    (0.04, 0.0, "centred"),    # below half a dot: not printable, so not claimed
])
def test_offsets_are_spelled_with_directions_not_signs(x_mm, y_mm, expected):
    assert format_calibration_offset(x_mm, y_mm) == expected


@pytest.mark.parametrize("label_size", ["d24", "d12", "62x29", "12"])
@pytest.mark.parametrize("x_mm, y_mm", [(0.5, -1.5), (-2.0, 0.0), (0.0, 0.0)])
def test_no_printed_caption_carries_its_direction_in_a_sign(
        service, drawn_lines, label_size, x_mm, y_mm):
    """The design rule, guarded: no glyph that can degrade into its opposite."""
    service._render_calibration_target(_calibrated(label_size, x_mm, y_mm))

    caption = " ".join(drawn_lines)
    assert "+" not in caption
    assert "-" not in caption
    assert "−" not in caption  # nor a typographic minus


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
@pytest.mark.parametrize("x_mm, y_mm", [
    (0.0, 0.0), (0.5, -1.5), (-2.0, 0.0), (2.0, -2.0),
    (CALIBRATION_LIMIT_MM, -CALIBRATION_LIMIT_MM),
    (-CALIBRATION_LIMIT_MM, CALIBRATION_LIMIT_MM),
])
def test_a_captioned_round_target_keeps_its_caption_inside_the_circle(
        service, label_size, x_mm, y_mm):
    """
    The caption's width has to be measured against the *chord at its own
    height*, not against the label's width: the circle has already narrowed by
    the time the caption is reached, so a caption that "fits the label" can
    still hang off the die cut and lose its first character -- which is the one
    carrying the direction.
    """
    target = service._render_calibration_target(_calibrated(label_size, x_mm, y_mm))

    assert _ink_outside_circle(target) == 0


@pytest.mark.parametrize("label_size", ["d12", "d24"])
@pytest.mark.parametrize("font_path", _available_fonts() or [None])
def test_the_caption_fits_the_circle_whatever_font_the_host_resolves(
        service, font_path, label_size):
    """
    The app picks its font by probing the host, so the caption's width is not
    known when the layout is written. A caption too wide for the chord has to
    shrink or be dropped -- never drawn over the rim.
    """
    if font_path is None:
        pytest.skip("no TrueType font available on this host")
    service.font_path = font_path

    for note in (None, "#5", "#123456789"):
        target = service._render_calibration_target(
            _calibrated(label_size, -2.0, 0.0), note=note)
        assert _ink_outside_circle(target) == 0, (font_path, note)


def test_a_sweep_step_keeps_its_number_even_on_the_smallest_label(service, drawn_lines):
    """A sweep is judged by picking a numbered label out of several."""
    service._render_calibration_target(_calibrated("d12", -1.0, 0.0), note="#3")

    assert any("#3" in line for line in drawn_lines)


def test_the_target_renders_without_a_font(service):
    """The scale is geometry; a host with no font still gets a usable target."""
    service.font_path = None

    target = service._render_calibration_target(_print_settings("d24"))

    assert target.size == SIZE_D24
    assert _ink_outside_circle(target) == 0
    _accept(target, "d24")


# --------------------------------------------------------------------------- #
# Printing the target, and the one-pass sweep
# --------------------------------------------------------------------------- #

def test_the_target_prints_with_the_offset_applied(service, sent):
    """
    The exception to "printing is calibrated, previews are not" cuts both ways:
    the target is printed *through* the calibration, because that is what is
    being judged.
    """
    service.print_calibration_target(_print_settings("d24"))
    service.print_calibration_target(_calibrated("d24", 2.0, 0.0))

    assert len(sent) == 2
    assert sent[0] != sent[1]


def test_a_plain_test_print_uses_a_single_label(service, sent):
    result = service.print_calibration_target(_calibrated("d24", 0.5, -0.5))

    assert len(sent) == 1
    assert result["offsets_mm"] == [{"x_mm": 0.5, "y_mm": -0.5}]
    assert result["label_size"] == "d24"


def test_a_sweep_brackets_the_current_offset(service):
    settings = _calibrated("d24", x_mm=1.0, y_mm=-0.5)

    offsets = service.plan_calibration_offsets(
        settings, {"axis": "x", "count": 5, "step_mm": 0.5})

    assert offsets == [
        {"x_mm": 0.0, "y_mm": -0.5},
        {"x_mm": 0.5, "y_mm": -0.5},
        {"x_mm": 1.0, "y_mm": -0.5},
        {"x_mm": 1.5, "y_mm": -0.5},
        {"x_mm": 2.0, "y_mm": -0.5},
    ]


def test_a_sweep_steps_one_axis_at_a_time(service):
    offsets = service.plan_calibration_offsets(
        _print_settings("d24"), {"axis": "y", "count": 3, "step_mm": 1.0})

    assert [o["y_mm"] for o in offsets] == [-1.0, 0.0, 1.0]
    assert {o["x_mm"] for o in offsets} == {0.0}


def test_a_sweep_never_steps_past_the_supported_range(service):
    offsets = service.plan_calibration_offsets(
        _calibrated("d24", x_mm=CALIBRATION_LIMIT_MM),
        {"axis": "x", "count": 3, "step_mm": 1.0})

    assert max(o["x_mm"] for o in offsets) == CALIBRATION_LIMIT_MM


@pytest.mark.parametrize("sweep", [
    {"axis": "diagonal", "count": 3, "step_mm": 0.5},
    {"axis": "x", "count": 1, "step_mm": 0.5},
    {"axis": "x", "count": 99, "step_mm": 0.5},
    {"axis": "x", "count": 3, "step_mm": 0.0},
    {"axis": "x", "count": 3, "step_mm": 50},
    {"axis": "x", "count": "many", "step_mm": 0.5},
])
def test_bad_sweep_parameters_are_a_client_error(service, sweep):
    with pytest.raises(ValidationError):
        service.plan_calibration_offsets(_print_settings("d24"), sweep)


def test_a_sweep_prints_one_numbered_label_per_step(service, sent, drawn_lines):
    result = service.print_calibration_target(
        _calibrated("d24", 0.0, 0.0), {"axis": "x", "count": 5, "step_mm": 0.5})

    assert len(sent) == 5
    assert result["offsets_mm"] == [
        {"x_mm": -1.0, "y_mm": 0.0}, {"x_mm": -0.5, "y_mm": 0.0},
        {"x_mm": 0.0, "y_mm": 0.0}, {"x_mm": 0.5, "y_mm": 0.0},
        {"x_mm": 1.0, "y_mm": 0.0},
    ]
    caption = " ".join(drawn_lines)
    for step in range(1, 6):
        assert f"#{step}" in caption
    # Each label really is shifted differently; the middle one is the current
    # setting and the two ends bracket it.
    assert len(set(sent)) == 5


def test_a_sweep_prints_one_label_per_step_regardless_of_copies(service, sent):
    """Burning five labels is bad enough; five times copies would be worse."""
    service.print_calibration_target(
        _calibrated("d24", 0.0, 0.0, copies=4), {"axis": "x", "count": 3, "step_mm": 0.5})

    assert len(sent) == 3


def test_printing_a_target_without_a_label_size_is_a_client_error(service):
    with pytest.raises(ValidationError):
        service.print_calibration_target({"printer_uri": "tcp://192.0.2.10",
                                          "printer_model": "QL-820NWB"})


def test_calibration_previews_are_png_data_urls(service):
    data_url = service.render_calibration_preview(_calibrated("d24", 0.5, 0.0))

    assert data_url.startswith("data:image/png;base64,")


# --------------------------------------------------------------------------- #
# The API surface
# --------------------------------------------------------------------------- #

@pytest.fixture
def controller(monkeypatch, tmp_path, service):
    """The calibration controller wired to throwaway settings and no queue."""
    module = pytest.importorskip("src.api.calibration_controller")
    settings = SettingsService(settings_file=str(tmp_path / "settings.json"))
    settings.save_settings(dict(DEFAULT_SETTINGS, label_size="62",
                                calibration={"d24": {"x_mm": -0.5, "y_mm": 1.0}}))

    queued = []

    class _Queue:
        def submit(self, job_type, label, fn, params=None, file_path=None):
            queued.append({"type": job_type, "label": label, "fn": fn, "params": params})
            return "job-1"

    monkeypatch.setattr(module, "settings_service", settings)
    monkeypatch.setattr(module, "printer_service", service)
    monkeypatch.setattr(module, "print_queue", _Queue())
    module.queued = queued  # exposed for the assertions below
    return module


def test_test_print_queues_a_job_and_reports_the_offsets(controller):
    response = controller.test_print_calibration({"label_size": "d24"})

    assert response["success"] is True
    assert response["job_id"] == "job-1"
    assert response["label_size"] == "d24"
    # The offsets come from the saved settings, keyed by the requested label.
    assert response["offsets_mm"] == [{"x_mm": -0.5, "y_mm": 1.0}]
    assert controller.queued[0]["type"] == "calibration"


def test_the_top_level_label_size_wins_over_the_saved_one(controller):
    response = controller.test_print_calibration({"label_size": "62x29"})

    assert response["label_size"] == "62x29"
    assert controller.queued[0]["params"]["settings"]["label_size"] == "62x29"


def test_an_inline_offset_overrides_the_stored_one_without_saving_it(controller):
    response = controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 1.5, "y_mm": 0}})

    assert response["offsets_mm"] == [{"x_mm": 1.5, "y_mm": 0.0}]
    # Nothing was written: the stored value is still what it was.
    assert controller.settings_service.get_settings()["calibration"]["d24"] == {
        "x_mm": -0.5, "y_mm": 1.0}


@pytest.mark.parametrize("offset", [
    {"x_mm": "1.5"},
    {"x_mm": True},
    {"x_min": 1.5},
    {"x_mm": CALIBRATION_LIMIT_MM + 1},
    "1.5",
    {"scale": "0.98"},
    {"scale": True},
    {"scale": CALIBRATION_SCALE_MAX + 0.5},
    {"skale": 0.98},
])
def test_a_bad_inline_offset_is_a_client_error(controller, offset):
    with pytest.raises(ValidationError):
        controller.test_print_calibration({"label_size": "d24", "offset": offset})


def test_an_inline_offset_keeps_the_stored_size_correction(controller):
    """
    The inline override exists to try an *alignment* on paper before saving it.
    Printing that trial at a different size than everything else would make it
    a trial of the wrong thing.
    """
    stored = controller.settings_service.get_settings()
    stored["calibration"]["d24"]["scale"] = 0.98
    controller.settings_service.save_settings(stored)

    controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 1.5, "y_mm": 0}})

    entry = controller.queued[0]["params"]["settings"]["calibration"]["d24"]
    assert entry == {"x_mm": 1.5, "y_mm": 0.0, "scale": 0.98}


def test_an_inline_scale_overrides_the_stored_one_without_saving_it(controller):
    response = controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 0, "y_mm": 0, "scale": 1.02}})

    assert response["scale"] == 1.02
    assert "scale" not in controller.settings_service.get_settings()["calibration"]["d24"]


def test_the_response_reports_what_the_printer_can_actually_do(controller):
    """
    The UI has to be able to say "3.5 mm is all this medium allows that way"
    rather than appearing to accept 4 mm and quietly doing less.
    """
    response = controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 4.0, "y_mm": 0}})

    assert response["offsets_mm"] == [{"x_mm": D24_TRAVEL_RIGHT_MM, "y_mm": 0.0}]
    assert response["requested_offsets_mm"] == [{"x_mm": 4.0, "y_mm": 0.0}]
    assert response["clamped"] is True
    assert response["sideways_travel_mm"] == {"min": D24_TRAVEL_LEFT_MM,
                                              "max": D24_TRAVEL_RIGHT_MM}


def test_a_reachable_offset_reports_no_clamp(controller):
    response = controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 1.0, "y_mm": 0}})

    assert response["clamped"] is False
    assert response["offsets_mm"] == response["requested_offsets_mm"]


def test_the_dry_run_answers_the_same_question_without_a_label(controller, sent):
    response = controller.test_print_calibration(
        {"label_size": "d24", "offset": {"x_mm": 4.0, "y_mm": 0}, "dry_run": True})

    assert response["would_print"]["clamped"] is True
    assert response["would_print"]["offsets_mm"] == [
        {"x_mm": D24_TRAVEL_RIGHT_MM, "y_mm": 0.0}]
    assert response["would_print"]["requested_offsets_mm"] == [{"x_mm": 4.0, "y_mm": 0.0}]
    assert response["would_print"]["sideways_travel_mm"]["max"] == D24_TRAVEL_RIGHT_MM
    assert not controller.queued
    assert not sent


def test_a_dry_run_neither_prints_nor_queues(controller, sent):
    response = controller.test_print_calibration(
        {"label_size": "d24", "sweep": {"axis": "x", "count": 3, "step_mm": 0.5},
         "dry_run": True})

    assert response["dry_run"] is True
    assert response["would_print"]["labels"] == 3
    assert response["would_print"]["width_px"] == SIZE_D24[0]
    assert not controller.queued
    assert not sent


def test_the_preview_endpoint_returns_a_data_url(controller):
    response = controller.preview_calibration({"label_size": "d24"})

    assert response["image"].startswith("data:image/png;base64,")


def test_the_preview_endpoint_rejects_an_unknown_offset_field(controller):
    with pytest.raises(ValidationError):
        controller.preview_calibration({"label_size": "d24", "offset": {"y": 1}})
