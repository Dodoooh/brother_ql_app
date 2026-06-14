"""
Tests for src/utils/uri_validation.py::validate_printer_uri.

Pure standard-library logic (urllib + ipaddress); no third-party deps and no
network access. Verifies that legitimate LAN/USB printer URIs are accepted while
disallowed schemes and SSRF/dangerous IP targets are rejected with a ValueError.
"""

import pytest

from src.utils.uri_validation import validate_printer_uri


# --- Acceptable URIs ---------------------------------------------------------

@pytest.mark.parametrize(
    "uri",
    [
        "tcp://10.50.60.20",          # private RFC1918 IP -> printers live here
        "tcp://192.168.1.5:9100",     # private IP with explicit port
        "tcp://printer.local",        # hostname (no DNS resolution performed)
        "usb://0x04f9:0x209c",        # USB device addressing
    ],
)
def test_valid_uris_accepted(uri):
    # Should return None and NOT raise.
    assert validate_printer_uri(uri) is None


def test_valid_uri_with_surrounding_whitespace():
    # Leading/trailing whitespace is stripped before validation.
    assert validate_printer_uri("  tcp://10.50.60.20  ") is None


# --- Rejected URIs -----------------------------------------------------------

@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",         # disallowed scheme -> arbitrary file access
        "http://x",                   # disallowed scheme
        "",                           # empty string
        "   ",                        # whitespace-only
        "tcp://169.254.169.254",      # link-local / cloud metadata (SSRF guard)
        "tcp://127.0.0.1",            # loopback
        "tcp://0.0.0.0",              # unspecified address
        "ftp://printer",             # unknown scheme
        "lpt://printer",             # unknown scheme
        "tcp://",                    # tcp with missing host
    ],
)
def test_invalid_uris_rejected(uri):
    with pytest.raises(ValueError):
        validate_printer_uri(uri)


def test_none_is_rejected():
    with pytest.raises(ValueError):
        validate_printer_uri(None)  # type: ignore[arg-type]


def test_error_message_has_prefix():
    with pytest.raises(ValueError) as exc:
        validate_printer_uri("file:///etc/passwd")
    assert "Invalid printer URI" in str(exc.value)


def test_ipv6_loopback_rejected():
    with pytest.raises(ValueError):
        validate_printer_uri("tcp://[::1]")
