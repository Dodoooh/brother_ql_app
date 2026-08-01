"""
Tests for per-medium bleed: printing into the strip Brother calls non-printable.

Every die-cut label is offered smaller than it is. brother_ql publishes 236x236
dots of a 24 mm round label that is 284x284 dots of paper, so 2.03 mm all round
is unreachable by any design -- which is exactly the ring of bare paper a user
measures on a finished label. Bleed hands that strip back, per medium, opt-in,
zero by default.

Bleed is **across the tape only**, and that is the most important property in
this file. An earlier version also lengthened the raster, on the reasoning that
a round label must grow equally on both axes to stay round. Printing it
disproved the reasoning: each raster line is one step of the feed, so the line
count is how far the media travels while the page prints, and adding 48 lines to
a d24 walked the cut off the die-cut gap until the roll lost registration.
There is no compensating command either -- ESC i d carries the label's own feed
margin and is packed unsigned. So a bled job must emit exactly as many raster
lines as an unbled one, and that is asserted directly rather than implied.

The consequence is that a bled round label is no longer square (a d24 is
284 x 236) and its drawable area is an **ellipse**. Round content is therefore
checked against two boundaries: the ellipse the renderer targets, and the real
die-cut circle, which is the physical one.

What else is measured, and why:

* A bigger canvas proves nothing. The interesting claim is that *ink* lands
  nearer the punched edge and that the label as a whole does not move while it
  gets there, so the assertions are on the dots in the instruction stream and on
  where they sit in the printer's device row rather than on image dimensions.
* Bleed and the sideways calibration offset both spend the same right margin, so
  they are tested together as well as apart, including the fact that a wider
  raster has less room left to move.
* Bleed shows in previews and calibration does not. That pair is the most likely
  thing for a later change to break, because the two features look alike; it is
  asserted here in one place, both halves together.
* Bleed 0 -- and no bleed map at all -- must produce byte-identical instructions
  on every medium, so no existing installation moves.

Nothing here claims anything about where the printer places the *first* raster
line of a page. That question is not settled, and bleed no longer depends on the
answer.
"""

import math
import os

import pytest

from src.config.default_settings import BLEED_LIMIT_MM, DEFAULT_SETTINGS
from src.services import printer_service as printer_module
from src.services.printer_service import (
    DOTS_PER_MM,
    NO_BLEED,
    PrinterService,
    _bleed_limit_dots,
    applied_calibration_offset,
    get_label_bleed,
    get_label_geometry,
    get_round_line_widths,
    get_round_safe_axes,
    get_round_safe_radius,
    placed_raster,
    plan_raster_placement,
)
from src.services.settings_service import SettingsService
from src.utils.exceptions import PrinterError, ValidationError

label_type_specs = pytest.importorskip("brother_ql.devicedependent").label_type_specs
ALL_LABELS = pytest.importorskip("brother_ql.labels").ALL_LABELS

Image = pytest.importorskip("PIL.Image")
ImageChops = pytest.importorskip("PIL.ImageChops")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
ImageOps = pytest.importorskip("PIL.ImageOps")

MODEL = "QL-820NWB"
DEVICE_WIDTH = 720
# The wide-format head, where the 62 mm media is not limited by the print head
# and can reach its full 1.52 mm of margin.
WIDE_MODEL = "QL-1100"
WIDE_DEVICE_WIDTH = 1296

# Every medium a bled print has to survive.
ALL_MEDIA = ["d24", "d12", "d58", "62x29", "12", "62"]
ROUND_MEDIA = ["d12", "d24", "d58"]
DIE_CUT_MEDIA = ["d24", "d12", "d58", "62x29"]

# d24, the medium the user had in hand. 284 dots of paper across, 236 published
# as printable, so 24 dots -- 2.03 mm -- of margin per side.
D24_PRINTABLE = 236
D24_TOTAL = 284
D24_MARGIN_DOTS = 24
D24_MARGIN_MM = 2.03
# Where the label's raster sits in the 720-dot device row: unbled at 442, bled
# at 418, and in both cases centred on column 559.5.
D24_BASE_X = 442
D24_BLED_BASE_X = 418
D24_CENTRE_COLUMN = 559.5


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
def settings_service(tmp_path):
    """A settings service backed by a throwaway file."""
    return SettingsService(settings_file=str(tmp_path / "settings.json"))


def _settings(label_size, bleed=None, model=MODEL, **overrides):
    """Print settings for one medium, with an optional bleed for it."""
    settings = {
        "printer_uri": "tcp://192.0.2.10",
        "printer_model": model,
        "label_size": label_size,
    }
    if bleed is not None:
        settings["bleed_mm"] = {label_size: bleed}
    settings.update(overrides)
    return settings


def _calibrated(label_size, bleed=None, model=MODEL, **entry):
    """Settings carrying both a bleed and a calibration entry for one medium."""
    settings = _settings(label_size, bleed, model)
    settings["calibration"] = {label_size: entry}
    return settings


def _device_width(model):
    return WIDE_DEVICE_WIDTH if model == WIDE_MODEL else DEVICE_WIDTH


def _catalogue(label_size):
    """The catalogue entry for a medium, for deriving expectations."""
    return next(entry for entry in ALL_LABELS if entry.identifier == label_size)


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


class _Ink:
    """The ink in one instruction stream: how much, where, and how many rows.

    ``dots`` is the count of black dots the printer is told to lay down, so it
    cannot be fooled by a bigger canvas -- a canvas that grew but gained no ink
    leaves it unchanged. ``first``/``last`` are the outermost device columns
    carrying ink, i.e. where on the tape the label ended up. ``rows`` is how many
    raster lines the job is, which is the distance the media advances while it
    prints, and is the number that must never change.
    """

    def __init__(self, dots, first, last, rows):
        self.dots = dots
        self.first = first
        self.last = last
        self.rows = rows

    @property
    def centre(self):
        """The middle of the inked span, in device columns."""
        return (self.first + self.last) / 2.0

    def __repr__(self):
        return (f"_Ink(dots={self.dots}, first={self.first}, last={self.last}, "
                f"rows={self.rows})")


