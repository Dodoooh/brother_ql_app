"""
Tests for mapping a printer's media report onto this app's label identifiers.

Everything here is offline: the media dicts are the ones
``src.services.ipp_client.extract_media`` produces from the payloads captured
from a QL-820NWB (see ``tests/media_payloads.py``), or the equivalent for media
that printer cannot hold.

The rule the whole module exists to enforce is that ambiguity surfaces as
ambiguity. Where two identifiers cannot be told apart from what a printer
reports, both come back; nothing here is ever allowed to pick one.
"""

import pytest

import media_payloads

from src.services.ipp_client import extract_media, _parse_attributes
from src.services.printer_service import (
    MEDIA_MATCH_TOLERANCE_MM,
    LabelIdentification,
    identify_label_candidates,
)


def _media(width, length, media_type, media_name=None, is_round=None):
    return {
        "width_mm": width,
        "length_mm": length,
        "media_type": media_type,
        "media_name": media_name,
        "is_round": is_round,
        "source": "media-col-ready",
    }


def _die_cut(width, length, media_name=None, is_round=None):
    return _media(width, length, "labels", media_name, is_round)


def _roll(width, media_name=None):
    return _media(width, 0.0, "roll", media_name, False)


def _catalogue():
    from brother_ql.labels import ALL_LABELS, FormFactor

    return [label for label in ALL_LABELS if label.form_factor != FormFactor.PTOUCH_ENDLESS]


def _die_cut_identifiers():
    from brother_ql.labels import FormFactor

    return [label.identifier for label in _catalogue()
            if label.form_factor == FormFactor.DIE_CUT]


def _round_identifiers():
    from brother_ql.labels import FormFactor

    return [label.identifier for label in _catalogue()
            if label.form_factor == FormFactor.ROUND_DIE_CUT]


def _nominal_size(identifier):
    """The size a printer reports for a medium: its nominal millimetres."""
    if identifier.startswith("d"):
        side = float(identifier[1:])
        return side, side
    width, _, length = identifier.partition("x")
    return float(width), float(length)


# --- die-cut: every size resolves to exactly one identifier -------------------

@pytest.mark.parametrize("identifier", _die_cut_identifiers())
def test_every_die_cut_size_resolves_to_exactly_one_identifier(identifier):
    width, length = _nominal_size(identifier)
    result = identify_label_candidates(_die_cut(width, length))
    assert result.candidates == (identifier,)
    assert result.ambiguous is False


@pytest.mark.parametrize("identifier", _round_identifiers())
def test_every_round_size_resolves_to_exactly_one_identifier(identifier):
    diameter, _ = _nominal_size(identifier)
    name = f'{diameter:g}mm Dia / 0.94" Dia'
    result = identify_label_candidates(_die_cut(diameter, diameter, name, is_round=True))
    assert result.candidates == (identifier,)


def test_die_cut_24mm_round_from_the_real_payload():
    media = extract_media(_parse_attributes(media_payloads.die_cut_24mm_round()))
    result = identify_label_candidates(media)
    assert result.candidates == ("d24",)
    assert result.matches("d24") is True
    assert result.matches("62") is False


def test_round_media_is_separated_from_square_die_cut_by_size_alone():
    """23x23 and d24 are the only near-square pair in the catalogue, and they
    are 1.02 mm apart -- outside the tolerance, so geometry decides without
    needing the tray's "Dia" marking at all."""
    without_hint = identify_label_candidates(_die_cut(24.0, 24.0, media_name=None))
    assert without_hint.candidates == ("d24",)
    assert identify_label_candidates(_die_cut(23.0, 23.0)).candidates == ("23x23",)


