"""
Default settings for the Brother QL Printer App.
These settings are used when no user-defined settings are available.
"""

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
