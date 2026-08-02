"""
Tests for src/services/ipp_client.py.

All standard-library; no real network access. The HTTP layer is exercised by
mocking ``http.client.HTTPConnection`` so no socket is ever opened.
"""

import struct
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import media_payloads

from src.services import ipp_client
from src.services.ipp_client import (
    EMPTY_MEDIA,
    _attr,
    _decode_datetime,
    _parse_attributes,
    extract_media,
    get_media_ready,
    get_printer_attributes,
    parse_input_tray,
    _TAG_ENUM,
    _TAG_INTEGER,
    _TAG_DATETIME,
    _TAG_END_OF_ATTRS,
)


# --- _decode_datetime --------------------------------------------------------

def _datetime_bytes(year, month, day, hour, minute, second, deci, direction, off_h, off_m):
    """Build an RFC 2579 DateAndTime 11-octet value."""
    return (
        struct.pack(">H", year)
        + bytes([month, day, hour, minute, second, deci])
        + direction.encode("ascii")
        + bytes([off_h, off_m])
    )


def test_decode_datetime_positive_offset():
    raw = _datetime_bytes(2026, 6, 14, 13, 45, 30, 0, "+", 2, 0)
    dt = _decode_datetime(raw)
    assert dt == datetime(2026, 6, 14, 13, 45, 30, tzinfo=timezone(timedelta(hours=2)))
    assert dt.utcoffset() == timedelta(hours=2)


def test_decode_datetime_negative_offset():
    raw = _datetime_bytes(2020, 1, 2, 3, 4, 5, 0, "-", 5, 30)
    dt = _decode_datetime(raw)
    assert dt == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5, minutes=-30)))
    assert dt.utcoffset() == timedelta(hours=-5, minutes=-30)


def test_decode_datetime_utc_zero_offset():
    raw = _datetime_bytes(2026, 12, 31, 23, 59, 59, 0, "+", 0, 0)
    dt = _decode_datetime(raw)
    assert dt == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def test_decode_datetime_too_short_returns_none():
    assert _decode_datetime(b"\x00\x00\x00") is None


def test_decode_datetime_invalid_values_return_none():
    # month 13 is out of range -> ValueError -> None
    raw = _datetime_bytes(2026, 13, 14, 13, 45, 30, 0, "+", 0, 0)
    assert _decode_datetime(raw) is None


# --- _parse_attributes -------------------------------------------------------

def _build_response(attr_payload: bytes, status: int = 0x0000, request_id: int = 1) -> bytes:
    """Prefix the 8-byte IPP response header that _parse_attributes skips."""
    return b"\x02\x00" + struct.pack(">H", status) + struct.pack(">I", request_id) + attr_payload


def test_parse_attributes_extracts_known_fields():
    dt_raw = _datetime_bytes(2026, 6, 14, 10, 20, 30, 0, "+", 1, 0)

    payload = b""
    # printer-state (enum 4 = processing)
    payload += _attr(_TAG_ENUM, b"printer-state", struct.pack(">i", 4))
    # printer-state-reasons (two values -> becomes a list)
    payload += _attr(0x44, b"printer-state-reasons", b"none")  # keyword tag 0x44
    payload += _attr(0x44, b"", b"media-low")                  # additional value, empty name
    # printer-make-and-model (text)
    payload += _attr(0x47, b"printer-make-and-model", b"Brother QL-820NWB")
    # printer-current-time (datetime)
    payload += _attr(_TAG_DATETIME, b"printer-current-time", dt_raw)
    payload += bytes([_TAG_END_OF_ATTRS])

    data = _build_response(payload)
    attrs = _parse_attributes(data)

    assert attrs["printer-state"] == 4
    assert attrs["printer-state-reasons"] == ["none", "media-low"]
    assert attrs["printer-make-and-model"] == "Brother QL-820NWB"
    assert attrs["printer-current-time"] == datetime(
        2026, 6, 14, 10, 20, 30, tzinfo=timezone(timedelta(hours=1))
    )