def test_no_round_size_coincides_with_a_rectangular_one():
    """The property the geometric round/rectangular decision rests on."""
    from brother_ql.labels import FormFactor

    rounds = [label for label in _catalogue() if label.form_factor == FormFactor.ROUND_DIE_CUT]
    rects = [label for label in _catalogue() if label.form_factor == FormFactor.DIE_CUT]
    for circle in rounds:
        diameter = circle.dots_total[0] * 25.4 / 300.0
        for rect in rects:
            sides = sorted(dots * 25.4 / 300.0 for dots in rect.dots_total)
            collides = (abs(sides[0] - diameter) <= MEDIA_MATCH_TOLERANCE_MM
                        and abs(sides[1] - diameter) <= MEDIA_MATCH_TOLERANCE_MM)
            assert not collides, f"{circle.identifier} collides with {rect.identifier}"


# --- the traps ---------------------------------------------------------------

def test_60x86_matches_although_tape_size_says_87():
    """The printer reports 60x86; brother_ql's tape_size claims (60, 87) and its
    dots_total works out to 59.94 x 86.70 mm. Matching tape_size alone would
    miss the label that is physically in the machine."""
    from brother_ql.labels import ALL_LABELS

    label = next(entry for entry in ALL_LABELS if entry.identifier == "60x86")
    assert label.tape_size == (60, 87)
    assert abs(label.tape_size[1] - 86.0) > MEDIA_MATCH_TOLERANCE_MM

    assert identify_label_candidates(_die_cut(60.0, 86.0)).candidates == ("60x86",)


def test_60x86_also_matches_if_a_printer_reports_87():
    assert identify_label_candidates(_die_cut(60.0, 87.0)).candidates == ("60x86",)


def test_the_dimension_pair_is_sorted_before_comparing():
    """IPP names this app's 62x29 as om_brother-label-29x62mm: the pair does not
    say which axis runs across the tape."""
    assert identify_label_candidates(_die_cut(29.0, 62.0)).candidates == ("62x29",)
    assert identify_label_candidates(_die_cut(62.0, 29.0)).candidates == ("62x29",)


def test_39x90_matches_its_physical_38mm_width():
    """The identifier says 39 but the media is 38 mm wide; both are accepted."""
    assert identify_label_candidates(_die_cut(38.0, 90.0)).candidates == ("39x90",)
    assert identify_label_candidates(_die_cut(39.0, 90.0)).candidates == ("39x90",)


@pytest.mark.parametrize("identifier, width, length", [
    ("102x51", 102.0, 51.0),
    ("102x152", 102.0, 152.0),
    ("103x164", 103.0, 164.0),
])
def test_ql1100_only_media_is_still_identified(identifier, width, length):
    """These three are absent from a QL-820NWB's media-supported entirely, so
    identification must not assume any particular printer's list."""
    assert identify_label_candidates(_die_cut(width, length)).candidates == (identifier,)


# --- the three unresolvable continuous cases ---------------------------------

def test_62mm_continuous_cannot_separate_plain_from_red():
    media = extract_media(_parse_attributes(media_payloads.continuous_62mm()))
    result = identify_label_candidates(media)
    assert result.candidates == ("62", "62red")
    assert result.ambiguous is True
    assert "mediacolor" in result.reason
    assert result.matches("62") is True
    assert result.matches("62red") is True
    assert result.matches("29") is False


def test_12mm_continuous_cannot_separate_12_from_12_plus_17():
    media = extract_media(_parse_attributes(media_payloads.continuous_12mm()))
    result = identify_label_candidates(media)
    assert result.candidates == ("12", "12+17")
    assert result.ambiguous is True
    assert "rendering choice" in result.reason


@pytest.mark.parametrize("reported", [103.0, 103.6, 104.0])
def test_103_and_104_cannot_be_separated(reported):
    """They differ by about 0.25 mm, below any resolution the printer reports."""
    result = identify_label_candidates(_roll(reported))
    assert result.candidates == ("103", "104")
    assert result.ambiguous is True


@pytest.mark.parametrize("identifier, width", [
    ("18", 18.0), ("29", 29.0), ("38", 38.0), ("50", 50.0), ("54", 54.0), ("102", 102.0),
])
def test_the_other_continuous_widths_are_unambiguous(identifier, width):
    result = identify_label_candidates(_roll(width))
    assert result.candidates == (identifier,)
    assert result.ambiguous is False