def _printed_ink(instructions, device_width=DEVICE_WIDTH):
    """Return the :class:`_Ink` the printer is instructed to lay down.

    The raster rows are transmitted mirrored (``add_raster_data`` flips them
    before packing, which is the wire convention, not a change of direction), so
    a bit's position is unflipped here to get back to the device column the dot
    belongs to.
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
    return _Ink(dots, first, last, len(rows))


def _solid_label(service, settings):
    """A label filling the medium's whole drawable canvas with black.

    Solid ink makes the placement unambiguous: the inked span in the device row
    is exactly the raster's span, so its centre is the label's centre.
    """
    geometry = get_label_geometry(settings.get("label_size"), settings)
    height = geometry.height or 200
    img = Image.new("RGB", (geometry.width, height), "black")
    path = os.path.join(service.upload_folder, "solid.png")
    img.save(path)
    return path


def _print(service, settings, sent, image_path=None):
    """Print one label and return the ink the printer was told to lay down."""
    sent.clear()
    service._send_to_printer(image_path or _solid_label(service, settings), settings)
    assert len(sent) == 1
    return _printed_ink(sent[0], _device_width(settings.get("printer_model")))


def _ink_bbox(img):
    """Bounding box of the black pixels, or None when the label is blank."""
    return ImageOps.invert(img.convert("L")).getbbox()


def _ink_reach(img):
    """How far the ink reaches from the canvas centre, in pixels."""
    box = _ink_bbox(img)
    assert box, "the label carries no ink to measure"
    half_w, half_h = img.width / 2.0, img.height / 2.0
    return max(half_w - box[0], box[2] - half_w,
               half_h - box[1], box[3] - half_h)


def _ink_radius(img):
    """How far from the canvas centre the outermost inked pixel sits.

    Radial, not axis-aligned, because on round media the ink nearest the die cut
    is usually a corner of the design rather than the middle of an edge. A bled
    canvas is concentric with the unbled one, so this number is directly
    comparable between the two: both are measured from the same physical point.
    """
    ink = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 127 else 0)
    centre_x = (img.width - 1) / 2.0
    centre_y = (img.height - 1) / 2.0
    furthest = 0.0
    for y in range(img.height):
        row = ink.crop((0, y, img.width, y + 1)).getbbox()
        if not row:
            continue
        for x in (row[0], row[2] - 1):
            furthest = max(furthest, math.hypot(x - centre_x, y - centre_y))
    return furthest


def _ink_outside_ellipse(img):
    """Count black pixels outside the ellipse inscribed in the canvas.

    This is the region the round renderer targets: a circle on an unbled (square)
    label, an oblong ellipse on a bled one. It is always inside the die cut, so
    a design that satisfies it is on the paper whatever the registration does.
    """
    ink = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 127 else 0)
    area = Image.new("L", img.size, 0)
    ImageDraw.Draw(area).ellipse((0, 0, img.width - 1, img.height - 1), fill=255)
    return sum(ImageChops.subtract(ink, area).histogram()[128:])


def _ink_outside_die_cut(img, label_size):
    """Count black pixels outside the *physical* punched circle.

    The die cut is a circle of the label's full width, concentric with the
    canvas -- so on a bled label it meets the canvas edge across the tape and
    extends past it along the feed. This is the boundary that decides what ends
    up on the label the user peels off, as opposed to the one the renderer aims
    at, and on a non-square canvas the two are genuinely different shapes.
    """
    radius = _catalogue(label_size).dots_total[0] / 2.0
    ink = ImageOps.invert(img.convert("L")).point(lambda p: 255 if p > 127 else 0)
    centre_x = (img.width - 1) / 2.0
    centre_y = (img.height - 1) / 2.0
    outside = 0
    for y in range(img.height):
        row = ink.crop((0, y, img.width, y + 1)).getbbox()
        if not row:
            continue
        for x in range(row[0], row[2]):
            if ink.getpixel((x, y)) > 127 and \
                    math.hypot(x - centre_x, y - centre_y) > radius:
                outside += 1
    return outside


def _open(path):
    """Load a rendered label eagerly so the file can be removed afterwards."""
    with Image.open(path) as img:
        return img.copy()


def _accept(img, label_size, bleed_dots=0):
    """Hand a rendered label to the real ``convert()``; fail loudly if rejected.

    The bleed is published the same way the print path publishes it, because
    that publication is the only reason a wider die-cut image is accepted at
    all -- checking the image against an unbled table would just re-test the old
    behaviour.
    """
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    qlr = raster(MODEL)
    bleed = NO_BLEED._replace(label_size=label_size, dots=bleed_dots)
    with placed_raster(qlr, label_size, 0, bleed):
        return convert(qlr, [img], label_size, rotate=0)


def _events(warnings, name):
    """The contexts of every warning with the given event name."""
    return [context for event, context in warnings if event == name]


# --------------------------------------------------------------------------- #
# Storage: shape, defaults, validation and inheritance
# --------------------------------------------------------------------------- #

def test_defaults_ship_an_empty_bleed_map():
    """No bleed anywhere is the default, so nothing existing grows."""
    assert DEFAULT_SETTINGS["bleed_mm"] == {}


def test_bleed_is_a_separate_setting_from_calibration():
    """The two maps stay apart, because their rules for previews differ.

    Calibration corrects a printer error and therefore never touches a preview;
    bleed enlarges the label being designed and therefore must. Nesting one
    inside the other would force one of those rules to give.
    """
    assert "bleed_mm" in DEFAULT_SETTINGS
    assert "calibration" in DEFAULT_SETTINGS
    assert "bleed" not in DEFAULT_SETTINGS["calibration"]
    settings = _calibrated("d24", bleed=1.0, x_mm=0.5)
    assert settings["bleed_mm"] == {"d24": 1.0}
    assert settings["calibration"] == {"d24": {"x_mm": 0.5}}


@pytest.mark.parametrize("value", [0, 0.5, 1.5, 2.03, BLEED_LIMIT_MM])
def test_validation_accepts_a_sensible_bleed(settings_service, value):
    settings_service._validate_settings(dict(DEFAULT_SETTINGS, bleed_mm={"d24": value}))


@pytest.mark.parametrize("bleed_map", [
    {"d24": -0.5},                       # bleed only ever adds area
    {"d24": BLEED_LIMIT_MM + 0.01},      # a wrong unit, not a request
    {"d24": "1.5"},                      # a string is not a measurement
    {"d24": True},                       # bool is an int subclass
    {"d24": None},
    {"": 1.0},                           # not a label identifier
])
def test_validation_rejects_a_nonsensical_bleed(settings_service, bleed_map):
    with pytest.raises(ValueError):
        settings_service._validate_settings(dict(DEFAULT_SETTINGS, bleed_mm=bleed_map))


def test_validation_rejects_a_bleed_map_that_is_not_a_map(settings_service):
    with pytest.raises(ValueError):
        settings_service._validate_settings(dict(DEFAULT_SETTINGS, bleed_mm=[1.0]))


def test_the_rejection_message_explains_the_real_limit(settings_service):
    """A refused value should say what the ceiling actually is."""
    with pytest.raises(ValueError) as excinfo:
        settings_service._validate_settings(dict(DEFAULT_SETTINGS, bleed_mm={"d24": 25.0}))
    assert "unit" in str(excinfo.value)


def test_bleed_is_inherited_by_a_request_that_omits_settings(settings_service):
    """A print that sends no settings must still render at the bled size.

    Not inheriting it would render the label at a different size than the saved
    configuration says -- and on die-cut media ``convert()`` would then reject
    the job outright.
    """
    saved = dict(DEFAULT_SETTINGS, label_size="d24", bleed_mm={"d24": 1.5})
    assert settings_service.save_settings(saved)
    assert settings_service.resolve_print_settings(None)["bleed_mm"] == {"d24": 1.5}


def test_a_request_can_override_the_saved_bleed(settings_service):
    saved = dict(DEFAULT_SETTINGS, label_size="d24", bleed_mm={"d24": 1.5})
    assert settings_service.save_settings(saved)
    assert settings_service.resolve_print_settings({"bleed_mm": {}})["bleed_mm"] == {}


# --------------------------------------------------------------------------- #
# Resolving a bleed: what each medium can really give, across the tape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size,expected_dots", [
    ("d12", 24),      # 142 total across, 94 printable
    ("d24", 24),      # 284 total across, 236 printable
    ("d58", 35),      # 688 total across, 618 printable
    ("23x23", 35),
    ("17x54", 18),
])
def test_the_margin_matches_the_catalogue(label_size, expected_dots):
    """The ceiling is half the gap between total and printable *width*."""
    assert _bleed_limit_dots(label_size, MODEL) == expected_dots


def test_d24_offers_exactly_the_margin_the_user_measured():
    limit = _bleed_limit_dots("d24", MODEL)
    assert limit == D24_MARGIN_DOTS
    assert round(limit / DOTS_PER_MM, 2) == D24_MARGIN_MM


def test_the_feed_margin_is_never_offered():
    """A rectangular die cut has more spare paper along the feed, and none of it
    is available: spending it moves the cut.

    62x29 is 341 dots long against 271 printable -- 2.96 mm a side, twice what
    it has across the tape -- and the bleed resolved for it is still the width's.
    """
    label = _catalogue("62x29")
    feed_margin = (label.dots_total[1] - label.dots_printable[1]) // 2
    assert feed_margin == 35                              # 2.96 mm of spare paper
    assert _bleed_limit_dots("62x29", WIDE_MODEL) == 18   # and 1.52 mm offered

    bleed = get_label_bleed(_settings("62x29", 5.0, model=WIDE_MODEL))
    assert bleed.dots == 18
    # Not merely zero: there is no such field, so nothing can start using it.
    assert not hasattr(bleed, "feed_dots")


def test_an_unknown_medium_offers_no_bleed(warnings):
    """Not knowing a margin is no reason to invent one and print off the paper."""
    assert _bleed_limit_dots("not-a-real-label", MODEL) is None
    bleed = get_label_bleed(_settings("not-a-real-label", 2.0), warn=True)
    assert bleed.is_zero
    assert bleed.was_clamped
    assert _events(warnings, "No media entry to bleed into; printing the "
                             "published printable area")


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_no_bleed_configured_resolves_to_nothing(label_size):
    """An absent map, an empty map and a zero all mean the same thing."""
    for settings in (_settings(label_size),
                     _settings(label_size, bleed=0),
                     dict(_settings(label_size), bleed_mm={})):
        bleed = get_label_bleed(settings)
        assert bleed.is_zero
        assert bleed.dots == 0
        assert not bleed.was_clamped


def test_a_bleed_for_another_medium_is_not_applied():
    settings = _settings("d24")
    settings["bleed_mm"] = {"d12": 2.0}
    assert get_label_bleed(settings).is_zero


def test_bleed_is_clamped_to_the_media_margin_with_the_reason_logged(warnings):
    """4 mm on a medium with 2.03 mm of margin gets 2.03 mm and says why."""
    bleed = get_label_bleed(_settings("d24", 4.0), warn=True)
    assert bleed.dots == D24_MARGIN_DOTS
    assert bleed.applied_mm == D24_MARGIN_MM
    assert bleed.requested_mm == 4.0
    assert bleed.was_clamped

    logged = _events(warnings, "Clamped bleed to the medium's non-printable margin")
    assert len(logged) == 1
    context = logged[0]
    assert context["label_size"] == "d24"
    assert context["requested_mm"] == 4.0
    assert context["applied_px"] == D24_MARGIN_DOTS
    assert context["limit_mm"] == D24_MARGIN_MM
    assert "no more label outside the printable area" in context["reason"]


def test_the_clamp_is_silent_unless_the_caller_asks(warnings):
    """Geometry lookups happen several times a render; one print, one warning."""
    get_label_bleed(_settings("d24", 4.0))
    assert not _events(warnings, "Clamped bleed to the medium's non-printable margin")


def test_the_print_head_caps_the_bleed_before_the_media_does(warnings):
    """62 mm tape is wider than a 720-dot head, so the head is the real limit.

    The tape offers 18 dots of margin per side, but 732 dots of raster cannot be
    sent to a printer whose row is 720 -- ``add_raster_data`` refuses it. Only 12
    dots per side are reachable, and asking for the full margin is a clamp.
    """
    narrow = get_label_bleed(_settings("62", 1.52, model=MODEL), warn=True)
    assert narrow.dots == 12
    assert narrow.was_clamped
    assert _events(warnings, "Clamped bleed to the medium's non-printable margin")

    wide = get_label_bleed(_settings("62", 1.52, model=WIDE_MODEL))
    assert wide.dots == 18
    assert not wide.was_clamped


def test_a_malformed_bleed_never_stops_a_print(warnings):
    """A bad number prints the label the way it printed before the setting."""
    for value in ("2mm", None, [1], {"mm": 1}):
        settings = _settings("d24")
        settings["bleed_mm"] = {"d24": value}
        assert get_label_bleed(settings).is_zero
    settings = _settings("d24")
    settings["bleed_mm"] = "1.5"
    assert get_label_bleed(settings).is_zero


# --------------------------------------------------------------------------- #
# Geometry: the width grows, the length never does
# --------------------------------------------------------------------------- #

def test_geometry_without_settings_is_the_published_printable_area():
    """The old signature keeps the old answer, so nothing unbled moves."""
    assert tuple(get_label_geometry("d24")[:2]) == (D24_PRINTABLE, D24_PRINTABLE)
    assert tuple(get_label_geometry("62x29")[:2]) == (696, 271)
    assert tuple(get_label_geometry("62")[:2]) == (696, 0)


@pytest.mark.parametrize("label_size", DIE_CUT_MEDIA)
def test_the_bled_width_is_the_whole_label_and_the_height_is_untouched(label_size):
    """The exact contract: width to dots_total, height to dots_printable.

    ``dots_total`` is capped by the print head first -- 62x29 is 732 dots of
    paper against a 720-dot row -- which is why the expectation is derived from
    the catalogue rather than written out.
    """
    label = _catalogue(label_size)
    plain = get_label_geometry(label_size)
    bled = get_label_geometry(label_size, _settings(label_size, BLEED_LIMIT_MM))

    assert plain[:2] == label.dots_printable
    assert bled.width == min(label.dots_total[0], DEVICE_WIDTH)
    assert bled.height == label.dots_printable[1]
    assert bled.height == plain.height


def test_a_full_bleed_d24_is_the_full_width_of_the_label():
    geometry = get_label_geometry("d24", _settings("d24", D24_MARGIN_MM))
    assert tuple(geometry[:2]) == (D24_TOTAL, D24_PRINTABLE)
    assert geometry.is_round and geometry.is_die_cut


def test_continuous_length_stays_unbounded():
    """0 is "no fixed length", not a length; adding to it would fix it."""
    geometry = get_label_geometry("62", _settings("62", 1.5, model=WIDE_MODEL))
    assert geometry.width == 732
    assert geometry.height == 0


# --------------------------------------------------------------------------- #
# Round media: the printable area is an ellipse once the label is bled
# --------------------------------------------------------------------------- #

def test_the_safe_area_is_a_circle_when_the_label_is_square():
    """Unbled behaviour is unchanged to the pixel, on every round medium."""
    for label_size in ROUND_MEDIA:
        width = get_label_geometry(label_size).width
        assert get_round_safe_axes(width) == (get_round_safe_radius(width),) * 2
        assert get_round_safe_axes(width, width) == get_round_safe_axes(width)


def test_the_safe_area_becomes_an_ellipse_when_the_label_is_bled():
    """Wider across, unchanged along -- and that is the shape, not a workaround."""
    unbled = get_label_geometry("d24")
    bled = get_label_geometry("d24", _settings("d24", D24_MARGIN_MM))

    plain_x, plain_y = get_round_safe_axes(unbled.width, unbled.height)
    wide_x, wide_y = get_round_safe_axes(bled.width, bled.height)

    assert plain_x == plain_y                 # a circle
    assert wide_x > plain_x                   # wider than it was
    assert wide_y == plain_y                  # and no longer at all
    assert wide_x > wide_y                    # an ellipse


def test_the_ellipse_keeps_the_whole_gain_instead_of_shrinking_to_a_circle():
    """The point of the ellipse: retreating to the smaller radius would throw the
    bleed away again.

    A circle of the *smaller* semi-axis is exactly the unbled circle, so every
    dot won across the tape would be handed straight back.
    """
    bled = get_label_geometry("d24", _settings("d24", D24_MARGIN_MM))
    wide_x, wide_y = get_round_safe_axes(bled.width, bled.height)
    fallback = min(wide_x, wide_y)

    assert fallback == get_round_safe_radius(get_label_geometry("d24").width)
    # The full 2 x 24 dots of bleed, still available on the centre line.
    assert 2 * (wide_x - fallback) == 2 * D24_MARGIN_DOTS


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
def test_the_safe_ellipse_stays_inside_the_punched_circle(label_size):
    """The geometric guarantee the whole thing rests on.

    A point on an ellipse with semi-axes (a, b) sits at radius
    ``sqrt(a^2 cos^2 t + b^2 sin^2 t) <= max(a, b)``, and the larger semi-axis is
    the label's own radius less the registration margin -- so no part of the
    drawable area leaves the paper.
    """
    settings = _settings(label_size, BLEED_LIMIT_MM)
    geometry = get_label_geometry(label_size, settings)
    axis_x, axis_y = get_round_safe_axes(geometry.width, geometry.height)
    die_cut_radius = _catalogue(label_size).dots_total[0] / 2.0

    assert max(axis_x, axis_y) < die_cut_radius
    for step in range(0, 360, 5):
        angle = math.radians(step)
        radius = math.hypot(axis_x * math.cos(angle), axis_y * math.sin(angle))
        assert radius <= die_cut_radius


def test_elliptical_chords_widen_towards_the_centre_line():
    """The chord rule generalises: with equal axes it is the old circle exactly."""
    radius = get_round_safe_radius(D24_PRINTABLE)
    assert get_round_line_widths(radius, 3, 40) == \
        get_round_line_widths(radius, 3, 40, radius_y=radius)

    bled = get_label_geometry("d24", _settings("d24", D24_MARGIN_MM))
    axis_x, axis_y = get_round_safe_axes(bled.width, bled.height)
    circular = get_round_line_widths(axis_y, 3, 40)
    elliptical = get_round_line_widths(axis_x, 3, 40, radius_y=axis_y)

    # Every line gets more room, and the widest is the one on the centre line.
    assert all(wide > narrow for wide, narrow in zip(elliptical, circular))
    assert max(elliptical) == elliptical[1]


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
def test_bled_text_stays_inside_the_die_cut_on_both_axes(service, label_size):
    settings = _settings(label_size, BLEED_LIMIT_MM, font_size=60)
    img = _open(service._create_text_label("HELLO<br>WORLD", settings))
    assert img.size == tuple(get_label_geometry(label_size, settings)[:2])
    assert _ink_outside_ellipse(img) == 0
    assert _ink_outside_die_cut(img, label_size) == 0


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
def test_a_bled_qr_code_stays_inside_the_die_cut_on_both_axes(service, label_size):
    settings = _settings(label_size, BLEED_LIMIT_MM)
    img = _open(service._create_qr_code("BLEED-TEST-1234", settings))
    assert img.size == tuple(get_label_geometry(label_size, settings)[:2])
    assert _ink_outside_ellipse(img) == 0
    assert _ink_outside_die_cut(img, label_size) == 0


@pytest.mark.parametrize("label_size", ROUND_MEDIA)
def test_a_bled_image_stays_inside_the_die_cut_on_both_axes(service, label_size):
    source = Image.new("RGB", (300, 200), "black")
    path = os.path.join(service.upload_folder, "wide.png")
    source.save(path)
    settings = _settings(label_size, BLEED_LIMIT_MM)
    img = _open(service._resize_image(path, label_size, settings))
    assert img.size == tuple(get_label_geometry(label_size, settings)[:2])
    assert _ink_outside_ellipse(img) == 0
    assert _ink_outside_die_cut(img, label_size) == 0


def test_bled_round_text_really_does_get_more_room(service):
    """More than a bigger canvas: the text itself is allowed to be larger."""
    text = "WIDER"
    unbled = _open(service._create_text_label(text, _settings("d24", font_size=100)))
    bled = _open(service._create_text_label(
        text, _settings("d24", D24_MARGIN_MM, font_size=100)))
    # Auto-fit shrinks the font until the stack fits; a wider ellipse keeps more.
    assert _ink_reach(bled) > _ink_reach(unbled)


def test_a_square_rectangular_label_is_not_treated_as_round():
    """23x23 is square and rectangular; its corners print, bled or not."""
    geometry = get_label_geometry("23x23", _settings("23x23", BLEED_LIMIT_MM))
    assert not geometry.is_round
    assert tuple(geometry[:2]) == (272, 202)


# --------------------------------------------------------------------------- #
# The print path: raster length, placement, ink and the media table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", DIE_CUT_MEDIA)
def test_a_bled_job_emits_the_same_number_of_raster_lines(service, sent, label_size):
    """The property whose violation moved the cutter, asserted directly.

    Each raster line is one step of the feed, so the line count is how far the
    media advances while the page prints. When bleed also lengthened the raster,
    a d24 page advanced 48 steps (4 mm) too far, the cut walked off the die-cut
    gap and the roll lost registration. Nothing in the stream can give those
    steps back -- ESC i d carries the label's own feed margin and is unsigned --
    so the only safe answer is not to spend them.
    """
    plain = _print(service, _settings(label_size, model=WIDE_MODEL), sent)
    bled = _print(service, _settings(label_size, BLEED_LIMIT_MM, model=WIDE_MODEL), sent)

    assert bled.rows == plain.rows
    assert bled.rows == _catalogue(label_size).dots_printable[1]
    # ...while the raster really did get wider, so this is not passing by
    # accident on a job where nothing changed at all.
    assert bled.dots > plain.dots


def test_a_bled_continuous_job_is_the_same_length_too(service, sent):
    """Continuous media has no die cut along the tape, and still must not grow.

    Re-checked rather than assumed: the length of a continuous label is whatever
    was rendered, so a bleed that leaked into it would silently change how much
    tape each label costs.
    """
    plain = _print(service, _settings("62", model=WIDE_MODEL), sent)
    bled = _print(service, _settings("62", BLEED_LIMIT_MM, model=WIDE_MODEL), sent)
    assert bled.rows == plain.rows
    assert bled.dots > plain.dots


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_zero_bleed_is_byte_identical_to_no_bleed(service, sent, label_size):
    """Nobody's existing setup moves, on any medium."""
    baseline = _print(service, _settings(label_size), sent)
    baseline_bytes = bytes(sent[0])

    for settings in (_settings(label_size, 0),
                     dict(_settings(label_size), bleed_mm={}),
                     dict(_settings(label_size), bleed_mm={"some-other-roll": 2.0})):
        _print(service, settings, sent)
        assert bytes(sent[0]) == baseline_bytes
    assert baseline.dots > 0


