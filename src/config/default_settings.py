"""
Default settings for the Brother QL Printer App.
These settings are used when no user-defined settings are available.
"""

from typing import FrozenSet, Optional, Tuple

# Largest print offset a calibration entry may carry, per axis. The errors this
# corrects are die-cut registration tolerance and per-model raster offsets, all
# of which are a couple of millimetres at worst; a value beyond this would push
# the content clean off the smallest supported media (d12 is under 8 mm of
# printable area) instead of nudging it, so it is far more likely to be a typo
# or a wrong unit than a real correction.
CALIBRATION_LIMIT_MM = 10.0

# Range a calibration ``scale`` may take: a multiplier on the size of the
# printed content, 1.0 being "print it as rendered".
#
# It corrects a printer that lays ink down slightly larger or smaller than
# asked -- head geometry, feed rate, media thickness -- and that error is a
# fraction of a percent to a couple of percent. Five percent is already double
# the worst case, and the range is deliberately not wider than the fault it
# corrects: content is scaled about the centre of a canvas that may not grow,
# so a "correction" used as a zoom clips at the rim, and the preview stays
# where it is (calibration is a printer correction, not a design change), which
# would leave the user looking at a design that does not match the paper on
# purpose. A tight range with a clear message is the honest place to stop.
CALIBRATION_SCALE_MIN = 0.95
CALIBRATION_SCALE_MAX = 1.05
DEFAULT_CALIBRATION_SCALE = 1.0

# Largest bleed a ``bleed_mm`` entry may carry, in millimetres per side.
#
# Bleed is not a correction and has nothing to do with calibration: it enlarges
# the area the user is allowed to design in, out into the strip around every
# label that brother_ql (following Brother) declares non-printable. It widens
# the label and never lengthens it -- extending the raster along the feed moves
# the cutter off the gap between labels, which was measured on paper -- so what
# is on offer is the *width* margin: 2.03 mm on d24, at most 2.96 mm on d58.
#
# Whatever is asked for is clamped at render time to the loaded medium's real
# margin, and to the width of the print head, which on the 62 mm media is the
# tighter of the two. So this bound only has to catch the values that are a typo
# or a wrong unit rather than a request. Five millimetres is comfortably above
# every real margin and far below "somebody typed centimetres".
BLEED_LIMIT_MM = 5.0


# --------------------------------------------------------------------------- #
# Relay power control.
#
# The printer's mains supply can be switched by a relay driven over a webhook,
# so it draws nothing between print runs. Two events exist: ``turn_on``, fired
# when a print job arrives at a printer that is not answering, and ``turn_off``,
# fired once everything has wound down.
# --------------------------------------------------------------------------- #

# The auto-power-off intervals a Brother QL offers, in minutes. The device's own
# menu has exactly these six steps and no free entry, so this is an enum rather
# than a bounded number: a free field could only ever hold a value the printer
# cannot be set to, and every wrong value here is a value that makes the relay
# fire at the wrong moment.
PRINTER_AUTO_POWER_OFF_CHOICES = (10, 20, 30, 40, 50, 60)
DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES = 10

# Longest delay that may be inserted between the end of the configured window
# and the ``turn_off`` webhook. The delay exists to let the printer's own
# auto-power-off complete first, so it is measured in single-digit minutes; an
# hour is already far past "safety margin" and into "a second, hidden window".
TURN_OFF_DELAY_LIMIT_MINUTES = 60
DEFAULT_TURN_OFF_DELAY_MINUTES = 5

# The one thing about this feature the app cannot check for the user, stated in
# one place so the settings model, the OpenAPI description and the status
# endpoint all say exactly the same words.
#
# The chain is: keep-alive stops at (duration - auto_power_off), the printer's
# own timer then runs for its real length, and the relay opens at
# (duration + delay). Those line up only while the number configured here
# matches the number set on the device. If the device's real interval is LONGER
# than the configured one, the printer is still awake when the relay opens.
AUTO_POWER_OFF_MISMATCH_WARNING = (
    "This app cannot read or change the printer's built-in auto-power-off "
    "time -- the value here is a statement about the device, not a setting on "
    "it, and nothing verifies the two agree. Set it to exactly what the "
    "printer's own menu shows. If the real interval on the device is LONGER "
    "than the value configured here, the relay will cut mains power while the "
    "printer is still running, which can interrupt a print mid-feed and can "
    "damage the printer."
)


