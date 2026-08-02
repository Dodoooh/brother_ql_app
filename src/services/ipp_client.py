"""
Minimal, dependency-free IPP (Internet Printing Protocol) client.

Used to query Brother QL network printers for their real status. On many
Brother QL models (e.g. QL-820NWB) SNMP is disabled and the raw 9100 port
offers no status read-back, while IPP (TCP 631) reliably answers
Get-Printer-Attributes with the printer state, state reasons, model name, the
printer clock and -- the reason this module grew -- the media actually loaded
in the device. This module speaks just enough IPP to read those.

IPP is the only channel on these printers that answers the media question
honestly. SNMP was measured against the same five physical states and got three
of them wrong: ``prtInputType`` reads 5 for continuous tape and die-cut labels
alike, the reported length for continuous media is nonsense (255, i.e. 2.55 mm)
and an open cover is not reflected at all. Everything below therefore reads from
IPP, and no SNMP path exists for media.

Only the Python standard library is used (http.client, struct, datetime, re).
"""

import re
import struct
import http.client
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

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
_TAG_OCTET_STRING = 0x30
_TAG_RANGE_OF_INTEGER = 0x33
_TAG_BEGIN_COLLECTION = 0x34
_TAG_END_COLLECTION = 0x37
_TAG_MEMBER_ATTR_NAME = 0x4A
_TAG_DATETIME = 0x31
_DELIMITERS = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05}

# printer-state enum (RFC 8011)
PRINTER_STATE = {3: "idle", 4: "processing", 5: "stopped"}

# Attributes requested on every Get-Printer-Attributes. The media attributes
# ride along with the status attributes deliberately: a status check and a media
# read are the same question asked of the same device, and folding them into one
# request means reading the loaded media costs no extra round-trip at all.
REQUESTED_ATTRIBUTES = (
    b"printer-state",
    b"printer-state-reasons",
    b"printer-make-and-model",
    b"printer-current-time",
    b"media-ready",
    b"media-col-ready",
    b"printer-input-tray",
)

# The media report, with every field None when nothing could be read. Callers
# distinguish "no media" from "unreachable" by the reachability flag that comes
# with it, never by this dict alone.
EMPTY_MEDIA: Dict[str, Any] = {
    "width_mm": None,
    "length_mm": None,
    "media_type": None,
    "media_name": None,
    "is_round": None,
    "source": None,
}

# ``media-ready`` names carry the size in their text form, e.g.
# "roll_current_62x0mm" or "om_brother-label-29x62mm_29x62mm". This is the
# fallback for printers that answer media-ready but not media-col-ready.
_MEDIA_READY_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)mm")

# Round stock is marked in the tray's ``medianame`` as a standalone "Dia" token
# ("24mm Dia / 0.94\" Dia"); rectangular stock never carries it ("62mm / 2.4\"").
_MEDIA_NAME_TOKEN_RE = re.compile(r"[\s/]+")

# ``media-col-ready`` dimensions are hundredths of a millimetre: a 24 mm round
# label reports x-dimension 2400. The tray blob declares "dimunit=micrometers"
# alongside the very same numbers, which cannot be true (6200 um is 6.2 mm, not
# the 62 mm roll that produced it) -- that declaration is a firmware bug and is
# deliberately ignored.
_MEDIA_DIMENSION_DIVISOR = 100.0


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