def test_a_bled_d24_is_284_by_236_and_convert_accepts_it(service):
    """The width the medium really is, the length it has always been."""
    settings = _settings("d24", D24_MARGIN_MM)
    img = _open(_solid_label(service, settings))
    assert img.size == (D24_TOTAL, D24_PRINTABLE)
    assert _accept(img, "d24", bleed_dots=D24_MARGIN_DOTS)


def test_convert_rejects_a_bled_image_without_the_publication(service):
    """Proof the publication is what lets the wider raster through at all."""
    convert = pytest.importorskip("brother_ql.conversion").convert
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    img = _open(_solid_label(service, _settings("d24", D24_MARGIN_MM)))
    with pytest.raises(ValueError):
        convert(raster(MODEL), [img], "d24", rotate=0)


def test_the_label_centre_does_not_move_when_it_bleeds(service, sent):
    """Half the growth comes out of the right margin, which is the whole trick.

    Widening the raster without paying for it out of the margin would drag the
    label sideways by half the growth -- 2 mm on d24 -- which is a worse defect
    than the one bleed exists to fix.
    """
    unbled = _print(service, _settings("d24"), sent)
    bled = _print(service, _settings("d24", D24_MARGIN_MM), sent)

    assert unbled.first == D24_BASE_X
    assert bled.first == D24_BLED_BASE_X
    assert unbled.centre == bled.centre == D24_CENTRE_COLUMN