# --------------------------------------------------------------------------- #
# The media a printer cannot tell apart, and what to call each of them.
#
# Detection resolves all 15 die-cut sizes to exactly one identifier. Three
# continuous cases cannot be resolved from the device and must not be: 62/62red
# are the same geometry with the colour reported as unknown, 12/12+17 are the
# same roll addressed two ways, and 103/104 differ by about a quarter of a
# millimetre. Identification therefore returns the whole group.
#
# The table lives in this module rather than beside the identification code
# because both halves of the feature need it and this module imports nothing, so
# sharing it here cannot create an import cycle: the printer service widens a
# geometric match into the whole group, and the settings service validates the
# owned-media list and the media memory against it. A second copy in either
# place would be a second thing to keep right.
#
# THE ORDER INSIDE EACH GROUP IS LOAD-BEARING. The first member is the plain
# variant, and it is three things at once: the key the media memory is stored
# under, the documented default when nothing else resolves the group, and
# therefore what a user gets who never expresses a preference. The second member
# is always the one that costs something to pick by accident -- red ink that is
# not loaded, a 29 mm-wide raster on a 12 mm roll, a quarter-millimetre of extra
# width -- which is exactly why it is never the default.
# --------------------------------------------------------------------------- #
MEDIA_EQUIVALENTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("62", "62red"),
     "62 and 62red are the same geometry and the printer reports "
     "'mediacolor: unknown', so the red tape cannot be distinguished"),
    (("12", "12+17"),
     "12 and 12+17 are the same physical 12 mm roll; 12+17 is a rendering "
     "choice, not a medium"),
    (("103", "104"),
     "103 and 104 differ by about 0.25 mm, below any resolution the printer "
     "reports"),
)


def medium_variants(label_size: str) -> Tuple[str, ...]:
    """Every identifier that names the same physical medium as ``label_size``.

    Args:
        label_size: A label identifier, e.g. "62red".

    Returns:
        The group it belongs to in plain-variant-first order, or a 1-tuple of
        the identifier itself when it names a medium of its own.
    """
    for group, _note in MEDIA_EQUIVALENTS:
        if label_size in group:
            return group
    return (str(label_size),)


def medium_key(label_size: str) -> str:
    """The canonical name of the medium ``label_size`` is a variant of.

    This is the key the media memory is stored under, and it was chosen to be
    the one thing about an ambiguous detection that does not move:

    * not the candidate list -- that is derived from the media catalogue, and a
      catalogue edit (a new variant, a reordered table, a renamed entry) would
      silently orphan every remembered choice;
    * not the list's order or index, for the same reason and more so;
    * not a UI label, which is a translation and a wording decision away from
      changing under a memory that has to outlive it;
    * not the reported millimetres either: the device quantises them, 103.6 mm
      arrives as 104, and a key that is a float is a key that occasionally is
      not equal to itself.

    What is left is the medium itself, named by its plain variant. "62" means
    the 62 mm continuous roll however many colour variants the catalogue grows,
    and a memory of ``{"62": "62red"}`` keeps meaning "red was in use on the
    62 mm roll" whether or not brother_ql ever renames, adds or drops one.

    Args:
        label_size: A label identifier.

    Returns:
        The plain variant of its group, or the identifier itself.
    """
    return medium_variants(label_size)[0]


def supported_label_identifiers() -> Optional[FrozenSet[str]]:
    """Every label identifier this app offers, for validating a settings map.

    P-touch media is excluded for the same reason the printer service excludes
    it from matching: those are TZe tapes for a different family of machines and
    are not in this app's label enum.

    Returns:
        The identifiers, or None when the media catalogue cannot be read at all.
        None means "cannot check", and a validator that cannot check must not
        reject: refusing to save a settings file because a library is missing
        would turn a degraded install into an unusable one.
    """
    try:
        from brother_ql.labels import ALL_LABELS, FormFactor

        return frozenset(label.identifier for label in ALL_LABELS
                         if label.form_factor != FormFactor.PTOUCH_ENDLESS)
    except Exception:  # noqa: BLE001 - no catalogue means no check, not a failure
        return None


