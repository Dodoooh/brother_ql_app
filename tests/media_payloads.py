"""
IPP payload fixtures for the five media states measured on a QL-820NWB.

Every payload here is built from a real Get-Printer-Attributes response
captured from the device at tcp://192.168.1.100, so the tests that use them run
entirely offline while still exercising the exact byte shapes the printer emits
-- including the ``media-col-ready`` collection, its nested ``media-size``
sub-collection, and the rangeOfInteger a continuous roll reports for its length.

The five states, and what the printer says in each:

    DK-11218, 24 mm round die-cut   media-ready om_brother-label-24x24mm_24x24mm
                                    media-type labels, state 3, reasons none
    DK-22214, 12 mm continuous      media-ready roll_current_12x0mm
                                    media-type roll,   state 3, reasons none
    62 mm continuous                media-ready roll_current_62x0mm
                                    media-type roll,   state 3, reasons none
    no roll, cover closed           media-ready '', media-col-ready empty
                                    state 3, reasons media-empty-report
    cover open (roll present)       media-ready roll_current_62x0mm
                                    media-type roll,   state 5, reasons cover-open

``media-default`` is included in every payload, always on the factory value it
was measured to be stuck on, so any test that accidentally started reading it
would produce 29x90 and fail loudly.
"""

import struct

from src.services.ipp_client import _attr

_TAG_KEYWORD = 0x44
_TAG_TEXT = 0x41
_TAG_OCTET_STRING = 0x30
_TAG_INTEGER = 0x21
_TAG_BOOLEAN = 0x22
_TAG_ENUM = 0x23
_TAG_RANGE = 0x33
_TAG_BEGIN_COLLECTION = 0x34
_TAG_END_COLLECTION = 0x37
_TAG_MEMBER_ATTR_NAME = 0x4A

_TAG_PRINTER_ATTRS = 0x04
_TAG_END_OF_ATTRS = 0x03

# Continuous media reports its length as a rangeOfInteger, not a number.
CONTINUOUS_LENGTH_RANGE = struct.pack(">ii", 1270, 100000)

FACTORY_MEDIA_DEFAULT = b"om_brother-label-29x90mm_29x90mm"


def member(tag: int, name: bytes, value: bytes) -> bytes:
    """One collection member: its name, then its value."""
    return _attr(_TAG_MEMBER_ATTR_NAME, b"", name) + _attr(tag, b"", value)


def collection(name: bytes, members: bytes) -> bytes:
    """A named IPP collection wrapping ``members``."""
    return (_attr(_TAG_BEGIN_COLLECTION, name, b"")
            + members
            + _attr(_TAG_END_COLLECTION, b"", b""))


def nested_collection(name: bytes, members: bytes) -> bytes:
    """A collection that is itself the value of a member."""
    return (_attr(_TAG_MEMBER_ATTR_NAME, b"", name)
            + _attr(_TAG_BEGIN_COLLECTION, b"", b"")
            + members
            + _attr(_TAG_END_COLLECTION, b"", b""))


def response(attr_payload: bytes, status: int = 0x0000, request_id: int = 1) -> bytes:
    """Prefix the 8-byte IPP response header and the printer-attributes group."""
    return (b"\x02\x00" + struct.pack(">H", status) + struct.pack(">I", request_id)
            + bytes([_TAG_PRINTER_ATTRS]) + attr_payload + bytes([_TAG_END_OF_ATTRS]))


def media_size(x_hundredths: int, y_value: bytes, y_tag: int = _TAG_INTEGER) -> bytes:
    """The nested media-size collection, dimensions in hundredths of a mm."""
    members = member(_TAG_INTEGER, b"x-dimension", struct.pack(">i", x_hundredths))
    members += member(y_tag, b"y-dimension", y_value)
    return nested_collection(b"media-size", members)


