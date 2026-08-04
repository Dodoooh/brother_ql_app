"""
Printer service for managing Brother QL printer operations.
"""

import os
import sys
import io
import base64
import math
import uuid
import structlog
import threading
import time
import socket
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Dict, Any, Iterator, List, NamedTuple, Optional, Tuple, Union
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageMath, ImageOps
import qrcode
from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.devicedependent import label_type_specs, right_margin_addition
from brother_ql.backends import backend_factory, guess_backend

from src.config.default_settings import (
    BLEED_LIMIT_MM,
    CALIBRATION_LIMIT_MM,
    CALIBRATION_SCALE_MAX,
    CALIBRATION_SCALE_MIN,
    DEFAULT_CALIBRATION_SCALE,
    DEFAULT_MAX_UPLOAD_IMAGE_PIXELS,
    MEDIA_EQUIVALENTS,
    medium_variants,
)
from src.services.settings_service import settings_service
# The relay service owns the keep-alive/turn-off arithmetic, so this module asks
# it how long the awake window really is rather than keeping a second copy of the
# rule. The dependency runs one way at import time: relay_service imports only
# the settings service here, and reaches back for the printer service lazily.
from src.services.relay_service import relay_service
from src.services.ipp_client import EMPTY_MEDIA, get_media_ready, get_printer_attributes
from src.services.pdf_renderer import render_pdf, parse_page_range
from src.utils.exceptions import PrinterError, ImageProcessingError, ValidationError
from src.utils.text_markup import (
    FontSet,
    Run,
    draw_runs,
    markup_enabled,
    measure_runs,
    parse_runs,
    runs_text,
    widest_word,
    wrap_runs,
)
from src.utils.uri_validation import validate_printer_uri

logger = structlog.get_logger()

# Printable width of 62 mm tape. Used as the fallback for label identifiers
# brother_ql does not know, which is what the app assumed unconditionally
# before, so unknown media keeps behaving as it always did.
DEFAULT_LABEL_WIDTH_PX = 696

# Auto-fit never shrinks below this; past it the text is unreadable anyway and
# clipping is the more honest outcome.
MIN_AUTO_FIT_FONT_SIZE = 8

# Upper bound for a lengthwise label, in printer dots. The QL series prints at
# 300 dpi, so this is a little under a metre of tape. Lengthwise rendering has
# no width to wrap against, which means a long enough message would silently
# unspool a good part of the roll; refusing is the cheaper failure.
MAX_LENGTHWISE_LENGTH_PX = 11811

# A round label's printable area is a circle, but the media is fed and cut
# mechanically, so the die cut never lands on exactly the same dot twice. Keep a
# sliver of the rim clear so a rounding error or a slightly misregistered cut
# takes white paper rather than the edge of a glyph.
ROUND_LABEL_MARGIN_RATIO = 0.02
MIN_ROUND_LABEL_MARGIN_PX = 2

# Fitting content to a round label looks at where the ink actually is, which
# means walking a whole source image on the print path. A print at 600 dpi from
# a phone camera is tens of megapixels, so the ink mask is max-pooled onto a
# coarser grid before the geometry runs: a block counts as ink when any dot in
# it does, which can only ever make the fit more cautious, never less. The grid
# is kept at least this wide, and the pooling factor never exceeds
# MAX_INK_PROBE_FACTOR so that a single dot in a block still survives Pillow's
# averaging (255 / factor^2 has to round to at least 1).
MAX_INK_PROBE_PX = 1024
MAX_INK_PROBE_FACTOR = 8

# Slack kept between a shifted text block and the chord it is measured against.
# Moving a block towards the rim stops where the chord is exactly as wide as the
# text, and "exactly" does not survive the trip through integer chord widths and
# rounded draw coordinates: the block would be pushed one dot too far, fail its
# own fit check and shrink the font for no visible reason. Two dots of slack
# costs nothing and makes the placement land inside every time.
ROUND_BLOCK_TRAVEL_SLACK_PX = 2

# Where a text block sits on the axis perpendicular to its reading direction.
# Only media with spare room on that axis can honour it: a die-cut label has a
# fixed height, and a continuous roll rendered lengthwise has the tape width to
# play with. Continuous tape rendered across grows to exactly fit the text, so
# there is no slack to distribute and the setting is a no-op there.
VERTICAL_ALIGNMENTS = ("top", "middle", "bottom")
DEFAULT_VERTICAL_ALIGNMENT = "middle"

# Clear space kept between a text block and the edge of a fixed-size label. The
# die cut and the cutter both have a tolerance of a dot or two, so a block flush
# against the very edge comes back trimmed however exact the render was.
LABEL_EDGE_MARGIN_PX = 10

# The QL series rasterises at 300 dpi, so a millimetre of label is this many
# printer dots. Every render path produces its image at the media's printable
# dot count, which makes this the one conversion between a calibration offset
# stated in millimetres and the pixels the content is actually moved by.
DOTS_PER_MM = 300.0 / 25.4

# Continuous media has no length of its own, so the calibration target is given
# a fixed one. 30 mm is long enough to carry the frame, the millimetre scale and
# a caption even on the narrowest roll, and short enough that iterating a few
# times does not eat a visible amount of tape.
CALIBRATION_TARGET_LENGTH_MM = 30.0

# The millimetre scale printed along both axes of the target: a mark every
# millimetre, every fifth one drawn long. This is what makes the error readable
# without a ruler -- the unit needed to measure the error is printed on the
# label, right next to it.
CALIBRATION_MAJOR_TICK_EVERY = 5

# Smallest caption the target will print. At 300 dpi 14 px is roughly 1.2 mm of
# type -- fine print, but still print. Much below that a thermal head turns
# letters into smudges, so the target shortens the caption and finally drops it
# instead: d12 has under 8 mm of printable circle, and a scale that can still
# be counted is worth more there than a note nobody can decipher.
MIN_CALIBRATION_FONT_PX = 14

# Upper bound for the one-pass sweep. Every step costs a physical label, so the
# cap is deliberately low: nine steps at 0.5 mm already cover +/-2 mm, which is
# more than the registration tolerance this feature exists to cancel out.
MAX_CALIBRATION_SWEEP_STEPS = 9


# --------------------------------------------------------------------------- #
# How big an uploaded image may be before it is turned away.
#
# Pillow's ``Image.MAX_IMAGE_PIXELS``, which the API controllers set to 50 MP,
# does NOT answer this question: it is a warning threshold, and Pillow only
# raises ``DecompressionBombError`` above *twice* that. Everything in between
# passed -- a 79 KB, 8000 x 8000 PNG (64 MP) was decoded and resized in full,
# and it was decoded before the printer URI was even validated, so the work
# happened even for a job that could never print.
#
# So the app states its own limit and checks it itself, at the point where the
# image is still nothing but a path on disk. ``Image.open`` reads the header and
# does not decode, which is what makes the check nearly free: dimensions are
# known long before any pixel buffer is allocated.
# --------------------------------------------------------------------------- #

def max_upload_image_pixels() -> int:
    """Return how many pixels an uploaded image may hold (robustly parsed).

    Reads ``MAX_UPLOAD_IMAGE_PIXELS`` from the environment on every call, so a
    deployment's value takes effect wherever the process reads it and tests can
    set it per case.

    The variable is deliberately NOT called ``MAX_IMAGE_PIXELS``: that name is
    already taken by Pillow's own module attribute with different semantics (a
    soft warning at the value, a hard error at twice it), and two settings
    sharing one name would be read as one setting by everybody who met either.

    ``0`` means "no limit". A missing, empty, negative or non-numeric value
    falls back to
    :data:`~src.config.default_settings.DEFAULT_MAX_UPLOAD_IMAGE_PIXELS`; only
    an explicit zero switches the guard off, because a blank variable in a
    compose file means "not configured", not "unprotected".

    Returns:
        Maximum pixel count, or 0 for "unlimited".
    """
    raw = os.environ.get("MAX_UPLOAD_IMAGE_PIXELS")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_MAX_UPLOAD_IMAGE_PIXELS
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_UPLOAD_IMAGE_PIXELS
    # Negative is nonsense rather than an opt-out; only an explicit 0 disables.
    return value if value >= 0 else DEFAULT_MAX_UPLOAD_IMAGE_PIXELS


def guard_image_pixels(image_path: str) -> None:
    """Reject an image whose pixel count exceeds :func:`max_upload_image_pixels`.

    Call this BEFORE anything opens the file for real: rotation, resizing and
    the round-label ink scan all walk the decoded bitmap, and the whole point is
    that the decode never happens for a file this size.

    A file that is not an image at all, or cannot be read, is deliberately NOT
    turned into an error here -- that is not the question being asked, and the
    pipeline behind this call already reports those cases in its own words. The
    guard only ever speaks about size.

    Args:
        image_path: Path to the image file as it arrived from the caller.

    Raises:
        ValidationError: If the image is larger than the configured limit, or
            large enough that Pillow refused to open it at all (-> HTTP 400).
    """
    limit = max_upload_image_pixels()
    if not limit:
        return

    try:
        with Image.open(image_path) as probe:
            width, height = probe.size
    except Image.DecompressionBombError as e:
        # Past twice Pillow's own threshold the header is all we get: Pillow
        # will not hand over the size. Its message carries the pixel count, so
        # quoting it still names both the actual value and the limit.
        raise ValidationError(
            f"Image is too large to print: {e} (this app allows at most "
            f"{limit} pixels; set MAX_UPLOAD_IMAGE_PIXELS to change it, "
            f"0 removes the limit).",
            field="image",
            details={"limit": limit},
        ) from e
    except Exception:  # noqa: BLE001 - not an image, or unreadable: not our call
        return

    pixels = width * height
    if pixels > limit:
        raise ValidationError(
            f"Image is too large to print: {width}x{height} is {pixels} pixels, "
            f"more than the limit of {limit}. Scale the image down, or raise "
            f"MAX_UPLOAD_IMAGE_PIXELS (0 removes the limit).",
            field="image",
            details={"width": width, "height": height,
                     "pixels": pixels, "limit": limit},
        )


# --------------------------------------------------------------------------- #
# Bleed: printing into the strip brother_ql calls non-printable.
#
# Every die-cut label is offered smaller than it is. A 24 mm round label is
# 284x284 dots of paper of which brother_ql publishes 236x236 as printable, so
# 2.03 mm of paper all round is simply not addressable by any design -- which is
# exactly the ring of white a user with the physical label in hand measures.
# The same is true, less dramatically, of continuous tape: 62 mm is 732 dots
# wide and 696 are offered.
#
# The two numbers that produce that behaviour, ``dots_printable`` and
# ``right_margin_dots``, both live in brother_ql's module-level media table, and
# raising the first while lowering the second by half the growth lets a larger
# raster through, still centred on the label. That is all bleed is.
#
# Bleed applies ACROSS THE TAPE ONLY. It never lengthens the raster, and the
# reason is a measured one rather than a theoretical one: a die-cut job whose
# raster was extended along the feed put the cut in the wrong place on a
# QL-820NWB, walking off the die-cut gap until the roll had to be re-seated.
# The protocol says why that is possible and why it cannot be compensated for.
# Each raster line is one feed step, so the number of lines transmitted *is* the
# distance the media advances while printing; adding 48 lines to a d24 page adds
# 4.06 mm to that advance. The only feed-related command in the stream is
# ``ESC i d`` (:meth:`BrotherQLRaster.add_margins`), which carries the label's
# own ``feed_margin`` -- 0 for d24 -- and is packed unsigned, so nothing in the
# raster language can give those steps back. Everything downstream of the print
# phase, the cut position included, moves with it.
#
# What the printer does with the *leading* edge of a longer raster -- whether it
# centres it on the label or starts where it always does -- is NOT established,
# and nothing here should be read as claiming it either way. It does not need
# to be: the cut is reason enough, and it stands whatever the answer is.
#
# Bleed is deliberately NOT part of the calibration map, and the distinction is
# worth stating plainly because the two features look alike from a distance and
# the next person will be tempted to fold them together:
#
#   calibration corrects a *printer error* -- ink that lands somewhere other
#       than where the raster says. It therefore never touches a preview: the
#       preview is the intended label, and the correction exists to make the
#       paper match it. Shifting the preview too would be chasing a moving
#       target.
#   bleed changes *what the user may design* -- how much label there is to draw
#       on. It is the same kind of setting as the label size, so it MUST show in
#       previews. A preview that hid it would be a picture of a smaller label
#       than the one being printed.
#
# They compose rather than overlap: bleed decides how big the raster is, then
# calibration decides where that raster lands.
#
# It is off by default and has to stay that way, because it is printing into an
# area the manufacturer declares non-printable and two of the consequences show
# up on the paper rather than in the log:
#
#   * the die cut is punched and the media fed with a tolerance of a few tenths
#     of a millimetre, so content taken right out to the rim shows that
#     variation label to label -- it is for backgrounds that are meant to run
#     off, not for anything that has to look deliberate;
#   * ink that overshoots the die cut lands on the liner between labels.
#
# Both are stated on the setting itself in openapi.yaml, where somebody is
# actually deciding whether to switch it on.
# --------------------------------------------------------------------------- #


class LabelBleed(NamedTuple):
    """How far outside the published printable area one medium may be printed.

    One dimension only. There is deliberately no feed-axis member: bleed grows
    the raster across the tape and never along it, so a second axis here would
    be a field that is always zero and an invitation to start using it.

    Attributes:
        label_size: The label identifier this was resolved for.
        requested_mm: What the settings asked for, per side.
        applied_mm: What the medium and the print head allow.
        dots: Per-side growth across the tape, in printer dots. The raster
            grows by twice this and the published right margin drops by exactly
            this, which is what keeps the label centred.
        limit_mm: The medium's own non-printable margin across the tape, after
            the print head's width has been taken into account.
        was_clamped: Whether more was asked for than could be given.
    """

    label_size: str
    requested_mm: float
    applied_mm: float
    dots: int
    limit_mm: float
    was_clamped: bool

    @property
    def is_zero(self) -> bool:
        """Whether this bleed changes nothing at all."""
        return not self.dots


NO_BLEED = LabelBleed("", 0.0, 0.0, 0, 0.0, False)


def _device_pixel_width(printer_model: str) -> Optional[int]:
    """Return the print head's width in dots, or None for an unknown model."""
    try:
        return int(BrotherQLRaster(str(printer_model or "")).get_pixel_width())
    except Exception:  # noqa: BLE001 - an unknown model simply has no known head
        return None


def _bleed_limit_dots(label_size: str, printer_model: str = "") -> Optional[int]:
    """Return how much bleed a medium physically has across the tape, per side.

    Two separate ceilings apply and the tighter one wins:

    * the medium's own non-printable margin, i.e. half the difference between
      the label's total width in dots and its printable width. Bleeding past it
      would ask for ink outside the paper.
    * the print head. ``convert()`` pastes the label's raster into a device row
      of a fixed width and ``add_raster_data`` refuses anything wider, so a
      raster grown past the head is not a wider label but a failed print. On the
      62 mm media this is the binding limit: 732 dots of tape against a 720-dot
      head on every QL-800-class printer, so only 12 of the 18 dots of margin
      are reachable.

    Growth is symmetric by construction -- the value returned is per side and
    the raster grows by twice it -- because the right margin can only absorb
    half the growth, and an asymmetric split would move the label sideways by
    the difference. An odd total margin therefore loses its odd dot (identifier
    "104" has 27 dots of margin and can bleed 13).

    The label's *length* margin is deliberately not returned, and it is not an
    oversight that it is bigger: a rectangular die cut has 2.96 mm of unused
    paper along the feed against 1.52 mm across it, and none of that 2.96 mm is
    reachable. Lengthening the raster moves the cut (see the section comment
    above), so the feed axis is not a resource bleed may spend.

    Args:
        label_size: Label identifier, e.g. "d24".
        printer_model: Model whose head width caps the growth. An unrecognised
            model leaves the cap off rather than inventing one.

    Returns:
        The per-side growth in dots, or None when the medium is not in the
        catalogue -- an unknown label has no known margin, and guessing one
        would print off the edge of the paper.
    """
    try:
        from brother_ql.labels import ALL_LABELS

        label = next((entry for entry in ALL_LABELS
                      if entry.identifier == str(label_size)), None)
    except Exception:  # noqa: BLE001 - catalogue unavailable, so no bleed
        logger.warning("Could not resolve the media catalogue for bleed",
                       label_size=str(label_size), exc_info=True)
        return None
    if label is None:
        return None

    total_width = label.dots_total[0]
    printable_width = label.dots_printable[0]

    head = _device_pixel_width(printer_model)
    if head is not None:
        total_width = min(total_width, head)
    return max(0, (total_width - printable_width) // 2)


def _requested_bleed_mm(settings: Optional[Dict[str, Any]], label_size: str) -> float:
    """Return the bleed one medium asks for, in millimetres, or 0.

    A malformed entry is ignored rather than fatal, the same bargain the
    calibration map makes: a bad number in a settings file should not stop a
    label printing, it should print the label the way it printed before the
    setting existed.

    Args:
        settings: Resolved print settings, possibly carrying ``bleed_mm``.
        label_size: Label identifier to look up.

    Returns:
        The requested bleed per side in millimetres, never negative and never
        above :data:`BLEED_LIMIT_MM`.
    """
    if not settings:
        return 0.0
    bleed_map = settings.get("bleed_mm")
    if not bleed_map:
        return 0.0
    if not isinstance(bleed_map, dict):
        logger.warning("Ignoring malformed bleed settings",
                       bleed_type=str(type(bleed_map)))
        return 0.0

    value = bleed_map.get(label_size)
    if value is None:
        return 0.0
    # bool is an int subclass, and "bleed by True millimetres" is not a request.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("Ignoring non-numeric bleed", label_size=label_size,
                       value=repr(value))
        return 0.0

    value = float(value)
    clamped = max(0.0, min(BLEED_LIMIT_MM, value))
    if clamped != value:
        logger.warning("Clamped out-of-range bleed", label_size=label_size,
                       requested_mm=value, applied_mm=clamped,
                       limit_mm=BLEED_LIMIT_MM)
    return clamped


def get_label_bleed(settings: Optional[Dict[str, Any]],
                    label_size: Optional[str] = None,
                    warn: bool = False) -> LabelBleed:
    """Resolve how much bleed a medium gets for this print.

    The stored value is a request in millimetres; what comes back is what the
    paper and the print head can actually give, in whole dots, per side, across
    the tape. A request beyond the medium's real margin is not merely useless:
    it would push the raster past the strip of label that exists, so it is
    clamped here and the reason is logged.

    Args:
        settings: Resolved print settings carrying ``bleed_mm`` and
            ``printer_model``.
        label_size: Label identifier. Defaults to the one in ``settings``.
        warn: Whether to log a clamp. False by default because this is resolved
            on every geometry lookup -- several times per render -- and the
            print path calls it once with ``warn=True``, so one print produces
            one warning at the point the bleed is really applied.

    Returns:
        A :class:`LabelBleed`. :data:`NO_BLEED` (with the key filled in) when
        nothing is configured, so an installation that never asks for bleed
        renders exactly as it always did.
    """
    settings = settings or {}
    key = str(label_size if label_size is not None else settings.get("label_size") or "")
    requested = _requested_bleed_mm(settings, key)
    if requested <= 0:
        return NO_BLEED._replace(label_size=key)

    limit = _bleed_limit_dots(key, str(settings.get("printer_model") or ""))
    if limit is None:
        if warn:
            logger.warning("No media entry to bleed into; printing the "
                           "published printable area", label_size=key,
                           requested_mm=requested)
        return NO_BLEED._replace(label_size=key, requested_mm=requested,
                                 was_clamped=True)

    wanted = int(round(requested * DOTS_PER_MM))
    dots = min(wanted, limit)

    bleed = LabelBleed(
        label_size=key,
        requested_mm=requested,
        # Re-derived from the dots that will actually be used, so a value read
        # back and stored again converts to exactly the same dots.
        applied_mm=round(dots / DOTS_PER_MM, 2),
        dots=dots,
        limit_mm=round(limit / DOTS_PER_MM, 2),
        was_clamped=dots < wanted,
    )
    if warn and bleed.was_clamped:
        logger.warning(
            "Clamped bleed to the medium's non-printable margin",
            label_size=key, printer_model=str(settings.get("printer_model") or ""),
            requested_mm=requested, requested_px=wanted,
            applied_px=dots, applied_mm=bleed.applied_mm,
            limit_mm=bleed.limit_mm,
            reason=("there is no more label outside the printable area, and a "
                    "raster wider than the print head cannot be sent at all"),
        )
    return bleed


class LabelGeometry(tuple):
    """The printable geometry of one label, as a tuple with named access.

    This *is* the historical ``(width, height, is_die_cut)`` 3-tuple -- it
    compares and unpacks exactly like one, so existing callers are unaffected --
    extended with the ``is_round`` flag. Round and rectangular die-cut media are
    both a fixed size, but only the round ones have a *circular* printable area:
    content laid out to the full square corners falls outside the die cut and is
    simply not on the label the user peels off.

    Attributes:
        width: Printable width in pixels.
        height: Printable height in pixels, 0 for continuous tape.
        is_die_cut: Whether the label has a fixed physical size.
        is_round: Whether the printable area is a circle of diameter ``width``.
    """

    def __new__(cls, width: int, height: int, is_die_cut: bool, is_round: bool = False):
        geometry = tuple.__new__(cls, (int(width), int(height), bool(is_die_cut)))
        geometry.is_round = bool(is_round)
        return geometry

    @property
    def width(self) -> int:
        return self[0]

    @property
    def height(self) -> int:
        return self[1]

    @property
    def is_die_cut(self) -> bool:
        return self[2]


def get_label_geometry(label_size: Optional[str],
                       settings: Optional[Dict[str, Any]] = None) -> LabelGeometry:
    """Return ``(drawable_width_px, drawable_height_px, is_die_cut)`` for a roll.

    brother_ql already knows the true printable area of every supported label,
    so look it up rather than assuming one. Continuous ("endless") rolls report
    a height of 0: their length is unbounded, so a label may grow downwards.
    Die-cut labels are a fixed physical size that the content has to fit inside
    -- ``convert()`` rejects any other height outright.

    This is the single source of the drawable size: the round chord maths, the
    die-cut canvas in :meth:`PrinterService._fit_to_label`, the text layout and
    the calibration target all size themselves from it. Which is why ``bleed_mm``
    is applied *here* rather than at each of them -- passing ``settings`` makes
    every one of those follow, print path and preview alike, and there is no
    second place that has to be kept in step. (The one thing it cannot reach is
    ``convert()``, which reads brother_ql's own table; see
    :func:`placed_raster`, which publishes the same numbers there.)

    Args:
        label_size: Label identifier, e.g. "62", "50", "62x29" or "d24".
        settings: Optional print settings. When they carry a ``bleed_mm`` entry
            for this medium, the *width* returned includes the bleed -- so the
            caller draws on the wider label. The height is never bled; see the
            bleed section comment for the measured reason. Omitting the settings
            yields the published printable area, which is what every caller got
            before bleed existed.

    Returns:
        A :class:`LabelGeometry`: the drawable width in pixels, the drawable
        height in pixels (0 for continuous tape) and whether the label is
        die-cut, plus an ``is_round`` attribute for round die-cut media.

        Note that a bled *round* label is no longer square: d24 becomes
        284 x 236. Its drawable area is correspondingly an ellipse rather than
        a circle, which the round layout handles directly -- see
        :func:`get_round_safe_axes`.
    """
    if label_size:
        try:
            from brother_ql.labels import ALL_LABELS, FormFactor

            for label in ALL_LABELS:
                if label.identifier == str(label_size):
                    is_round = label.form_factor == FormFactor.ROUND_DIE_CUT
                    die_cut = is_round or label.form_factor == FormFactor.DIE_CUT
                    width, height = label.dots_printable
                    width += 2 * get_label_bleed(settings, str(label_size)).dots
                    return LabelGeometry(width, height, die_cut, is_round)
        except Exception:
            logger.warning(
                "Could not resolve label geometry, falling back to 62 mm",
                label_size=label_size,
                exc_info=True,
            )
    return LabelGeometry(DEFAULT_LABEL_WIDTH_PX, 0, False, False)


def get_label_width(label_size: Optional[str],
                    settings: Optional[Dict[str, Any]] = None) -> int:
    """Return the drawable width in pixels for a label identifier."""
    return get_label_geometry(label_size, settings)[0]


# --------------------------------------------------------------------------- #
# Identifying the medium the printer says is loaded.
#
# The printer reports a size in millimetres; the app speaks in brother_ql label
# identifiers. Mapping one onto the other is a lookup, but it is a lookup with
# three measured traps in it, and every one of them is a wrong answer rather
# than a missing one -- which is why this returns a *list* of candidates and a
# reason instead of a single best guess. Where the media genuinely cannot be
# told apart, that has to reach the user as ambiguity, not as a coin flip.
#
#   1. The catalogue states each medium's size three different ways and they
#      disagree. ``60x86`` is the clearest case: the printer reports 60x86,
#      ``dots_total`` works out to 59.94 x 86.70 mm, and ``tape_size`` claims
#      (60, 87). Matching ``tape_size`` alone misses the label the printer is
#      holding, so a medium matches when the report is close to *any* of the
#      three sizes the catalogue gives for it.
#   2. IPP sorts the dimension pair. ``om_brother-label-29x62mm`` is this app's
#      ``62x29``; the pair carries no indication of which axis runs across the
#      tape, so both sides are sorted before they are compared.
#   3. A printer's ``media-supported`` is not the catalogue. ``102x51``,
#      ``102x152`` and ``103x164`` are absent from a QL-820NWB entirely -- they
#      are QL-1100-series media -- so nothing here may assume a particular
#      printer's list, and matching runs against the whole app-supported set.
# --------------------------------------------------------------------------- #

# How far the reported size may sit from a catalogue size and still match. The
# report is quantised to whole millimetres in the media name and to hundredths
# in media-col-ready, while the catalogue rounds through printer dots, so a few
# tenths of disagreement is normal. 0.8 mm absorbs that and still separates
# every distinct medium in the catalogue -- the closest pair that must stay
# apart is 52x29 and 54x29, 1.35 mm away from each other.
MEDIA_MATCH_TOLERANCE_MM = 0.8

# ``12+17`` is not a medium. It is the 12 mm roll with the app rendering a
# 29 mm-wide raster onto it, so its dots_total describes a rendering choice
# rather than a piece of paper -- taken at face value it would match a 29 mm
# roll, which is a different roll entirely. It is therefore kept out of
# geometric matching and reached only through the equivalence group below.
_RENDERING_ONLY_IDENTIFIERS = frozenset({"12+17"})