DEFAULT_SETTINGS = {
    "printer_uri": "tcp://192.168.1.100",
    "printer_model": "QL-800",
    "label_size": "62",
    "font_size": 50,
    "alignment": "left",
    "orientation": "across",  # across | lengthwise (continuous rolls only)
    "vertical_alignment": "middle",  # top | middle | bottom (no effect on continuous + across)
    "rotate": 0,
    "threshold": 70.0,
    "dither": False,
    "compress": False,
    "red": False,
    "copies": 1,
    "cut_mode": "each",  # each | end | none
    "dpi_600": False,
    "hq": True,
    "keep_alive_enabled": False,
    "keep_alive_interval": 60,  # seconds
    "keep_alive_mode": "forever",  # forever | timed
    "keep_alive_duration_seconds": 7200,  # used when mode == "timed" (default 2h)
    "ipp_port": 631,  # IPP port for network status/keep-alive (IANA standard)
    # Per-label print corrections, keyed by label identifier:
    #   {"d24": {"x_mm": -0.5, "y_mm": 1.0, "scale": 0.98}}
    # x_mm positive moves the printed content right on the tape, y_mm positive
    # moves it down (later in the feed direction). Sideways the whole raster is
    # placed further along the print head, so nothing is ever cut off -- only
    # the head's own width limits the travel; along the feed the content moves
    # inside the label's canvas, so a large offset can push content off the
    # edge. scale multiplies the size of the content about the centre of that
    # same canvas (0.98 prints 2 % smaller); scaling up can clip, and warns.
    # Applied on the print path only, never to previews. An empty map -- the
    # default -- means every label prints exactly as it always did.
    "calibration": {},
    # Per-label bleed, keyed by label identifier, in millimetres per side:
    #   {"d24": 1.5}
    # brother_ql offers only the inner part of every label as printable -- 20 mm
    # of a 24 mm round die cut, leaving 2.03 mm of paper all round that no
    # design can reach. Bleed hands some or all of that strip back across the
    # tape: the raster is rendered wider and placed so the label stays centred,
    # so content can run out to the punched edge.
    #
    # Across the tape only. Lengthening the raster instead makes the media
    # advance further per label and walks the cutter off the die-cut gap, which
    # is a measured result rather than a precaution. A bled round label is
    # therefore wider than it is long, and its printable area is an ellipse.
    #
    # Deliberately NOT part of "calibration". Calibration corrects a printer
    # that puts ink in the wrong place and therefore never touches a preview --
    # the preview is the target the correction aims at. Bleed changes how big
    # the label the user is designing is, so it *does* show in previews, like
    # the label size itself. Absent or 0 -- the default -- means the app prints
    # exactly the area it always did.
    "bleed_mm": {},
    # Follow the printer: when it reports a roll the configured label_size is
    # not consistent with, adopt the detected one.
    #
    # OFF by default, and it has to stay that way. Every other setting here
    # changes what happens when the user asks for something; this one changes a
    # stored setting -- label_size, which decides how every label is rendered --
    # without the user acting at all. A feature that edits the configuration on
    # its own has to be chosen, not inherited from a default.
    #
    # It never switches away from a label type that is already consistent with
    # what is loaded: a user printing on 62red who turns this on is not moved to
    # 62, because 62red is one of the things a 62 mm roll can be. It only acts
    # when the setting and the paper genuinely disagree.
    #
    # Where the medium is ambiguous and nothing below resolves it, this switches
    # nothing and reports the ambiguity, exactly as the manual path does. A
    # missing label is a visible error; the wrong label is a bad print.
    "media_auto_switch": False,
    # The media the user actually owns, as label identifiers:
    #   ["62red", "d24"]
    # A hint for resolving an ambiguous detection, never a filter. A medium the
    # printer reports is reported and identified whether or not it appears here
    # -- the list narrows what the app has to guess between, it does not decide
    # what may be seen. Empty (the default) means no claim is made.
    "owned_media": [],
    # The label type last settled on for each medium, keyed by the medium's
    # plain variant (see medium_key above):
    #   {"62": "62red"}
    # This is what makes automatic switching defensible on the three media the
    # printer cannot pin down. Without it, loading a 62 mm roll means guessing
    # between 62 and 62red; with it, the app is recalling what was in use last
    # time that roll was loaded rather than guessing at all.
    #
    # Written only while media_auto_switch is on, and only when a settings write
    # moves label_size to something the loaded medium could actually be -- see
    # PrinterService.record_label_choice for why that is the moment a choice
    # counts as settled.
    "media_memory": {},
    # The variant that wins for a medium, keyed the same way the memory is --
    # on the medium's plain variant (see medium_key above):
    #   {"62": "62red"}
    # "When a 62 mm roll is loaded, I mean the red one." Consulted first, ahead
    # of the memory, the owned-media list and the plain-variant default.
    #
    # It exists because ownership was supposed to settle these three media and
    # mostly cannot. 12/12+17 and 103/104 are one physical roll under two
    # identifiers, so owning "both" is not a thing anyone can do; and 62/62red is
    # only settled by ownership for a user who owns the red roll and no plain
    # one. Own a black roll and a black/red roll -- the ordinary case for the
    # only medium where guessing wrong costs a finished bad label -- and
    # ownership narrows nothing, leaving the plain-variant default to decide
    # something the user may well have an opinion about.
    #
    # Ahead of the memory deliberately. The memory is inferred: it records what
    # happened last. A preference is stated: it records what was meant. One
    # contrary pick on some Tuesday -- a single run of plain labels on the red
    # roll -- should not quietly repeal a standing instruction, and it does not:
    # the pick is still recorded (see PrinterService.record_label_choice), it
    # simply does not outrank the instruction.
    #
    # Empty by default, so an install that never sets one resolves exactly as it
    # did before: memory, then ownership, then the plain variant.
    #
    # An entry for a medium with only one variant -- {"d24": "d24"} -- is inert
    # rather than rejected: it states the only thing that medium could resolve
    # to, so it can neither change an outcome nor break one. See
    # SettingsService._validate_media_preference.
    "media_preference": {},
    # ----------------------------------------------------------------------- #
    # Relay power control via webhook.
    #
    # OFF by default, and every field below is inert while it is. An install
    # that never touches this feature makes no outbound request, keeps the
    # keep-alive timing it always had, and behaves exactly as before.
    # ----------------------------------------------------------------------- #
    "relay_webhook_enabled": False,
    # POSTed when a print job arrives and the printer does not answer. The body
    # carries {"action": "turn_on", ...}; see RelayPowerService.build_payload.
    "relay_webhook_turn_on_url": "",
    # POSTed when everything has wound down. Empty means "use the turn_on URL",
    # which is the right default for a relay that switches on the body it is
    # sent. Two separate URLs exist because plenty of relays switch on the
    # *address* instead (.../relay/0?turn=on vs .../relay/0?turn=off) and cannot
    # read a body at all.
    "relay_webhook_turn_off_url": "",
    # Sending turn_off is opt-in on its own, separately from the feature. Some
    # installations only ever want the printer woken and are content to let it
    # sleep on its own afterwards; cutting mains power is the half of this
    # feature that can go wrong, so it is not inherited from switching the
    # feature on.
    "relay_webhook_turn_off_enabled": False,
    # How long after the configured window closes the turn_off webhook goes out.
    # This is the safety margin that lets the printer's own auto-power-off
    # complete before the mains are cut.
    "relay_webhook_turn_off_delay_minutes": DEFAULT_TURN_OFF_DELAY_MINUTES,
    # The printer's built-in auto-power-off interval, in minutes, as set on the
    # device. It is subtracted from keep_alive_duration_seconds so that the
    # printer goes to sleep at exactly the moment the user asked for rather than
    # that moment plus the hardware's own timer: a 4 h window with a 10 min
    # device timer means keep-alive stops at 3:50 and the printer switches
    # itself off at 4:00.
    #
    # See AUTO_POWER_OFF_MISMATCH_WARNING: nothing here can check this value
    # against the device, and getting it wrong in one direction can cut power to
    # a running printer.
    "printer_auto_power_off_minutes": DEFAULT_PRINTER_AUTO_POWER_OFF_MINUTES,
    "printers": [
        {
            "id": "default",
            "name": "Default Printer",
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-800",
            "label_size": "62"
        }
    ]
}