@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_the_label_centre_holds_on_every_medium(service, sent, label_size):
    unbled = _print(service, _settings(label_size), sent)
    bled = _print(service, _settings(label_size, BLEED_LIMIT_MM), sent)
    assert bled.centre == unbled.centre


def test_ink_reaches_nearer_the_rim_in_the_instruction_stream(service, sent):
    """Measured in dots the printer is told to lay, not inferred from a canvas.

    A canvas can grow without gaining a dot of ink. What matters is that the
    outermost inked column moves outwards, and by how much: across the tape the
    ink stops 24 dots (2.03 mm) short of the die cut on each side, and full
    bleed closes that to nothing.
    """
    unbled = _print(service, _settings("d24"), sent)
    bled = _print(service, _settings("d24", D24_MARGIN_MM), sent)

    die_cut_left = D24_CENTRE_COLUMN - D24_TOTAL / 2.0
    die_cut_right = D24_CENTRE_COLUMN + D24_TOTAL / 2.0

    unbled_gap = min(unbled.first - die_cut_left, die_cut_right - 1 - unbled.last)
    bled_gap = min(bled.first - die_cut_left, die_cut_right - 1 - bled.last)

    assert unbled_gap == pytest.approx(D24_MARGIN_DOTS, abs=1)
    assert bled_gap == pytest.approx(0, abs=1)
    assert bled.dots > unbled.dots