def test_parse_attributes_integer_value():
    payload = _attr(_TAG_INTEGER, b"copies-default", struct.pack(">i", 7))
    payload += bytes([_TAG_END_OF_ATTRS])
    attrs = _parse_attributes(_build_response(payload))
    assert attrs["copies-default"] == 7


def test_parse_attributes_empty_payload():
    # Only the header, no attributes.
    assert _parse_attributes(_build_response(b"")) == {}


# --- collections -------------------------------------------------------------
#
# media-col-ready is an IPP collection whose members carry no attribute names of
# their own, so the parser has to understand begin/end-collection (0x34/0x37)
# and memberAttrName (0x4A) rather than reading the members positionally.

def test_parse_attributes_reads_a_collection_as_a_dict():
    payload = media_payloads.collection(
        b"media-col-ready",
        media_payloads.member(0x44, b"media-type", b"labels")
        + media_payloads.member(0x21, b"media-bottom-margin", struct.pack(">i", 303)),
    )
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"] == {"media-type": "labels", "media-bottom-margin": 303}


def test_parse_attributes_reads_a_nested_collection():
    payload = media_payloads.collection(
        b"media-col-ready",
        media_payloads.member(0x44, b"media-type", b"labels")
        + media_payloads.media_size(2400, struct.pack(">i", 2400)),
    )
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"]["media-size"] == {"x-dimension": 2400, "y-dimension": 2400}


def test_parse_attributes_decodes_a_range_of_integer():
    """Continuous media reports its length as a range, which must survive."""
    payload = media_payloads.collection(
        b"media-col-ready",
        media_payloads.media_size(6200, media_payloads.CONTINUOUS_LENGTH_RANGE, y_tag=0x33),
    )
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"]["media-size"]["y-dimension"] == (1270, 100000)


def test_parse_attributes_keeps_attributes_after_a_collection():
    """A collection must not swallow the attributes that follow it."""
    payload = media_payloads.collection(
        b"media-col-ready", media_payloads.member(0x44, b"media-type", b"roll"))
    payload += _attr(_TAG_ENUM, b"printer-state", struct.pack(">i", 5))
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"] == {"media-type": "roll"}
    assert attrs["printer-state"] == 5


def test_parse_attributes_collects_repeated_collections_into_a_list():
    members = media_payloads.member(0x44, b"media-type", b"labels")
    payload = media_payloads.collection(b"media-col-ready", members)
    payload += media_payloads.collection(b"", media_payloads.member(0x44, b"media-type", b"roll"))
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"] == [{"media-type": "labels"}, {"media-type": "roll"}]


def test_parse_attributes_survives_an_empty_collection():
    payload = media_payloads.collection(b"media-col-ready", b"")
    payload += _attr(0x44, b"media-ready", b"")
    attrs = _parse_attributes(_build_response(payload + bytes([_TAG_END_OF_ATTRS])))
    assert attrs["media-col-ready"] == {}
    assert attrs["media-ready"] == ""


# --- printer-input-tray ------------------------------------------------------

def test_parse_input_tray_splits_key_value_pairs():
    fields = parse_input_tray('type=sheetFeedManual;medianame=62mm / 2.4";mediacolor=unknown;')
    assert fields["medianame"] == '62mm / 2.4"'
    assert fields["mediacolor"] == "unknown"


def test_parse_input_tray_ignores_a_non_string():
    assert parse_input_tray(None) == {}


@pytest.mark.parametrize("name, expected", [
    ('24mm Dia / 0.94" Dia', True),
    ('62mm / 2.4"', False),
    ('29mm x 90mm', False),
    (None, None),
    ("", None),
])
def test_round_media_is_marked_by_a_dia_token(name, expected):
    assert ipp_client._is_round_media_name(name) is expected


# --- extract_media: the five measured states ---------------------------------

