"""
Tests for src/services/ipp_client.py.

All standard-library; no real network access. The HTTP layer is exercised by
mocking ``http.client.HTTPConnection`` so no socket is ever opened.
"""

import struct
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.services import ipp_client
from src.services.ipp_client import (
    _attr,
    _decode_datetime,
    _parse_attributes,
    get_printer_attributes,
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


def test_get_printer_attributes_reachable_on_200():
    conn = MagicMock()
    conn.getresponse.return_value = _make_response(200, _good_ipp_payload())

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn) as ctor:
        result = get_printer_attributes("10.50.60.20", port=631, timeout=0.1)

    assert result["reachable"] is True
    assert result["printer_state"] == "idle"          # enum 3 mapped via PRINTER_STATE
    assert result["make_and_model"] == "Brother QL-800"
    assert "error" not in result
    ctor.assert_called_with("10.50.60.20", 631, timeout=0.1)
    conn.close.assert_called()


def test_get_printer_attributes_unreachable_on_connection_error():
    conn = MagicMock()
    conn.request.side_effect = OSError("connection refused")

    with patch.object(ipp_client.http.client, "HTTPConnection", return_value=conn):
        result = get_printer_attributes("10.50.60.20", port=631, timeout=0.1)

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
        result = get_printer_attributes("10.50.60.20", timeout=0.1)

    assert result["reachable"] is False
    assert result["error"] == "HTTP 500"