def test_ink_reaches_nearer_the_rim_in_a_real_design(service):
    """The same claim for content the user would actually print.

    An image fitted to a round label is scaled until its corners meet the safe
    area, so the radius its ink reaches is the number that has to improve. The
    remaining gap is the registration margin the renderer keeps on purpose,
    which is exactly the variation the user is warned about.
    """
    source = Image.new("RGB", (400, 400), "black")
    path = os.path.join(service.upload_folder, "block.png")
    source.save(path)

    unbled = _open(service._resize_image(path, "d24", _settings("d24")))
    bled = _open(service._resize_image(path, "d24", _settings("d24", D24_MARGIN_MM)))

    # Both canvases are concentric with the 284-dot die cut, so the radii are
    # measured from the same physical point and compare directly.
    die_cut_radius = D24_TOTAL / 2.0
    unbled_gap = die_cut_radius - _ink_radius(unbled)
    bled_gap = die_cut_radius - _ink_radius(bled)

    assert unbled_gap > 25          # over 2 mm of bare paper, as measured
    assert bled_gap < unbled_gap    # and the bled design gets closer
    assert bled_gap > 0             # while staying inside the die cut


def test_continuous_tape_bleeds_across_the_tape(service, sent):
    """62 mm tape leaves 1.5 mm bare on each edge; the length is already free.

    Included because it is the same mechanism -- a wider raster and a smaller
    right margin -- on media where the length was never in question.
    """
    unbled = _print(service, _settings("62", model=WIDE_MODEL), sent)
    bled = _print(service, _settings("62", 1.52, model=WIDE_MODEL), sent)

    assert bled.last - bled.first == 731         # the full 732 dots of tape
    assert unbled.last - unbled.first == 695
    assert bled.centre == unbled.centre
    assert bled.dots > unbled.dots