def _media_from(payload: bytes):
    return extract_media(_parse_attributes(payload))


def test_media_die_cut_24mm_round():
    media = _media_from(media_payloads.die_cut_24mm_round())
    assert media["width_mm"] == 24.0
    assert media["length_mm"] == 24.0
    assert media["media_type"] == "labels"
    assert media["media_name"] == '24mm Dia / 0.94" Dia'
    assert media["is_round"] is True
    assert media["source"] == "media-col-ready"


def test_media_continuous_12mm():
    media = _media_from(media_payloads.continuous_12mm())
    assert media["width_mm"] == 12.0
    # Continuous tape has no length of its own; the y-dimension is a range.
    assert media["length_mm"] == 0.0
    assert media["media_type"] == "roll"
    assert media["is_round"] is False


def test_media_continuous_62mm():
    media = _media_from(media_payloads.continuous_62mm())
    assert media["width_mm"] == 62.0
    assert media["length_mm"] == 0.0
    assert media["media_type"] == "roll"
    assert media["media_name"] == '62mm / 2.4"'


def test_media_no_roll_reports_nothing_at_all():
    """An empty media-ready must not be dressed up with tray metadata."""
    media = _media_from(media_payloads.no_media())
    assert media == EMPTY_MEDIA


def test_media_cover_open_still_reports_the_loaded_roll():
    media = _media_from(media_payloads.cover_open())
    assert media["width_mm"] == 62.0
    assert media["media_type"] == "roll"


def test_media_default_is_never_used():
    """media-default stays on the factory 29x90 in every state, including with
    no roll at all, so reading it would be reading a lie."""
    attrs = _parse_attributes(media_payloads.continuous_62mm())
    assert attrs["media-default"] == media_payloads.FACTORY_MEDIA_DEFAULT.decode()
    assert extract_media(attrs)["width_mm"] == 62.0
    assert extract_media(_parse_attributes(media_payloads.no_media())) == EMPTY_MEDIA


def test_media_dimensions_are_hundredths_of_a_millimetre():
    """Despite the tray blob declaring dimunit=micrometers -- a firmware bug,
    since 6200 um would be 6.2 mm rather than the 62 mm roll that produced it."""
    attrs = _parse_attributes(media_payloads.continuous_62mm())
    assert "dimunit=micrometers" in attrs["printer-input-tray"]
    assert attrs["media-col-ready"]["media-size"]["x-dimension"] == 6200
    assert extract_media(attrs)["width_mm"] == 62.0


def test_media_falls_back_to_the_media_ready_name():
    """A printer that answers media-ready but not media-col-ready still works."""
    payload = _attr(0x44, b"media-ready", b"om_brother-label-29x62mm_29x62mm")
    payload += media_payloads.input_tray(b"29mm x 62mm")
    media = extract_media(_parse_attributes(
        media_payloads.response(payload)))
    assert media["width_mm"] == 29.0
    assert media["length_mm"] == 62.0
    assert media["media_type"] == "labels"
    assert media["source"] == "media-ready"


def test_media_falls_back_for_a_roll_name():
    payload = _attr(0x44, b"media-ready", b"roll_current_62x0mm")
    media = extract_media(_parse_attributes(media_payloads.response(payload)))
    assert media["width_mm"] == 62.0
    assert media["media_type"] == "roll"
    assert media["source"] == "media-ready"


def test_media_empty_attributes_give_the_empty_report():
    assert extract_media({}) == EMPTY_MEDIA


# --- get_printer_attributes (mocked HTTP) ------------------------------------

def _make_response(status: int, payload: bytes):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = payload
    return resp


def _good_ipp_payload():
    payload = _attr(_TAG_ENUM, b"printer-state", struct.pack(">i", 3))  # idle
    payload += _attr(0x47, b"printer-make-and-model", b"Brother QL-800")
    payload += bytes([_TAG_END_OF_ATTRS])
    return _build_response(payload)

def _make_response(status: int, payload: bytes):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = payload
    return resp