def test_12_plus_17_is_never_matched_geometrically():
    """Its dots_total describes a 29 mm-wide raster, not a 29 mm roll -- taken
    at face value it would claim a 29 mm roll is a 12 mm one."""
    assert identify_label_candidates(_roll(29.0)).candidates == ("29",)


def test_a_62mm_roll_is_not_confused_with_a_62mm_die_cut_label():
    """The form factor comes from media-type, so the same width means different
    media depending on what the printer says it is."""
    assert identify_label_candidates(_roll(62.0)).candidates == ("62", "62red")
    assert identify_label_candidates(_die_cut(62.0, 29.0)).candidates == ("62x29",)


def test_102_continuous_is_not_the_102x51_die_cut():
    assert identify_label_candidates(_roll(102.0)).candidates == ("102",)
    assert identify_label_candidates(_die_cut(102.0, 51.0)).candidates == ("102x51",)


# --- catalogue-wide consistency ----------------------------------------------

@pytest.mark.parametrize("identifier", [label.identifier for label in _catalogue()])
def test_every_supported_identifier_is_among_its_own_candidates(identifier):
    """Round-trip: report what the catalogue says a medium is, and the medium
    must be one of the answers."""
    from brother_ql.labels import ALL_LABELS, FormFactor

    label = next(entry for entry in ALL_LABELS if entry.identifier == identifier)
    if label.form_factor == FormFactor.ENDLESS:
        width = float(label.tape_size[0])
        media = _roll(width)
    else:
        width, length = _nominal_size(identifier)
        media = _die_cut(width, length)
    assert identifier in identify_label_candidates(media).candidates


def test_ptouch_media_is_never_offered():
    """P-touch tapes belong to a different family of machines and are not in
    the app's label enum."""
    for width in (10.84, 12.0, 18.0, 24.0, 36.0, 43.35):
        candidates = identify_label_candidates(_roll(width)).candidates
        assert not any(candidate.startswith("pt") for candidate in candidates)


# --- not knowing ---------------------------------------------------------------

def test_no_media_gives_no_candidates():
    media = extract_media(_parse_attributes(media_payloads.no_media()))
    result = identify_label_candidates(media)
    assert result.candidates == ()
    assert result.resolved is False
    assert "no media" in result.reason.lower()


def test_a_missing_report_gives_no_candidates():
    assert identify_label_candidates(None).candidates == ()
    assert identify_label_candidates({}).candidates == ()


def test_an_unsupported_size_is_reported_as_unmatched_rather_than_guessed():
    result = identify_label_candidates(_roll(40.0))
    assert result.candidates == ()
    assert "does not match any label" in result.reason


def test_matches_is_none_rather_than_false_when_nothing_was_identified():
    """"We do not know" must never be readable as "they disagree"."""
    result = identify_label_candidates(None)
    assert result.matches("62") is None
    assert LabelIdentification(("62",), "x").matches(None) is None


def test_an_unreadable_width_is_not_guessed_at():
    result = identify_label_candidates(_roll("wide"))
    assert result.candidates == ()
    assert "unreadable" in result.reason


def test_a_missing_media_type_falls_back_to_the_reported_length():
    """No form factor reported: a medium with no length of its own is tape."""
    assert identify_label_candidates(
        _media(62.0, 0.0, None)).candidates == ("62", "62red")
    assert identify_label_candidates(
        _media(62.0, 29.0, None)).candidates == ("62x29",)


def test_the_tolerance_admits_reporting_slack_but_not_a_different_medium():
    # 52x29 and 54x29 are the closest distinct pair, 1.35 mm apart.
    assert identify_label_candidates(_die_cut(52.0, 29.0)).candidates == ("52x29",)
    assert identify_label_candidates(_die_cut(54.0, 29.0)).candidates == ("54x29",)
