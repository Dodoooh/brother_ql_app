"""
Tests for PrinterService.check_printer_status.

The real device communication (``get_printer_attributes``, ``guess_backend``,
``_tcp_reachable``) is mocked so the tests are deterministic and never touch the
network. ``get_printer_attributes`` is patched in the printer_service namespace
because the module imports it by name.

Two things are under test here beyond the old reachability check:

* **Availability.** A printer with its cover open answers IPP perfectly well and
  cannot print a thing; so does one with no roll in it. ``available`` used to be
  true in both cases. It now means "nothing known prevents printing", and the
  reachability signal it used to double as lives in ``reachable``.
* **Media.** The status response carries what is loaded, which label identifiers
  that could be, and whether the configured label_size is among them -- with
  "unknown" kept distinguishable from "they disagree".
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import media_payloads

from src.services.ipp_client import EMPTY_MEDIA, _parse_attributes, extract_media
from src.services.printer_service import blocking_state_reasons, printer_service


def _media_for(state_name):
    return extract_media(_parse_attributes(media_payloads.PAYLOADS[state_name]()))


def _reachable_ipp(**overrides):
    base = {
        "reachable": True,
        "make_and_model": "Brother QL-820NWB",
        "printer_state": "idle",
        "printer_state_reasons": "none",
        "current_time": datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc),
        "media": _media_for("continuous-62mm"),
    }
    base.update(overrides)
    return base


def _unreachable_ipp(**overrides):
    base = {
        "reachable": False,
        "make_and_model": None,
        "printer_state": None,
        "printer_state_reasons": None,
        "current_time": None,
        "media": dict(EMPTY_MEDIA),
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_media_cache():
    """Each test starts with a cold media cache."""
    printer_service._media_cache.clear()
    yield
    printer_service._media_cache.clear()


def _status(ipp, tcp=False, **kwargs):
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes", return_value=ipp), \
         patch.object(printer_service, "_tcp_reachable", return_value=tcp):
        return printer_service.check_printer_status(
            "tcp://192.168.1.100", "QL-820NWB", **kwargs)


# --- existing behaviour ------------------------------------------------------

def test_reachable_ipp_reports_available():
    result = _status(_reachable_ipp())

    assert result["available"] is True
    details = result["details"]
    assert details["source"] == "ipp"
    assert details["printer_state"] == "idle"
    assert details["reported_model"] == "Brother QL-820NWB"
    assert "clock" in details
    assert details["clock"]["printer_time"] is not None
    assert "is idle" in result["status"]


def test_unreachable_ipp_and_tcp_reports_not_available():
    result = _status(_unreachable_ipp(error="timed out"), tcp=False)

    assert result["available"] is False
    assert result["status"] == "Printer not reachable"
    assert result["details"]["source"] == "tcp"
    assert result["details"]["error"] == "timed out"


def test_tcp_fallback_reachable_when_ipp_fails():
    result = _status(_unreachable_ipp(), tcp=True)

    assert result["available"] is True
    assert result["details"]["source"] == "tcp"
    assert "no IPP status" in result["status"]


def test_invalid_uri_rejected_before_probe():
    # file:// is rejected by validate_printer_uri; no backend/IPP work happens.
    with patch("src.services.printer_service.get_printer_attributes") as ipp:
        result = printer_service.check_printer_status("file:///etc/passwd", "QL-800")

    assert result["available"] is False
    assert "Invalid printer URI" in result["status"]
    ipp.assert_not_called()


# --- blocking_state_reasons --------------------------------------------------

@pytest.mark.parametrize("state, reasons, expected", [
    ("idle", "none", []),
    ("idle", None, []),
    ("idle", "", []),
    ("stopped", "cover-open", ["cover-open"]),
    # The severity suffix comes off: an empty printer is empty whatever
    # severity the firmware felt like attaching.
    ("idle", "media-empty-report", ["media-empty"]),
    ("idle", "media-empty-warning", ["media-empty"]),
    ("idle", "media-jam-error", ["media-jam"]),
    ("idle", ["cover-open", "media-empty-report"], ["cover-open", "media-empty"]),
    ("idle", "cover-open, media-empty-report", ["cover-open", "media-empty"]),
    # A stop with no usable reason is still a stop.
    ("stopped", "none", ["printer-stopped"]),
    ("stopped", "other-report", ["printer-stopped"]),
    # Informational reasons are not blocking.
    ("idle", "media-low-report", []),
    ("idle", "toner-low", []),
])
def test_blocking_state_reasons(state, reasons, expected):
    assert blocking_state_reasons(state, reasons) == expected


def test_blocking_state_reasons_does_not_repeat_itself():
    assert blocking_state_reasons("stopped", "cover-open,cover-open-report") == ["cover-open"]


# --- availability for each measured state ------------------------------------

def test_idle_with_media_is_ready():
    result = _status(_reachable_ipp())

    assert result["reachable"] is True
    assert result["available"] is True
    assert result["state"] == "ready"
    assert result["blocking_reasons"] == []


def test_cover_open_is_reachable_but_not_available():
    """The bug this fixes: the printer answered, said "stopped" and
    "cover-open", and the endpoint still reported available=true."""
    result = _status(_reachable_ipp(
        printer_state="stopped",
        printer_state_reasons="cover-open",
        media=_media_for("cover-open"),
    ))

    assert result["reachable"] is True
    assert result["available"] is False
    assert result["state"] == "blocked"
    assert result["blocking_reasons"] == ["cover-open"]
    assert "cannot print" in result["status"]
    # The roll is still in the machine and still reported.
    assert result["media"]["candidates"] == ["62", "62red"]


def test_no_media_is_reachable_but_not_available():
    """No roll, cover closed: the printer reports itself *idle* and only
    media-empty-report gives it away."""
    result = _status(_reachable_ipp(
        printer_state="idle",
        printer_state_reasons="media-empty-report",
        media=_media_for("no-media"),
    ))

    assert result["reachable"] is True
    assert result["available"] is False
    assert result["state"] == "blocked"
    assert result["blocking_reasons"] == ["media-empty"]
    assert result["media"]["detection"] == "no-media"
    assert result["media"]["candidates"] == []
    assert result["media"]["matches_label_size"] is None


def test_unreachable_is_neither_reachable_nor_available():
    result = _status(_unreachable_ipp(error="timed out"), tcp=False)

    assert result["reachable"] is False
    assert result["available"] is False
    assert result["state"] == "unreachable"
    assert result["media"]["detection"] == "unreachable"
    assert result["media"]["matches_label_size"] is None


def test_tcp_only_reachability_leaves_readiness_unknown():
    """A device that accepts a connection but says nothing: readiness is not
    known, and the response says so instead of guessing either way."""
    result = _status(_unreachable_ipp(), tcp=True)

    assert result["reachable"] is True
    assert result["available"] is True
    assert result["state"] == "unknown"
    assert result["media"]["detection"] == "unsupported"


def test_invalid_uri_is_unreachable_with_unknown_media():
    with patch("src.services.printer_service.get_printer_attributes"):
        result = printer_service.check_printer_status("file:///etc/passwd", "QL-800")

    assert result["reachable"] is False
    assert result["available"] is False
    assert result["state"] == "unreachable"
    assert result["media"]["detection"] == "unreachable"


def test_a_usb_printer_is_ready_but_cannot_report_media():
    class _Backend:
        def __init__(self, uri):
            pass

        def dispose(self):
            pass

    with patch("src.services.printer_service.guess_backend", return_value="pyusb"), \
         patch("src.services.printer_service.backend_factory",
               return_value={"backend_class": _Backend}):
        result = printer_service.check_printer_status("usb://0x04f9:0x209b", "QL-820NWB")

    assert result["reachable"] is True
    assert result["available"] is True
    assert result["state"] == "ready"
    assert result["media"]["detection"] == "unsupported"
    assert result["media"]["matches_label_size"] is None


def test_a_failing_backend_is_unreachable():
    with patch("src.services.printer_service.guess_backend", return_value="pyusb"), \
         patch("src.services.printer_service.backend_factory",
               side_effect=RuntimeError("no such device")):
        result = printer_service.check_printer_status("usb://0x04f9:0x209b", "QL-820NWB")

    assert result["reachable"] is False
    assert result["available"] is False
    assert result["state"] == "unreachable"


# --- the media section -------------------------------------------------------

def test_media_is_carried_with_the_candidates_and_the_comparison():
    result = _status(_reachable_ipp(), label_size="62")
    media = result["media"]

    assert media["width_mm"] == 62.0
    assert media["media_type"] == "roll"
    assert media["media_name"] == '62mm / 2.4"'
    assert media["is_round"] is False
    assert media["source"] == "media-col-ready"
    assert media["detected"] is True
    assert media["detection"] == "ok"
    assert media["candidates"] == ["62", "62red"]
    assert media["ambiguous"] is True
    assert media["label_size"] == "62"
    assert media["matches_label_size"] is True


def test_an_ambiguous_medium_matches_either_candidate():
    for label_size in ("62", "62red"):
        result = _status(_reachable_ipp(), label_size=label_size)
        assert result["media"]["matches_label_size"] is True


def test_a_disagreement_is_reported_as_false_not_unknown():
    result = _status(_reachable_ipp(), label_size="d24")
    assert result["media"]["matches_label_size"] is False
    assert result["media"]["candidates"] == ["62", "62red"]


def test_a_die_cut_roll_is_identified_from_the_real_payload():
    result = _status(_reachable_ipp(media=_media_for("die-cut-24mm-round")),
                     label_size="d24")
    media = result["media"]

    assert media["candidates"] == ["d24"]
    assert media["ambiguous"] is False
    assert media["is_round"] is True
    assert media["matches_label_size"] is True


def test_media_this_app_does_not_support_is_reported_as_unidentified():
    unknown = {
        "width_mm": 40.0, "length_mm": 0.0, "media_type": "roll",
        "media_name": "40mm", "is_round": False, "source": "media-col-ready",
    }
    result = _status(_reachable_ipp(media=unknown), label_size="62")
    media = result["media"]

    assert media["detection"] == "unidentified"
    assert media["detected"] is False
    assert media["candidates"] == []
    assert media["matches_label_size"] is None
    assert media["width_mm"] == 40.0


def test_the_configured_label_size_is_used_when_the_request_omits_one():
    with patch("src.services.printer_service.settings_service.get_settings",
               return_value={"label_size": "62red"}):
        result = _status(_reachable_ipp())

    assert result["media"]["label_size"] == "62red"
    assert result["media"]["matches_label_size"] is True


def test_an_explicit_label_size_wins_over_the_configured_one():
    with patch("src.services.printer_service.settings_service.get_settings",
               return_value={"label_size": "62"}):
        result = _status(_reachable_ipp(), label_size="d24")

    assert result["media"]["label_size"] == "d24"
    assert result["media"]["matches_label_size"] is False


def test_a_broken_settings_file_does_not_fail_the_status_check():
    with patch("src.services.printer_service.settings_service.get_settings",
               side_effect=OSError("no settings")):
        result = _status(_reachable_ipp())

    assert result["media"]["label_size"] is None
    assert result["media"]["matches_label_size"] is None


# --- the media cache ---------------------------------------------------------

def test_a_status_check_costs_a_single_ipp_request():
    """The media rides along with the status attributes; reading it must not
    add a second round-trip to a call the UI makes every 30 seconds."""
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes",
               return_value=_reachable_ipp()) as ipp, \
         patch("src.services.printer_service.get_media_ready") as media_read:
        printer_service.check_printer_status("tcp://192.168.1.100", "QL-820NWB")

    assert ipp.call_count == 1
    media_read.assert_not_called()


def test_a_status_check_warms_the_media_cache():
    _status(_reachable_ipp())

    with patch("src.services.printer_service.get_media_ready") as media_read:
        media = printer_service.get_loaded_media("tcp://192.168.1.100")

    media_read.assert_not_called()
    assert media["width_mm"] == 62.0


def test_a_cold_cache_reads_the_media_once_and_then_serves_it():
    loaded = _media_for("continuous-12mm")
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_media_ready",
               return_value=loaded) as media_read:
        first = printer_service.get_loaded_media("tcp://192.168.1.100")
        second = printer_service.get_loaded_media("tcp://192.168.1.100")

    assert media_read.call_count == 1
    assert first == second == loaded


def test_the_cache_is_kept_per_uri():
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_media_ready",
               side_effect=[_media_for("continuous-12mm"),
                            _media_for("continuous-62mm")]):
        first = printer_service.get_loaded_media("tcp://192.168.1.100")
        second = printer_service.get_loaded_media("tcp://10.50.60.21")

    assert first["width_mm"] == 12.0
    assert second["width_mm"] == 62.0


def test_a_stale_reading_expires():
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_media_ready",
               side_effect=[_media_for("continuous-12mm"),
                            _media_for("continuous-62mm")]), \
         patch("src.services.printer_service.MEDIA_CACHE_TTL_SECONDS", 0.0):
        first = printer_service.get_loaded_media("tcp://192.168.1.100")
        second = printer_service.get_loaded_media("tcp://192.168.1.100")

    assert first["width_mm"] == 12.0
    assert second["width_mm"] == 62.0


def test_an_unreachable_printer_forgets_its_cached_media():
    """Remembered media for a printer that is no longer there would be exactly
    the confident wrong answer this feature exists to avoid."""
    _status(_reachable_ipp())
    assert printer_service.get_loaded_media("tcp://192.168.1.100")["width_mm"] == 62.0

    result = _status(_unreachable_ipp(), tcp=False)

    assert result["media"] == {
        **EMPTY_MEDIA,
        "detected": False,
        "detection": "unreachable",
        "candidates": [],
        "ambiguous": False,
        "reason": result["media"]["reason"],
        "label_size": result["media"]["label_size"],
        "matches_label_size": None,
        # Nothing was identified, so nothing resolves and nothing is switched.
        "resolution": {"label_size": None, "resolved_by": None,
                       "reason": result["media"]["resolution"]["reason"]},
        "auto_switch": {"enabled": False, "action": "none",
                        "from": result["media"]["label_size"], "to": None,
                        "reason": result["media"]["auto_switch"]["reason"]},
    }
    assert "tcp://192.168.1.100" not in printer_service._media_cache


def test_a_non_network_printer_reports_no_media_without_a_probe():
    with patch("src.services.printer_service.guess_backend", return_value="pyusb"), \
         patch("src.services.printer_service.get_media_ready") as media_read:
        media = printer_service.get_loaded_media("usb://0x04f9:0x209b")

    assert media == EMPTY_MEDIA
    media_read.assert_not_called()


def test_an_invalid_uri_is_never_probed_for_media():
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_media_ready") as media_read:
        media = printer_service.get_loaded_media("tcp://169.254.169.254")

    assert media == EMPTY_MEDIA
    media_read.assert_not_called()


# --- the controller ----------------------------------------------------------

def test_the_controller_passes_the_requested_label_size_through():
    from src.api import printer_controller

    with patch.object(printer_controller.printer_service, "check_printer_status",
                      return_value={"available": True}) as check:
        printer_controller.check_printer_status({
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-820NWB",
            "label_size": "d24",
        })

    check.assert_called_once_with("tcp://192.168.1.100", "QL-820NWB", label_size="d24")


def test_the_controller_leaves_the_label_size_to_the_service_when_omitted():
    from src.api import printer_controller

    with patch.object(printer_controller.printer_service, "check_printer_status",
                      return_value={"available": True}) as check:
        printer_controller.check_printer_status({
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-820NWB",
        })

    check.assert_called_once_with("tcp://192.168.1.100", "QL-820NWB", label_size=None)


def test_the_controller_rejects_a_non_string_label_size():
    from src.api import printer_controller
    from src.utils.exceptions import ValidationError

    with pytest.raises(ValidationError):
        printer_controller.check_printer_status({
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-820NWB",
            "label_size": 62,
        })


# --- callers of the old `available` meaning ----------------------------------

def test_a_dry_run_reports_reachability_not_readiness():
    """``available`` used to mean "the device answered" and the dry run read it
    for exactly that. A blocked printer is still reachable."""
    from src.utils import dry_run

    blocked = {
        "available": False,
        "reachable": True,
        "state": "blocked",
        "blocking_reasons": ["cover-open"],
        "media": {"candidates": ["62", "62red"], "matches_label_size": True},
    }
    with patch.object(dry_run.printer_service, "check_printer_status", return_value=blocked):
        response = dry_run.build_dry_run_response({"label_size": "62", "copies": 1})

    assert response["printer_reachable"] is True
    assert response["media"]["candidates"] == ["62", "62red"]


def test_a_dry_run_reports_an_unreachable_printer_as_unreachable():
    from src.utils import dry_run

    unreachable = {"available": False, "reachable": False, "state": "unreachable",
                   "media": {"detection": "unreachable"}}
    with patch.object(dry_run.printer_service, "check_printer_status", return_value=unreachable):
        response = dry_run.build_dry_run_response({"label_size": "62"})

    assert response["printer_reachable"] is False


def test_a_dry_run_survives_a_status_check_that_raises():
    from src.utils import dry_run

    with patch.object(dry_run.printer_service, "check_printer_status",
                      side_effect=RuntimeError("boom")):
        response = dry_run.build_dry_run_response({"label_size": "62"})

    assert response["printer_reachable"] is False
    assert "media" not in response