def _good_ipp_payload():
    payload = _attr(_TAG_ENUM, b"printer-state", struct.pack(">i", 3))  # idle
    payload += _attr(0x47, b"printer-make-and-model", b"Brother QL-800")
    payload += bytes([_TAG_END_OF_ATTRS])
    return _build_response(payload)


def test_get_printer_attributes_reachable_on_200():
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, _good_ipp_payload())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn) as ctor:
        result = get_printer_attributes("192.168.1.100", port=631, timeout=0.1)

    assert result["reachable"] is True
    assert result["printer_state"] == "idle"          # enum 3 mapped via PRINTER_STATE
    assert result["make_and_model"] == "Brother QL-800"
    assert "error" not in result
    ctor.assert_called_with("192.168.1.100", 631, timeout=0.1)
    conn.close.assert_called()


def test_get_printer_attributes_unreachable_on_connection_error():
    conn = MagicMock()
    conn.request.side_effect = OSError("connection refused")

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("192.168.1.100", port=631, timeout=0.1)

    assert result["reachable"] is False
    assert "connection refused" in result["error"]


def test_get_printer_attributes_retries_alternate_path_on_non_200():
    """First path returns 404; the second path ('/') returns a usable 200."""
    bad = _make_response(404, b"")
    good = _make_response(200, _good_ipp_payload())
    conn = MagicMock()
    conn.getresponse.side_effect = [bad, good]

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("printer.local", timeout=0.1)

    assert result["reachable"] is True
    assert conn.getresponse.call_count == 2


def test_get_printer_attributes_unreachable_when_all_paths_fail():
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(500, b"")

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("192.168.1.100", timeout=0.1)

    assert result["reachable"] is False
    assert result["error"] == "HTTP 500"


def test_get_printer_attributes_carries_the_media():
    """The media rides along with the status in one request, so reading it
    costs no extra round-trip."""
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, media_payloads.continuous_62mm())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("192.168.1.100", timeout=0.1)

    assert result["reachable"] is True
    assert result["printer_state"] == "idle"
    assert result["media"]["width_mm"] == 62.0
    assert result["media"]["media_type"] == "roll"
    assert conn.request.call_count == 1


def test_get_printer_attributes_requests_the_media_attributes():
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, media_payloads.continuous_62mm())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        get_printer_attributes("192.168.1.100", timeout=0.1)

    body = conn.request.call_args[0][2]
    for attribute in (b"media-ready", b"media-col-ready", b"printer-input-tray"):
        assert attribute in body
    # media-default is measured to be stuck on the factory value, so it is not
    # even asked for.
    assert b"media-default" not in body


def test_get_printer_attributes_reports_empty_media_when_unreachable():
    conn = MagicMock()
    conn.request.side_effect = OSError("connection refused")

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("192.168.1.100", timeout=0.1)

    assert result["reachable"] is False
    assert result["media"] == EMPTY_MEDIA


def test_get_printer_attributes_survives_a_broken_media_report():
    """A media read must never take a status read down with it."""
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, media_payloads.continuous_62mm())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn), \
         patch.object(ipp_client, "extract_media", side_effect=ValueError("boom")):
        result = get_printer_attributes("192.168.1.100", timeout=0.1)

    assert result["reachable"] is True
    assert result["printer_state"] == "idle"
    assert result["media"] == EMPTY_MEDIA


def test_get_media_ready_returns_only_the_media():
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, media_payloads.die_cut_24mm_round())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        media = get_media_ready("192.168.1.100", timeout=0.1)

    assert media["width_mm"] == 24.0
    assert media["is_round"] is True


def test_get_media_ready_is_all_none_when_unreachable():
    conn = MagicMock()
    conn.request.side_effect = OSError("no route to host")

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        media = get_media_ready("192.168.1.100", timeout=0.1)

    assert media == EMPTY_MEDIA
    assert all(value is None for value in media.values())