# The three media that cannot be told apart from what the printer reports.
# Whichever member matches, the whole group is returned, because picking one
# would be inventing information the device did not supply.
#
# The table itself lives in src.config.default_settings as MEDIA_EQUIVALENTS,
# because the settings validator needs the same groups to check the media memory
# and importing this module from there would be a cycle. Its first member is the
# plain variant; see the comment there for why that ordering carries weight.


class LabelIdentification(NamedTuple):
    """What medium the printer is holding, in the app's own identifiers.

    Attributes:
        candidates: Every label identifier the report is consistent with, in
            catalogue order. Empty when nothing matched or nothing was read.
        reason: Human-readable account of the match -- what was measured, and
            where two identifiers could not be separated.
    """

    candidates: Tuple[str, ...]
    reason: str

    @property
    def resolved(self) -> bool:
        """Whether the report matched at least one supported medium."""
        return bool(self.candidates)

    @property
    def ambiguous(self) -> bool:
        """Whether more than one identifier remains possible."""
        return len(self.candidates) > 1

    def matches(self, label_size: Optional[str]) -> Optional[bool]:
        """Whether ``label_size`` is among the candidates.

        Returns None -- not False -- when nothing was identified, so "we do not
        know" stays distinguishable from "they disagree".
        """
        if not self.candidates or not label_size:
            return None
        return str(label_size) in self.candidates


def _supported_labels() -> List[Any]:
    """Every label this app offers, in catalogue order.

    P-touch media is excluded: those are TZe tapes for a different family of
    machines, they are not in the app's label enum, and several of them share a
    dots_total that would collide with QL media.
    """
    from brother_ql.labels import ALL_LABELS, FormFactor

    return [label for label in ALL_LABELS if label.form_factor != FormFactor.PTOUCH_ENDLESS]


def _is_continuous(label: Any) -> bool:
    from brother_ql.labels import FormFactor

    return label.form_factor == FormFactor.ENDLESS


def _is_round(label: Any) -> bool:
    from brother_ql.labels import FormFactor

    return label.form_factor == FormFactor.ROUND_DIE_CUT


def _dots_to_mm(dots: int) -> float:
    """Convert printer dots to millimetres at the QL series' fixed 300 dpi."""
    return dots * 25.4 / 300.0


def _identifier_sizes(identifier: str) -> Tuple[float, ...]:
    """The size an identifier states about itself, e.g. "62x29" -> (62, 29).

    Round identifiers ("d24") are skipped: their diameter is already covered by
    dots_total, and reading the digits out of them would invite a round label to
    match a rectangular one of the same nominal width.
    """
    if identifier.startswith("d"):
        return ()
    parts = identifier.split("x")
    sizes: List[float] = []
    for part in parts:
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return ()
        sizes.append(float(digits))
    return tuple(sizes)


def _catalogue_sizes(label: Any) -> List[Tuple[float, ...]]:
    """Every size the catalogue states for one medium, each sorted ascending.

    Continuous media yields 1-tuples (the tape width); die-cut media yields
    sorted 2-tuples, because the report does not say which axis is which.
    """
    continuous = _is_continuous(label)
    sizes: List[Tuple[float, ...]] = []
    if continuous:
        sizes.append((_dots_to_mm(label.dots_total[0]),))
        sizes.append((float(label.tape_size[0]),))
        identifier = _identifier_sizes(label.identifier)
        if len(identifier) == 1:
            sizes.append(identifier)
    else:
        sizes.append(tuple(sorted(_dots_to_mm(dots) for dots in label.dots_total)))
        sizes.append(tuple(sorted(float(side) for side in label.tape_size)))
        identifier = _identifier_sizes(label.identifier)
        if len(identifier) == 2:
            sizes.append(tuple(sorted(identifier)))
    return sizes


def _sizes_match(reported: Tuple[float, ...], candidate: Tuple[float, ...]) -> bool:
    if len(reported) != len(candidate):
        return False
    return all(abs(a - b) <= MEDIA_MATCH_TOLERANCE_MM
               for a, b in zip(reported, candidate, strict=True))


def _describe_media(width_mm: float, length_mm: Optional[float],
                    continuous: bool, is_round: Optional[bool]) -> str:
    if continuous:
        return f"{width_mm:g} mm continuous tape"
    shape = "round " if is_round else ""
    return f"{width_mm:g} x {float(length_mm or 0.0):g} mm {shape}die-cut label"


def identify_label_candidates(media: Optional[Dict[str, Any]]) -> LabelIdentification:
    """Map a media report from the printer onto this app's label identifiers.

    Takes the dict produced by :func:`src.services.ipp_client.extract_media`
    (``width_mm``, ``length_mm``, ``media_type``, ``media_name``, ``is_round``)
    and returns every identifier the report is consistent with, never a single
    guess. Three continuous media are genuinely indistinguishable from what a
    printer reports and always come back as a pair; see ``MEDIA_EQUIVALENTS``.

    The form factor comes from ``media_type`` -- ``labels`` means die-cut,
    ``roll`` means continuous -- and falls back to the reported length when the
    printer does not say (a length of 0 or none is continuous tape).

    Round versus rectangular is decided **geometrically**, not from the tray's
    "Dia" marking: no round size coincides with a rectangular one anywhere in
    Brother's catalogue (the nearest pair, d24 at 24.05 mm and 23x23 at
    23.03 mm, is 1.02 mm apart, comfortably outside the tolerance), so the
    measured size settles it on its own. That is the signal to depend on
    because it survives a printer that reports no tray string, an empty
    ``medianame`` or a localised one. The "Dia" token is still read -- it is
    carried through as ``is_round`` and cross-checked here, and a disagreement
    is logged rather than silently resolved.

    Args:
        media: The media report, or None.

    Returns:
        A :class:`LabelIdentification`. ``candidates`` is empty when the printer
        reported no media, was unreachable, or reported a medium this app does
        not support -- the reason says which.
    """
    if not media or media.get("width_mm") is None:
        return LabelIdentification((), "No media reported by the printer")

    try:
        width_mm = float(media["width_mm"])
    except (TypeError, ValueError):
        return LabelIdentification((), "Printer reported an unreadable media width")

    length_raw = media.get("length_mm")
    try:
        length_mm = float(length_raw) if length_raw is not None else None
    except (TypeError, ValueError):
        length_mm = None

    media_type = str(media.get("media_type") or "").strip().lower()
    if media_type == "roll":
        continuous = True
    elif media_type == "labels":
        continuous = False
    else:
        # No form factor reported: a medium with no length of its own is tape.
        continuous = not length_mm

    reported = (width_mm,) if continuous else tuple(sorted((width_mm, length_mm or 0.0)))
    description = _describe_media(width_mm, length_mm, continuous, media.get("is_round"))

    try:
        labels = _supported_labels()
    except Exception:
        logger.warning("Could not read the label catalogue for media identification", exc_info=True)
        return LabelIdentification((), "Label catalogue unavailable")

    order = {label.identifier: index for index, label in enumerate(labels)}
    matched: List[str] = []
    for label in labels:
        if _is_continuous(label) != continuous:
            continue
        if label.identifier in _RENDERING_ONLY_IDENTIFIERS:
            continue
        if any(_sizes_match(reported, size) for size in _catalogue_sizes(label)):
            matched.append(label.identifier)

    if not matched:
        return LabelIdentification(
            (), f"{description} does not match any label this app supports")

    notes: List[str] = []
    for group, note in MEDIA_EQUIVALENTS:
        if any(identifier in matched for identifier in group):
            for identifier in group:
                if identifier in order and identifier not in matched:
                    matched.append(identifier)
            notes.append(note)

    # Cross-check against the tray's "Dia" marking. With the current catalogue
    # this can never disagree -- geometry already separates round from
    # rectangular -- so a disagreement means a printer reporting something new
    # and is worth a log line rather than a silent override.
    hint = media.get("is_round")
    if hint is not None and not continuous:
        wrong_shape = [identifier for identifier in matched
                       if _is_round(labels[order[identifier]]) != bool(hint)]
        if wrong_shape and len(wrong_shape) < len(matched):
            logger.warning("Media shape hint disagrees with the measured size",
                           media_name=media.get("media_name"), is_round=hint,
                           dropped=wrong_shape)
            matched = [identifier for identifier in matched if identifier not in wrong_shape]

    matched.sort(key=lambda identifier: order.get(identifier, len(order)))
    reason = description
    if len(matched) == 1:
        reason = f"{description} identifies {matched[0]}"
    elif notes:
        reason = f"{description} cannot be resolved further: " + "; ".join(notes)
    else:
        reason = f"{description} matches {', '.join(matched)}"
    return LabelIdentification(tuple(matched), reason)


# --------------------------------------------------------------------------- #
# Resolving an ambiguous detection, and following the printer automatically.
#
# Identification stops at "these are the identifiers the report is consistent
# with", because that is all the device says. Automatic switching needs one
# identifier, and the whole question is where the extra bit of information comes
# from -- because it must not come from a coin flip. Four sources are consulted,
# in this order, and each of them is a thing the *user* said rather than a thing
# the app inferred:
#
#   1. preference -- which variant of this medium the user has declared they
#      mean. "For 62 mm rolls I mean the red one." A standing instruction.
#   2. memory -- what was last settled on for this very medium. A recollection,
#      not a guess: if 62red was in use the last time a 62 mm roll was loaded,
#      loading a 62 mm roll again returns to 62red.
#   3. owned media -- what the user says they actually have. If only one
#      candidate is a tape they own, the others are not in the building.
#   4. the documented plain-variant default -- 62 over 62red, 12 over 12+17,
#      103 over 104. Not a tie-break so much as a stated convention: the plain
#      variant is the one that costs nothing to be wrong about, and the other
#      member of every group is the one that has to be chosen deliberately.
#
# The preference goes first, ahead of the memory, and the difference between the
# two is the reason. The memory is *inferred* -- it is the app noticing what the
# user did last time. The preference is *stated*. When the two disagree, one of
# them is a standing instruction and the other is a single afternoon's work, and
# a standing instruction that any one contrary pick could repeal would not be
# one. So the pick is still remembered (see record_label_choice), it just does
# not outrank what the user said in as many words.
#
# It also fills a real gap rather than adding a preference for its own sake:
# ownership cannot settle two of these three media at all. 12/12+17 and 103/104
# are one physical roll addressed two ways, so nobody can own one and not the
# other; and 62/62red is settled by ownership only for a user who owns the red
# roll and no plain one. A user with a black roll and a black/red roll -- the
# ordinary case, on the one medium where a wrong guess produces a finished bad
# label instead of an error -- gets no help from ownership at all.
#
# If none of the four settles it, nothing is chosen. That is the point of the
# order rather than a gap in it: automatic mode leaves label_size exactly as it
# was and reports the ambiguity, which is what the manual path already does. A
# label that did not print is a question; a label printed on the wrong medium is
# a bad label that looks deliberate.
#
# Ownership narrows; it never censors. A medium the user has not claimed to own
# is still read, still identified and still reported -- the list decides what the
# app may pick between when the device is genuinely ambiguous, and nothing else.
# A printer holding a roll the user forgot to list is a fact, and facts do not
# get filtered out of a status report.
# --------------------------------------------------------------------------- #

# How a single label identifier was arrived at, reported so a client can show
# why one candidate won rather than presenting the result as an oracle.
MEDIA_RESOLVED_BY_DETECTION = "detection"    # the device left only one possibility
MEDIA_RESOLVED_BY_PREFERENCE = "preference"  # the variant the user declared for it
MEDIA_RESOLVED_BY_MEMORY = "memory"          # last settled on for this medium
MEDIA_RESOLVED_BY_OWNED = "owned"            # the only candidate the user owns
MEDIA_RESOLVED_BY_DEFAULT = "default"        # the documented plain variant

# What automatic mode wants the client to do about it.
MEDIA_SWITCH_NONE = "none"            # nothing to do (or the feature is off)
MEDIA_SWITCH_APPLY = "switch"         # adopt auto_switch.to as the label size
MEDIA_SWITCH_AMBIGUOUS = "ambiguous"  # a roll is loaded, but the app must not pick


class MediaResolution(NamedTuple):
    """Which single label identifier an identification comes down to.

    Attributes:
        label_size: The winning identifier, or None when the medium could not be
            narrowed to one. None is a result, not a failure: it is what stops
            automatic mode from picking.
        resolved_by: Which step settled it -- one of ``detection``,
            ``preference``, ``memory``, ``owned`` or ``default`` -- or None when
            nothing did.
        reason: Human-readable account of why this candidate won, or of why no
            candidate could.
    """

    label_size: Optional[str]
    resolved_by: Optional[str]
    reason: str

    @property
    def resolved(self) -> bool:
        """Whether exactly one identifier came out of it."""
        return self.label_size is not None


def media_memory_key(candidates: Tuple[str, ...]) -> Optional[str]:
    """The key a set of candidates is remembered under, or None.

    The key names the *medium*, so it is the plain variant of the group the
    candidates belong to (see :func:`src.config.default_settings.medium_key` for
    why that is the stable choice). A single candidate names its own medium.

    Args:
        candidates: The identifiers the printer's report is consistent with.

    Returns:
        The medium's key, or None when the candidates do not all belong to one
        medium. That last case cannot arise from today's catalogue -- only the
        three documented groups produce more than one candidate -- and returning
        None rather than a first-in-the-list guess is what keeps it that way: an
        ambiguity nobody has documented gets no key, no default, and therefore no
        automatic switch.
    """
    if not candidates:
        return None
    group = medium_variants(candidates[0])
    if all(candidate in group for candidate in candidates):
        return group[0]
    return None