def _decode_value(tag: int, raw: bytes) -> Any:
    """Decode a single IPP value. Unknown types fall back to text."""
    if tag in (_TAG_INTEGER, _TAG_ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == _TAG_BOOLEAN and len(raw) == 1:
        return bool(raw[0])
    if tag == _TAG_DATETIME:
        return _decode_datetime(raw)
    if tag == _TAG_RANGE_OF_INTEGER and len(raw) == 8:
        # Continuous media reports its length as a range (min, max) rather than
        # a single integer, so this type has to survive the parse to be
        # recognisable as "not a fixed length".
        return tuple(struct.unpack(">ii", raw))
    return raw.decode("utf-8", "replace")


def _read_field(data: bytes, i: int, n: int) -> Optional[Tuple[str, bytes, int]]:
    """Read one name/value field at ``i``. Returns (name, raw_value, next_i)."""
    if i + 2 > n:
        return None
    name_len = struct.unpack(">H", data[i:i + 2])[0]
    i += 2
    name = data[i:i + name_len].decode("utf-8", "replace")
    i += name_len
    if i + 2 > n:
        return None
    value_len = struct.unpack(">H", data[i:i + 2])[0]
    i += 2
    raw = data[i:i + value_len]
    i += value_len
    return name, raw, i


def _add_value(container: Dict[str, Any], key: str, value: Any, repeated: bool) -> None:
    """Store a value, turning an attribute into a list on its second value."""
    if repeated and key in container:
        existing = container[key]
        if isinstance(existing, list):
            existing.append(value)
        else:
            container[key] = [existing, value]
    else:
        container[key] = value


def _parse_collection(data: bytes, i: int, n: int) -> Tuple[Dict[str, Any], int]:
    """Parse the members of an IPP collection into a dict.

    Called with ``i`` positioned just after a begCollection (0x34) field. A
    collection is encoded as an alternating run of memberAttrName (0x4A) and
    value fields, terminated by endCollection (0x37); a member whose value is
    itself a collection opens a nested begCollection, which recurses here.
    Members may repeat (1setOf), in which case the extra values are appended.

    Returns the member dict and the index just past the endCollection.
    """
    members: Dict[str, Any] = {}
    member_name: Optional[str] = None
    while i < n:
        tag = data[i]
        if tag in _DELIMITERS:
            # A group delimiter inside a collection means the payload is
            # malformed; hand the position back untouched so the outer parser
            # can resynchronise on it rather than swallowing the rest.
            break
        i += 1
        field = _read_field(data, i, n)
        if field is None:
            break
        _name, raw, i = field
        if tag == _TAG_END_COLLECTION:
            break
        if tag == _TAG_MEMBER_ATTR_NAME:
            member_name = raw.decode("utf-8", "replace")
            continue
        if member_name is None:
            continue
        if tag == _TAG_BEGIN_COLLECTION:
            value, i = _parse_collection(data, i, n)
        else:
            value = _decode_value(tag, raw)
        _add_value(members, member_name, value, repeated=True)
    return members, i


def _parse_attributes(data: bytes) -> Dict[str, Any]:
    """Parse IPP attribute groups into a flat {name: value} dict.

    Multi-value attributes (e.g. state-reasons) become a list. Collections
    (``media-col-ready`` and friends) become nested dicts -- they are parsed
    structurally rather than flattened into the surrounding namespace, because
    a collection's members carry no attribute names of their own and a
    positional reading of them breaks the moment a firmware reorders, adds or
    omits one.
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
        field = _read_field(data, i, n)
        if field is None:
            break
        name, raw, i = field
        value: Any
        if tag == _TAG_BEGIN_COLLECTION:
            value, i = _parse_collection(data, i, n)
        elif tag == _TAG_END_COLLECTION:
            # Stray terminator (a collection we did not open): ignore it.
            continue
        else:
            value = _decode_value(tag, raw)

        key = name if name else current_name
        if key is None:
            continue
        if not name:
            # Additional value of the previous attribute -> turn into a list
            _add_value(attrs, key, value, repeated=True)
        else:
            attrs[key] = value
            current_name = key
    return attrs


def _first(value: Any) -> Any:
    """IPP 1setOf values arrive as a list; take the first entry."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _dimension_mm(value: Any) -> Optional[float]:
    """Convert an IPP media dimension (hundredths of a mm) to millimetres.

    Anything that is not a plain integer -- notably the rangeOfInteger a
    continuous roll reports for its length -- yields None, because a range is
    not a measurement.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return round(value / _MEDIA_DIMENSION_DIVISOR, 2)


def parse_input_tray(raw: Any) -> Dict[str, str]:
    """Parse the ``printer-input-tray`` blob into a lower-cased key/value dict.

    The attribute is a PWG 5107.2 octetString of ``key=value;`` pairs, e.g.
    ``...;medianame=24mm Dia / 0.94" Dia;mediacolor=unknown;``.
    """
    fields: Dict[str, str] = {}
    if not isinstance(raw, str):
        return fields
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        fields[key.strip().lower()] = value.strip()
    return fields


def _is_round_media_name(media_name: Optional[str]) -> Optional[bool]:
    """Whether a tray ``medianame`` describes round stock.

    Brother marks round die-cut media with a standalone ``Dia`` token per unit
    ("24mm Dia / 0.94\" Dia"); rectangular media never carries one ("62mm /
    2.4\""). Returns None when there is no name to judge.
    """
    if not media_name:
        return None
    tokens = [token.strip().lower() for token in _MEDIA_NAME_TOKEN_RE.split(media_name)]
    return "dia" in tokens


def extract_media(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a parsed Get-Printer-Attributes response to the loaded media.

    Reads, in order of preference, ``media-col-ready`` (structured, exact) and
    ``media-ready`` (the size embedded in the media name). ``media-default`` is
    deliberately never consulted: it was measured to stay on the factory value
    ``om_brother-label-29x90mm_29x90mm`` through every media change *and* with
    no roll in the printer at all, so it describes nothing that is loaded.

    Returns a copy of :data:`EMPTY_MEDIA` when no medium could be read, which is
    also what an empty ``media-ready`` (no roll, cover closed) produces.
    """
    media = dict(EMPTY_MEDIA)

    col = _first(attrs.get("media-col-ready"))
    if isinstance(col, dict) and col:
        size = col.get("media-size")
        width = length = None
        if isinstance(size, dict):
            width = _dimension_mm(size.get("x-dimension"))
            length = _dimension_mm(size.get("y-dimension"))
        media_type = _first(col.get("media-type")) or None
        if width is not None or media_type:
            media.update(
                width_mm=width,
                length_mm=length,
                media_type=media_type if isinstance(media_type, str) else None,
                source="media-col-ready",
            )

    ready = _first(attrs.get("media-ready"))
    if isinstance(ready, str) and ready:
        parsed = _MEDIA_READY_SIZE_RE.search(ready)
        if media["width_mm"] is None and parsed:
            media.update(
                width_mm=float(parsed.group(1)),
                length_mm=float(parsed.group(2)),
                source=media["source"] or "media-ready",
            )
        if not media["media_type"] and (parsed or ready.startswith("roll")):
            media["media_type"] = "roll" if ready.startswith("roll") else "labels"

    if media["width_mm"] is None and not media["media_type"]:
        # Nothing loaded (or nothing reported): stay entirely None rather than
        # half-populated, so a caller cannot mistake tray metadata for media.
        return dict(EMPTY_MEDIA)

    tray = parse_input_tray(_first(attrs.get("printer-input-tray")))
    media["media_name"] = tray.get("medianame") or None
    if media["media_type"] == "roll":
        # Continuous tape has no length of its own, and reports none: the
        # y-dimension comes back as a rangeOfInteger and the media name says 0.
        media["length_mm"] = 0.0
        media["is_round"] = False
    else:
        media["is_round"] = _is_round_media_name(media["media_name"])
    return media


def get_printer_attributes(host: str, port: int = 631, timeout: float = 2.0) -> Dict[str, Any]:
    """Query a printer via IPP Get-Printer-Attributes.

    Returns a normalized dict:
        reachable (bool), make_and_model (str|None), printer_state (str|None),
        printer_state_reasons (str|None), current_time (datetime|None),
        media (dict, see :data:`EMPTY_MEDIA`), error (str, only when not
        reachable).

    The alternate path ``/`` is only tried when the printer actually answered
    over HTTP at ``/ipp/print`` (wrong path / empty body). A connection-level
    failure (timeout, refused, unreachable) means the device is not answering at
    all, so we fail fast instead of waiting out a second full timeout.
    """
    body = _build_get_printer_attributes(host, list(REQUESTED_ATTRIBUTES))
    result: Dict[str, Any] = {
        "reachable": False,
        "make_and_model": None,
        "printer_state": None,
        "printer_state_reasons": None,
        "current_time": None,
        "media": dict(EMPTY_MEDIA),
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
        try:
            media = extract_media(attrs)
        except Exception:  # noqa: BLE001 - media is a bonus; never fail a status read for it
            logger.warning("Could not read loaded media from IPP response", host=host, exc_info=True)
            media = dict(EMPTY_MEDIA)
        result.update(
            reachable=True,
            make_and_model=attrs.get("printer-make-and-model"),
            printer_state=PRINTER_STATE.get(state, state if state is not None else None),
            printer_state_reasons=reasons,
            current_time=attrs.get("printer-current-time"),
            media=media,
        )
        result.pop("error", None)
        return result
    return result


def get_media_ready(host: str, port: int = 631, timeout: float = 2.0) -> Dict[str, Any]:
    """Read only the media currently loaded in a printer.

    Convenience wrapper around :func:`get_printer_attributes` for callers that
    do not care about the printer state. Every field is None when the printer
    cannot be reached -- and, indistinguishably from this dict alone, when the
    printer is reachable but empty. Use :func:`get_printer_attributes` when the
    two have to be told apart.
    """
    return get_printer_attributes(host, port=port, timeout=timeout).get("media", dict(EMPTY_MEDIA))