def test_continuous_tape_fills_the_head_exactly_when_the_head_is_the_limit(
        service, sent):
    """On a 720-dot head the bled 62 mm raster is the row, edge to edge."""
    bled = _print(service, _settings("62", BLEED_LIMIT_MM), sent)
    assert (bled.first, bled.last) == (0, DEVICE_WIDTH - 1)


@pytest.mark.parametrize("label_size", ["d24", "62x29", "62"])
def test_a_bled_print_goes_through_the_public_paths(service, sent, label_size):
    """End to end, not just through the internals the other tests poke at.

    The render path and the publication have to agree about the size on every
    content type: if they disagree, ``convert()`` rejects a die-cut label
    outright and silently rescales a continuous one.
    """
    settings = _settings(label_size, BLEED_LIMIT_MM, font_size=30)
    before = dict(label_type_specs[label_size])

    service.print_text("EDGE", settings)
    assert _printed_ink(sent[-1]).dots > 0

    service.print_qr_code("BLEED", settings)
    assert _printed_ink(sent[-1]).dots > 0

    source = Image.new("RGB", (200, 200), "black")
    path = os.path.join(service.upload_folder, "src.png")
    source.save(path)
    service.print_image(path, settings)
    assert _printed_ink(sent[-1]).dots > 0

    service.print_text_image(path, "EDGE", settings)
    assert _printed_ink(sent[-1]).dots > 0

    # And every one of them left the media table exactly as it found it.
    assert label_type_specs[label_size] == before


# --------------------------------------------------------------------------- #
# Composition with the calibration offsets
# --------------------------------------------------------------------------- #

def test_bleed_and_the_sideways_offset_compose(service, sent):
    """Both spend the same right margin, and they must add rather than fight."""
    base = _print(service, _settings("d24", D24_MARGIN_MM), sent)
    moved = _print(service, _calibrated("d24", D24_MARGIN_MM, x_mm=-2.0), sent)
    assert moved.centre == base.centre - round(2.0 * DOTS_PER_MM)
    # Moving the label does not cost it a dot: that is the point of moving the
    # raster rather than the ink inside it.
    assert moved.dots == base.dots


def test_a_wider_raster_has_less_room_to_move(service, sent):
    """d24 sits near the end of the head, and bleeding it eats into the travel.

    Unbled it has 42 dots of rightward travel; at full bleed the raster is 48
    dots wider, so only 18 are left. A request beyond that is clamped, and the
    clamp is what the API and the caption report.
    """
    unbled = applied_calibration_offset(_calibrated("d24", None, x_mm=3.0), "d24")
    bled = applied_calibration_offset(
        _calibrated("d24", D24_MARGIN_MM, x_mm=3.0), "d24")

    assert unbled.travel_mm[1] == pytest.approx(3.56, abs=0.01)
    assert bled.travel_mm[1] == pytest.approx(1.52, abs=0.01)
    assert not unbled.was_clamped
    assert bled.was_clamped
    assert bled.x_mm < unbled.x_mm

    reached = _print(service, _calibrated("d24", D24_MARGIN_MM, x_mm=3.0), sent)
    assert reached.last == DEVICE_WIDTH - 1


