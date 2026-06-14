"""
Minimal, dependency-free IPP (Internet Printing Protocol) client.

Used to query Brother QL network printers for their real status. On many
Brother QL models (e.g. QL-820NWB) SNMP is disabled and the raw 9100 port
offers no status read-back, while IPP (TCP 631) reliably answers
Get-Printer-Attributes with the printer state, state reasons, model name and
the printer clock. This module speaks just enough IPP to read those.

Only the Python standard library is used (http.client, struct, datetime).
"""

import struct
import http.client
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import structlog

logger = structlog.get_logger()

# IPP operation / version
_IPP_VERSION = b"\x02\x00"            # 2.0
_OP_GET_PRINTER_ATTRIBUTES = b"\x00\x0b"

# Delimiter tags
_TAG_OPERATION_ATTRS = 0x01
_TAG_END_OF_ATTRS = 0x03

# Value tags we care about when parsing
_TAG_INTEGER = 0x21
_TAG_BOOLEAN = 0x22
_TAG_ENUM = 0x23
_TAG_DATETIME = 0x31
_DELIMITERS = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05}

# printer-state enum (RFC 8011)
PRINTER_STATE = {3: "idle", 4: "processing", 5: "stopped"}


def _attr(tag: int, name: bytes, value: bytes) -> bytes:
    return bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def _build_get_printer_attributes(host: str, requested) -> bytes:
    printer_uri = f"ipp://{host}/ipp/print".encode()
    body = _IPP_VERSION + _OP_GET_PRINTER_ATTRIBUTES + b"\x00\x00\x00\x01"  # request-id 1
    body += bytes([_TAG_OPERATION_ATTRS])
    body += _attr(0x47, b"attributes-charset", b"utf-8")
    body += _attr(0x48, b"attributes-natural-language", b"en")
    body += _attr(0x45, b"printer-uri", printer_uri)
    first = True
    for name in requested:
        body += _attr(0x44, b"requested-attributes" if first else b"", name)
        first = False
    body += bytes([_TAG_END_OF_ATTRS])
    return body


def _decode_datetime(value: bytes) -> Optional[datetime]:
    """Decode an RFC 2579 DateAndTime (11 octets) into a timezone-aware datetime."""
    if len(value) < 11:
        return None
    try:
        year = struct.unpack(">H", value[0:2])[0]
        month, day, hour, minute, second, _deci = value[2], value[3], value[4], value[5], value[6], value[7]
        direction = chr(value[8])
        off_h, off_m = value[9], value[10]
        offset = timedelta(hours=off_h, minutes=off_m)
        if direction == "-":
            offset = -offset
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except (ValueError, struct.error):
        return None


def _parse_attributes(data: bytes) -> Dict[str, Any]:
    """Parse IPP attribute groups into a flat {name: value} dict.

    Multi-value attributes (e.g. state-reasons) become a list. Only the value
    types relevant to status reporting are decoded; others fall back to text.
    """
    attrs: Dict[str, Any] = {}
    # Skip the 8-byte response header (version[2], status-code[2], request-id[4])
    i = 8
    current_name: Optional[str] = None
    n = len(data)
    while i < n:
        tag = data[i]
        i += 1
        if tag in _DELIMITERS:
            current_name = None
            continue
        if i + 2 > n:
            break
        name_len = struct.unpack(">H", data[i:i + 2])[0]
        i += 2
        name = data[i:i + name_len].decode("utf-8", "replace")
        i += name_len
        if i + 2 > n:
            break
        value_len = struct.unpack(">H", data[i:i + 2])[0]
        i += 2
        raw = data[i:i + value_len]
        i += value_len

        if tag in (_TAG_INTEGER, _TAG_ENUM) and len(raw) == 4:
            value: Any = struct.unpack(">i", raw)[0]
        elif tag == _TAG_BOOLEAN and len(raw) == 1:
            value = bool(raw[0])
        elif tag == _TAG_DATETIME:
            value = _decode_datetime(raw)
        else:
            value = raw.decode("utf-8", "replace")

        key = name if name_len > 0 else current_name
        if key is None:
            continue
        if name_len == 0:
            # Additional value of the previous attribute -> turn into a list
            existing = attrs.get(key)
            if isinstance(existing, list):
                existing.append(value)
            else:
                attrs[key] = [existing, value]
        else:
            attrs[key] = value
            current_name = key
    return attrs


def get_printer_attributes(host: str, port: int = 631, timeout: float = 2.0) -> Dict[str, Any]:
    """Query a printer via IPP Get-Printer-Attributes.

    Returns a normalized dict:
        reachable (bool), make_and_model (str|None), printer_state (str|None),
        printer_state_reasons (str|None), current_time (datetime|None),
        error (str, only when not reachable).

    The alternate path ``/`` is only tried when the printer actually answered
    over HTTP at ``/ipp/print`` (wrong path / empty body). A connection-level
    failure (timeout, refused, unreachable) means the device is not answering at
    all, so we fail fast instead of waiting out a second full timeout.
    """
    requested = [
        b"printer-state",
        b"printer-state-reasons",
        b"printer-make-and-model",
        b"printer-current-time",
    ]
    body = _build_get_printer_attributes(host, requested)
    result: Dict[str, Any] = {
        "reachable": False,
        "make_and_model": None,
        "printer_state": None,
        "printer_state_reasons": None,
        "current_time": None,
    }
    for path in ("/ipp/print", "/"):
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request("POST", path, body, {"Content-Type": "application/ipp"})
            resp = conn.getresponse()
            payload = resp.read()
            conn.close()
        except (OSError, http.client.HTTPException) as exc:
            # Connection-level failure -> device not answering; do not retry paths.
            result["error"] = str(exc)
            break
        if resp.status != 200 or not payload:
            # Printer answered over HTTP but not usable here; try the next path.
            result["error"] = f"HTTP {resp.status}"
            continue
        attrs = _parse_attributes(payload)
        state = attrs.get("printer-state")
        reasons = attrs.get("printer-state-reasons")
        if isinstance(reasons, list):
            reasons = ", ".join(str(r) for r in reasons)
        result.update(
            reachable=True,
            make_and_model=attrs.get("printer-make-and-model"),
            printer_state=PRINTER_STATE.get(state, state if state is not None else None),
            printer_state_reasons=reasons,
            current_time=attrs.get("printer-current-time"),
        )
        result.pop("error", None)
        return result
    return result