def owned_media(settings: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """The label identifiers the user says they own, in the order given.

    A malformed list is ignored rather than fatal, the same bargain the
    calibration and bleed maps make: this is a hint used to narrow a guess, and a
    bad value in it must not cost a status check.

    Args:
        settings: Settings possibly carrying ``owned_media``.

    Returns:
        The identifiers, with anything that is not a non-empty string dropped.
    """
    if not settings:
        return ()
    owned = settings.get("owned_media")
    if not owned:
        return ()
    if not isinstance(owned, (list, tuple)):
        logger.warning("Ignoring malformed owned media list",
                       owned_type=str(type(owned)))
        return ()
    return tuple(entry for entry in owned if isinstance(entry, str) and entry.strip())


def _medium_choice(settings: Optional[Dict[str, Any]], key: Optional[str],
                   setting: str) -> Optional[str]:
    """The identifier a per-medium map names for the medium ``key``, if any.

    ``media_memory`` and ``media_preference`` are the same shape -- a medium's
    plain variant to one of that medium's identifiers -- and are read the same
    way, including the bargain about malformed data: a bad value is logged and
    ignored rather than raised, because both are hints consulted on the status
    path and neither may cost a status check.

    Args:
        settings: The settings to read from, or None.
        key: The medium's plain variant, or None when the candidates name no one
            medium.
        setting: Which map to read -- ``media_preference`` or ``media_memory``.

    Returns:
        The identifier stored for that medium, or None.
    """
    if not settings or not key:
        return None
    stored = settings.get(setting)
    if not stored:
        return None
    if not isinstance(stored, dict):
        logger.warning("Ignoring a malformed per-medium map",
                       setting=setting, value_type=str(type(stored)))
        return None
    chosen = stored.get(key)
    if chosen is None:
        return None
    if not isinstance(chosen, str) or not chosen.strip():
        logger.warning("Ignoring a malformed per-medium entry",
                       setting=setting, medium=key, value=repr(chosen))
        return None
    return chosen


def _preferred_label(settings: Optional[Dict[str, Any]],
                     key: Optional[str]) -> Optional[str]:
    """The variant the user has declared for the medium ``key``, if any."""
    return _medium_choice(settings, key, "media_preference")


def _remembered_label(settings: Optional[Dict[str, Any]],
                      key: Optional[str]) -> Optional[str]:
    """The label identifier last settled on for the medium ``key``, if any."""
    return _medium_choice(settings, key, "media_memory")


def resolve_media_label(candidates: Tuple[str, ...],
                        settings: Optional[Dict[str, Any]] = None) -> MediaResolution:
    """Narrow a detection to one label identifier, or decline to.

    The steps are tried in the order documented in the section comment above --
    the declared preference, then memory, then owned media, then the
    plain-variant default -- and the first one that produces exactly one
    identifier wins. A preferred or remembered choice that is not among the
    current candidates (a preference for a medium that is not loaded, a roll that
    was changed, a catalogue that moved underneath) is skipped rather than
    trusted, and so is an owned-media list that leaves two candidates standing.

    Args:
        candidates: The identifiers the printer's report is consistent with.
        settings: Settings carrying ``media_preference``, ``media_memory`` and
            ``owned_media``.

    Returns:
        A :class:`MediaResolution`. ``label_size`` is None when nothing settled
        it, which is the signal that automatic mode must leave the setting alone.
    """
    candidates = tuple(str(candidate) for candidate in (candidates or ()))
    if not candidates:
        return MediaResolution(
            None, None, "No medium was identified, so there is nothing to resolve")
    if len(candidates) == 1:
        return MediaResolution(
            candidates[0], MEDIA_RESOLVED_BY_DETECTION,
            f"{candidates[0]} is the only label type the reported medium can be")

    listed = ", ".join(candidates)
    key = media_memory_key(candidates)

    preferred = _preferred_label(settings, key)
    if preferred and preferred in candidates:
        return MediaResolution(
            preferred, MEDIA_RESOLVED_BY_PREFERENCE,
            f"{preferred} is the variant set as preferred for this medium")
    if preferred:
        logger.info("Ignoring a preferred label the loaded medium cannot be",
                    medium=key, preferred=preferred, candidates=list(candidates))

    remembered = _remembered_label(settings, key)
    if remembered and remembered in candidates:
        return MediaResolution(
            remembered, MEDIA_RESOLVED_BY_MEMORY,
            f"{remembered} was the label type last used on this medium")
    if remembered:
        logger.info("Ignoring a remembered label the loaded medium cannot be",
                    medium=key, remembered=remembered, candidates=list(candidates))

    owned = tuple(candidate for candidate in candidates
                  if candidate in owned_media(settings))
    if len(owned) == 1:
        return MediaResolution(
            owned[0], MEDIA_RESOLVED_BY_OWNED,
            f"{owned[0]} is the only one of {listed} in the owned media list")

    if key and key in candidates:
        others = ", ".join(candidate for candidate in candidates if candidate != key)
        return MediaResolution(
            key, MEDIA_RESOLVED_BY_DEFAULT,
            f"{key} is the plain variant of this medium and the documented "
            f"default; {others} has to be chosen deliberately")

    return MediaResolution(
        None, None,
        f"{listed} cannot be told apart, and nothing recorded says which of them "
        f"is loaded")


def plan_media_switch(resolution: MediaResolution, candidates: Tuple[str, ...],
                      matches_label_size: Optional[bool],
                      label_size: Optional[str],
                      settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Say what automatic mode wants done about the loaded medium.

    This decides; it does not act. See :meth:`PrinterService._media_report` for
    why the switch is left to the one component that already writes
    ``label_size``.

    Args:
        resolution: What :func:`resolve_media_label` made of the candidates.
        candidates: The identifiers the report is consistent with.
        matches_label_size: Whether the configured label size is among them.
        label_size: The configured label size, i.e. what a switch moves away
            from.
        settings: Settings carrying ``media_auto_switch``.

    Returns:
        ``enabled``, ``action`` (``none`` / ``switch`` / ``ambiguous``), ``from``,
        ``to`` (the identifier to store, null unless the action is ``switch``)
        and ``reason``.
    """
    enabled = bool((settings or {}).get("media_auto_switch"))

    def plan(action: str, to: Optional[str], reason: str) -> Dict[str, Any]:
        return {"enabled": enabled, "action": action, "from": label_size,
                "to": to, "reason": reason}

    if not enabled:
        # Off is off: no directive at all, whatever is loaded. The resolution is
        # still reported beside this, so a client can offer the switch as a
        # question -- but nothing here asks for it to be made.
        return plan(MEDIA_SWITCH_NONE, None,
                    "Automatic media switching is off, so the label type is "
                    "left as configured")
    if not candidates:
        return plan(MEDIA_SWITCH_NONE, None,
                    "No medium was identified, so there is nothing to switch to")
    if matches_label_size:
        # The configured size is one of the things the loaded roll can be. There
        # is nothing to correct, and moving off it -- to the plain variant of an
        # ambiguous medium, say -- would be the feature overriding a choice the
        # user has already made and is happily printing with.
        return plan(MEDIA_SWITCH_NONE, None,
                    f"The configured label type {label_size} is consistent with "
                    f"the loaded medium")
    if resolution.resolved:
        return plan(MEDIA_SWITCH_APPLY, resolution.label_size, resolution.reason)
    return plan(MEDIA_SWITCH_AMBIGUOUS, None, resolution.reason)


# --------------------------------------------------------------------------- #
# Availability: what a single boolean is allowed to claim.
#
# A printer with its cover open answers IPP perfectly happily. It reports
# printer-state 5 (stopped) and printer-state-reasons "cover-open", and it
# cannot print a thing. A printer with no roll in it is worse: it reports state
# 3 (idle) and only the reason "media-empty-report" gives it away. So neither
# the state nor the reasons alone decide readiness -- both have to be read, and
# the severity suffix IPP appends to a reason ("-report", "-warning", "-error")
# has to come off first, because "media-empty-report" is an empty printer
# whatever severity the firmware felt like attaching to it.
#
# The status response therefore separates two questions that used to share one
# field:
#
#   reachable -- the device answered. This is what the keep-alive loop and the
#       dry-run preflight actually want to know, and it is what ``available``
#       used to mean in practice.
#   available -- nothing known is stopping it from printing. False when a
#       blocking condition is reported, false when the device is not there.
#
# ``state`` spells out which of the two produced the answer, including the case
# neither can: a printer that accepts a TCP connection but does not speak IPP
# is reachable and its readiness is simply unknown.
# --------------------------------------------------------------------------- #

# Reasons (severity suffix stripped) that mean the printer cannot print now.
_BLOCKING_STATE_REASONS = frozenset({
    "cover-open",
    "door-open",
    "media-empty",
    "media-jam",
    "media-needed",
    "input-tray-missing",
    "output-tray-missing",
    "marker-supply-empty",
    "paused",
    "moving-to-paused",
    "shutdown",
    "stopped-partly",
})

_STATE_REASON_SEVERITIES = ("-report", "-warning", "-error")

# Printer states in which nothing will print, even without a specific reason.
_BLOCKING_PRINTER_STATES = frozenset({"stopped"})

# How long a media reading stays usable. The status endpoint is polled by the
# UI every 30 s and the read costs roughly 85 ms on the wire, so the first rule
# is that a status check must not pay for it twice: the media attributes are
# requested in the *same* Get-Printer-Attributes that already fetches the
# printer state, which makes the media free on that path and, better than any
# cache could manage, never stale. The cache covers everything else -- a dry
# run, a second browser tab, a client asking for the media on its own -- so a
# burst of those costs one round-trip rather than one each. It is kept short
# because a roll change is exactly the event this feature exists to notice.
MEDIA_CACHE_TTL_SECONDS = 15.0

# The three states a media reading can be in besides "we read it": each is a
# different kind of not-knowing and the UI has to be able to tell them apart.
MEDIA_DETECTION_UNREACHABLE = "unreachable"   # the printer did not answer
MEDIA_DETECTION_UNSUPPORTED = "unsupported"   # this backend cannot report media
MEDIA_DETECTION_NO_MEDIA = "no-media"         # answered, nothing loaded
MEDIA_DETECTION_UNIDENTIFIED = "unidentified"  # media read, no supported match
MEDIA_DETECTION_OK = "ok"


def _state_reason_list(reasons: Any) -> List[str]:
    """Normalize printer-state-reasons into a list of bare keywords."""
    if reasons is None:
        return []
    if isinstance(reasons, str):
        parts = reasons.replace(";", ",").split(",")
    elif isinstance(reasons, (list, tuple)):
        parts = [str(part) for part in reasons]
    else:
        parts = [str(reasons)]
    return [part.strip().lower() for part in parts if part and part.strip()]


def blocking_state_reasons(printer_state: Optional[str], reasons: Any) -> List[str]:
    """Return the reported conditions that prevent printing, severity stripped.

    Args:
        printer_state: The IPP printer-state, already mapped to a name.
        reasons: printer-state-reasons, as a string, comma-joined string or list.

    Returns:
        The blocking keywords in reported order, e.g. ``["cover-open"]`` or
        ``["media-empty"]``. Empty when nothing blocks. A stopped printer that
        gives no usable reason still yields ``["printer-stopped"]``, because the
        stop itself is the blocking fact.
    """
    blocking: List[str] = []
    for reason in _state_reason_list(reasons):
        if reason in ("none", ""):
            continue
        base = reason
        for suffix in _STATE_REASON_SEVERITIES:
            if base.endswith(suffix):
                base = base[:-len(suffix)]
                break
        if base in _BLOCKING_STATE_REASONS and base not in blocking:
            blocking.append(base)
    if not blocking and str(printer_state or "").lower() in _BLOCKING_PRINTER_STATES:
        blocking.append("printer-stopped")
    return blocking


def get_round_safe_axes(width: int, height: Optional[int] = None) -> Tuple[float, float]:
    """Return the semi-axes content may occupy on a round label.

    Unbled, a round label's drawable area is square and this is a circle: both
    axes come back equal and everything downstream behaves exactly as it always
    has. Bled, it is not square any more -- bleed widens the raster but never
    lengthens it, so a d24 is drawn on 284 x 236 -- and the usable area is the
    **ellipse inscribed in that rectangle**.

    That ellipse is the right answer rather than a convenient one. It is
    entirely inside the die cut: a point on it is at radius
    ``sqrt(a^2 cos^2 t + b^2 sin^2 t) <= a``, and ``a`` is at most the label's
    own radius, so no part of it leaves the circle of paper. And it is the
    largest such region available -- retreating to a circle of the *smaller*
    semi-axis would hand back every dot the bleed just won, which is the whole
    point of the feature.

    The registration sliver is an absolute distance, so the same margin comes
    off both axes; it is sized from the smaller of the two, which keeps the
    unbled square case numerically identical to what it was before.

    Args:
        width: The label's drawable width in pixels.
        height: The label's drawable height in pixels. Defaults to ``width``,
            i.e. the square (circular) case.

    Returns:
        ``(semi_axis_x, semi_axis_y)`` in pixels, both shrunk by the die-cut
        registration margin.
    """
    if height is None:
        height = width
    margin = max(MIN_ROUND_LABEL_MARGIN_PX,
                 int(round(min(width, height) * ROUND_LABEL_MARGIN_RATIO)))
    return max(1.0, width / 2.0 - margin), max(1.0, height / 2.0 - margin)


def get_round_safe_radius(diameter: int) -> float:
    """Return the radius content may occupy on a round label of ``diameter``.

    The circular special case of :func:`get_round_safe_axes`, kept because most
    callers and most media are square and asking for one number is clearer than
    unpacking two identical ones.

    Args:
        diameter: The label's drawable width in pixels.

    Returns:
        The radius in pixels, shrunk by the die-cut registration margin.
    """
    return get_round_safe_axes(diameter)[0]


def get_vertical_alignment(settings: Dict[str, Any]) -> str:
    """Return the requested vertical alignment, defaulting to ``middle``.

    An unrecognised value falls back to the default rather than raising: this is
    a layout hint, and a typo in it is not worth refusing a print over -- the
    same treatment ``orientation`` gets.

    Args:
        settings: Print settings, possibly carrying ``vertical_alignment``.

    Returns:
        One of ``top``, ``middle`` or ``bottom``.
    """
    value = str(settings.get("vertical_alignment", DEFAULT_VERTICAL_ALIGNMENT)).lower()
    return value if value in VERTICAL_ALIGNMENTS else DEFAULT_VERTICAL_ALIGNMENT


def get_vertical_offset(available_height: int, block_height: int,
                        vertical_alignment: str = DEFAULT_VERTICAL_ALIGNMENT,
                        margin: int = LABEL_EDGE_MARGIN_PX) -> int:
    """Return the y of a text block's first line inside ``available_height``.

    The margin is respected in every direction: a block pinned flush against the
    edge of a die-cut label is the part the cut tolerance eats, so ``top`` means
    "as high as the label safely allows", not "at pixel zero". When the block is
    taller than the space it has, the margin wins and the overflow is clipped
    symmetrically, exactly as a centred block always was.

    Args:
        available_height: Height of the canvas the block is placed on.
        block_height: Total height of the stack of lines.
        vertical_alignment: One of ``top``, ``middle`` or ``bottom``.
        margin: Clear space to keep against the top and bottom edges.

    Returns:
        The y coordinate the first line is drawn at.
    """
    if vertical_alignment == "top":
        return margin
    if vertical_alignment == "bottom":
        return max(margin, available_height - block_height - margin)
    return max(margin, (available_height - block_height) // 2)


def get_round_block_top(radius: float, block_height: float, block_width: float,
                        vertical_alignment: str = DEFAULT_VERTICAL_ALIGNMENT,
                        radius_y: Optional[float] = None) -> float:
    """Return the top of a text block, measured from a round label's centre.

    Moving a block up or down a circle costs width, because the chord narrows
    towards the rim. So the travel is bounded by the block's own width: a block
    ``block_width`` wide only has the circle behind it while it stays within
    ``sqrt(radius^2 - (block_width / 2)^2)`` of the centre line. Pushing past
    that would print the ends of the lines onto the backing paper, which is the
    whole failure mode round media exists to avoid.

    A block that already fills that span has nothing left to shift, so it stays
    centred -- and if it overflows, centring is what keeps the overflow
    symmetric instead of dumping all of it on one edge.

    Args:
        radius: Usable semi-axis across the label, in pixels (see
            :func:`get_round_safe_axes`).
        block_height: Total height of the stack of lines, in pixels.
        block_width: Width of the widest line in the stack, in pixels.
        vertical_alignment: One of ``top``, ``middle`` or ``bottom``.
        radius_y: Usable semi-axis along the label. Defaults to ``radius``, the
            circular case. On a bled round label the two differ and the travel
            is bounded by the ellipse instead: a block of half-width ``w`` has
            the label behind it while it stays within
            ``radius_y * sqrt(1 - (w / radius)^2)`` of the centre line.

    Returns:
        The block's top edge as an offset from the label's centre (negative is
        above the centre).
    """
    centred = -block_height / 2.0
    if vertical_alignment not in ("top", "bottom"):
        return centred
    needed = block_width + ROUND_BLOCK_TRAVEL_SLACK_PX
    if radius_y is None or radius_y == radius:
        # The circle, in its original form. Algebraically the ellipse case
        # collapses to this, but not bit for bit, and a placement that shifts by
        # a dot would move every existing round label for no reason.
        half_span = math.sqrt(max(0.0, radius * radius - (needed / 2.0) ** 2))
    else:
        half_span = radius_y * math.sqrt(
            max(0.0, 1.0 - (needed / (2.0 * radius)) ** 2))
    if block_height >= 2 * half_span:
        return centred
    return -half_span if vertical_alignment == "top" else half_span - block_height


def get_round_line_widths(radius: float, line_count: int, line_height: int,
                          block_top: Optional[float] = None,
                          radius_y: Optional[float] = None) -> List[int]:
    """Return the usable width of each line of a stack on a round label.

    A circle is only as wide as its chord at a given height, so a stack of lines
    gets a different budget per line: the lines nearest the centre may use
    nearly the full diameter while those towards the rim are pinched. This is
    what keeps a single centred line big -- an inscribed square would hand it
    only ``diameter / sqrt(2)`` (about 70 %) whatever the line height.

    Because the budget depends on *where* the stack sits, the caller has to pass
    the same ``block_top`` it will draw at (see :func:`get_round_block_top`).
    Computing centred chords and then drawing the text somewhere else is how ink
    ends up outside the die cut.

    Args:
        radius: Usable semi-axis across the label, in pixels (see
            :func:`get_round_safe_axes`).
        line_count: Number of lines in the stack.
        line_height: Height of one line box in pixels.
        block_top: Top of the stack as an offset from the label's centre.
            Defaults to a stack centred on the label.
        radius_y: Usable semi-axis along the label. Defaults to ``radius``, the
            circular case. When the two differ -- a bled round label, which is
            wider than it is long -- the chord is the ellipse's,
            ``2 * radius * sqrt(1 - (offset / radius_y)^2)``, which is what
            hands the extra width to the lines near the centre instead of
            throwing it away.

    Returns:
        One width per line, top to bottom. A line whose box falls entirely
        outside the printable area gets 0.
    """
    widths: List[int] = []
    # Vertical offsets are measured from the centre of the label.
    if block_top is None:
        block_top = -(line_count * line_height) / 2.0
    # See get_round_block_top: the circle keeps its original expression so that
    # existing round layouts are unchanged to the dot.
    circular = radius_y is None or radius_y == radius
    limit = radius if circular else radius_y
    for index in range(line_count):
        top = block_top + index * line_height
        bottom = top + line_height
        # The chord narrows towards the rim, so a line only fits if its *worst*
        # edge fits -- the one furthest from the centre line.
        offset = max(abs(top), abs(bottom))
        if offset >= limit:
            widths.append(0)
        elif circular:
            widths.append(int(2 * math.sqrt(radius * radius - offset * offset)))
        else:
            widths.append(
                int(2 * radius * math.sqrt(1.0 - (offset / radius_y) ** 2)))
    return widths


def _calibration_axis_mm(value: Any, label_size: str, axis: str) -> float:
    """Return one axis of a calibration entry as a usable millimetre value.

    A malformed or out-of-range entry never refuses the print. The offset is a
    correction on top of a label that is otherwise ready to go, so the useful
    failure is "print it the way it always was and say so in the log", not
    "lose the job". Out-of-range values are clamped rather than dropped: they
    reach here only if they bypassed settings validation, and the clamped
    correction is still the closest thing to what was asked for.

    Args:
        value: The raw ``x_mm`` / ``y_mm`` value from the settings.
        label_size: Label identifier the entry belongs to (for the log).
        axis: Field name being read (for the log).

    Returns:
        The offset in millimetres, clamped to +/-``CALIBRATION_LIMIT_MM``.
        0.0 for anything that is not a number.
    """
    if value is None:
        return 0.0
    # bool is an int subclass; "shift by True mm" is not a correction.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("Ignoring non-numeric calibration offset",
                       label_size=label_size, axis=axis, value=repr(value))
        return 0.0
    clamped = max(-CALIBRATION_LIMIT_MM, min(CALIBRATION_LIMIT_MM, float(value)))
    if clamped != float(value):
        logger.warning("Clamped out-of-range calibration offset",
                       label_size=label_size, axis=axis,
                       requested_mm=float(value), applied_mm=clamped)
    return clamped


def get_calibration_offset(settings: Dict[str, Any],
                           label_size: Optional[str] = None) -> Tuple[float, float]:
    """Return the ``(x_mm, y_mm)`` print offset configured for a label.

    The offsets live in a top-level ``calibration`` map keyed by label
    identifier, because what they correct -- die-cut registration tolerance and
    the raster offset of the loaded media -- is a property of the media, not of
    the job::

        {"calibration": {"d24": {"x_mm": -0.5, "y_mm": 1.0}}}

    ``x_mm`` positive moves the printed content right on the tape, ``y_mm``
    positive moves it down (increasing pixel y, i.e. later in the feed
    direction). A missing map, a missing entry and an entry of zeros all mean
    the same thing: print exactly as before.

    The two axes are honoured by different mechanisms -- see
    :func:`plan_raster_placement` for sideways and
    :meth:`PrinterService._shift_within_canvas` for the feed -- but both take
    their value from here, in the same units and with the same signs.

    Args:
        settings: Resolved print settings, possibly carrying ``calibration``.
        label_size: Label identifier to look up. Defaults to the one in
            ``settings``.

    Returns:
        The offset as ``(x_mm, y_mm)``, both clamped to the supported range.
    """
    calibration = settings.get("calibration")
    if not calibration:
        return (0.0, 0.0)
    if not isinstance(calibration, dict):
        logger.warning("Ignoring malformed calibration settings",
                       calibration_type=str(type(calibration)))
        return (0.0, 0.0)

    key = str(label_size if label_size is not None else settings.get("label_size") or "")
    offset = calibration.get(key)
    if offset is None:
        return (0.0, 0.0)
    if not isinstance(offset, dict):
        logger.warning("Ignoring malformed calibration entry",
                       label_size=key, entry_type=str(type(offset)))
        return (0.0, 0.0)

    return (
        _calibration_axis_mm(offset.get("x_mm"), key, "x_mm"),
        _calibration_axis_mm(offset.get("y_mm"), key, "y_mm"),
    )


def get_calibration_scale(settings: Dict[str, Any],
                          label_size: Optional[str] = None) -> float:
    """Return the size correction configured for a label.

    The multiplier lives beside the offsets, in the same per-label entry::

        {"calibration": {"d24": {"x_mm": -0.5, "scale": 0.98}}}

    ``0.98`` prints the content 2 % smaller. It corrects a printer that lays
    ink down slightly larger or smaller than it was asked to, so like the
    offsets it applies to prints only and never to a preview: the preview is
    the design the user means to have, and the correction exists to make the
    paper match it.

    A malformed value never refuses the print -- the label itself is ready to
    go -- and an out-of-range one is clamped rather than dropped, for the same
    reason :func:`_calibration_axis_mm` clamps: it can only get here by
    bypassing settings validation, and the clamped value is the closest thing
    to what was asked for.

    Args:
        settings: Resolved print settings, possibly carrying ``calibration``.
        label_size: Label identifier to look up. Defaults to the one in
            ``settings``.

    Returns:
        The multiplier, clamped to the supported range. 1.0 -- print as
        rendered -- for a missing map, entry or field.
    """
    calibration = settings.get("calibration")
    if not isinstance(calibration, dict) or not calibration:
        return DEFAULT_CALIBRATION_SCALE

    key = str(label_size if label_size is not None else settings.get("label_size") or "")
    entry = calibration.get(key)
    if not isinstance(entry, dict):
        return DEFAULT_CALIBRATION_SCALE

    value = entry.get("scale")
    if value is None:
        return DEFAULT_CALIBRATION_SCALE
    # bool is an int subclass, and "print it True times as large" is not a
    # correction anybody meant to make.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("Ignoring non-numeric calibration scale",
                       label_size=key, value=repr(value))
        return DEFAULT_CALIBRATION_SCALE

    clamped = max(CALIBRATION_SCALE_MIN, min(CALIBRATION_SCALE_MAX, float(value)))
    if clamped != float(value):
        logger.warning("Clamped out-of-range calibration scale",
                       label_size=key, requested=float(value), applied=clamped)
    return clamped


def format_calibration_offset(x_mm: float, y_mm: float,
                              scale: float = DEFAULT_CALIBRATION_SCALE) -> str:
    """Describe a calibration offset for printing on the target itself.

    The direction is carried by a letter -- ``R``/``L``/``U``/``D`` -- and never
    by a sign. A label states its own offset in type barely a millimetre tall,
    and a thermal head printing a 1-bit raster is not obliged to keep every
    stroke of a glyph: at some sizes a ``+`` loses its upright and comes out as
    a bare bar, at which point the label says the exact opposite of the truth
    and the user calibrates in the wrong direction. ``R``, ``L``, ``U`` and
    ``D`` have no hairline to lose, they are shorter than a signed pair (so
    they fit smaller media), and they are the same vocabulary the web UI uses,
    so screen and paper agree.

    An axis that does not move is left out entirely -- zero has no direction --
    and an offset that moves nothing at all is named as such. A size correction
    is named as a percentage, and only when there is one: it is not swept, but
    two targets printed at different sizes are otherwise indistinguishable, and
    "%" cannot degrade into its own opposite the way a sign can.

    Args:
        x_mm: Sideways offset in millimetres; positive is right. This should be
            the offset that was *applied*, not the one requested: the label is
            held up precisely to judge what the printer did.
        y_mm: Feed-direction offset in millimetres; positive is down.
        scale: Size correction applied to the label, 1.0 for none.

    Returns:
        A short description such as ``"R0.5 D1.5"``, ``"L2.0 98%"`` or
        ``"centred"``.
    """
    # Rounded first: an offset below half a dot is not printable and must not
    # acquire a direction it cannot express.
    x_rounded = round(x_mm, 1)
    y_rounded = round(y_mm, 1)
    parts = []
    if x_rounded:
        parts.append(f"{'R' if x_rounded > 0 else 'L'}{abs(x_rounded):.1f}")
    if y_rounded:
        parts.append(f"{'D' if y_rounded > 0 else 'U'}{abs(y_rounded):.1f}")
    if round(scale, 4) != DEFAULT_CALIBRATION_SCALE:
        parts.append(f"{scale * 100:.0f}%")
    return " ".join(parts) if parts else "centred"


def calibration_offset_px(x_mm: float, y_mm: float) -> Tuple[int, int]:
    """Convert a millimetre offset into whole printer dots.

    Args:
        x_mm: Horizontal offset in millimetres (positive moves right).
        y_mm: Vertical offset in millimetres (positive moves down).

    Returns:
        The offset in pixels of the rendered raster, rounded to whole dots --
        the printer cannot place ink at a fraction of one.
    """
    return (int(round(x_mm * DOTS_PER_MM)), int(round(y_mm * DOTS_PER_MM)))


# Serializes the temporary edit of brother_ql's media table (see
# :func:`plan_raster_placement` for why the table has to be edited at all).
#
# The table is process-global, so two conversions overlapping in time would see
# each other's edit and one of them would print at the other's offset. The
# print queue's single worker happens to be the only thread that converts
# today, but it shares the process with the keep-alive thread and with every
# request thread, and "there is exactly one worker" is a property of another
# module that this one should not quietly depend on.
#
# Deliberately *not* PrinterService._io_lock. That lock guards the printer's
# port 9100 -- a device that accepts one connection at a time -- and the
# keep-alive heartbeat acquires it non-blockingly, so a busy port costs a beat
# rather than queueing one up. What is guarded here is a dictionary inside a
# third-party module, held across a CPU-bound rasterisation that touches no
# port at all: folding the two together would make the heartbeat skip while an
# image is being converted, and would hold the port open for the conversion as
# well as for the write. It is module-level rather than an attribute because
# the table it protects is module-level too -- a second PrinterService instance
# (the tests build their own) must contend for the same lock.
_LABEL_SPEC_LOCK = threading.Lock()


class RasterPlacement(NamedTuple):
    """Where a label's raster is placed inside the printer's device row.

    Attributes:
        requested_dots: Sideways travel asked for, in dots; positive is right.
        applied_dots: Travel actually available and applied, in dots. Differs
            from ``requested_dots`` only when the head runs out of room.
        right_margin_dots: The value to publish in the media table for the
            duration of the conversion, i.e. the label's own
            ``right_margin_dots`` less the per-side bleed (which keeps a wider
            raster centred) and less ``applied_dots`` (which then moves it).
        base_x_offset: Where the raster would sit with no calibration -- with
            the bleed already in it, since the bleed is not an offset and a
            bled label is meant to sit exactly where an unbled one did.
        max_x_offset: Largest paste position that still fits the device row.
    """

    requested_dots: int
    applied_dots: int
    right_margin_dots: int
    base_x_offset: int
    max_x_offset: int

    @property
    def was_clamped(self) -> bool:
        """Whether the printer could not deliver the full requested travel."""
        return self.applied_dots != self.requested_dots


def plan_raster_placement(device_pixel_width: int, printer_model: str, label_size: str,
                          dx_dots: int, warn: bool = True,
                          bleed_dots: int = 0) -> Optional[RasterPlacement]:
    """Work out how to move a whole label sideways on the tape.

    ``convert()`` pastes the finished label into the printer's full device row
    at::

        x_offset = device_pixel_width - label_width - right_margin_dots

    so the paste position -- not the content inside the canvas -- is the lever
    that moves ink sideways on the physical label, and the only one the raster
    language offers. Moving it relocates every dot of the label together and
    loses none of them, where translating the content inside a fixed canvas
    throws away whatever crosses the edge. ``right_margin_dots`` is read from
    brother_ql's global media table rather than taken as an argument, which is
    why applying this needs :func:`placed_raster` rather than a keyword.

    Sign: the image is pasted unflipped, so its column 0 lands at ``x_offset``
    and its content keeps its order across the row. Increasing ``x_offset``
    therefore moves the label in the same direction that increasing a pixel's
    x moves it -- which is rightwards on the printed label. Since ``x_offset``
    falls as ``right_margin_dots`` rises, moving ink *right* by N dots means
    publishing a right margin N dots *smaller*.

    Travel is finite: the paste position has to stay within
    ``[0, device_pixel_width - label_width]`` or the head clips the raster,
    which is the very loss this mechanism exists to avoid. A label already
    sitting close to one end of the head therefore has little room that way --
    on a QL-820NWB a d24 label starts 442 dots from the left of the row but
    only 42 dots (3.5 mm) from the right. A request beyond that is clamped, and
    the shortfall is logged: the printer cannot put ink where it has no head.

    Args:
        device_pixel_width: Width of the printer's device row in dots, i.e.
            ``BrotherQLRaster.get_pixel_width()``.
        printer_model: Model name, needed for the model's own margin addition.
        label_size: Label identifier, e.g. "d24".
        dx_dots: Requested sideways travel in dots; positive moves right.
        warn: Whether to log a shortfall. False when the caller is only asking
            what *would* happen -- reporting the travel to an API client or
            captioning a target -- so that a single print logs a single
            warning, at the point the offset is really applied.
        bleed_dots: Per-side growth of the raster from ``bleed_mm``, in dots
            (see :func:`get_label_bleed`). The two corrections compose here
            because they pull on the same string: bleed widens the label by
            ``2 * bleed_dots`` and spends ``bleed_dots`` of the right margin
            keeping it centred, then the calibration offset spends more of what
            is left moving it. A wider raster has *less* room to move -- the
            travel bounds are the head minus the label -- so full bleed on d24
            cuts the rightward travel from 42 dots to 18.

    Returns:
        The placement to apply, or None when this medium offers no travel at
        all (an identifier brother_ql does not know, or tape as wide as the
        head, where ``convert()`` does not paste in the first place). None does
        not mean the bleed is off: :func:`placed_raster` publishes the bleed
        either way, because a medium with no room to move sideways still has
        its non-printable margin to reach into.
    """
    specs = label_type_specs.get(label_size)
    if not specs:
        if warn:
            logger.warning("No media entry to place the raster in; printing "
                           "uncalibrated sideways", label_size=str(label_size))
        return None

    label_width = specs["dots_printable"][0] + 2 * bleed_dots
    max_x_offset = device_pixel_width - label_width
    if max_x_offset <= 0:
        # The label fills the head (and for continuous media convert() skips
        # the paste entirely). There is nowhere to move it.
        if warn:
            logger.warning("Medium leaves no sideways travel; printing uncalibrated "
                           "sideways", label_size=str(label_size),
                           label_width=label_width,
                           device_pixel_width=device_pixel_width)
        return None

    # Half the growth has to come out of the right margin or the label does not
    # stay put: convert() pastes at ``device - width - margin``, so widening the
    # raster by 2N while leaving the margin alone drags the whole label N dots
    # to the left. Taking N off the margin puts the centre back exactly where it
    # was, which is the property the tests measure.
    margin = (specs["right_margin_dots"] - bleed_dots
              + right_margin_addition.get(printer_model, 0))
    base_x_offset = device_pixel_width - label_width - margin
    applied_x_offset = max(0, min(max_x_offset, base_x_offset + dx_dots))
    applied_dots = applied_x_offset - base_x_offset

    placement = RasterPlacement(
        requested_dots=dx_dots,
        applied_dots=applied_dots,
        # The table holds the label's own margin; convert() adds the model's
        # addition back on top, so only the label's share is rewritten here.
        right_margin_dots=specs["right_margin_dots"] - bleed_dots - applied_dots,
        base_x_offset=base_x_offset,
        max_x_offset=max_x_offset,
    )
    if placement.was_clamped and warn:
        logger.warning(
            "Calibration offset exceeds the printer's sideways travel",
            label_size=str(label_size), printer_model=str(printer_model),
            requested_px=dx_dots, applied_px=applied_dots,
            requested_mm=round(dx_dots / DOTS_PER_MM, 2),
            applied_mm=round(applied_dots / DOTS_PER_MM, 2),
            travel_px={"left": -base_x_offset, "right": max_x_offset - base_x_offset},
        )
    return placement


class AppliedCalibration(NamedTuple):
    """What a printer can really do with a requested calibration offset.

    Attributes:
        x_mm: Sideways offset the printer will actually apply.
        y_mm: Feed offset, which is never limited by the head.
        requested_x_mm: What was asked for.
        was_clamped: Whether the two differ.
        travel_mm: ``(min, max)`` sideways travel this medium allows on this
            printer, or None when it could not be worked out (unknown model or
            identifier, or media as wide as the head).
    """

    x_mm: float
    y_mm: float
    requested_x_mm: float
    was_clamped: bool
    travel_mm: Optional[Tuple[float, float]]


def applied_calibration_offset(settings: Dict[str, Any],
                               label_size: Optional[str] = None) -> AppliedCalibration:
    """Resolve a calibration offset into what the printer can actually deliver.

    The stored offset is a request. Sideways it is bounded by how much print
    head there is beside the loaded media, which depends on the model as well
    as on the medium, so the number a user reads back -- on the label, in an
    API response, in a preview -- has to be the applied one. A target captioned
    with a correction the printer could not make is the same defect as a
    caption whose sign has been eaten by the thermal head: the label is being
    held up precisely to judge what the printer did.

    Nothing here warns. The print path logs the shortfall once, where the
    offset is really applied; this is the reporting road to the same numbers.

    Args:
        settings: Print settings carrying ``printer_model`` and ``calibration``.
        label_size: Label identifier. Defaults to the one in ``settings``.

    Returns:
        An :class:`AppliedCalibration`. With an unknown model or medium the
        requested offset is returned unchanged and ``travel_mm`` is None: not
        knowing the limit is no reason to invent one.
    """
    key = str(label_size if label_size is not None else settings.get("label_size") or "")
    x_mm, y_mm = get_calibration_offset(settings, key)
    printer_model = str(settings.get("printer_model") or "")

    placement = None
    try:
        device_pixel_width = BrotherQLRaster(printer_model).get_pixel_width()
    except Exception:  # noqa: BLE001 - an unknown model simply has no known head
        logger.debug("Cannot resolve the print head width; reporting the offset "
                     "as requested", printer_model=printer_model)
    else:
        placement = plan_raster_placement(
            device_pixel_width, printer_model, key,
            calibration_offset_px(x_mm, y_mm)[0], warn=False,
            # Bleed changes the answer: a wider raster has less room left to
            # move, so the travel this reports -- to an API client, to a
            # target's caption -- has to be the travel the bled label really
            # has, not the travel it would have had unbled.
            bleed_dots=get_label_bleed(settings, key).dots)

    if placement is None:
        return AppliedCalibration(x_mm, y_mm, x_mm, False, None)

    travel = (round(-placement.base_x_offset / DOTS_PER_MM, 2),
              round((placement.max_x_offset - placement.base_x_offset) / DOTS_PER_MM, 2))
    if not placement.was_clamped:
        return AppliedCalibration(x_mm, y_mm, x_mm, False, travel)
    # Rounded to the same two decimals the API and the settings speak in, and
    # deliberately re-derived from dots: fed back in, it converts to exactly
    # the dots that were applied.
    applied_x = round(placement.applied_dots / DOTS_PER_MM, 2)
    return AppliedCalibration(applied_x, y_mm, x_mm, True, travel)


@contextmanager
def placed_raster(qlr: BrotherQLRaster, label_size: str, dx_dots: int,
                  bleed: Optional[LabelBleed] = None
                  ) -> Iterator[Optional[RasterPlacement]]:
    """Publish a raster placement and size for the duration of one conversion.

    brother_ql reads both the paste position and the size it will accept out of
    ``label_type_specs``, a module global, so the only way to influence either
    is to edit that table -- there is no keyword argument, and rebuilding
    ``convert()`` locally to avoid the edit would fork a moving target for the
    sake of two integers.

    Two entries are rewritten and they are two halves of one act, which is why
    they share this one mechanism rather than growing a second:

    * ``dots_printable`` is the size ``convert()`` demands of a die-cut image
      and the width it resizes continuous tape to. Raising its *width* by twice
      the per-side bleed is what lets the wider raster through at all -- without
      it a bled die-cut label is rejected with "Bad image dimensions" and a
      bled continuous one is silently scaled back down. The length is passed
      through untouched: ``convert()`` turns it straight into the raster line
      count, which is the distance the media advances while printing, and
      changing that is what moved the cutter off the die-cut gap.
    * ``right_margin_dots`` is where the raster is pasted in the device row. It
      absorbs the bleed (half the growth, so the label stays centred) and then
      the sideways calibration offset (which moves it deliberately).

    The edit is made as small and as short-lived as it can be. The label's spec
    is *replaced* by a modified copy rather than mutated in place, so a reader
    that is not holding the lock still sees one internally consistent dict; the
    original object is put back in a ``finally``, so a conversion that raises
    leaves the table exactly as it found it; and the whole window is serialized
    on :data:`_LABEL_SPEC_LOCK`, so no two conversions in this process can see
    each other's edit. A print with neither an offset nor a bleed touches
    nothing at all and never so much as takes the lock.

    The placement is planned *inside* the lock, not before it: the plan is
    arithmetic on the very entry it is about to replace, so planning it outside
    would read whatever another thread had temporarily published there and
    correct a label by the difference between two offsets.

    Args:
        qlr: The rasterizer the conversion will use; supplies the model and the
            device row width.
        label_size: Label identifier the conversion is for.
        dx_dots: Requested sideways travel in dots; positive moves right.
        bleed: The bleed in force, from :func:`get_label_bleed`. None or
            :data:`NO_BLEED` publishes the medium's own printable size. It only
            ever widens; the published length is never touched.

    Yields:
        The :class:`RasterPlacement` in force, or None when the medium offers
        no sideways travel. A None placement says nothing about the bleed,
        which is published regardless -- 62 mm tape on a 720-dot head fills the
        row exactly once bled, so it has bleed and no travel at the same time.
    """
    bleed_dots = bleed.dots if bleed else 0
    if not dx_dots and not bleed_dots:
        yield None
        return

    with _LABEL_SPEC_LOCK:
        placement = plan_raster_placement(
            qlr.get_pixel_width(), qlr.model, label_size, dx_dots,
            bleed_dots=bleed_dots)

        original = label_type_specs.get(label_size)
        changes: Dict[str, Any] = {}
        if original is not None:
            if bleed_dots:
                width, height = original["dots_printable"]
                # Width only. The length is handed back exactly as it came, so
                # a bled job emits precisely as many raster lines as an unbled
                # one and the printer's per-page feed is unchanged.
                changes["dots_printable"] = (width + 2 * bleed_dots, height)
            if placement is not None:
                changes["right_margin_dots"] = placement.right_margin_dots
            elif bleed_dots:
                # No travel to plan, but the label still has to stay centred
                # after being widened.
                changes["right_margin_dots"] = original["right_margin_dots"] - bleed_dots

        published = None
        if changes and any(original[key] != value for key, value in changes.items()):
            published = original
            label_type_specs[label_size] = dict(original, **changes)
        try:
            yield placement
        finally:
            if published is not None:
                label_type_specs[label_size] = published


def _flattened_onto_white(img: "Image.Image") -> "Image.Image":
    """Drop transparency the way ``convert()`` does, onto white.

    A transparent PNG pasted straight onto a canvas takes its background with
    it and prints solid black, so alpha and palette images are flattened before
    any calibration touches them.
    """
    if not (img.mode.endswith("A") or img.mode == "P"):
        return img
    flattened = Image.new("RGB", img.size, (255, 255, 255))
    with_alpha = img.convert("RGBA")
    flattened.paste(with_alpha, mask=with_alpha.split()[-1])
    return flattened


def _clipping_losses(img: "Image.Image", content: "Image.Image",
                     dx: int, dy: int) -> Dict[str, int]:
    """Return how much ink, per side, falls outside the canvas.

    The bounding box of everything non-white is the honest measure: how far the
    *content* reaches, not how big the canvas it was drawn on is.

    Args:
        img: The canvas the content is being placed on.
        content: The content being placed (the same image, or a resized copy).
        dx: Where the content's left edge lands on the canvas.
        dy: Where the content's top edge lands on the canvas.

    Returns:
        Pixels lost per side, all zero when nothing is lost.
    """
    ink = ImageOps.invert(content.convert("L")).getbbox()
    if not ink:
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}
    return {
        "left": max(0, -(ink[0] + dx)),
        "top": max(0, -(ink[1] + dy)),
        "right": max(0, (ink[2] + dx) - img.width),
        "bottom": max(0, (ink[3] + dy) - img.height),
    }


def _ink_outside_die_cut_px(img: "Image.Image") -> int:
    """Count black pixels outside the ellipse inscribed in the canvas.

    Round media is die-cut to a circle, and the raster is built on a rectangle
    that touches that circle across the tape and stops short of it along the
    feed, so ink can leave the label without leaving the canvas -- it lands on
    the backing paper and is simply not on the label the user peels off. The
    inscribed ellipse is the right region either way: on an unbled (square)
    canvas it is the circle, and on a bled one it is the largest area that is
    both on the canvas and inside the die cut. Done with whole-image operations
    rather than a Python loop, because this runs on the print path.

    Only ever call this for media the catalogue calls round. A rectangular
    canvas is not the same thing as a round label: 23x23 is a square
    *rectangular* label whose corners print perfectly well, and treating it as a
    circle would report a quarter of it as lost.

    Args:
        img: The label image, from round media.

    Returns:
        The number of black pixels outside the die cut.
    """
    if img.width < 2 or img.height < 2:
        return 0
    ink = ImageOps.invert(img.convert("L"))
    die_cut = Image.new("L", img.size, 0)
    ImageDraw.Draw(die_cut).ellipse((0, 0, img.width - 1, img.height - 1), fill=255)
    # Ink minus the die cut: everything inside it is subtracted to black, so
    # what stays bright is exactly the ink that missed the label.
    outside = ImageChops.subtract(ink, die_cut)
    return sum(outside.histogram()[128:])


def _sum_of_fields(first: "Image.Image", second: "Image.Image") -> "Image.Image":
    """Add two floating-point images dot by dot.

    ``ImageChops`` only accepts 8-bit images, and 8 bits is not enough to carry
    a squared radius without the rounding showing up as whole missing dots on
    the label, so the sum goes through ``ImageMath``. Pillow 10.3 replaced its
    string form with a callable one; both are accepted here because the app is
    also run from source against whatever Pillow the host already has.
    """
    if hasattr(ImageMath, "lambda_eval"):
        return ImageMath.lambda_eval(lambda args: args["a"] + args["b"], a=first, b=second)
    return ImageMath.eval("a + b", a=first, b=second)


def _probe_extents(size: int, factor: int) -> List[float]:
    """How far each probe column (or row) reaches from the image's centre.

    A dot of ink is a filled square, not a point: column ``x`` covers
    ``[x, x + 1)``, so what has to stay inside the die cut is the far edge of
    the dot rather than its middle. Taking the outer edge is also what keeps a
    fully inked rectangle on exactly the historical half-diagonal fit -- its
    corner dot reaches ``width / 2``, which is the number that rule uses.

    When the ink mask has been max-pooled, one probe column stands for
    ``factor`` source columns and inherits the farthest edge of the whole block.

    Args:
        size: The source image's width (or height) in pixels.
        factor: The max-pooling factor the ink mask was reduced by, 1 for none.

    Returns:
        One outer distance per probe column, in source pixels; the list is as
        long as ``Image.reduce(factor)`` makes the mask.
    """
    centre = size / 2.0
    extents: List[float] = []
    for index in range(-(-size // factor)):
        first = index * factor
        last = min(size, first + factor) - 1
        extents.append(max(centre - first, last + 1 - centre))
    return extents


def _largest_ink_scale(ink: "Image.Image", semi_x: float, semi_y: float,
                   source_size: Tuple[int, int], factor: int = 1) -> Optional[float]:
    """Largest centre-scale that keeps every inked dot inside the safe ellipse.

    There is a closed form, so no search is needed. Scaling by ``k`` about the
    centre moves a dot at offset ``(dx, dy)`` to ``(k dx, k dy)``, which is
    inside the ellipse with semi-axes ``(a, b)`` exactly when
    ``hypot(k dx / a, k dy / b) <= 1``. The binding dot is therefore the one
    with the largest ``hypot(dx / a, dy / b)`` and ``k`` is its reciprocal.

    That expression separates: ``(dx / a)^2`` depends only on the column and
    ``(dy / b)^2`` only on the row, so the field is built by stretching two
    one-dimensional ramps and adding them. Everything after that is a
    whole-image operation -- no Python ever touches an individual dot -- which
    is what makes this affordable on the print path.

    Args:
        ink: Max-pooled ink mask, non-zero where the printer will lay ink down.
        semi_x: Semi-axis of the safe ellipse across the tape, in pixels.
        semi_y: Semi-axis of the safe ellipse along the feed, in pixels.
        source_size: The unpooled image's ``(width, height)``, which is the
            coordinate system the returned scale applies to.
        factor: The max-pooling factor ``ink`` was reduced by.

    Returns:
        The scale, or ``None`` when the image carries no ink at all and there is
        consequently nothing to fit.
    """
    width, height = ink.size
    columns = Image.new("F", (width, 1))
    columns.putdata([(extent / semi_x) ** 2 for extent in _probe_extents(source_size[0], factor)])
    rows = Image.new("F", (1, height))
    rows.putdata([(extent / semi_y) ** 2 for extent in _probe_extents(source_size[1], factor)])

    field = _sum_of_fields(
        columns.resize((width, height), Image.Resampling.NEAREST),
        rows.resize((width, height), Image.Resampling.NEAREST),
    )
    # Blank the dots that print white. Every value in the field is strictly
    # positive, so a maximum of zero means the whole image was blank.
    field.paste(0.0, ink.point(lambda level: 0 if level else 255, mode="1"))
    worst = field.getextrema()[1]
    if worst <= 0.0:
        return None
    return 1.0 / math.sqrt(worst)


class _CutAtEndRaster(BrotherQLRaster):
    """BrotherQLRaster that forces "auto-cut every N labels".

    brother_ql's ``convert(cut=True)`` always calls ``add_cut_every(1)`` (cut
    after every label). To cut only once at the end of a multi-label job we
    override ``add_cut_every`` so it uses a fixed N (the total label count),
    making the printer cut a single time after the last label.
    """

    def __init__(self, model, cut_every_n):
        super().__init__(model)
        self._cut_every_n = max(1, int(cut_every_n))

    def add_cut_every(self, n=1):
        return super().add_cut_every(self._cut_every_n)


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
        # Serializes raw access to the printer's port 9100 so the keep-alive
        # heartbeat never collides with an in-progress print job (the printer
        # accepts only one 9100 connection at a time).
        self._io_lock = threading.Lock()
        # How many times this process has begun writing a raster to a printer.
        #
        # It exists so a caller that wants to retry a failed print can tell the
        # two cases apart: a job that never reached the wire (the printer
        # refused the connection, or was not there) can be tried again for
        # nothing, while a job that did reach it may have printed part of what
        # was asked -- a page, a copy, a label -- and repeating it prints that
        # part twice. Counted at the *start* of the write, deliberately: a write
        # that raised halfway through has still put bytes on the wire.
        #
        # Incremented under ``_io_lock`` next to the write itself, so it counts
        # exactly what went to the printer and not what merely got rendered.
        self._write_attempts = 0
        # Timestamp of the last print attempt. The "timed" keep-alive mode keeps
        # the printer awake for a configurable window after this moment, then
        # pauses until the next print. Initialised to now so enabling keep-alive
        # gives one window straight away.
        #
        # That initial value is a *fallback*, not a print, and the two must never
        # be confused: a status display reading it as "last print" would name a
        # print that never happened. ``_printed_since_start`` records which of
        # the two the timestamp is, and :meth:`last_print_origin` hands both out
        # together so no caller can read one without the other.
        self._last_print_at = time.time()
        self._printed_since_start = False
        # Loaded media, per printer URI, with the timestamp it was read at. See
        # MEDIA_CACHE_TTL_SECONDS for why this exists.
        self._media_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._media_cache_lock = threading.Lock()
        # Single source of truth for the upload folder. Precedence:
        #   1. explicit constructor argument
        #   2. UPLOAD_FOLDER environment variable (lets operators relocate or
        #      persist the otherwise-ephemeral scratch directory)
        #   3. the historical code-relative default (unchanged behaviour when no
        #      env var is set, so there is no regression).
        self.upload_folder = (
            upload_folder
            or os.environ.get("UPLOAD_FOLDER")
            or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "uploads",
            )
        )
        
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
    
    def _cleanup_temp_files(self, paths: List[str]) -> None:
        """
        Remove intermediate render/resize artifacts produced by this service
        (e.g. ``text_label_*``, ``resized_*``, ``rotated_*``, ``qrcode_*``).

        This plugs a disk leak: those PNGs accumulated in the uploads folder
        forever. We only ever pass paths generated *internally* here -- the
        original uploaded image is never included, so we don't double-delete a
        file the image controller may own.

        Failures are logged and swallowed so cleanup can never break a print.
        """
        for path in paths:
            if not path:
                continue
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug("Removed temporary print artifact", path=path)
            except OSError as e:
                logger.warning("Failed to remove temporary print artifact",
                               path=path, error=str(e))

    def get_printers(self) -> List[Dict[str, Any]]:
        """
        Get list of configured printers.
        
        Returns:
            List of printer configurations.
        """
        settings = settings_service.get_settings()
        return settings.get("printers", [])
    
    def _store_media(self, printer_uri: str, media: Dict[str, Any]) -> Dict[str, Any]:
        """Remember a media reading for ``printer_uri``."""
        with self._media_cache_lock:
            self._media_cache[printer_uri] = (time.monotonic(), media)
        return media

    def _forget_media(self, printer_uri: str) -> None:
        """Drop a cached reading, e.g. because the printer stopped answering.

        Serving remembered media for a printer that is no longer there would be
        precisely the kind of confident wrong answer this feature exists to
        avoid.
        """
        with self._media_cache_lock:
            self._media_cache.pop(printer_uri, None)

    def _cached_media(self, printer_uri: str) -> Optional[Dict[str, Any]]:
        with self._media_cache_lock:
            entry = self._media_cache.get(printer_uri)
        if entry and (time.monotonic() - entry[0]) < MEDIA_CACHE_TTL_SECONDS:
            return entry[1]
        return None

    def get_loaded_media(self, printer_uri: str,
                         ipp_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return the media currently loaded in a printer.

        Args:
            printer_uri: The printer to ask about; also the cache key.
            ipp_result: An already-fetched :func:`get_printer_attributes`
                result. When given, its media is used and cached -- this is how
                the status check gets the media without a second round-trip.
                When omitted, a fresh reading is taken unless a recent one is
                still cached (see :data:`MEDIA_CACHE_TTL_SECONDS`).

        Returns:
            The media dict described by
            :data:`src.services.ipp_client.EMPTY_MEDIA`; all-None when the
            printer could not be reached, has nothing loaded, or is not a
            network printer.
        """
        if ipp_result is not None:
            if not ipp_result.get("reachable"):
                self._forget_media(printer_uri)
                return dict(EMPTY_MEDIA)
            return self._store_media(printer_uri, ipp_result.get("media") or dict(EMPTY_MEDIA))

        cached = self._cached_media(printer_uri)
        if cached is not None:
            return cached

        if not printer_uri or guess_backend(printer_uri) != "network":
            # USB and kernel-backed printers offer no status channel at all.
            return dict(EMPTY_MEDIA)
        try:
            validate_printer_uri(printer_uri)
        except ValueError:
            return dict(EMPTY_MEDIA)
        media = get_media_ready(self._extract_ip_from_uri(printer_uri), port=self._get_ipp_port())
        return self._store_media(printer_uri, media)

    def _media_report(self, media: Optional[Dict[str, Any]],
                      label_size: Optional[str],
                      unavailable: Optional[str] = None,
                      settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build the ``media`` section of a status response.

        Carries the medium as reported, the label identifiers it could be, and
        whether ``label_size`` is one of them. ``matches_label_size`` is None --
        never False -- whenever nothing was identified, so "we do not know" can
        never be read as "they disagree".

        On top of that it carries ``resolution`` -- which single identifier the
        documented order (preference, memory, owned media, plain-variant default)
        comes down to and which step got there -- and ``auto_switch``, what
        automatic mode wants done about it. The two are separate on purpose: the resolution is
        reported whether or not automatic mode is on, because a client can use it
        to *offer* the switch, while ``auto_switch`` is the only field that ever
        asks for one.

        **The server reports the switch; it does not perform it.** ``label_size``
        already has exactly one writer -- the client, through PUT /settings, on
        every change -- and a second one here would be racing it from a poll the
        UI makes every 30 seconds, in however many tabs are open. update_settings
        is a read-modify-write of a whole JSON file, so a status check that wrote
        label_size could land between a user's edit and its save and quietly undo
        it. Beyond the race there is the plainer objection: POST /printers/status
        is a read. An orchestrator asking whether the printer is up should not
        find that the question changed the configuration. So the resolution
        travels back with the status, and the component that already owns the
        value applies it.
        """
        if unavailable:
            report = dict(EMPTY_MEDIA)
            identification = LabelIdentification((), {
                MEDIA_DETECTION_UNREACHABLE:
                    "Printer could not be reached, so the loaded media is unknown",
                MEDIA_DETECTION_UNSUPPORTED:
                    "Media detection needs a network (tcp://) printer; this "
                    "backend cannot report what is loaded",
            }.get(unavailable, "Loaded media is unknown"))
            detection = unavailable
        elif not media or media.get("width_mm") is None:
            report = dict(EMPTY_MEDIA)
            identification = LabelIdentification((), "The printer reports no media loaded")
            detection = MEDIA_DETECTION_NO_MEDIA
        else:
            report = {key: media.get(key) for key in EMPTY_MEDIA}
            identification = identify_label_candidates(media)
            detection = (MEDIA_DETECTION_OK if identification.resolved
                         else MEDIA_DETECTION_UNIDENTIFIED)

        matches = identification.matches(label_size)
        resolution = resolve_media_label(identification.candidates, settings)
        report.update({
            "detected": detection == MEDIA_DETECTION_OK,
            "detection": detection,
            "candidates": list(identification.candidates),
            "ambiguous": identification.ambiguous,
            "reason": identification.reason,
            "label_size": label_size,
            "matches_label_size": matches,
            "resolution": {
                "label_size": resolution.label_size,
                "resolved_by": resolution.resolved_by,
                "reason": resolution.reason,
            },
            "auto_switch": plan_media_switch(resolution, identification.candidates,
                                             matches, label_size, settings),
        })
        return report

    def _status_response(self, reachable: bool, state: str, status: str,
                         details: Dict[str, Any],
                         media: Optional[Dict[str, Any]] = None,
                         media_unavailable: Optional[str] = None,
                         blocking_reasons: Optional[List[str]] = None,
                         label_size: Optional[str] = None,
                         settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Assemble a status response with honest availability.

        ``available`` means "nothing known is stopping this printer from
        printing": it is False when the device does not answer and False when it
        reports a blocking condition. ``reachable`` carries the reachability
        signal on its own, and ``state`` says which question was actually
        answered -- including ``unknown``, for a printer that accepts a TCP
        connection but tells us nothing more.
        """
        blocking = list(blocking_reasons or [])
        return {
            "available": bool(reachable) and state != "blocked",
            "reachable": bool(reachable),
            "state": state,
            "blocking_reasons": blocking,
            "status": status,
            "media": self._media_report(media, label_size, media_unavailable, settings),
            "details": details,
        }

    @staticmethod
    def _status_settings() -> Dict[str, Any]:
        """The saved settings a status check reads, or an empty dict.

        A status check must survive a missing or unreadable settings file: the
        printer is answering, and that answer is worth reporting even when the
        app cannot read its own configuration. Everything the settings supply
        here -- the configured label size, the owned media, the preference, the
        memory, the automatic-switch flag -- degrades to "not configured" on
        their own.
        """
        try:
            return settings_service.get_settings() or {}
        except Exception:  # noqa: BLE001 - a broken settings file must not fail a status check
            logger.warning("Could not read settings for the status check", exc_info=True)
            return {}

    def _status_label_size(self, label_size: Optional[str],
                           settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """The label size a status check should compare the media against.

        An explicit request value wins; otherwise the configured one is used, so
        a client that sends nothing still gets the comparison instead of having
        to redo it in the browser.
        """
        if label_size:
            return str(label_size)
        settings = self._status_settings() if settings is None else settings
        configured = settings.get("label_size")
        return str(configured) if configured else None

    def check_printer_status(self, printer_uri: str, printer_model: str,
                             label_size: Optional[str] = None) -> Dict[str, Any]:
        """
        Check whether a printer answers and whether it can actually print.

        Args:
            printer_uri: URI of the printer to check.
            printer_model: Model of the printer.
            label_size: Label identifier to compare the loaded media against.
                Defaults to the configured ``label_size``.

        Returns:
            Dict with ``available`` (nothing known prevents printing),
            ``reachable`` (the device answered), ``state`` (``ready`` /
            ``blocked`` / ``unknown`` / ``unreachable``), ``blocking_reasons``,
            ``status``, ``media`` and ``details``. The ``media`` section carries
            the loaded medium, the identifiers it could be, which single one
            those come down to (``resolution``) and what automatic mode wants
            done about it (``auto_switch``) -- see :meth:`_media_report`.

        Raises:
            PrinterError: If there's an error checking the printer status.
        """
        settings = self._status_settings()
        label_size = self._status_label_size(label_size, settings)

        # Defense in depth: never probe an unvetted URI. This guards against
        # SSRF (e.g. tcp://169.254.169.254) and disallowed schemes even if a
        # bad value somehow bypassed settings validation.
        try:
            validate_printer_uri(printer_uri)
        except ValueError as ve:
            logger.warning("Rejected printer URI before status check",
                           printer_uri=printer_uri, error=str(ve))
            return self._status_response(
                reachable=False,
                state="unreachable",
                status=f"Invalid printer URI: {str(ve)}",
                details={
                    "printer_uri": printer_uri,
                    "printer_model": printer_model,
                    "error": str(ve),
                },
                media_unavailable=MEDIA_DETECTION_UNREACHABLE,
                label_size=label_size,
                settings=settings,
            )

        backend_type = guess_backend(printer_uri)
        details: Dict[str, Any] = {
            "printer_uri": printer_uri,
            "printer_model": printer_model,
            "backend": backend_type,
        }

        # Network printers: query the real device state via IPP (TCP 631).
        # This works on Brother QL models where SNMP is disabled and the raw
        # 9100 port offers no status read-back. A plain TCP connect is used as
        # a reachability fallback when IPP does not answer.
        if backend_type == "network":
            ip_address = self._extract_ip_from_uri(printer_uri)
            ipp = get_printer_attributes(ip_address, port=self._get_ipp_port())
            if ipp.get("reachable"):
                state = ipp.get("printer_state") or "unknown"
                reasons = ipp.get("printer_state_reasons")
                blocking = blocking_state_reasons(state, reasons)
                details.update({
                    "printer_state": state,
                    "printer_state_reasons": reasons,
                    "reported_model": ipp.get("make_and_model"),
                    "source": "ipp",
                    "clock": self._build_clock_info(ipp.get("current_time")),
                })
                media = self.get_loaded_media(printer_uri, ipp_result=ipp)
                status_text = f"Printer is {state}"
                if blocking:
                    status_text += f" and cannot print ({', '.join(blocking)})"
                return self._status_response(
                    reachable=True,
                    state="blocked" if blocking else "ready",
                    status=status_text,
                    details=details,
                    media=media,
                    blocking_reasons=blocking,
                    label_size=label_size,
                settings=settings,
                )
            self._forget_media(printer_uri)
            if self._tcp_reachable(ip_address):
                details["source"] = "tcp"
                # The device is there but says nothing: readiness and media are
                # both genuinely unknown, and the response says so rather than
                # guessing either way.
                return self._status_response(
                    reachable=True,
                    state="unknown",
                    status="Printer reachable (no IPP status)",
                    details=details,
                    media_unavailable=MEDIA_DETECTION_UNSUPPORTED,
                    label_size=label_size,
                settings=settings,
                )
            details["source"] = "tcp"
            if ipp.get("error"):
                details["error"] = ipp["error"]
            return self._status_response(
                reachable=False,
                state="unreachable",
                status="Printer not reachable",
                details=details,
                media_unavailable=MEDIA_DETECTION_UNREACHABLE,
                label_size=label_size,
                settings=settings,
            )

        # Non-network backends (usb://, file://): constructing the backend is
        # the available reachability check. Neither offers a media channel.
        try:
            backend = backend_factory(backend_type)["backend_class"](printer_uri)
            backend.dispose()
            return self._status_response(
                reachable=True,
                state="ready",
                status="Printer is ready",
                details=details,
                media_unavailable=MEDIA_DETECTION_UNSUPPORTED,
                label_size=label_size,
                settings=settings,
            )
        except Exception as e:
            logger.error("Error checking printer status",
                        printer_uri=printer_uri,
                        printer_model=printer_model,
                        error=str(e),
                        exc_info=True)
            details["error"] = str(e)
            return self._status_response(
                reachable=False,
                state="unreachable",
                status=f"Printer error: {str(e)}",
                details=details,
                media_unavailable=MEDIA_DETECTION_UNREACHABLE,
                label_size=label_size,
                settings=settings,
            )

    def record_label_choice(self, new_settings: Dict[str, Any],
                            previous_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Remember the label type the user has just settled on for this medium.

        Registered with the settings service as an update hook (see
        :meth:`SettingsService.register_update_hook`), so what it returns is
        folded into the very same write that carries the choice. That is
        deliberate: a memory saved separately could be saved when the choice was
        not, and the two would then disagree about what the user decided.

        **What counts as settling on a label type.** Not every write of
        ``label_size``: the client persists the whole settings object on every
        change, so "it was written" says nothing about intent. Three conditions
        together do:

        * the value actually *changed*. Re-saving the same label size is not a
          decision, and treating it as one would let an unrelated settings save
          re-date a memory;
        * the new value is one of the identifiers the medium **currently
          loaded** can be. A user picking 102x51 while a 62 mm roll is in the
          machine is preparing a job for a roll they have not loaded yet -- it
          is not a statement about the 62 mm roll, and recording it against that
          roll would be recording the wrong thing;
        * automatic switching is on. The memory exists to feed it, and a feature
          that is off writes nothing at all -- no extra keys in the settings
          file, no probe on the printer. Off means off.

        **A standing preference does not switch the recording off.** When
        ``media_preference`` names a variant for this medium the memory can never
        win the resolution while that entry stands, so recording one looks like
        writing a value nothing will read. It is recorded anyway, for two
        reasons. A preference is a settings entry like any other and can be
        cleared; the moment it is, the memory is what the chain falls back to,
        and it should be the user's most recent actual choice rather than
        whatever was last recorded before the preference was set -- which could
        be months older than everything they have done since. And skipping it
        would mean deliberately forgetting a deviation: a user who prefers 62red
        but ran a batch on plain 62 did make that choice, and the honest record
        of it is what makes the preference's precedence a *ranking* rather than a
        silencing. The two settings answer different questions -- what was meant,
        and what happened -- so neither one's presence is a reason to stop
        answering the other.

        The medium is read through the ordinary media cache, so a settings save
        that follows a status check costs nothing; only a change of
        ``label_size`` with a cold cache pays a round trip, and an unreachable
        printer simply means no medium and therefore no memory. Nothing here can
        fail a settings save.

        Args:
            new_settings: The settings about to be written, already merged.
            previous_settings: What was on disk before this write.

        Returns:
            ``{"media_memory": {...}}`` to fold into the write, or ``{}`` when
            there is nothing to remember.
        """
        if not bool(new_settings.get("media_auto_switch")):
            return {}

        chosen = new_settings.get("label_size")
        if not isinstance(chosen, str) or not chosen.strip():
            return {}
        if previous_settings.get("label_size") == chosen:
            return {}

        printer_uri = new_settings.get("printer_uri")
        if not printer_uri:
            return {}

        try:
            media = self.get_loaded_media(str(printer_uri))
        except Exception:  # noqa: BLE001 - a settings save never fails over this
            logger.warning("Could not read the loaded media to record a label choice",
                           printer_uri=printer_uri, exc_info=True)
            return {}

        candidates = identify_label_candidates(media).candidates
        if chosen not in candidates:
            return {}
        key = media_memory_key(candidates)
        if not key:
            return {}

        # Prefer a map the write itself carries: a client that deliberately
        # edits the memory must not have its edit overwritten by the copy on
        # disk.
        memory = new_settings.get("media_memory")
        if not isinstance(memory, dict):
            memory = previous_settings.get("media_memory")
        memory = dict(memory) if isinstance(memory, dict) else {}
        if memory.get(key) == chosen:
            return {}

        memory[key] = chosen
        logger.info("Remembered the label type chosen for a medium",
                    medium=key, label_size=chosen, candidates=list(candidates))
        return {"media_memory": memory}

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
        # Track intermediate artifacts so they can be cleaned up afterwards.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"text_{uuid.uuid4().hex[:8]}"

            logger.info("Processing text print request", job_id=job_id, text_length=len(text))

            # Create label image
            image_path = self._create_text_label(text, settings)
            temp_files.append(image_path)
            logger.info("Text label created", job_id=job_id, image_path=image_path)

            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Text printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for text print", error=str(e))
            raise ValidationError(f"Error printing text: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing text", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing text: {str(e)}") from e
        finally:
            # All tracked files are generated by this service (no original upload).
            self._cleanup_temp_files(temp_files)
    
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
            ValidationError: If the image exceeds the configured pixel limit.
            ValueError: If settings are invalid.
        """
        # Size check first, and outside the try below on purpose: it must run
        # before the first decode (rotation already walks the full bitmap), and
        # the caller is better served by the guard's own message -- which names
        # the limit, the actual size and the field -- than by a re-wrapped copy
        # of it that keeps only the text.
        guard_image_pixels(image_path)

        # Track only the artifacts generated *here* (resized_/rotated_). The
        # original uploaded ``image_path`` is intentionally NOT tracked so we
        # don't double-delete a file owned by the image controller.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"image_{uuid.uuid4().hex[:8]}"

            logger.info("Processing image print request", job_id=job_id, image_path=image_path)

            # Rotate before resizing. Resizing fits the image to the label
            # width, so rotating afterwards swaps the axes and leaves the image
            # narrower than the label -- convert() then scales it back up to
            # fill the tape, which visually undoes the rotation. Rotating first
            # means the resize fits whichever edge really runs across the tape.
            rotate = settings.get("rotate", 0)
            source_path = image_path
            if rotate != 0:
                source_path = self._apply_rotation(image_path, rotate)
                temp_files.append(source_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Resize image to fit label width
            resized_path = self._resize_image(source_path, settings.get("label_size"), settings)
            temp_files.append(resized_path)
            logger.info("Image resized", job_id=job_id, resized_path=resized_path)

            # Send to printer
            self._send_to_printer(resized_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Image printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for image print", error=str(e))
            raise ValidationError(f"Error printing image: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing image", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing image: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def print_text_image(self, image_path: str, text: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print a label with an uploaded image and a text block side by side.

        Mirrors the text+QR layout but uses an uploaded image instead of a
        generated QR code. The image is placed on ``settings['image_position']``
        ("left" or "right") and the text block on the opposite side. The
        composed label then runs through the standard rotation/convert/send
        pipeline so it honours all standard settings (label_size, rotate,
        threshold, dither, red, copies, cut_mode).

        Args:
            image_path: Path to the uploaded image file.
            text: Text content to render alongside the image.
            settings: Dict containing layout and print settings. Recognised
                layout keys: ``image_position`` ("left"/"right", default
                "right"), ``text_alignment`` ("left"/"center"/"right", default
                "left") and ``text_font_size`` (default 30).

        Returns:
            Dict containing the result of the print operation.

        Raises:
            PrinterError: If there's an error printing the label.
            ImageProcessingError: If there's an error composing the label.
            ValidationError: If the image exceeds the configured pixel limit.
            ValueError: If settings are invalid.
        """
        # Size check before the composition decodes the upload (see print_image
        # for why this sits outside the try).
        guard_image_pixels(image_path)

        # Track only the artifacts generated *here* (composed label and its
        # rotated derivative). The original uploaded ``image_path`` is owned by
        # the controller and intentionally not tracked.
        temp_files: List[str] = []
        try:
            job_id = f"textimage_{uuid.uuid4().hex[:8]}"

            logger.info("Processing text+image print request", job_id=job_id, image_path=image_path)

            # Compose the side-by-side label (image + text).
            composed_path = self._create_text_image_label(image_path, text, settings)
            temp_files.append(composed_path)
            logger.info("Text+image label created", job_id=job_id, image_path=composed_path)

            # Apply rotation if specified.
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                composed_path = self._apply_rotation(composed_path, rotate)
                temp_files.append(composed_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer.
            self._send_to_printer(composed_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "Text+image label printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for text+image print", error=str(e))
            raise ValidationError(f"Error printing text+image label: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing text+image label", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing text+image label: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def _create_text_image_label(self, image_path: str, text: str, settings: Dict[str, Any]) -> str:
        """
        Compose an uploaded image and a multi-line text block side by side.

        Lays the image and text out on a 696px-wide label (image 1/3, text 2/3),
        with the image on ``image_position`` ("left"/"right") and the text on the
        opposite side. The uploaded image is scaled to the image area while
        preserving its aspect ratio. The result is saved as a PNG.

        Args:
            image_path: Path to the uploaded image file.
            text: Text content to render alongside the image.
            settings: Dict containing layout settings.

        Returns:
            Path to the created label image file.

        Raises:
            ImageProcessingError: If there's an error composing the label.
        """
        try:
            text_alignment = settings.get("text_alignment", "left")
            image_position = settings.get("image_position", "right")
            text_font_size = int(settings.get("text_font_size", settings.get("font_size", 30)))
            font = ImageFont.truetype(self.font_path, text_font_size)

            # Layout geometry: the roll's printable width, image 1/3, text 2/3.
            # The 20 px padding is kept for anything from 24 mm up, but scales
            # down on narrow media: three gutters of 20 px are wider than a
            # 12 mm roll's printable area, which left the image column with a
            # negative width and crashed the resize.
            width = get_label_width(settings.get("label_size"), settings)
            padding = min(20, max(2, width // 20))
            image_area_width = max(1, int(width * 1 / 3) - padding * 2)
            text_area_width = max(1, width - image_area_width - padding * 3)

            # Load the uploaded image, flattening transparency onto white, and
            # scale it into the image area while preserving aspect ratio.
            with Image.open(image_path) as src:
                src.load()
                if src.mode in ("RGBA", "LA", "P"):
                    src = src.convert("RGBA")
                    background = Image.new("RGB", src.size, "white")
                    background.paste(src, mask=src.split()[-1])
                    user_img = background
                else:
                    user_img = src.convert("RGB")

                img_w, img_h = user_img.size
                scale = image_area_width / img_w
                scaled_size = (image_area_width, max(1, int(img_h * scale)))
                user_img = user_img.resize(scaled_size, Image.Resampling.LANCZOS)

            img_w, img_h = user_img.size

            # Parse text into lines and measure each.
            text_lines = text.split("\n")
            line_spacing = 10
            text_metrics = []
            total_text_height = 0

            dummy_img = Image.new("RGB", (width, 10), "white")
            dummy_draw = ImageDraw.Draw(dummy_img)
            for line in text_lines:
                bbox = dummy_draw.textbbox((0, 0), line, font=font)
                line_width = bbox[2] - bbox[0]
                line_height = bbox[3] - bbox[1]
                text_metrics.append((line, line_width, line_height))
                total_text_height += line_height + line_spacing
            total_text_height -= line_spacing  # No trailing spacing.

            # Combined canvas: tall enough for the larger of image/text.
            total_height = max(img_h, total_text_height) + padding * 2
            new_img = Image.new("RGB", (width, total_height), "white")

            # Determine horizontal positions based on image_position.
            if image_position == "left":
                img_x = padding
                text_area_x = image_area_width + padding * 2
            else:  # image on the right (default)
                img_x = text_area_width + padding * 2
                text_area_x = padding

            # Paste image, vertically centered.
            img_y = (total_height - img_h) // 2
            new_img.paste(user_img, (img_x, img_y))

            # Draw text, vertically centered, honouring alignment.
            draw = ImageDraw.Draw(new_img)
            text_y = (total_height - total_text_height) // 2
            for line, line_width, line_height in text_metrics:
                if text_alignment == "center":
                    text_x = text_area_x + (text_area_width - line_width) // 2
                elif text_alignment == "right":
                    text_x = text_area_x + text_area_width - line_width
                else:  # left alignment (default)
                    text_x = text_area_x
                draw.text((text_x, text_y), line, font=font, fill="black")
                text_y += line_height + line_spacing

            # The composition is built at the roll's printable width and grows
            # downwards, which only a continuous roll allows. Fit it to the
            # medium so a die-cut label gets its exact canvas (and a round one
            # keeps the block inside the circle) instead of a "Bad image
            # dimensions" rejection.
            new_img = self._fit_to_label(new_img, settings.get("label_size"), settings)

            label_path = os.path.join(self.upload_folder, f"text_image_{uuid.uuid4().hex[:8]}.png")
            new_img.save(label_path)

            return label_path
        except Exception as e:
            logger.error("Error creating text+image label", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating text+image label: {str(e)}")

    def print_pdf(self, pdf_path: str, settings: Dict[str, Any],
                  pages=None, scale_mode: str = "fit") -> Dict[str, Any]:
        """
        Render a PDF and print each selected page on its own label.

        Each page is rasterised to a PIL image (300 DPI by default, 600 DPI when
        ``settings['dpi_600']`` is set), saved as a temporary grayscale PNG and
        then fed through the *existing* image print pipeline
        (``_resize_image`` -> optional rotation -> ``_send_to_printer``). As a
        result every page automatically inherits the standard print settings:
        copies, cut_mode, dpi_600, red, rotate, threshold and dither.

        Args:
            pdf_path: Path to the PDF file to print.
            settings: Dict of print settings (same shape as ``print_image``).
            pages: 1-based page-range spec (e.g. ``"1-3,5"``); empty/``None``/
                ``"all"`` prints every page. Validated via ``parse_page_range``.
            scale_mode: ``"fit"`` (default) or ``"fill"``. NOTE: scaling is
                currently delegated entirely to ``_resize_image`` (fit-to-width),
                so ``"fill"`` is accepted and validated but behaves like
                ``"fit"`` for now -- it is a pass-through parameter until a
                die-cut-aware fill path is implemented.

        Returns:
            Dict with ``success``, ``job_id``, ``message`` and ``pages_printed``.

        Raises:
            ValidationError: For invalid ``scale_mode`` / page spec, or for a
                selection larger than ``MAX_PDF_PAGES`` allows (-> 400).
            PrinterError: For render/IO/printer failures (-> 500).
        """
        # Only artifacts generated *here* (temporary PNGs and their
        # resized_/rotated_ derivatives) are tracked for cleanup. The original
        # ``pdf_path`` is owned by the caller and intentionally not deleted.
        temp_files: List[str] = []
        job_id = f"pdf_{uuid.uuid4().hex[:8]}"
        try:
            # --- Input/validation phase (-> ValidationError -> 400) ---
            try:
                if scale_mode not in ("fit", "fill"):
                    raise ValueError("scale_mode must be one of: fit, fill")

                dpi = 600 if settings.get("dpi_600") else 300

                # Render the requested pages. parse_page_range (invoked inside
                # render_pdf) raises ValueError for a bad page spec; an unreadable
                # / non-PDF file also raises ValueError -- both are caller input
                # problems and surface as a 400. A selection over the page limit
                # raises ValidationError instead, before any page is rasterised;
                # it passes straight through this handler and is re-raised
                # untouched below, so its message and details survive.
                images = render_pdf(pdf_path, pages, dpi=dpi)
            except ValueError as e:
                logger.warning("Invalid input for PDF print", job_id=job_id, error=str(e))
                raise ValidationError(f"Error printing PDF: {str(e)}") from e

            logger.info("Processing PDF print request",
                        job_id=job_id,
                        pdf_path=pdf_path,
                        pages=pages,
                        dpi=dpi,
                        scale_mode=scale_mode,
                        page_count=len(images))

            # --- Render/IO/printer phase (-> PrinterError -> 500) ---
            rotate = settings.get("rotate", 0)
            pages_printed = 0
            for index, page_image in enumerate(images, start=1):
                # Persist each rendered page as a grayscale PNG so it can flow
                # through the standard image pipeline.
                temp_png = os.path.join(
                    self.upload_folder, f"pdf_page_{uuid.uuid4().hex[:8]}.png"
                )
                page_image.convert("L").save(temp_png)
                temp_files.append(temp_png)

                # Rotate first, then fit: resizing to the label width before
                # rotating leaves the page narrower than the tape and convert()
                # scales it back up, undoing the rotation.
                page_source = temp_png
                if rotate != 0:
                    page_source = self._apply_rotation(temp_png, rotate)
                    temp_files.append(page_source)

                # Fit the page to the label width (same path as image printing).
                resized_path = self._resize_image(page_source, settings.get("label_size"), settings)
                temp_files.append(resized_path)

                # Send to the printer (inherits copies/cut_mode/dpi/red/etc.).
                self._send_to_printer(resized_path, settings)
                pages_printed += 1
                logger.info("PDF page sent to printer",
                            job_id=job_id, page=index, total=len(images))

            logger.info("PDF print job completed successfully",
                        job_id=job_id, pages_printed=pages_printed)

            return {
                "success": True,
                "job_id": job_id,
                "message": f"PDF printed ({pages_printed} page(s))",
                "pages_printed": pages_printed,
            }
        except ValidationError:
            # Already classified as a client (400) error above.
            raise
        except (ValueError, ImageProcessingError) as e:
            # Render/processing problems surfacing here are real failures.
            logger.error("Error printing PDF", job_id=job_id, error=str(e), exc_info=True)
            raise PrinterError(f"Error printing PDF: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing PDF", job_id=job_id, error=str(e), exc_info=True)
            raise PrinterError(f"Error printing PDF: {str(e)}") from e
        finally:
            # All tracked files are generated by this service (never the original PDF).
            self._cleanup_temp_files(temp_files)

    # ------------------------------------------------------------------ #
    # Server-side "render-only" previews.
    #
    # These run the *exact* same render pipeline as printing (same fonts,
    # layout, rotation and 1-bit black/white conversion) but return the
    # rendered label as a base64 PNG data URL instead of sending it to the
    # printer. They never call ``_send_to_printer``.
    # ------------------------------------------------------------------ #

    def _to_print_appearance(self, img: "Image.Image", settings: Dict[str, Any]) -> "Image.Image":
        """
        Transform a finished label image into how it will actually look when
        printed: a 1-bit black/white rendering.

        This mirrors what ``brother_ql.conversion.convert`` does internally so
        the preview is truthful:

        * Convert to grayscale ("L") first.
        * If ``dither`` is set, use Floyd-Steinberg dithering via
          ``img.convert("1")`` (Pillow's default error-diffusion).
        * Otherwise apply a hard threshold. convert maps the 0-100 ``threshold``
          setting to a 0-255 pixel cutoff as ``(100 - threshold) * 255 / 100``;
          we replicate that exactly so the preview matches the print.

        The result is returned as RGB for a clean, broadly-compatible PNG.
        """
        # Grayscale baseline (same starting point as convert()).
        gray = img.convert("L")

        if settings.get("dither"):
            # Floyd-Steinberg error diffusion (Pillow default for mode "1").
            bw = gray.convert("1")
        else:
            # Replicate convert()'s threshold mapping: a 0-100 setting becomes a
            # 0-255 cutoff via (100 - threshold) * 255 / 100. Pixels at or above
            # the cutoff stay white, below it become black.
            try:
                threshold = float(settings.get("threshold", 70))
            except (TypeError, ValueError):
                threshold = 70.0
            cutoff = (100.0 - threshold) * 255.0 / 100.0
            bw = gray.point(lambda p, c=cutoff: 255 if p >= c else 0, mode="1")

        return bw.convert("RGB")

    def _encode_png_data_url(self, img: "Image.Image") -> str:
        """Encode a PIL image as a ``data:image/png;base64,...`` data URL."""
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def render_text_preview(self, text: str, settings: Dict[str, Any]) -> str:
        """
        Render a text label exactly as it would be printed and return it as a
        base64 PNG data URL (no printing).

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_text_{uuid.uuid4().hex[:8]}"
            logger.info("Rendering text preview", job_id=job_id, text_length=len(text))

            image_path = self._create_text_label(text, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Text preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for text preview", error=str(e))
            raise ValidationError(f"Error rendering text preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering text preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering text preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_qrcode_preview(self, settings: Dict[str, Any]) -> str:
        """
        Render a QR code label exactly as it would be printed and return it as a
        base64 PNG data URL (no printing).

        The QR ``data`` is taken from ``settings`` (the controller injects it,
        same as the print path which derives text from data when absent).

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_qrcode_{uuid.uuid4().hex[:8]}"
            data = settings.get("data", "")
            logger.info("Rendering QR code preview", job_id=job_id, data_length=len(data))

            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("QR code preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for QR code preview", error=str(e))
            raise ValidationError(f"Error rendering QR code preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering QR code preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering QR code preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_label_preview(self, settings: Dict[str, Any]) -> str:
        """
        Render a combined text + QR code label exactly as it would be printed and
        return it as a base64 PNG data URL (no printing).

        Uses the same side-by-side composition path as the combined label print
        (``_create_qr_code`` with side-by-side settings). The QR ``data`` is
        taken from ``settings``.

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        temp_files: List[str] = []
        try:
            job_id = f"preview_label_{uuid.uuid4().hex[:8]}"
            data = settings.get("data", "")
            logger.info("Rendering label preview", job_id=job_id, data_length=len(data))

            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)

            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)

            with Image.open(image_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Label preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for label preview", error=str(e))
            raise ValidationError(f"Error rendering label preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering label preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering label preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_image_preview(self, image_path: str, settings: Dict[str, Any]) -> str:
        """
        Render an uploaded image exactly as it would be printed (resized to the
        label width, optional rotation, 1-bit black/white) and return it as a
        base64 PNG data URL (no printing).

        The original ``image_path`` is owned by the caller and is NOT cleaned up
        here -- only the resized/rotated derivatives generated internally are.

        Raises:
            ValidationError: For invalid input/settings, including an image
                over the configured pixel limit (-> 400).
            PrinterError: For render failures (-> 500).
        """
        # A preview decodes exactly what a print decodes, so it is guarded the
        # same way -- and being the synchronous endpoint, this is where the
        # caller sees the 400 immediately rather than on a queued job.
        guard_image_pixels(image_path)

        temp_files: List[str] = []
        try:
            job_id = f"preview_image_{uuid.uuid4().hex[:8]}"
            logger.info("Rendering image preview", job_id=job_id, image_path=image_path)

            # Same order as printing (rotate, then fit) so the preview matches.
            rotate = settings.get("rotate", 0)
            source_path = image_path
            if rotate != 0:
                source_path = self._apply_rotation(image_path, rotate)
                temp_files.append(source_path)

            resized_path = self._resize_image(source_path, settings.get("label_size"), settings)
            temp_files.append(resized_path)

            with Image.open(resized_path) as img:
                preview = self._to_print_appearance(img, settings)
                data_url = self._encode_png_data_url(preview)

            logger.info("Image preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for image preview", error=str(e))
            raise ValidationError(f"Error rendering image preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering image preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering image preview: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    @staticmethod
    def _wrap_text_to_width(text: str, font: "ImageFont.FreeTypeFont", max_width: int) -> List[str]:
        """Word-wrap ``text`` so each line fits within ``max_width`` pixels.

        Splits on existing newlines (blank lines preserved for visual spacing),
        wraps at word boundaries, and hard-breaks any single word that is itself
        wider than ``max_width``. Returns the list of lines to render. The app
        owns the font/metrics, so this produces an exact fit the client cannot.
        """
        if max_width <= 0:
            return text.split("\n")
        lines: List[str] = []
        for paragraph in text.split("\n"):
            if paragraph == "":
                lines.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                candidate = word if not current else current + " " + word
                if font.getlength(candidate) <= max_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    current = ""
                if font.getlength(word) <= max_width:
                    current = word
                else:
                    # Hard-break a word wider than the whole line.
                    chunk = ""
                    for ch in word:
                        if font.getlength(chunk + ch) <= max_width:
                            chunk += ch
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    current = chunk
            lines.append(current)
        return lines

    @staticmethod
    def _wrap_text_to_widths(lines: List[str], font: "ImageFont.FreeTypeFont",
                             widths: List[int]) -> List[str]:
        """Word-wrap ``lines`` where every output line has its own width budget.

        The sibling of :meth:`_wrap_text_to_width` for media whose usable width
        is not constant: on a round label each line of the stack gets the
        circle's chord at its own height, so the limit changes as the wrap
        progresses. ``widths`` is indexed by output line; once it runs out (the
        text wrapped into more lines than were budgeted for) the last entry is
        reused, and the caller re-runs the wrap with the new line count.

        Args:
            lines: Input lines (existing breaks are preserved).
            font: Font the text will be measured and drawn with.
            widths: Usable width per output line, top to bottom.

        Returns:
            The wrapped lines.
        """
        if not widths:
            return list(lines)

        wrapped: List[str] = []

        def limit_for_next_line() -> int:
            return widths[min(len(wrapped), len(widths) - 1)]

        for paragraph in lines:
            if paragraph == "":
                wrapped.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                limit = limit_for_next_line()
                candidate = word if not current else current + " " + word
                # A non-positive budget means this line is off the label
                # entirely; wrapping harder cannot save it, and hard-breaking
                # against it would loop forever, so take the text as-is and let
                # auto-fit shrink the font.
                if limit <= 0 or font.getlength(candidate) <= limit:
                    current = candidate
                    continue
                if current:
                    wrapped.append(current)
                    current = ""
                    limit = limit_for_next_line()
                if limit <= 0 or font.getlength(word) <= limit:
                    current = word
                else:
                    # Hard-break a word that is wider than the line it starts on.
                    chunk = ""
                    for character in word:
                        if font.getlength(chunk + character) <= limit:
                            chunk += character
                            continue
                        if chunk:
                            wrapped.append(chunk)
                            limit = limit_for_next_line()
                        chunk = character
                    current = chunk
            wrapped.append(current)
        return wrapped

    @staticmethod
    def _wrapping_broke_a_word(lines: List[str], wrapped: List[str]) -> bool:
        """Whether wrapping had to split a word instead of breaking between words.

        Hard-breaking always "fits", so any fitting rule that only asks whether
        the lines are short enough is trivially satisfiable by chopping a word in
        half. Comparing the word sequence before and after the wrap is what tells
        the two apart: a break between words leaves the sequence untouched, a
        break inside one does not.

        Args:
            lines: The input lines, before wrapping.
            wrapped: The lines produced by the wrap.

        Returns:
            True if at least one word was split.
        """
        return " ".join(lines).split() != " ".join(wrapped).split()

    @staticmethod
    def _widest_word(lines: List[str], font: "ImageFont.FreeTypeFont") -> float:
        """Width in pixels of the widest single word across ``lines``.

        Used to decide whether wrapping would have to hard-break a word, which
        is the point where a narrow roll needs a smaller font rather than more
        line breaks.
        """
        widest = 0.0
        for line in lines:
            for word in line.replace("\n", " ").split(" "):
                if word:
                    widest = max(widest, font.getlength(word))
        return widest

    def _render_round_text(self, lines: List[str], settings: Dict[str, Any],
                           diameter: int,
                           length: Optional[int] = None) -> "Image.Image":
        """
        Lay a block of text out inside the printable area of a round die cut.

        A round label reports a rectangular drawable area, but only the
        inscribed ellipse is actually on the paper -- a circle when the label is
        unbled and square, an oblong ellipse once bleed has widened it without
        lengthening it. Rather than retreat to the inscribed rectangle -- which
        would throw away 36 % of the label and make a single centred line
        needlessly small -- each line is measured against the chord at its own
        height (see :func:`get_round_line_widths`), so the middle of the label
        is used at nearly full width and only the top and bottom lines are
        pinched.

        ``vertical_alignment`` moves the stack up or down, but only as far as
        the chord it needs still exists (see :func:`get_round_block_top`). The
        default stays ``middle``, because the label is narrowest exactly where a
        top-aligned block starts and centring is what keeps the first line from
        being the one that gets cut off.

        Args:
            lines: The text lines to render (explicit breaks already applied).
            settings: Print settings; ``font_size``, ``alignment``,
                ``vertical_alignment``, ``text_wrap`` and ``auto_fit`` are
                honoured.
            diameter: The label's drawable width in pixels.
            length: The label's drawable height in pixels. Defaults to
                ``diameter``, i.e. the unbled square label.

        Returns:
            The rendered label, exactly ``diameter`` x ``length`` pixels.
        """
        if length is None:
            length = diameter
        font_size = int(settings.get("font_size", 50))
        alignment = settings.get("alignment", "left")
        vertical_alignment = get_vertical_alignment(settings)
        wrap = settings.get("text_wrap", True)
        auto_fit = settings.get("auto_fit", True)
        radius, radius_y = get_round_safe_axes(diameter, length)

        def block_top_for(current_lines, current_font, line_height):
            """Where this exact stack may sit, given the width it needs."""
            block_width = max(
                (current_font.getlength(line) for line in current_lines), default=0.0)
            return get_round_block_top(
                radius,
                len(current_lines) * line_height,
                # A block wider than the label has no room to travel at all; cap
                # it so the geometry stays real instead of going imaginary.
                min(block_width, 2 * radius),
                vertical_alignment,
                radius_y,
            )

        def layout(current_font):
            """Wrap ``lines`` to the circle and return (lines, widths, height, top)."""
            ascent, descent = current_font.getmetrics()
            line_height = ascent + descent
            rendered = list(lines)
            if wrap:
                # Chord widths depend on how many lines there are *and* on where
                # the stack sits; the line count depends on the widths, and the
                # position depends on the line count and the widest line. All
                # three are settled in the same iteration -- resolving them in
                # separate passes is what leaves the widths describing one
                # placement while the text is drawn at another. It converges in
                # a step or two; the cap is only there so a pathological input
                # cannot spin.
                block_top = block_top_for(rendered, current_font, line_height)
                for _ in range(4):
                    widths = get_round_line_widths(
                        radius, max(1, len(rendered)), line_height, block_top,
                        radius_y)
                    rewrapped = self._wrap_text_to_widths(lines, current_font, widths)
                    rewrapped_top = block_top_for(rewrapped, current_font, line_height)
                    settled = len(rewrapped) == len(rendered) and rewrapped_top == block_top
                    rendered, block_top = rewrapped, rewrapped_top
                    if settled:
                        break
            rendered = rendered or [""]
            # Whatever the iteration settled on, the widths handed back describe
            # the placement the caller is about to draw at, never another one.
            block_top = block_top_for(rendered, current_font, line_height)
            widths = get_round_line_widths(radius, len(rendered), line_height,
                                           block_top, radius_y)
            return rendered, widths, line_height, block_top

        font = ImageFont.truetype(self.font_path, font_size)
        rendered, widths, line_height, block_top = layout(font)

        def fits(current_font, current_lines, current_widths, current_line_height):
            # The stack has to fit the diameter, and every line has to fit the
            # chord it sits on. Wrapping already enforces the second for all but
            # unwrappable input, so this mostly guards text_wrap = false.
            if len(current_lines) * current_line_height > 2 * radius_y:
                return False
            # ...and no word may have been cut in half to get there. A narrow
            # chord needs a smaller font, not more line breaks: hard-breaking
            # satisfies every width test trivially, so without this the search
            # stops at a large font and prints "Kalibrier / t 2026" when the very
            # same text fits one unbroken line two steps further down.
            if self._wrapping_broke_a_word(lines, current_lines):
                return False
            return all(
                current_font.getlength(line) <= width
                for line, width in zip(current_lines, current_widths)
            )

        if auto_fit:
            while (font_size > MIN_AUTO_FIT_FONT_SIZE
                   and not fits(font, rendered, widths, line_height)):
                font_size -= 2
                font = ImageFont.truetype(self.font_path, font_size)
                rendered, widths, line_height, block_top = layout(font)

        logger.debug("Laid out round label text",
                     diameter=diameter,
                     length=length,
                     font_size=font_size,
                     line_count=len(rendered),
                     line_height=line_height,
                     vertical_alignment=vertical_alignment)

        image = Image.new("RGB", (diameter, length), "white")
        draw = ImageDraw.Draw(image)

        centre = diameter / 2.0
        # The very same offset the chords above were measured at, taken from the
        # label's own vertical centre -- which is no longer the horizontal one
        # once bleed has made the canvas wider than it is long.
        y = length / 2.0 + block_top
        for line, width in zip(rendered, widths):
            line_width = font.getlength(line)
            # Alignment is relative to the chord this line may use, not to the
            # rectangle, so "left" still lands on paper near the top and bottom.
            chord_left = centre - width / 2.0
            if alignment == "center":
                x = centre - line_width / 2.0
            elif alignment == "right":
                x = chord_left + width - line_width
            else:
                x = chord_left
            draw.text((int(round(x)), int(round(y))), line, font=font, fill="black")
            y += line_height

        return image

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
            
            # Render at the loaded roll's true printable width. Anything else is
            # rescaled by convert() on the way to the printer, which silently
            # changes the effective font size and softens the result.
            geometry = get_label_geometry(settings.get("label_size"), settings)
            width, label_height, is_die_cut = geometry

            # A round label needs its own layout: the usable width is the
            # circle's chord, which changes from line to line, so there is no
            # single text area for the rectangular path below to work with.
            if geometry.is_round and label_height:
                image = self._render_round_text(lines, settings, width, label_height)
                image_path = os.path.join(
                    self.upload_folder, f"text_label_{uuid.uuid4().hex[:8]}.png"
                )
                image.save(image_path)
                return image_path

            font_size = int(settings.get("font_size", 50))
            alignment = settings.get("alignment", "left")
            vertical_alignment = get_vertical_alignment(settings)
            text_area = width - 20  # 10 px margin on either side

            # Continuous tape has a spare axis: its length is unbounded. Running
            # the text along it instead of across it turns the printable width
            # into the line height and lets the label grow with the message, so
            # a long text on a narrow roll comes out as one readable strip
            # rather than a column of hard-wrapped fragments in a tiny font.
            # A die-cut label is a fixed size in both directions and has no such
            # axis to spend, so it always renders across.
            lengthwise = (
                str(settings.get("orientation", "across")).lower() == "lengthwise"
                and not (is_die_cut and label_height)
            )

            wrap = settings.get("text_wrap", True)
            auto_fit = settings.get("auto_fit", True)

            # Emphasis, when it is asked for. Off by default and then invisible:
            # every line becomes one plain run in the face this app has always
            # drawn in, and the measurements below are the ones it has always
            # made. On, the base drops to the regular weight so **bold** has
            # something to be heavier than -- see src/utils/text_markup.py.
            markup = markup_enabled(settings)
            fonts = FontSet(self.font_path, font_size, markup)
            font = fonts.regular
            # A newline inside a line is a line break too. Pillow's draw.text()
            # quietly renders one, but it refuses to *measure* multiline text --
            # and measuring is what everything here does first. Split only on
            # the markup path: the plain path measures with textbbox, which
            # tolerates it, and has laid such text out this way all along.
            line_runs = [
                parse_runs(part, markup)
                for line in lines
                for part in (line.split("\n") if markup else [line])
            ]

            # Wrapping and auto-fit both measure before anything is drawn, so
            # they need a canvas of their own.
            measure_draw = ImageDraw.Draw(Image.new("RGB", (width, 10), "white"))

            def wrap_all(current_fonts):
                # Auto-wrap long lines to the label width (default on) so text is
                # never silently truncated. Disable with settings.text_wrap = false.
                # Lengthwise there is no width to wrap against -- the tape grows
                # with the message -- so lines break only where the input said.
                if not wrap or lengthwise:
                    return list(line_runs)
                if markup:
                    return [
                        piece
                        for runs in line_runs
                        for piece in wrap_runs(measure_draw, runs, current_fonts, text_area)
                    ]
                # Unmarked text keeps the wrapper it has always used, so no
                # existing label re-flows because this feature arrived.
                return [
                    [Run(piece)]
                    for line in lines
                    for piece in self._wrap_text_to_width(line, current_fonts.regular, text_area)
                ]

            def widest(current_fonts):
                # Markup measures per run, because a bold word is wider than the
                # same word plain. Without it the original measurement stands,
                # down to the function that made it: this decides the font size,
                # and a different answer re-flows labels that have nothing to do
                # with emphasis.
                if markup:
                    return widest_word(measure_draw, line_runs, current_fonts)
                return self._widest_word(lines, current_fonts.regular)

            wrapped = wrap_all(fonts)

            # auto_fit shrinks the font until the text fits the medium. What
            # "fits" means depends on the medium, so the cases differ.
            if auto_fit and lengthwise:
                # The lines stack across the tape, so the printable width is the
                # height budget. Length is free, so nothing else constrains it.
                while font_size > MIN_AUTO_FIT_FONT_SIZE:
                    ascent, descent = font.getmetrics()
                    if 20 + len(wrapped) * (ascent + descent) <= width:
                        break
                    font_size -= 2
                    fonts = fonts.at_size(font_size)
                    font = fonts.regular
            elif auto_fit and wrap:
                if is_die_cut and label_height:
                    # Fixed physical height: shrink until the wrapped text fits
                    # inside it *and* no word had to be hard-broken to get there.
                    # Height alone is satisfiable by chopping a word in half, so
                    # on its own it stops at a large font and prints
                    # "Kalibrierungs / etikett" where a smaller one would have
                    # kept the word whole -- the same trade a narrow continuous
                    # roll makes below.
                    while font_size > MIN_AUTO_FIT_FONT_SIZE:
                        ascent, descent = font.getmetrics()
                        if (20 + len(wrapped) * (ascent + descent) <= label_height
                                and widest(fonts) <= text_area):
                            break
                        font_size -= 2
                        fonts = fonts.at_size(font_size)
                        font = fonts.regular
                        wrapped = wrap_all(fonts)
                else:
                    # Continuous tape grows downwards, so height is never the
                    # constraint -- width is. On a narrow roll a single word can
                    # be wider than the whole label, and hard-breaking it turns a
                    # sentence into a column of letters metres long. Shrink until
                    # every word fits a line of its own instead.
                    while (font_size > MIN_AUTO_FIT_FONT_SIZE
                           and widest(fonts) > text_area):
                        font_size -= 2
                        fonts = fonts.at_size(font_size)
                        font = fonts.regular
                    wrapped = wrap_all(fonts)

            line_runs = wrapped

            # Create a dummy image to calculate text dimensions
            dummy_image = Image.new("RGB", (width, 10), "white")
            dummy_draw = ImageDraw.Draw(dummy_image)

            # Measure with the font's own line box instead of the ink bounding
            # box. The bbox covers only the glyphs actually present, so a line
            # without descenders measures short -- but draw.text() still
            # positions from the ascent line and reserves descent space, which
            # pushed the final line's descenders off the bottom of the canvas.
            # ascent+descent already includes the gap below the baseline, so no
            # extra leading is added on top.
            ascent, descent = font.getmetrics()
            line_height = ascent + descent

            total_height = 10
            line_metrics = []
            for runs in line_runs:
                if markup:
                    # Advance widths, summed per run: two faces set the same
                    # string to different widths, and that difference is what
                    # alignment would otherwise get wrong. Rounded to whole
                    # pixels like the bounding box below, because a lengthwise
                    # canvas is sized from this and Image.new takes integers.
                    line_width = int(round(measure_runs(dummy_draw, runs, fonts)))
                else:
                    bbox = dummy_draw.textbbox((0, 0), runs_text(runs), font=font)
                    line_width = bbox[2] - bbox[0]
                total_height += line_height
                line_metrics.append((runs, line_width))

            total_height += 10

            if lengthwise:
                # Lay the label out unrolled: the text reads left to right on a
                # canvas as tall as the tape is wide, and the whole thing is
                # rotated onto the tape once it is drawn. So here the canvas
                # width is the tape *length* and grows with the longest line.
                line_area = max((w for _, w in line_metrics), default=0) + 20
                if line_area > MAX_LENGTHWISE_LENGTH_PX:
                    raise ValidationError(
                        f"Lengthwise label would need {line_area} px of tape "
                        f"(limit {MAX_LENGTHWISE_LENGTH_PX} px, about 1 m). "
                        "Shorten the text or reduce the font size."
                    )
                canvas_height = width
                # The tape width is the axis with room to spare here, so it is
                # the one vertical_alignment acts on: the stack may sit against
                # either edge of the tape or, by default, centred across it --
                # on a narrow roll the difference is the whole margin. The
                # canvas is still in reading orientation at this point, so "top"
                # is the edge above the first line as the label reads; the
                # transpose below carries it onto the tape. alignment continues
                # to position the lines along the tape.
                y = get_vertical_offset(
                    canvas_height, len(line_metrics) * line_height, vertical_alignment)
            else:
                # A die-cut label is a fixed physical size, so pin the canvas to
                # it: convert() raises "Bad image dimensions" for anything else.
                # With auto_fit on the font was already stepped down to fit; with
                # it off the requested size is honoured and the overflow is
                # clipped, rather than inventing a label the printer cannot cut.
                if is_die_cut and label_height:
                    total_height = label_height
                line_area = width
                canvas_height = total_height
                if is_die_cut and label_height:
                    # A die-cut label is a fixed piece of paper, so there is
                    # slack between the stack and the two long edges to hand to
                    # vertical_alignment. It stays centred by default: on media
                    # where the printable area narrows towards the edge the
                    # first line is the one that gets cut off.
                    y = get_vertical_offset(
                        canvas_height, len(line_metrics) * line_height, vertical_alignment)
                else:
                    # Continuous tape rendered across grows to exactly fit the
                    # text, so there is no spare length for the stack to move
                    # into -- only the 10 px lead-in, which every value keeps.
                    y = 10

            image = Image.new("RGB", (line_area, canvas_height), "white")
            draw = ImageDraw.Draw(image)

            # Draw text
            for runs, line_width in line_metrics:
                if alignment == "center":
                    x = (line_area - line_width) // 2
                elif alignment == "right":
                    x = line_area - line_width - 10
                else:
                    x = 10
                if markup:
                    draw_runs(draw, (x, y), runs, fonts)
                else:
                    draw.text((x, y), runs_text(runs), font=font, fill="black")
                y += line_height

            if lengthwise:
                # Rotate counter-clockwise so the message reads bottom-to-top
                # with the strip held upright; pair with rotate=180 for the other
                # direction. transpose() is a lossless pixel move, where rotate()
                # would resample. The result is exactly the roll's printable
                # width, so convert() passes it through without rescaling.
                image = image.transpose(Image.Transpose.ROTATE_90)

            # Save image
            image_path = os.path.join(self.upload_folder, f"text_label_{uuid.uuid4().hex[:8]}.png")
            image.save(image_path)
            
            return image_path
        except ValidationError:
            # Already a precise client error (the lengthwise length cap), so let
            # it through; wrapping it would turn a 400 into a 500.
            raise
        except Exception as e:
            logger.error("Error creating text label", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating text label: {str(e)}")
    
    def _fit_to_label(self, img: "Image.Image", label_size: Optional[str] = None,
                      settings: Optional[Dict[str, Any]] = None,
                      margin_is_content: bool = False) -> "Image.Image":
        """
        Centre-fit a finished label image onto the medium's exact canvas.

        Every render path ends here, because what "the right size" means is a
        property of the loaded media, not of the content:

        * Continuous tape only fixes the width; the length grows with the
          content. The image is scaled to the printable width and returned --
          exactly what ``convert()`` would otherwise do on the way out, done
          here so the pipeline and the preview agree on the pixels.
        * A rectangular die-cut label is a fixed size in both directions and
          ``convert()`` raises "Bad image dimensions" for anything else, so the
          content is scaled to fit inside it and centred on a white canvas of
          the label's own size.
        * A round die-cut label reports the same square size, but its printable
          area is the inscribed *circle*, and there the image's rectangle is the
          wrong thing to measure. A rectangle fits a circle only when its
          half-diagonal fits the radius, which costs 29 % of the diameter --
          and it costs it whether or not there is anything in the corners to
          pay for. A ring drawn to the edge of its own bitmap came off a 24 mm
          label 13 mm across for exactly that reason. So the scale is set by the
          ink instead: the largest one that keeps every dot the printer will
          actually blacken inside the safe ellipse. On artwork that does reach
          its corners the corner dot is the binding one and the result is the
          half-diagonal rule again, unchanged to the last bit.

        The aspect ratio is never distorted; the leftover area is white padding.
        Fitting by the ink can make the *image* wider than the label even though
        the ink is not, in which case the overhanging strips are white by
        definition and are simply cropped by the paste.

        Args:
            img: The finished label image.
            label_size: Label identifier the image is destined for. Defaults to
                62 mm tape when not given.
            settings: Optional print settings, so a ``bleed_mm`` entry enlarges
                the canvas and the circle along with it. The whole point of
                putting bleed in :func:`get_label_geometry` is that this method
                needs no case of its own: a bled round label is a bigger ellipse
                and the fit follows it unchanged. ``threshold``/``dither`` are
                read as well, because which dots count as ink is exactly the
                question they answer.
            margin_is_content: Set when the white around the ink is part of the
                design rather than waste, in which case the image's rectangle is
                measured as before. A QR code is the case that matters: its
                quiet zone is white but a scanner needs it, so growing the
                modules into it would trade a cosmetic win for a label that no
                longer reads.

        Returns:
            The image fitted to the medium (the original object when it already
            fits and nothing has to change).

        Raises:
            ImageProcessingError: If the image has no usable dimensions.
        """
        geometry = get_label_geometry(label_size, settings)
        source_width, source_height = img.size
        if source_width <= 0 or source_height <= 0:
            raise ImageProcessingError(f"Cannot fit an empty image to label {label_size}")

        if not (geometry.is_die_cut and geometry.height):
            # Continuous tape: only the width is fixed, the label may be as long
            # as it needs to be.
            if source_width == geometry.width:
                return img
            aspect_ratio = source_height / source_width
            new_height = max(1, int(geometry.width * aspect_ratio))
            return img.resize((geometry.width, new_height), Image.Resampling.LANCZOS)

        # Die-cut media from here on: the canvas is the label itself.
        # Paletted/alpha images are flattened first so the white padding really
        # is white, LANCZOS has continuous tone to work with, and a transparent
        # corner counts as empty rather than as whatever colour hid behind it.
        img = _flattened_onto_white(img)
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")

        ink_scale = None
        if geometry.is_round:
            radius_x, radius_y = get_round_safe_axes(geometry.width, geometry.height)
            if radius_x == radius_y:
                # Circle: the half-diagonal meets the radius.
                scale = radius_x / (math.hypot(source_width, source_height) / 2.0)
            else:
                # Ellipse (a bled round label, wider than it is long). A centred
                # rectangle fits when its corner satisfies
                # (X/a)^2 + (Y/b)^2 <= 1, so the largest scale is the reciprocal
                # of that expression's square root. With a == b it is the same
                # half-diagonal rule; kept separate only so the circle keeps its
                # exact arithmetic.
                scale = 1.0 / math.hypot(source_width / (2.0 * radius_x),
                                         source_height / (2.0 * radius_y))
            if not margin_is_content:
                ink_scale = self._ink_fitted_scale(img, radius_x, radius_y, scale, settings)
                scale = ink_scale if ink_scale is not None else scale
        else:
            scale = min(geometry.width / source_width, geometry.height / source_height)

        new_size = (
            max(1, int(source_width * scale)),
            max(1, int(source_height * scale)),
        )
        if new_size != img.size:
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        # White canvas, so the untouched area of the label stays blank rather
        # than picking up whatever the source image had in its corners. A
        # negative offset here is the ink fit having outgrown the label with
        # white; paste crops it away.
        canvas = Image.new(img.mode, (geometry.width, geometry.height), (255,) * len(img.mode))
        canvas.paste(img, ((geometry.width - new_size[0]) // 2, (geometry.height - new_size[1]) // 2))

        logger.debug("Fitted image to die-cut label",
                     label_size=label_size,
                     is_round=geometry.is_round,
                     fitted_to="ink" if ink_scale is not None else "bounding box",
                     source_size=(source_width, source_height),
                     content_size=new_size,
                     canvas_size=(geometry.width, geometry.height))
        return canvas

    def _ink_fitted_scale(self, img: "Image.Image", semi_x: float, semi_y: float,
                          box_scale: float,
                          settings: Optional[Dict[str, Any]]) -> Optional[float]:
        """
        Scale a round label's content by where its ink is, not by its rectangle.

        What counts as ink is decided by :meth:`_to_print_appearance`, i.e. by
        the same ``threshold`` and ``dither`` the job will print with. Any other
        definition would disagree with the printer somewhere near the rim, which
        is the one place the disagreement shows.

        Two bounds keep the result sane:

        * Nothing to fit -- a blank label, or a source whose ink already reaches
          a corner -- hands the decision straight back to the bounding box. The
          corner case is not merely an optimisation: a dot in the corner *is*
          the binding dot, so the ink answer and the rectangle answer are the
          same number, and returning the rectangle's own arithmetic keeps a
          full-bleed photo byte-for-byte what it was.
        * The scale never climbs above 1:1, because upsampling a bitmap invents
          no detail and a label carrying a single dot near one corner would
          otherwise blow that dot up to fill the medium. Where the bounding-box
          rule was already enlarging a small image, that stays the ceiling --
          this fit is here to stop giving diameter away, not to start magnifying
          more than before.

        Args:
            img: The flattened content, in its own pixel coordinates.
            semi_x: Semi-axis of the safe ellipse across the tape, in pixels.
            semi_y: Semi-axis of the safe ellipse along the feed, in pixels.
            box_scale: The scale the bounding-box rule would use.
            settings: Print settings, for the threshold/dither the job will use.

        Returns:
            The scale to use, or ``None`` to keep the bounding-box rule.
        """
        printed = self._to_print_appearance(img, settings or {})
        ink = ImageOps.invert(printed.convert("L"))

        # Max-pool a large source so the geometry runs on a bounded grid.
        # reduce() averages, so a block holding a single inked dot still comes
        # back non-zero and is kept: the fit can only get more cautious.
        factor = 1
        while factor < MAX_INK_PROBE_FACTOR and max(img.size) / factor > MAX_INK_PROBE_PX:
            factor *= 2
        if factor > 1:
            ink = ink.reduce(factor).point(lambda level: 255 if level else 0)

        width, height = ink.size
        corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        if any(ink.getpixel(corner) for corner in corners):
            return None

        scale = _largest_ink_scale(ink, semi_x, semi_y, img.size, factor)
        if scale is None:
            return None
        return max(box_scale, min(scale, max(1.0, box_scale)))

    def _resize_image(self, image_path: str, label_size: Optional[str] = None,
                      settings: Optional[Dict[str, Any]] = None) -> str:
        """
        Resize an image to fit the label.

        Args:
            image_path: Path to the image file.
            label_size: Label identifier the image is destined for. Defaults to
                62 mm tape when not given.
            settings: Optional print settings, so a ``bleed_mm`` entry fits the
                image to the bled label rather than the published one.

        Returns:
            Path to the resized image file.

        Raises:
            ImageProcessingError: If there's an error resizing the image.
        """
        try:
            with Image.open(image_path) as img:
                # Fit to the medium: the printable width on continuous tape, the
                # label's exact canvas on die-cut media (which convert() insists
                # on) with the content centred inside it.
                img = self._fit_to_label(img, label_size, settings)

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
    
    # ------------------------------------------------------------------ #
    # Print alignment calibration.
    #
    # Content can land off-centre on the paper even when the raster is
    # mathematically centred: the die cut is punched with a tolerance, the
    # media wanders a little on the roll and the models differ in where they
    # start the raster. Round labels show it plainly -- a design centred in the
    # raster prints with a visibly uneven gap around the punched circle.
    #
    # The remedy is a per-label offset the user dials in by eye: print the
    # target below, read how far it is out, enter the correction, print again.
    # ------------------------------------------------------------------ #

    def _calibration_font(self, size: int) -> Optional["ImageFont.FreeTypeFont"]:
        """Return a TrueType font at ``size`` px, or None when none is usable.

        The target has to stay printable on a host without fonts, so a missing
        font drops the caption instead of failing the print -- the ring, the
        crosshair and the millimetre scale are what the measurement is made
        with, and all three are pure geometry.
        """
        if not self.font_path:
            return None
        try:
            return ImageFont.truetype(self.font_path, size)
        except (OSError, ValueError):
            logger.warning("No usable font for the calibration caption",
                           font_path=self.font_path, size=size)
            return None

    def _render_calibration_target(self, settings: Dict[str, Any],
                                   note: Optional[str] = None) -> "Image.Image":
        """
        Draw the calibration target for the medium in ``settings``.

        The target is built so the error can be read off the printed label with
        nothing but eyes:

        * A ring (round media) or a frame (rectangular and continuous media) at
          the edge of the printable area. It is concentric with the label by
          construction, so any print offset shows up as an uneven gap to the
          punched or cut edge -- and on a circle the gap varies all the way
          round, which is why round media makes the error so obvious.
        * A crosshair through the centre of the printable area.
        * A millimetre scale along both axes: a mark every millimetre, every
          fifth one drawn long. This is the part that removes the ruler -- the
          gap to measure and the unit to measure it in end up side by side on
          the same piece of paper. Reading rule: the error is half the
          difference between two opposite gaps, because a shift widens one side
          by exactly as much as it narrows the other.
        * A caption naming the label and the offset the target was printed
          with, so three iterations held side by side can be told apart, plus
          the step number when a sweep is printed. The offset is spelled with
          direction letters rather than signs (see
          :func:`format_calibration_offset`), because a sign is a hairline and
          hairlines do not survive every font at every size.

        Continuous media has no length of its own, so the target is cut to a
        fixed ``CALIBRATION_TARGET_LENGTH_MM``.

        On the smallest media the caption is shortened and then dropped
        entirely: d12 offers under 8 mm of printable circle, where a scale that
        can still be counted is worth more than a caption nobody can read.

        Args:
            settings: Print settings; ``label_size`` selects the medium and the
                ``calibration`` map supplies the offset shown in the caption.
            note: Optional short marker printed above the caption, used to
                number the labels of a sweep.

        Returns:
            The rendered target at the medium's exact drawable size (die-cut)
            or drawable width (continuous) -- including any bleed, because the
            target is printed through the same path as everything else and
            ``convert()`` would reject a target rendered to the unbled size.
            Bleeding the target is also the more useful gauge: its reference
            ring then sits on the die cut itself rather than 2 mm inside it.
        """
        label_size = settings.get("label_size")
        geometry = get_label_geometry(label_size, settings)
        width = geometry.width
        height = geometry.height or max(
            1, int(round(CALIBRATION_TARGET_LENGTH_MM * DOTS_PER_MM)))
        # The *applied* offset, not the stored one. A medium can sit closer to
        # one end of the print head than the correction asks for, and a target
        # captioned with travel the printer could not make would be read as
        # evidence that the correction did not work.
        applied = applied_calibration_offset(settings, label_size)
        x_mm, y_mm = applied.x_mm, applied.y_mm
        scale = get_calibration_scale(settings, label_size)

        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)

        centre_x = (width - 1) / 2.0
        centre_y = (height - 1) / 2.0
        smallest = min(width, height)
        # One dot of line is invisible on 58 mm media and three dots swallow a
        # millimetre of d12, so the weight follows the label.
        stroke = 1 if smallest < 150 else 2
        minor_tick = max(3, int(round(smallest * 0.035)))
        major_tick = max(minor_tick + 3, int(round(smallest * 0.085)))
        # The printable area of round media is the ellipse inscribed in the
        # drawable rectangle -- a circle on an unbled label, which is square,
        # and a genuine ellipse once bleed has widened it without lengthening
        # it. The two semi-axes are kept separate so the marks near the rim are
        # measured against the shape that is really there.
        radius = smallest / 2.0
        radius_x = width / 2.0
        radius_y = height / 2.0
        circular = radius_x == radius_y

        def half_width_at(offset: float) -> float:
            """Half the printable width ``offset`` px above or below centre."""
            if circular:
                return math.sqrt(max(0.0, radius * radius - offset * offset))
            return radius_x * math.sqrt(
                max(0.0, 1.0 - (offset / radius_y) ** 2)) if offset < radius_y else 0.0

        def half_height_at(offset: float) -> float:
            """Half the printable height ``offset`` px left or right of centre."""
            if circular:
                return math.sqrt(max(0.0, radius * radius - offset * offset))
            return radius_y * math.sqrt(
                max(0.0, 1.0 - (offset / radius_x) ** 2)) if offset < radius_x else 0.0

        # --- The reference edge ----------------------------------------- #
        if geometry.is_round:
            draw.ellipse((0, 0, width - 1, height - 1), outline="black", width=stroke)
            left = centre_x - half_width_at(stroke)
            right = centre_x + half_width_at(stroke)
            top = centre_y - half_height_at(stroke)
            bottom = centre_y + half_height_at(stroke)
        else:
            draw.rectangle((0, 0, width - 1, height - 1), outline="black", width=stroke)
            left, right = 0.0, width - 1.0
            top, bottom = 0.0, height - 1.0

        # --- Crosshair --------------------------------------------------- #
        draw.line((left, round(centre_y), right, round(centre_y)), fill="black", width=stroke)
        draw.line((round(centre_x), top, round(centre_x), bottom), fill="black", width=stroke)

        # --- Millimetre scale along both axes ---------------------------- #
        # The marks of the horizontal scale sit *above* its line, which leaves
        # the band directly below the centre free for the caption: the caption
        # then costs a few marks out of the middle of the vertical scale, where
        # they matter least, instead of cutting a hole in either scale near the
        # rim -- which is exactly where the gaps being measured are.
        for step in range(1, int((width / 2.0) / DOTS_PER_MM) + 1):
            length = (major_tick if step % CALIBRATION_MAJOR_TICK_EVERY == 0
                      else minor_tick)
            for direction in (-1, 1):
                # Rounded to a whole dot: a mark placed at a fractional
                # coordinate lands a dot early or late depending on which side
                # of the centre it is on, and a scale whose marks are not
                # evenly spaced is not a scale.
                x = round(centre_x + direction * step * DOTS_PER_MM)
                reach = length
                if geometry.is_round:
                    # A mark near the rim has less circle above it than one
                    # near the centre; shortening it keeps the scale complete
                    # instead of dropping its outermost -- and most useful --
                    # marks off the label. The chord is measured at the far
                    # side of the mark's own width, since that is the part that
                    # would land outside the die cut.
                    available = half_height_at(abs(x - centre_x) + stroke) - stroke
                    if available <= 1:
                        continue
                    reach = min(reach, available)
                elif not (0 <= x <= width - 1):
                    continue
                draw.line((x, centre_y - reach, x, centre_y),
                          fill="black", width=stroke)

        for step in range(1, int((height / 2.0) / DOTS_PER_MM) + 1):
            length = (major_tick if step % CALIBRATION_MAJOR_TICK_EVERY == 0
                      else minor_tick)
            for direction in (-1, 1):
                y = round(centre_y + direction * step * DOTS_PER_MM)
                half = length / 2.0
                if geometry.is_round:
                    available = half_width_at(abs(y - centre_y) + stroke) - stroke
                    if available <= 1:
                        continue
                    half = min(half, available)
                elif not (0 <= y <= height - 1):
                    continue
                draw.line((centre_x - half, y, centre_x + half, y),
                          fill="black", width=stroke)

        # --- Caption ----------------------------------------------------- #
        offset_text = format_calibration_offset(x_mm, y_mm, scale)
        full_lines = ([note] if note else []) + (
            [str(label_size)] if label_size else []) + [offset_text]
        compact_lines = [f"{note + ' ' if note else ''}{offset_text}"]
        # Last resort on the smallest media: a sweep is judged by picking one
        # label out of several, so its number is the one thing that cannot be
        # dropped -- the offset it stands for is on the screen the sweep was
        # started from.
        marker_lines = [note] if note else []
        # Directly under the centre line: the widest part of a round label, so
        # the caption gets the largest type the medium can carry, and the part
        # of the scales that is least needed for the measurement.
        pad = max(2, stroke)
        block_top_y = centre_y + stroke + pad

        def caption_box(lines, font):
            """Return the caption's line height and the box it knocks out.

            The box is measured from the ink, not from the advance width: a
            glyph can reach further left or right than ``textlength`` reports,
            and on a circle that overhang is exactly the part that lands on the
            backing paper instead of the label.
            """
            ascent, descent = font.getmetrics()
            line_height = ascent + descent
            block_height = line_height * len(lines)
            widest = 0.0
            for line in lines:
                ink = draw.textbbox((0, 0), line, font=font)
                widest = max(widest, ink[2] - ink[0], draw.textlength(line, font=font))
            half_width = widest / 2.0 + pad
            return line_height, widest, (centre_x - half_width, block_top_y - pad,
                                         centre_x + half_width,
                                         block_top_y + block_height + pad)

        def caption_fits(box) -> bool:
            """Whether the caption's box stays on the printable area."""
            x0, y0, x1, y1 = box
            if geometry.is_round:
                # Every corner has to be inside the circle. Measuring the text
                # box against the label's width instead would let a caption
                # that fits "the label" hang off a circle that has already
                # narrowed by the time it reaches the caption's own height.
                limit = radius - stroke
                return all(math.hypot(x - centre_x, y - centre_y) <= limit
                           for x in (x0, x1) for y in (y0, y1))
            return (x0 >= stroke + 1 and x1 <= width - 1 - stroke - 1
                    and y0 >= stroke + 1 and y1 <= height - 1 - stroke - 1)

        caption_font = None
        caption_lines: List[str] = []
        # Roughly a tenth of the label's short side, but never so large that a
        # 58 mm round label prints a caption bigger than the scale it explains.
        start_size = max(MIN_CALIBRATION_FONT_PX, min(28, int(smallest * 0.09)))
        for candidate in (full_lines, compact_lines, marker_lines):
            if not candidate:
                continue
            for size in range(start_size, MIN_CALIBRATION_FONT_PX - 1, -1):
                font = self._calibration_font(size)
                if font is None:
                    break
                if caption_fits(caption_box(candidate, font)[2]):
                    caption_font, caption_lines = font, candidate
                    break
            if caption_font is not None:
                break

        if caption_font is not None:
            line_height, _, box = caption_box(caption_lines, caption_font)
            # Knock the background out first. The caption sits over the centre
            # line, and a scale mark showing through a glyph is the one thing
            # that could make the printed offset misread.
            draw.rectangle(box, fill="white")
            y = block_top_y
            for line in caption_lines:
                line_width = draw.textlength(line, font=caption_font)
                draw.text((centre_x - line_width / 2.0, y), line,
                          font=caption_font, fill="black")
                y += line_height

        logger.debug("Rendered calibration target",
                     label_size=label_size, size=(width, height),
                     offset_mm=(x_mm, y_mm), scale=scale, note=note,
                     captioned=caption_font is not None)
        return image

    def _create_calibration_label(self, settings: Dict[str, Any],
                                  note: Optional[str] = None) -> str:
        """
        Render the calibration target and save it as a PNG.

        Args:
            settings: Print settings selecting the medium and carrying the
                offset to print on the target.
            note: Optional step marker for a sweep (e.g. ``"#3"``).

        Returns:
            Path to the created image file.

        Raises:
            ImageProcessingError: If the target could not be rendered.
        """
        try:
            image = self._render_calibration_target(settings, note=note)
            image_path = os.path.join(
                self.upload_folder, f"calibration_{uuid.uuid4().hex[:8]}.png"
            )
            image.save(image_path)
            return image_path
        except Exception as e:
            logger.error("Error creating calibration target", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating calibration target: {str(e)}")

    def plan_calibration_offsets(self, settings: Dict[str, Any],
                                 sweep: Optional[Dict[str, Any]] = None) -> List[Dict[str, float]]:
        """
        Work out which offsets a calibration run should print.

        Without ``sweep`` that is a single target carrying the offset currently
        in force. With it, the run becomes the one-pass variant: N numbered
        targets whose offsets step around the current value, so the user picks
        the label that looks centred instead of iterating. It is opt-in
        precisely because each step costs a physical label.

        Args:
            settings: Print settings supplying the label and current offset.
            sweep: Optional ``{axis, count, step_mm}``. ``axis`` is ``x`` or
                ``y`` -- one axis at a time, because a diagonal sweep cannot
                tell which of the two errors a given label is showing.

        Returns:
            One ``{"x_mm": ..., "y_mm": ...}`` per label to print, in print
            order.

        Raises:
            ValidationError: If the sweep parameters are out of range.
        """
        base_x, base_y = get_calibration_offset(settings, settings.get("label_size"))
        if not sweep:
            return [{"x_mm": base_x, "y_mm": base_y}]

        axis = str(sweep.get("axis", "x")).lower()
        if axis not in ("x", "y"):
            raise ValidationError("sweep.axis must be x or y", "sweep.axis")
        try:
            count = int(sweep.get("count", 5))
            step_mm = float(sweep.get("step_mm", 0.5))
        except (TypeError, ValueError) as e:
            raise ValidationError("sweep.count and sweep.step_mm must be numbers", "sweep") from e
        if not (2 <= count <= MAX_CALIBRATION_SWEEP_STEPS):
            raise ValidationError(
                f"sweep.count must be between 2 and {MAX_CALIBRATION_SWEEP_STEPS}",
                "sweep.count",
            )
        if not (0.1 <= step_mm <= 5.0):
            raise ValidationError("sweep.step_mm must be between 0.1 and 5.0", "sweep.step_mm")

        offsets: List[Dict[str, float]] = []
        for index in range(count):
            # Centred on the offset already in force, so the middle label is
            # "what you have now" and the rest bracket it.
            delta = (index - (count - 1) / 2.0) * step_mm
            stepped = round(
                max(-CALIBRATION_LIMIT_MM,
                    min(CALIBRATION_LIMIT_MM, (base_x if axis == "x" else base_y) + delta)),
                2,
            )
            offsets.append({"x_mm": stepped, "y_mm": base_y} if axis == "x"
                           else {"x_mm": base_x, "y_mm": stepped})
        return offsets

    def describe_calibration_run(self, settings: Dict[str, Any],
                                 sweep: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Plan a calibration run and report what the printer can really do.

        The planned offsets are a request; sideways they are bounded by how
        much print head there is beside the loaded media. Everything a client
        needs to say so is reported together, so a UI can tell the user "3.5 mm
        is all this medium allows in that direction" instead of appearing to
        accept a value and quietly doing less with it.

        Deterministic and side-effect free, which is what lets the response go
        out before the queue worker has printed anything, and lets the dry run
        answer the same question without a label.

        Args:
            settings: Print settings selecting the medium, the printer and the
                calibration entry.
            sweep: Optional sweep description, see
                :meth:`plan_calibration_offsets`.

        Returns:
            Dict with ``offsets_mm`` (what will be applied, in print order),
            ``requested_offsets_mm``, ``clamped``, ``scale``,
            ``sideways_travel_mm`` as ``{min, max}`` -- None when the printer
            model or the medium is unknown, since not knowing the limit is no
            reason to invent one -- and ``bleed``, which reports the medium's
            real non-printable margin across the tape as well as how much of it
            is in use. There is no feed-axis counterpart: bleed never lengthens
            the raster.

            The travel already accounts for the bleed, and has to: bleed
            widens the raster, and a wider raster has less room left to move
            inside the same print head.

        Raises:
            ValidationError: For bad sweep parameters.
        """
        requested = self.plan_calibration_offsets(settings, sweep)
        label_size = str(settings.get("label_size") or "")
        applied_offsets: List[Dict[str, float]] = []
        clamped = False
        travel: Optional[Dict[str, float]] = None

        for offset in requested:
            probe = dict(settings)
            probe["calibration"] = {label_size: dict(offset)}
            applied = applied_calibration_offset(probe, label_size)
            applied_offsets.append({"x_mm": applied.x_mm, "y_mm": applied.y_mm})
            clamped = clamped or applied.was_clamped
            if applied.travel_mm is not None:
                # The travel is a property of the medium and the printer, so
                # every step reports the same pair.
                travel = {"min": applied.travel_mm[0], "max": applied.travel_mm[1]}

        # Reported here because this is the one endpoint that already answers
        # "what can this medium on this printer actually do?", and a UI offering
        # a bleed control needs the per-medium ceiling from somewhere: 2.03 mm
        # on d24, 2.96 mm on d58, 1.02 mm on 62 mm tape once the print head has
        # had its say.
        # The ceiling is looked up directly rather than read off the resolved
        # bleed: get_label_bleed short-circuits on a zero request (it runs on
        # every geometry lookup, several times a render), so with no bleed
        # configured it has no limits to report -- and "no bleed set" is exactly
        # when a UI most needs to know how much is available.
        bleed = get_label_bleed(settings, label_size)
        limit = _bleed_limit_dots(label_size, str(settings.get("printer_model") or ""))
        limit_mm = round(limit / DOTS_PER_MM, 2) if limit is not None else None
        return {
            "offsets_mm": applied_offsets,
            "requested_offsets_mm": requested,
            "clamped": clamped,
            "sideways_travel_mm": travel,
            "scale": get_calibration_scale(settings, label_size),
            "bleed": {
                "requested_mm": bleed.requested_mm,
                "applied_mm": bleed.applied_mm,
                "limit_mm": limit_mm,
                "clamped": bleed.was_clamped,
            },
        }

    def print_calibration_target(self, settings: Dict[str, Any],
                                 sweep: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Print the calibration target on the configured medium.

        This is the one print whose whole purpose is to show where the ink
        lands, so it goes out with the calibration offset applied -- via the
        same ``_send_to_printer`` funnel as every other job, so what the user
        judges is exactly what a real label would do.

        Rotation is deliberately ignored: the target's axes are the printer's
        axes, and turning it would turn the readout with it.

        Args:
            settings: Print settings (label size, printer, calibration map).
            sweep: Optional sweep description, see
                :meth:`plan_calibration_offsets`. Each step prints one label,
                so ``copies`` is forced to 1 for a sweep.

        Returns:
            Dict with ``success``, ``job_id``, ``message``, ``label_size`` and
            the fields of :meth:`describe_calibration_run`: the ``offsets_mm``
            actually applied, in print order, what was ``requested``, whether
            anything was ``clamped``, the travel available and the ``scale``.

        Raises:
            ValidationError: For a missing label size or bad sweep parameters.
            PrinterError: If a target could not be rendered or printed.
        """
        temp_files: List[str] = []
        try:
            job_id = f"calibration_{uuid.uuid4().hex[:8]}"
            label_size = settings.get("label_size")
            if not label_size:
                raise ValidationError("label_size is required", "label_size")

            described = self.describe_calibration_run(settings, sweep)
            offsets = described["requested_offsets_mm"]
            scale = get_calibration_scale(settings, label_size)
            logger.info("Processing calibration print request",
                        job_id=job_id, label_size=label_size, labels=len(offsets),
                        scale=scale, clamped=described["clamped"])

            for index, offset in enumerate(offsets, start=1):
                # Each label is printed with its own offset, and the target
                # prints that same offset on itself -- otherwise a handful of
                # calibration labels on a desk are indistinguishable. The size
                # correction is carried across too: it is not swept, but a
                # target printed at a different size from the labels it is
                # calibrating would be measuring the wrong thing.
                step_settings = dict(settings)
                step_entry = dict(offset)
                if round(scale, 4) != DEFAULT_CALIBRATION_SCALE:
                    step_entry["scale"] = scale
                step_settings["calibration"] = {str(label_size): step_entry}
                note = f"#{index}" if len(offsets) > 1 else None
                if len(offsets) > 1:
                    step_settings["copies"] = 1

                image_path = self._create_calibration_label(step_settings, note=note)
                temp_files.append(image_path)
                self._send_to_printer(image_path, step_settings)
                logger.info("Calibration target sent to printer",
                            job_id=job_id, step=index, total=len(offsets),
                            offset_mm=offset)

            return {
                "success": True,
                "job_id": job_id,
                "message": (f"Calibration sweep printed ({len(offsets)} labels)"
                            if len(offsets) > 1 else "Calibration target printed"),
                "label_size": label_size,
                **described,
            }
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for calibration print", error=str(e))
            if isinstance(e, ValidationError):
                raise
            raise ValidationError(f"Error printing calibration target: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing calibration target", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing calibration target: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)

    def render_calibration_preview(self, settings: Dict[str, Any]) -> str:
        """
        Render the calibration target as a base64 PNG data URL (no printing).

        This is the one preview in the app that *does* show the calibration
        offset. Every other preview stands for the design the user means to
        have, and calibration exists to make the paper match it; but the
        target's entire subject is where the ink lands, so a preview of it
        without the shift would be a picture of the wrong thing.

        Args:
            settings: Print settings selecting the medium and the offset.

        Returns:
            The rendered target as a ``data:image/png;base64,...`` URL.

        Raises:
            ValidationError: For invalid input/settings (-> 400).
            PrinterError: For render failures (-> 500).
        """
        try:
            label_size = settings.get("label_size")
            if not label_size:
                raise ValidationError("label_size is required", "label_size")

            job_id = f"preview_calibration_{uuid.uuid4().hex[:8]}"
            logger.info("Rendering calibration preview",
                        job_id=job_id, label_size=label_size)

            image = self._render_calibration_target(settings)
            # A picture, not a print: both axes are simulated inside the canvas
            # so the user can see where the ink is heading relative to the
            # label. The printer moves the raster sideways instead (see
            # :func:`plan_raster_placement`), so a sideways offset the picture
            # shows running off the edge is really ink landing off the die cut
            # rather than ink the head refuses to lay down.
            #
            # The *applied* offset is what is drawn, for the same reason the
            # caption names it: the picture carries that caption, and a picture
            # that disagrees with its own caption is worse than either. Scale
            # and order follow the print path exactly, so the preview of a
            # target is the target.
            applied = applied_calibration_offset(settings, label_size)
            dx, dy = calibration_offset_px(applied.x_mm, applied.y_mm)
            image = self._scale_within_canvas(
                image, get_calibration_scale(settings, label_size), label_size)
            image = self._shift_within_canvas(image, dx, dy, label_size)
            data_url = self._encode_png_data_url(self._to_print_appearance(image, settings))

            logger.info("Calibration preview rendered", job_id=job_id)
            return data_url
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid input for calibration preview", error=str(e))
            if isinstance(e, ValidationError):
                raise
            raise ValidationError(f"Error rendering calibration preview: {str(e)}") from e
        except Exception as e:
            logger.error("Error rendering calibration preview", error=str(e), exc_info=True)
            raise PrinterError(f"Error rendering calibration preview: {str(e)}") from e

    def _shift_within_canvas(self, img: "Image.Image", dx: int, dy: int,
                             label_size: Optional[str] = None) -> "Image.Image":
        """
        Translate a finished label image inside a canvas of the same size.

        This is how the *feed* axis of a calibration offset is applied, and on
        the print path it is used for that axis only. The asymmetry is
        deliberate rather than an oversight:

        * Sideways, the printer has a lever. The raster is pasted into a device
          row wider than the label, so moving the paste position relocates the
          whole label on the tape and loses nothing -- see
          :func:`plan_raster_placement`.
        * Along the feed there is no such lever. The raster starts where the
          feed starts; the row above the first row of the label is not part of
          the label. A vertical offset therefore has to move the content inside
          the canvas, and the canvas cannot grow to absorb it: ``convert()``
          rejects any die-cut image that is not exactly the label's printable
          size, so a canvas one millimetre taller would simply refuse to print.

        Content pushed past an edge is consequently clipped, and clipping is
        logged with the amount, because a label that quietly comes out with a
        corner missing is worse than one the log warned about.

        Alpha and palette images are flattened onto white first, the same way
        ``convert()`` does on its way to the printer, so a transparent
        background does not turn into a black one when it is pasted.

        Args:
            img: The finished label image, at the medium's printable size.
            dx: Horizontal translation in pixels; positive moves right.
            dy: Vertical translation in pixels; positive moves down (later in
                the feed).
            label_size: Label identifier, for the log only.

        Returns:
            The shifted image, or ``img`` unchanged when nothing moves.
        """
        if not dx and not dy:
            return img

        img = _flattened_onto_white(img)

        lost = _clipping_losses(img, img, dx, dy)
        if any(lost.values()):
            logger.warning(
                "Calibration offset clips part of the label",
                label_size=str(label_size),
                offset_px=(dx, dy),
                offset_mm=(round(dx / DOTS_PER_MM, 2), round(dy / DOTS_PER_MM, 2)),
                clipped_px=lost,
                clipped_mm={side: round(px / DOTS_PER_MM, 2)
                            for side, px in lost.items()},
            )

        canvas = Image.new(img.mode, img.size, (255,) * len(img.mode))
        canvas.paste(img, (dx, dy))
        logger.debug("Shifted label within its canvas",
                     label_size=str(label_size), offset_px=(dx, dy),
                     canvas_size=canvas.size)
        return canvas

    def _scale_within_canvas(self, img: "Image.Image", scale: float,
                             label_size: Optional[str] = None) -> "Image.Image":
        """
        Resize a finished label's content about the centre of its own canvas.

        This is the size half of a calibration entry: it corrects a printer
        that lays ink down slightly larger or smaller than it was asked to, so
        it belongs on the print path only, exactly like the offsets.

        The canvas keeps its dimensions -- ``convert()`` rejects any die-cut
        image that is not exactly the label's printable size -- so the content
        is resized and re-centred inside it. Scaling down leaves white margin
        all round; scaling up crops at the rim and warns with the amount, the
        same bargain the feed axis makes.

        On round media the printable area is the circle inscribed in that
        square canvas, so ink can leave the label without leaving the canvas.
        Growing the content is therefore also checked against the die cut, and
        ink pushed outside it is reported even when the canvas kept every dot:
        that ink lands on the backing paper, which is no better than losing it.

        Args:
            img: The finished label image, at the medium's printable size.
            scale: Multiplier about the centre. 1.0 returns ``img`` untouched,
                so an uncalibrated print is byte-identical.
            label_size: Label identifier, for the log only.

        Returns:
            The rescaled image on a canvas of the original size, or ``img``
            unchanged when there is nothing to do.
        """
        if round(scale, 4) == DEFAULT_CALIBRATION_SCALE:
            return img

        img = _flattened_onto_white(img)
        # At least one pixel each way: a label with no dots in it is not a
        # smaller label, it is a blank one.
        content = img.resize(
            (max(1, int(round(img.width * scale))),
             max(1, int(round(img.height * scale)))),
            Image.LANCZOS,
        )
        # Centred to within a dot. The remainder of an odd difference goes to
        # the left/top, which is where int() sends it consistently for both
        # axes rather than drifting with the parity of the label.
        left = (img.width - content.width) // 2
        top = (img.height - content.height) // 2

        canvas = Image.new(img.mode, img.size, (255,) * len(img.mode))
        canvas.paste(content, (left, top))

        lost = _clipping_losses(img, content, left, top)
        # Only round media has a printable area smaller than its canvas. Asking
        # the catalogue rather than measuring the canvas: 23x23 is square and
        # rectangular, and its corners print.
        #
        # No settings are passed, and none are needed: the question here is
        # "is this medium round?", which no bleed changes, and the circle being
        # measured against is the one inscribed in whatever canvas arrived --
        # already the bled one when there is a bleed.
        outside_circle = 0
        if get_label_geometry(label_size).is_round:
            outside_circle = max(0, _ink_outside_die_cut_px(canvas)
                                 - _ink_outside_die_cut_px(img))
        if any(lost.values()) or outside_circle:
            logger.warning(
                "Calibration scale clips part of the label",
                label_size=str(label_size), scale=scale,
                clipped_px=lost,
                clipped_mm={side: round(px / DOTS_PER_MM, 2)
                            for side, px in lost.items()},
                outside_die_cut_px=outside_circle,
            )

        logger.debug("Scaled label within its canvas",
                     label_size=str(label_size), scale=scale,
                     content_size=content.size, canvas_size=canvas.size)
        return canvas

    def _reject_transposed_die_cut(self, image_path: str, settings: Dict[str, Any],
                                   bleed: LabelBleed) -> None:
        """
        Turn a rotated die-cut label into an explanation rather than a mismatch.

        A die-cut label is a fixed piece of paper, so a 90 or 270 degree
        rotation transposes the canvas into a shape ``convert()`` will not
        accept. The app has always behaved this way for rectangular die-cut
        media -- 62x29 rotated a quarter turn has never printed -- but the
        message it produced was ``Bad image dimensions: (271, 696). Expecting:
        (696, 271).``, which says nothing about rotation and arrives as a 500.

        It matters more now, because bleed makes a *round* label non-square too:
        a bled d24 is 284 x 236, so a quarter turn breaks it exactly as it
        breaks a rectangular one, where an unbled d24 was square and survived.
        That is a real narrowing and the user is entitled to be told which of
        their settings caused it rather than being shown two tuples.

        Nothing is repaired here on purpose. Silently re-fitting the rotated
        design would change a refusal into a squashed label, which is a
        different decision with different consequences and should be taken
        deliberately rather than as a side effect of adding bleed.

        Args:
            image_path: The rendered label about to be converted.
            settings: Print settings, for the medium and the rotation.
            bleed: The bleed in force, so the message can name it when it is
                what turned a square canvas into an oblong one.

        Raises:
            ValidationError: If the image is the transpose of the label canvas.
        """
        label_size = settings.get("label_size")
        geometry = get_label_geometry(label_size, settings)
        if not (geometry.is_die_cut and geometry.height):
            return
        try:
            with Image.open(image_path) as opened:
                size = opened.size
        except Exception:  # noqa: BLE001 - let convert() report a bad file
            return
        if size != (geometry.height, geometry.width) or geometry.width == geometry.height:
            return

        rotate = settings.get("rotate", 0)
        because = (f" A bleed of {bleed.applied_mm} mm makes this label "
                   f"{geometry.width}x{geometry.height} rather than square, so a "
                   f"quarter turn no longer fits it." if bleed.dots else "")
        raise ValidationError(
            f"Cannot rotate a {label_size} label by {rotate} degrees: the label "
            f"is a fixed {geometry.width}x{geometry.height} dots and a quarter "
            f"turn would make the design {size[0]}x{size[1]}.{because} Use "
            f"rotate 0 or 180, or design the label the other way round.",
            "rotate",
        )

    def _send_to_printer(self, image_path: str, settings: Dict[str, Any]) -> None:
        """
        Send an image to the printer.

        Args:
            image_path: Path to the image file.
            settings: Dict containing print settings.

        Raises:
            PrinterError: If there's an error sending to the printer.
        """
        # Extract settings
        printer_uri = settings.get("printer_uri")
        printer_model = settings.get("printer_model")
        label_size = settings.get("label_size")
        # settings["rotate"] is deliberately not read here: callers apply the
        # rotation to the image themselves before handing it over.
        dither = settings.get("dither", False)
        compress = settings.get("compress", False)
        red = settings.get("red", False)
        copies = settings.get("copies", 1)
        cut_mode = settings.get("cut_mode", "each")
        dpi_600 = settings.get("dpi_600", False)
        hq = settings.get("hq", True)

        # --- Input/validation phase (-> ValidationError -> 400) ---
        # Bad/missing settings and disallowed/SSRF URIs are caller mistakes,
        # not printer faults, so they are classified as validation errors.
        try:
            threshold = float(settings.get("threshold", 70.0))

            # Validate required settings
            if not printer_uri:
                raise ValueError("printer_uri is required")
            if not printer_model:
                raise ValueError("printer_model is required")
            if not label_size:
                raise ValueError("label_size is required")

            # Defense in depth: validate the destination URI immediately before
            # handing it to the backend. Rejects disallowed schemes (file://,
            # lpt://, ...) and SSRF/metadata targets even if settings validation
            # was bypassed. Private/LAN IPs and hostnames stay valid.
            validate_printer_uri(printer_uri)

            # Copies and cut mode.
            try:
                copies = int(copies)
            except (TypeError, ValueError):
                raise ValueError("copies must be an integer")
            if copies < 1 or copies > 100:
                raise ValueError("copies must be between 1 and 100")
            if cut_mode not in ("each", "end", "none"):
                raise ValueError("cut_mode must be one of: each, end, none")
        except (ValidationError, ValueError) as e:
            logger.warning("Invalid print settings", error=str(e))
            raise ValidationError(f"Invalid print settings: {str(e)}") from e

        # A quarter turn on a die-cut label is the caller's mistake, not the
        # printer's fault, so it is refused here -- outside the block below,
        # which classifies everything it catches as a printer error.
        bleed = get_label_bleed(settings, label_size, warn=True)
        self._reject_transposed_die_cut(image_path, settings, bleed)

        # --- Printer/IO phase (-> PrinterError -> 500) ---
        try:
            # Calibration is applied here and nowhere else. Every print path
            # (text, image, QR, text+image, PDF pages) funnels through this
            # method, so one offset covers every content type -- and because the
            # rotation has already been applied by the caller, the offset is
            # expressed in the raster that actually reaches the printer.
            #
            # The three corrections reach the printer by different routes,
            # because the printer offers a lever for one and not for the rest:
            #
            #   scale  resizes the content about the centre of the label's own
            #          canvas, which may not grow.
            #   x      moves the whole raster within the device row, by way of
            #          the paste position convert() computes
            #          (plan_raster_placement). Nothing is clipped; the travel
            #          is bounded by the head.
            #   y      moves the content inside the label's own canvas, because
            #          the raster begins where the feed begins. Content pushed
            #          past an edge is clipped, and said so.
            #
            # Order: scale first, then the offsets. Both are corrections of
            # different things -- how big the ink comes out, and where it lands
            # -- and they have to stay independent dials. Scaling about the
            # centre after an offset would multiply that offset too (4 mm at
            # 0.98 becomes 3.92 mm), so correcting the size would silently
            # un-correct the alignment the user had just measured, and each
            # dial would have to be re-measured whenever the other moved.
            # Scaling first leaves the content centred where it started, and
            # the offset then moves it by exactly the distance requested.
            #
            # The previews deliberately do NOT get this treatment: a preview
            # answers "is my design right?" and stands for the label the user
            # means to have. Calibration exists precisely so the paper ends up
            # matching that preview, so shifting the preview too would leave the
            # user chasing a moving target. Preview = intent, print = intent +
            # calibration.
            #
            # Bleed is the exception that proves that rule, and it is NOT one of
            # the three above. It does not correct anything; it enlarges the
            # label the user gets to design on, so it has already been applied
            # by the render path (via get_label_geometry) to the image that
            # arrived here, and it shows in the previews for the same reason the
            # label size does. All this method adds is telling convert() about
            # it, below, through the same lock-protected publication the
            # sideways offset uses.
            #
            # With no offset and no bleed configured the original file is handed
            # to convert() untouched and the media table is not touched at all,
            # so an uncalibrated install produces byte-identical instructions to
            # before this existed.
            print_source: Union[str, "Image.Image"] = image_path
            x_mm, y_mm = get_calibration_offset(settings, label_size)
            scale = get_calibration_scale(settings, label_size)
            dx, dy = calibration_offset_px(x_mm, y_mm)
            if dy or round(scale, 4) != DEFAULT_CALIBRATION_SCALE:
                with Image.open(image_path) as opened:
                    rendered = opened.copy()
                rendered = self._scale_within_canvas(rendered, scale, label_size)
                print_source = self._shift_within_canvas(rendered, 0, dy, label_size)

            # One image per copy; the cut mode decides how the rasterizer cuts.
            images = [print_source] * copies
            if cut_mode == "none":
                qlr = BrotherQLRaster(printer_model)
                cut = False
            elif cut_mode == "end" and copies > 1:
                # Cut a single time after the last of the N labels.
                qlr = _CutAtEndRaster(printer_model, copies)
                cut = True
            else:  # "each" (and "end" with a single label, where they coincide)
                qlr = BrotherQLRaster(printer_model)
                cut = True
            qlr.exception_on_warning = True

            # Convert image(s) to printer instructions.
            #
            # rotate=0, NOT the caller's value: every path into this method has
            # already rotated the image with _apply_rotation. Passing the angle
            # on makes convert() rotate a second time, returning the image to
            # its original orientation before fitting it to the tape -- so the
            # logs report "Rotation applied" while the label comes out
            # unrotated.
            #
            # The sideways half of the calibration and the bleed both live in
            # what the context manager publishes for the length of this call,
            # and are undone again the moment it returns -- including when it
            # raises.
            #
            # The bleed published here only ever widens. An earlier version
            # lengthened the raster too, on the reasoning that a round label
            # needs to grow equally on both axes to stay round; printing it
            # showed that reasoning is beaten by a mechanical fact. Each raster
            # line is one step of the feed, so the line count is the distance
            # the media travels while the page prints. Adding 48 lines to a d24
            # added 4 mm to that travel, the cut walked off the die-cut gap and
            # the roll lost registration and had to be re-seated. Nothing in the
            # stream can give the steps back either: ESC i d (add_margins) is
            # the only feed lever, it carries the label's own feed_margin -- 0
            # for d24 -- and it is packed unsigned.
            #
            # So the length passed to convert() is the medium's own, always, and
            # a bled job emits exactly as many raster lines as an unbled one.
            # The round label is simply not square any more, and its printable
            # area is the inscribed ellipse; get_round_safe_axes handles that
            # directly rather than retreating to the smaller circle.
            with placed_raster(qlr, label_size, dx, bleed) as placement:
                instructions = convert(
                    qlr=qlr,
                    images=images,
                    label=label_size,
                    rotate=0,
                    threshold=threshold,
                    dither=dither,
                    compress=compress,
                    red=red,
                    cut=cut,
                    dpi_600=dpi_600,
                    hq=hq,
                )

            # Send to printer (serialized against the keep-alive heartbeat).
            with self._io_lock:
                backend = backend_factory(guess_backend(printer_uri))["backend_class"](printer_uri)
                try:
                    # Counted before the write, not after: from here on bytes may
                    # have reached the printer, and a caller retrying this job
                    # would print them a second time. See ``_write_attempts``.
                    self._write_attempts += 1
                    backend.write(instructions)
                finally:
                    # In a finally because a failed write must still hand the
                    # socket back. The printer accepts one connection on 9100 at
                    # a time, so a socket left hanging off a traceback is the
                    # next attempt's "printer busy".
                    backend.dispose()

            # Record print activity so the "timed" keep-alive mode extends its
            # awake window from this moment. From here on the timestamp really is
            # a print, which is what lets the status endpoint say so instead of
            # saying "since the app started".
            self._last_print_at = time.time()
            self._printed_since_start = True
            # Same origin for the relay's turn-off clock. Both windows have to be
            # measured from the same instant or they drift apart, so the two
            # timestamps are taken here together rather than each where it is
            # convenient.
            relay_service.note_print_activity(self._last_print_at)

            logger.info("Print job sent to printer",
                       printer_uri=printer_uri,
                       printer_model=printer_model,
                       label_size=label_size,
                       copies=copies,
                       cut_mode=cut_mode,
                       calibration_mm=(x_mm, y_mm),
                       calibration_x_px=(placement.applied_dots if placement else 0),
                       calibration_y_px=dy,
                       calibration_scale=scale,
                       bleed_mm=bleed.applied_mm,
                       bleed_px=bleed.dots)
        except Exception as e:
            logger.error("Error sending to printer", error=str(e), exc_info=True)
            raise PrinterError(f"Error sending to printer: {str(e)}") from e

    def writes_begun(self) -> int:
        """How many raster writes this process has started, ever.

        Only the *change* across an operation means anything: read it before a
        print and again after it failed, and an unchanged number says nothing
        reached the printer, so repeating the job cannot print anything twice.
        See :attr:`_write_attempts` for why it counts attempts rather than
        successes.
        """
        return self._write_attempts

    def last_print_origin(self) -> Tuple[float, bool]:
        """Return the moment every timed window is measured from, and its nature.

        This is the single origin the whole timing chain hangs off: the
        "timed" keep-alive window is measured from it (see
        ``_keep_alive_worker``) and so is the relay's turn-off clock, which is
        why they are set together in the print path rather than each where it
        happened to be convenient.

        The second element is what stops the first from being misread. The
        timestamp is initialised to the process start time on purpose, so that
        switching keep-alive on gives one window immediately, which means it is
        *not* always "when something was last printed". A caller rendering it as
        "Last print 13:42" when nothing has printed since the container came up
        would be stating something that never happened.

        Returns:
            ``(timestamp, printed)``: the unix timestamp the window runs from,
            and whether it is a real print (True) or still the startup fallback
            (False).
        """
        return self._last_print_at, self._printed_since_start

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

            # Disable keep alive if backend is not 'network'
            if guess_backend(printer_uri) != "network":
                logger.info("Keep alive disabled: backend is not 'network'", printer_uri=printer_uri, backend=guess_backend(printer_uri))
                return {
                    "success": False,
                    "message": f"Keep alive is not useful for non-network backends"
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
        # Track intermediate artifacts so they can be cleaned up afterwards.
        temp_files: List[str] = []
        try:
            # Generate a unique job ID
            job_id = f"qrcode_{uuid.uuid4().hex[:8]}"

            logger.info("Processing QR code print request", job_id=job_id, data_length=len(data))

            # Create QR code image
            image_path = self._create_qr_code(data, settings)
            temp_files.append(image_path)
            logger.info("QR code created", job_id=job_id, image_path=image_path)

            # Apply rotation if specified
            rotate = settings.get("rotate", 0)
            if rotate != 0:
                image_path = self._apply_rotation(image_path, rotate)
                temp_files.append(image_path)
                logger.info("Rotation applied", job_id=job_id, rotate=rotate)

            # Send to printer
            self._send_to_printer(image_path, settings)
            logger.info("Print job completed successfully", job_id=job_id)

            return {
                "success": True,
                "job_id": job_id,
                "message": "QR code printed successfully"
            }
        except (ValidationError, ValueError) as e:
            # Pure input/validation problems (bad settings, invalid URI, ...)
            # must surface as a client error (-> 400), not a printer fault.
            logger.warning("Invalid input for QR code print", error=str(e))
            raise ValidationError(f"Error printing QR code: {str(e)}") from e
        except Exception as e:
            logger.error("Error printing QR code", error=str(e), exc_info=True)
            raise PrinterError(f"Error printing QR code: {str(e)}") from e
        finally:
            self._cleanup_temp_files(temp_files)
    
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
            # 1. Render the bare QR code (encoding + optional resize).
            qr_img = self._generate_qr_image(data, settings)

            # 2. Compose with text according to the requested layout
            #    (side-by-side, or text above/below). No-op if no text.
            qr_img = self._compose_qr_with_text(qr_img, data, settings)

            # 3. Fit the composition to the loaded media. The QR code is
            #    rendered at whatever qr_size asks for, which is a size the
            #    printer knows nothing about: on continuous tape it has to be
            #    scaled to the printable width, and on a die-cut label it has to
            #    land on the label's exact canvas -- convert() refuses anything
            #    else outright, so without this a round label could never print.
            #
            #    The white ring qr_border draws around the modules is the
            #    scanner's quiet zone, not slack: a fit that measured the ink
            #    would grow the modules into it and hand back a symbol that no
            #    longer reads. So this one composition is fitted by its
            #    rectangle, as every path was before.
            qr_img = self._fit_to_label(qr_img, settings.get("label_size"), settings,
                                        margin_is_content=True)

            # 4. Persist the result.
            image_path = os.path.join(self.upload_folder, f"qrcode_{uuid.uuid4().hex[:8]}.png")
            qr_img.save(image_path)

            return image_path
        except Exception as e:
            logger.error("Error creating QR code", error=str(e), exc_info=True)
            raise ImageProcessingError(f"Error creating QR code: {str(e)}")

    def _generate_qr_image(self, data: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Encode ``data`` into a QR code image and resize it to the configured
        overall size (maintaining aspect ratio).

        Returns:
            The rendered QR code as an RGB ``PIL.Image``.
        """
        # Extract QR code settings
        qr_version = settings.get("qr_version", 1)  # QR code version (1-40)
        qr_box_size = settings.get("qr_box_size", 10)  # Size of each box in pixels
        qr_border = settings.get("qr_border", 4)  # Border size in boxes
        error_correction = settings.get("error_correction", "M")  # L, M, Q, H

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

        return qr_img

    def _compose_qr_with_text(self, qr_img: Image.Image, data: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Combine the rendered QR code with any configured text.

        Dispatches to the side-by-side layout when requested (and side text is
        present), otherwise to the text-above/below layout when text display is
        enabled. If neither applies the QR image is returned unchanged.
        """
        show_text = settings.get("show_text", False)
        text = settings.get("text", data)  # Use data as default text if not provided

        # Layout settings
        side_by_side = settings.get("side_by_side", False)  # Whether to show text and QR code side by side
        side_text = settings.get("side_text", "")  # Text to show on the side

        # Check if we should use side-by-side layout
        if side_by_side and side_text:
            return self._layout_side_by_side(qr_img, side_text, settings)
        # If text should be shown with the QR code (only if not using side-by-side)
        elif show_text and text:
            return self._layout_text_above_below(qr_img, text, settings)

        return qr_img

    def _layout_side_by_side(self, qr_img: Image.Image, side_text: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Place the QR code and multi-line text side by side (text 2/3, QR 1/3).

        ``qr_position`` selects whether the QR sits on the left or the right;
        ``text_alignment`` controls horizontal alignment of the text block.
        """
        text_alignment = settings.get("text_alignment", "center")  # Text alignment: "left", "center", or "right"
        qr_position = settings.get("qr_position", "right")  # Position of QR code: "left" or "right"

        # Get QR code dimensions
        qr_width, qr_height = qr_img.size

        # Use text_font_size if provided, otherwise fall back to font_size or default
        text_font_size = settings.get("text_font_size", settings.get("font_size", 30))
        font = ImageFont.truetype(self.font_path, text_font_size)

        # Fix the label to the loaded roll's printable width, split into a text
        # column (2/3) and a QR column (1/3), then wrap the text to its column
        # (default on) so long names stay readable instead of being truncated or
        # ballooning the label. Disable with settings.text_wrap = false.
        padding = 20
        total_width = get_label_width(settings.get("label_size"), settings)
        text_area_width = int(total_width * 2 / 3) - padding * 2
        qr_area_width = total_width - text_area_width - padding * 3

        if settings.get("text_wrap", True):
            side_text_lines = self._wrap_text_to_width(side_text, font, text_area_width)
        else:
            side_text_lines = side_text.split('\n')

        # Measure each (wrapped) line.
        text_metrics = []
        total_text_height = 0
        line_spacing = 10
        dummy_draw = ImageDraw.Draw(qr_img)
        for line in side_text_lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            text_metrics.append((line, line_width, line_height))
            total_text_height += line_height + line_spacing
        # Remove extra line spacing after the last line.
        total_text_height -= line_spacing

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

        return new_img

    def _layout_text_above_below(self, qr_img: Image.Image, text: str, settings: Dict[str, Any]) -> Image.Image:
        """
        Stack a single line of text above or below the QR code.

        ``text_position`` ("top"/"bottom") selects placement; ``text_alignment``
        controls horizontal alignment.
        """
        text_position = settings.get("text_position", "bottom")  # Position of text: "top", "bottom", or "none"
        text_alignment = settings.get("text_alignment", "center")  # Text alignment: "left", "center", or "right"

        # Get QR code dimensions
        qr_width, qr_height = qr_img.size

        # Create a new image with space for text
        # Use text_font_size if provided, otherwise fall back to font_size or default
        text_font_size = settings.get("text_font_size", settings.get("font_size", 30))
        font = ImageFont.truetype(self.font_path, text_font_size)

        # Wrap the caption to the QR width (default on) so long captions are
        # never truncated; disable with settings.text_wrap = false.
        margin = 10
        if settings.get("text_wrap", True):
            text_lines = self._wrap_text_to_width(text, font, qr_width - margin * 2)
        else:
            text_lines = text.split('\n')

        # Measure each line of the (possibly wrapped) caption.
        dummy_draw = ImageDraw.Draw(qr_img)
        line_spacing = 6
        line_metrics = []
        text_block_height = 0
        for line in text_lines:
            bbox = dummy_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            if not line:
                line_height = sum(font.getmetrics())  # give blank lines a real gap
            line_metrics.append((line, line_width, line_height))
            text_block_height += line_height + line_spacing
        text_block_height = max(0, text_block_height - line_spacing)

        padding = 20  # Padding between QR code and text
        new_height = qr_height + text_block_height + padding
        new_img = Image.new("RGB", (qr_width, new_height), "white")
        draw = ImageDraw.Draw(new_img)

        if text_position == "top":
            text_top = padding // 2
            qr_top = text_block_height + padding
        else:  # bottom (default)
            qr_top = 0
            text_top = qr_height + padding // 2

        new_img.paste(qr_img, (0, qr_top))

        # Draw each caption line with the requested horizontal alignment.
        y = text_top
        for line, line_width, line_height in line_metrics:
            if text_alignment == "center":
                x = (qr_width - line_width) // 2
            elif text_alignment == "right":
                x = qr_width - line_width - margin
            else:  # left alignment
                x = margin
            draw.text((x, y), line, font=font, fill="black")
            y += line_height + line_spacing

        return new_img
    
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
    
    def _get_ipp_port(self) -> int:
        """IPP port used for status/keep-alive. Defaults to the IANA-standard
        631 (the same on virtually all Brother network printers) but can be
        overridden via the ``ipp_port`` setting for non-standard setups."""
        try:
            return int(settings_service.get_settings().get("ipp_port", 631) or 631)
        except Exception:  # noqa: BLE001 - an unreadable setting must not stop a status check
            return 631

    def _build_clock_info(self, printer_time: Any) -> Dict[str, Any]:
        """Compare the printer's reported clock against the server clock (UTC).

        ``printer_time`` is whatever the IPP response carried for
        printer-current-time. That is normally a datetime, but a printer may
        answer with a string, an out-of-band "unknown", or a type this client
        does not decode. The clock is a readout beside the status, so anything
        unusable becomes a note rather than an exception: a device that reports
        its time oddly must not make the whole status check fail.

        Args:
            printer_time: The reported clock, as a datetime, a string, or None.

        Returns:
            The clock block for the status payload, always well-formed.
        """
        now = datetime.now(timezone.utc)
        info: Dict[str, Any] = {
            "server_time": now.isoformat(timespec="seconds"),
            "printer_time": None,
            "drift_seconds": None,
            "in_sync": None,
            "note": None,
        }
        if printer_time is None:
            info["note"] = "Printer did not report a clock"
            return info
        if not isinstance(printer_time, datetime):
            text = str(printer_time).strip()
            try:
                printer_time = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("Printer reported an unreadable clock",
                             reported=text[:64])
                info["printer_time"] = text[:64] or None
                info["note"] = "Printer reported a clock this app could not read"
                return info
        if printer_time.tzinfo is None:
            # A naive reading is the printer's local time; comparing it to UTC
            # would invent a drift of the timezone offset.
            info["printer_time"] = printer_time.isoformat(timespec="seconds")
            info["note"] = "Printer reported a clock without a timezone"
            return info
        info["printer_time"] = printer_time.isoformat(timespec="seconds")
        drift = (printer_time.astimezone(timezone.utc) - now).total_seconds()
        info["drift_seconds"] = round(drift, 1)
        info["in_sync"] = abs(drift) <= 120  # within 2 minutes (UTC compare)
        if not info["in_sync"]:
            info["note"] = (
                "Printer clock differs from server time (UTC). It cannot be set "
                "remotely; adjust it on the device LCD / Brother Printer Setting "
                "Tool, check the CR2032 backup battery, and verify the timezone "
                "in the printer web UI."
            )
        return info

    def _ipp_ping(self, ip_address: str) -> bool:
        """Keep-alive/reachability probe via IPP Get-Printer-Attributes (TCP 631).

        Unlike a bare TCP connect this is a real request/response round-trip and
        is the only working status channel on Brother QL models with SNMP off.
        """
        try:
            return bool(get_printer_attributes(ip_address, port=self._get_ipp_port()).get("reachable"))
        except Exception as e:
            logger.debug("IPP ping failed", ip_address=ip_address, error=str(e))
            return False

    def _write_keepalive(self, ip_address: str, port: int = 9100, timeout: float = 3.0) -> bool:
        """Active keep-alive heartbeat: send a harmless ``ESC @`` (initialize) to
        the raster/print port (TCP 9100).

        Rationale: a status *read* (IPP/SNMP/TCP-connect) does NOT reset a Brother
        QL's auto-power-off / sleep timer — per Brother's docs only *received
        print data* does. ``ESC @`` (0x1B 0x40) is the printer-reset command; it
        prints nothing and feeds nothing, but it is real data on the print
        channel, so it is the best app-side attempt to keep the device awake.

        Serialized via ``self._io_lock`` so it never interleaves with a real
        print. If a print is already in progress the lock is held — that print is
        itself activity, so we skip this heartbeat and report success.
        """
        host = ip_address.split(":")[0] if ":" in ip_address else ip_address
        if not self._io_lock.acquire(blocking=False):
            # A print job holds the port right now -> that already keeps the
            # printer awake; treat this cycle as a successful heartbeat.
            logger.debug("Keep alive skipped: print in progress", ip_address=host)
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                if sock.connect_ex((host, port)) != 0:
                    return False
                sock.sendall(b"\x1b\x40")  # ESC @ = initialize (no print, no feed)
                return True
            finally:
                sock.close()
        except Exception as e:
            logger.debug("Keep alive write failed", ip_address=host, error=str(e))
            return False
        finally:
            self._io_lock.release()

    def _tcp_reachable(self, ip_address: str, port: int = 9100, timeout: float = 1.5) -> bool:
        """Single quick TCP connect probe (reachability only).

        Used as the status-check fallback after IPP. Unlike ``_tcp_ping`` it does
        not sweep several ports, so an offline printer fails in ~1.5s instead of
        summing multiple timeouts.
        """
        host = ip_address.split(":")[0] if ":" in ip_address else ip_address
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception:
            return False

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
    
    def _keep_alive_worker(self, printer_uri: str, printer_model: str, interval: int, stop_event: threading.Event) -> None:
        """
        Worker function for the keep alive thread.
        
        Args:
            printer_uri: URI of the printer to keep alive.
            printer_model: Model of the printer.
            interval: Time interval between pings in seconds.
            stop_event: Event to signal the thread to stop.
        """
        backend_type = guess_backend(printer_uri)
        if backend_type != "network":
            logger.info("Keep alive worker exiting: backend is not 'network'", printer_uri=printer_uri, backend=backend_type)
            return
        
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
        last_ok: Optional[bool] = None  # for state-change (INFO/WARN) logging
        last_active: Optional[bool] = None  # for timed-window pause/resume logging
        
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
                
                # Dynamic keep-alive window. In "timed" mode we only keep the
                # printer awake for a window after the last print; outside that
                # window we pause the heartbeat (letting the printer sleep) until
                # the next print resets the timer. In "forever" mode (or
                # duration<=0) we always ping. Settings are re-read each tick so
                # changes take effect live.
                #
                # How long that window really is comes from the relay service,
                # because relay power control changes the answer: the printer's
                # own auto-power-off interval is subtracted, so the heartbeat
                # stops early and the device switches itself off at exactly the
                # moment the user configured rather than that moment plus its own
                # timer. With relay power control off the configured duration is
                # returned unchanged and this behaves exactly as it always did.
                #
                # A window of 0 is a real answer (duration == the hardware
                # interval): the heartbeat then does nothing at all and the
                # printer's own timer carries the whole window.
                ka = settings_service.get_settings()
                ka_window = relay_service.effective_keep_alive_seconds(ka)
                if ka_window is not None and (time.time() - self._last_print_at) > ka_window:
                    if last_active is not False:
                        logger.info("Keep alive paused: no print within the configured window",
                                    printer_uri=printer_uri, window_seconds=ka_window)
                        last_active = False
                    stop_event.wait(interval)
                    continue
                if last_active is False:
                    logger.info("Keep alive resumed after print activity", printer_uri=printer_uri)
                last_active = True

                logger.debug("Sending keep alive ping", ip_address=ip_address)

                # PRIMARY: write a harmless ESC @ to port 9100. A status *read*
                # (IPP/SNMP/TCP-connect) does not reset the printer's
                # auto-power-off timer — only *received print data* does — so the
                # write is the only app-side attempt that can actually keep the
                # device awake. Fall back to IPP/TCP purely for reachability
                # reporting if the write channel is unavailable.
                method = None
                if self._write_keepalive(ip_address):
                    method = "raw9100"
                elif self._ipp_ping(ip_address):
                    method = "ipp"
                elif self._tcp_ping(ip_address):
                    method = "tcp"

                if method is not None:
                    consecutive_failures = 0
                    if last_ok is not True:
                        logger.info("Keep alive: printer reachable",
                                    printer_uri=printer_uri, ip_address=ip_address, method=method)
                    else:
                        logger.debug("Keep alive ping successful",
                                     printer_uri=printer_uri, ip_address=ip_address, method=method)
                    last_ok = True
                else:
                    consecutive_failures += 1
                    if last_ok is not False:
                        logger.warning("Keep alive: printer not reachable",
                                       printer_uri=printer_uri, ip_address=ip_address,
                                       consecutive_failures=consecutive_failures)
                    else:
                        logger.debug("Keep alive ping failed (repeated)",
                                     printer_uri=printer_uri, ip_address=ip_address,
                                     consecutive_failures=consecutive_failures)
                    last_ok = False
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

# Let a settings write remember which label type was chosen for the loaded
# medium. The dependency only runs this way -- this module already imports the
# settings service, and the settings service knows nothing about media -- so the
# hook is what keeps the media rules out of a module that has no business
# holding them, without a cycle and without a second write.
settings_service.register_update_hook(printer_service.record_label_choice)