def test_the_clamped_offset_is_what_gets_printed(service, sent):
    """Asking for more travel than there is prints the travel there is."""
    at_limit = _print(service, _calibrated("d24", D24_MARGIN_MM, x_mm=1.52), sent)
    beyond = _print(service, _calibrated("d24", D24_MARGIN_MM, x_mm=9.0), sent)
    assert beyond.first == at_limit.first
    assert beyond.centre == at_limit.centre


def test_bleed_composes_with_the_feed_offset(service, sent):
    """The y offset still moves content inside the canvas, and the canvas is the
    same length it always was.

    y is the axis bleed does not touch, so the two are independent by
    construction; the assertion that matters is that adding a feed offset to a
    bled job still emits the medium's own number of raster lines.
    """
    img = Image.new("RGB", (D24_TOTAL, D24_PRINTABLE), "white")
    ImageDraw.Draw(img).rectangle((110, 80, 173, 155), fill="black")
    path = os.path.join(service.upload_folder, "block.png")
    img.save(path)

    still = _print(service, _settings("d24", D24_MARGIN_MM), sent, path)
    pulled = _print(service, _calibrated("d24", D24_MARGIN_MM, y_mm=1.0), sent, path)

    assert pulled.rows == still.rows == D24_PRINTABLE
    assert pulled.first == still.first          # the sideways placement is untouched
    assert pulled.dots == still.dots            # and nothing was clipped


def test_bleed_composes_with_scale(service, sent):
    """The size correction works on the bled canvas, about its centre."""
    plain = _print(service, _settings("d24", D24_MARGIN_MM), sent)
    shrunk = _print(service, _calibrated("d24", D24_MARGIN_MM, scale=0.95), sent)

    assert shrunk.dots < plain.dots
    assert shrunk.first > plain.first
    assert shrunk.last < plain.last
    assert shrunk.centre == pytest.approx(plain.centre, abs=1)


def test_bleed_offset_and_scale_all_at_once(service, sent):
    """The three dials stay independent on a fully loaded configuration."""
    settings = _calibrated("d24", D24_MARGIN_MM, x_mm=-1.0, y_mm=0.5, scale=0.98)
    ink = _print(service, settings, sent)
    assert ink.rows == D24_PRINTABLE
    assert ink.dots > 0
    assert ink.first > D24_BLED_BASE_X - round(1.0 * DOTS_PER_MM)
    assert ink.centre < D24_CENTRE_COLUMN


# --------------------------------------------------------------------------- #
# A bled round label is not square any more
# --------------------------------------------------------------------------- #

def test_a_quarter_turn_on_a_bled_round_label_is_refused_clearly(service, sent):
    """Bleed narrows what rotate can do, and the user is told which setting did it.

    A die-cut label is a fixed piece of paper, so a quarter turn transposes the
    canvas into something ``convert()`` will not take. That has always been true
    of rectangular die cuts; bleed makes it true of round ones too, because they
    stop being square. The refusal is a 400 naming the bleed, not a 500 quoting
    two tuples at the user.
    """
    service.print_text("ROT", _settings("d24", font_size=30, rotate=90))
    assert _printed_ink(sent[-1]).dots > 0      # square: still fine

    with pytest.raises(ValidationError) as excinfo:
        service.print_text("ROT", _settings("d24", D24_MARGIN_MM,
                                            font_size=30, rotate=90))
    message = str(excinfo.value)
    assert "284x236" in message
    assert "bleed" in message.lower()
    assert "rotate 0 or 180" in message


@pytest.mark.parametrize("rotate", [0, 180])
def test_a_half_turn_on_a_bled_round_label_still_prints(service, sent, rotate):
    """180 degrees does not transpose the canvas, so it is unaffected."""
    service.print_text("ROT", _settings("d24", D24_MARGIN_MM,
                                        font_size=30, rotate=rotate))
    assert _printed_ink(sent[-1]).rows == D24_PRINTABLE


def test_continuous_media_may_still_be_rotated_when_bled(service, sent):
    """Only a fixed canvas can be transposed into something that will not fit."""
    service.print_text("ROT", _settings("62", BLEED_LIMIT_MM,
                                        font_size=30, rotate=90))
    assert _printed_ink(sent[-1]).dots > 0


# --------------------------------------------------------------------------- #
# brother_ql's media table is borrowed, never kept
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("label_size", ALL_MEDIA)
def test_the_media_table_is_unchanged_after_a_bled_print(service, sent, label_size):
    before = dict(label_type_specs[label_size])
    _print(service, _settings(label_size, BLEED_LIMIT_MM), sent)
    assert label_type_specs[label_size] == before
    assert label_type_specs[label_size]["dots_printable"] == before["dots_printable"]
    assert label_type_specs[label_size]["right_margin_dots"] == before["right_margin_dots"]


def test_the_media_table_is_unchanged_when_convert_raises(service, sent):
    """A conversion that blows up must not leave the table edited."""
    before = dict(label_type_specs["d24"])
    wrong_size = Image.new("RGB", (100, 100), "white")
    path = os.path.join(service.upload_folder, "wrong.png")
    wrong_size.save(path)

    with pytest.raises(PrinterError):
        service._send_to_printer(path, _settings("d24", D24_MARGIN_MM))
    assert label_type_specs["d24"] == before


def test_the_table_is_only_edited_for_the_duration_of_the_conversion():
    """Seen from inside, edited; seen from outside, pristine.

    And the published *length* is the medium's own, which is what keeps the cut
    where it belongs.
    """
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    before = dict(label_type_specs["d24"])
    bleed = NO_BLEED._replace(label_size="d24", dots=D24_MARGIN_DOTS)
    with placed_raster(raster(MODEL), "d24", 0, bleed):
        published = label_type_specs["d24"]
        assert published["dots_printable"] == (D24_TOTAL, D24_PRINTABLE)
        assert published["dots_printable"][1] == before["dots_printable"][1]
        assert published["right_margin_dots"] == \
            before["right_margin_dots"] - D24_MARGIN_DOTS
    assert label_type_specs["d24"] == before


def test_an_unbled_uncalibrated_print_never_takes_the_lock(monkeypatch):
    """The cheapest possible path stays the cheapest possible path."""
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    taken = []
    real_acquire = printer_module._LABEL_SPEC_LOCK.acquire

    class _Watched:
        def __enter__(self):
            taken.append(True)
            return real_acquire()

        def __exit__(self, *_exc):
            printer_module._LABEL_SPEC_LOCK.release()

    monkeypatch.setattr(printer_module, "_LABEL_SPEC_LOCK", _Watched())
    with placed_raster(raster(MODEL), "d24", 0, NO_BLEED) as placement:
        assert placement is None
    assert not taken