def media_col_ready(media_type: bytes, size: bytes) -> bytes:
    """media-col-ready as the QL-820NWB emits it, margins and all."""
    members = member(_TAG_KEYWORD, b"media-type", media_type)
    members += size
    members += member(_TAG_INTEGER, b"media-bottom-margin", struct.pack(">i", 303))
    members += member(_TAG_INTEGER, b"media-left-margin", struct.pack(">i", 154))
    members += member(_TAG_INTEGER, b"media-right-margin", struct.pack(">i", 154))
    members += member(_TAG_INTEGER, b"media-top-margin", struct.pack(">i", 303))
    members += member(_TAG_KEYWORD, b"media-source", b"main")
    members += member(_TAG_BOOLEAN, b"media-auto-dimension", b"\x00")
    members += nested_collection(
        b"media-source-properties",
        member(_TAG_KEYWORD, b"media-source-feed-direction", b"short-edge-first")
        + member(_TAG_ENUM, b"media-source-feed-orientation", struct.pack(">i", 3)),
    )
    return collection(b"media-col-ready", members)


def input_tray(media_name: bytes, media_x_feed: int = 6200) -> bytes:
    """The printer-input-tray blob. dimunit really does claim micrometers."""
    blob = (b"type=sheetFeedManual;mediafeed=-1;mediaxfeed=" + str(media_x_feed).encode()
            + b";maxcapacity=-2;level=-2;status=0;name=Media;index=1;"
            + b"dimunit=micrometers;unit=sheets;medianame=" + media_name
            + b";mediaweight=-2;mediatype=stationery;mediacolor=unknown;")
    return _attr(_TAG_OCTET_STRING, b"printer-input-tray", blob)


def build(media_ready: bytes, media_col: bytes, tray: bytes,
          state: int, reasons: bytes) -> bytes:
    """Assemble a full response in the printer's own attribute order."""
    payload = _attr(_TAG_KEYWORD, b"media-ready", media_ready)
    payload += media_col
    payload += tray
    payload += _attr(_TAG_ENUM, b"printer-state", struct.pack(">i", state))
    payload += _attr(_TAG_KEYWORD, b"printer-state-reasons", reasons)
    payload += _attr(_TAG_KEYWORD, b"media-default", FACTORY_MEDIA_DEFAULT)
    payload += _attr(_TAG_TEXT, b"printer-make-and-model", b"Brother QL-820NWB")
    return response(payload)


def die_cut_24mm_round() -> bytes:
    """DK-11218: 24 mm round die-cut, printer idle."""
    return build(
        media_ready=b"om_brother-label-24x24mm_24x24mm",
        media_col=media_col_ready(b"labels", media_size(2400, struct.pack(">i", 2400))),
        tray=input_tray(b'24mm Dia / 0.94" Dia', media_x_feed=2400),
        state=3,
        reasons=b"none",
    )


def continuous_12mm() -> bytes:
    """DK-22214: 12 mm continuous tape, printer idle."""
    return build(
        media_ready=b"roll_current_12x0mm",
        media_col=media_col_ready(
            b"roll", media_size(1200, CONTINUOUS_LENGTH_RANGE, y_tag=_TAG_RANGE)),
        tray=input_tray(b'12mm / 0.47"', media_x_feed=1200),
        state=3,
        reasons=b"none",
    )


def continuous_62mm() -> bytes:
    """62 mm continuous tape, printer idle."""
    return build(
        media_ready=b"roll_current_62x0mm",
        media_col=media_col_ready(
            b"roll", media_size(6200, CONTINUOUS_LENGTH_RANGE, y_tag=_TAG_RANGE)),
        tray=input_tray(b'62mm / 2.4"'),
        state=3,
        reasons=b"none",
    )


def no_media() -> bytes:
    """No roll with the cover closed: empty media-ready, empty media-col-ready."""
    return build(
        media_ready=b"",
        media_col=collection(b"media-col-ready", b""),
        tray=input_tray(b""),
        state=3,
        reasons=b"media-empty-report",
    )


def cover_open() -> bytes:
    """Cover open with the 62 mm roll still in place: state 5, cover-open."""
    return build(
        media_ready=b"roll_current_62x0mm",
        media_col=media_col_ready(
            b"roll", media_size(6200, CONTINUOUS_LENGTH_RANGE, y_tag=_TAG_RANGE)),
        tray=input_tray(b'62mm / 2.4"'),
        state=5,
        reasons=b"cover-open",
    )


#: The five measured states, keyed by name.
PAYLOADS = {
    "die-cut-24mm-round": die_cut_24mm_round,
    "continuous-12mm": continuous_12mm,
    "continuous-62mm": continuous_62mm,
    "no-media": no_media,
    "cover-open": cover_open,
}