def test_the_publication_survives_a_medium_with_no_sideways_travel():
    """62 mm tape on a 720-dot head fills the row: bleed but nowhere to move.

    Without this, ``convert()`` would never hear about the bleed and would
    quietly resize the 720-dot raster back down to 696.
    """
    raster = pytest.importorskip("brother_ql.raster").BrotherQLRaster
    before = dict(label_type_specs["62"])
    bleed = get_label_bleed(_settings("62", BLEED_LIMIT_MM))
    assert bleed.dots == 12
    with placed_raster(raster(MODEL), "62", 0, bleed) as placement:
        assert placement is None      # no travel at all
        assert label_type_specs["62"]["dots_printable"] == (DEVICE_WIDTH, 0)
    assert label_type_specs["62"] == before


def test_the_placement_plan_accounts_for_the_bleed():
    """The arithmetic the whole feature rests on, in one place."""
    plain = plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", 0, warn=False)
    bled = plan_raster_placement(DEVICE_WIDTH, MODEL, "d24", 0, warn=False,
                                 bleed_dots=D24_MARGIN_DOTS)
    assert plain.base_x_offset == D24_BASE_X
    assert bled.base_x_offset == D24_BLED_BASE_X
    assert bled.right_margin_dots == plain.right_margin_dots - D24_MARGIN_DOTS
    # Base offset plus half the label is the centre, and it is the same one.
    assert plain.base_x_offset + D24_PRINTABLE / 2 == bled.base_x_offset + D24_TOTAL / 2


# --------------------------------------------------------------------------- #
# The pair most likely to be broken later: previews vs calibration
# --------------------------------------------------------------------------- #

def _preview_size(data_url):
    """Decode a preview data URL and return the image's size."""
    import base64
    import io

    assert data_url.startswith("data:image/png;base64,")
    raw = base64.b64decode(data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw)) as img:
        return img.size


def test_previews_do_reflect_bleed_and_calibration_does_not(service):
    """The distinction, asserted in one test so it cannot rot by halves.

    Bleed changes how large the label being designed is, so a preview that hid
    it would be a picture of a smaller label than the one that gets printed.
    Calibration corrects a printer that puts ink in the wrong place; the preview
    is the target that correction aims at, so shifting it too would leave the
    user chasing a moving target.
    """
    plain = service.render_text_preview("EDGE", _settings("d24", font_size=40))
    bled = service.render_text_preview(
        "EDGE", _settings("d24", D24_MARGIN_MM, font_size=40))
    calibrated = service.render_text_preview(
        "EDGE", dict(_calibrated("d24", None, x_mm=2.0, y_mm=1.0, scale=0.96),
                     font_size=40))

    # Bleed shows: the previewed label is the wider one.
    assert _preview_size(plain) == (D24_PRINTABLE, D24_PRINTABLE)
    assert _preview_size(bled) == (D24_TOTAL, D24_PRINTABLE)
    assert bled != plain

    # Calibration does not show: same size, same pixels, byte for byte.
    assert calibrated == plain


def test_every_preview_path_reflects_bleed(service):
    """Text, QR, combined and image previews all size themselves the same way."""
    settings = _settings("d24", D24_MARGIN_MM, font_size=30)
    qr_settings = dict(settings, data="BLEED")

    source = Image.new("RGB", (200, 200), "black")
    path = os.path.join(service.upload_folder, "src.png")
    source.save(path)

    previews = [
        service.render_text_preview("EDGE", settings),
        service.render_qrcode_preview(qr_settings),
        service.render_label_preview(dict(qr_settings, text="EDGE")),
        service.render_image_preview(path, settings),
    ]
    for data_url in previews:
        assert _preview_size(data_url) == (D24_TOTAL, D24_PRINTABLE)


def test_the_calibration_target_is_rendered_at_the_bled_size(service):
    """It has to be: it prints through the same path, which now expects it.

    A target rendered to the unbled size would be rejected by ``convert()`` the
    moment the bleed was published. Its reference ring becomes an ellipse, which
    is the truthful picture of the area that can be drawn on -- meeting the die
    cut across the tape, stopping short of it along the feed.
    """
    plain = service._render_calibration_target(_settings("d24"))
    bled = service._render_calibration_target(_settings("d24", D24_MARGIN_MM))
    assert plain.size == (D24_PRINTABLE, D24_PRINTABLE)
    assert bled.size == (D24_TOTAL, D24_PRINTABLE)
    assert _ink_outside_ellipse(bled) == 0
    assert _ink_outside_die_cut(bled, "d24") == 0


def test_the_calibration_run_reports_the_media_bleed_ceiling(service):
    """A UI offering a bleed control needs the per-medium limit from somewhere."""
    described = service.describe_calibration_run(_settings("d24"))
    assert described["bleed"]["limit_mm"] == D24_MARGIN_MM
    assert described["bleed"]["applied_mm"] == 0.0
    assert not described["bleed"]["clamped"]
    # No feed counterpart is reported, because none is ever available.
    assert "feed_mm" not in described["bleed"]
    assert "feed_limit_mm" not in described["bleed"]

    described = service.describe_calibration_run(_settings("d24", 4.0))
    assert described["bleed"]["requested_mm"] == 4.0
    assert described["bleed"]["applied_mm"] == D24_MARGIN_MM
    assert described["bleed"]["clamped"]

    # And the reported travel already accounts for it.
    assert described["sideways_travel_mm"]["max"] == pytest.approx(1.52, abs=0.01)


def test_the_head_capped_ceiling_is_reported_per_printer(service):
    """The same tape, two printers, two honest answers."""
    narrow = service.describe_calibration_run(_settings("62", model=MODEL))
    wide = service.describe_calibration_run(_settings("62", model=WIDE_MODEL))
    assert narrow["bleed"]["limit_mm"] == pytest.approx(1.02, abs=0.01)
    assert wide["bleed"]["limit_mm"] == pytest.approx(1.52, abs=0.01)
