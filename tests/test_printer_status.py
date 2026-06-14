"""
Tests for PrinterService.check_printer_status.

The real device communication (``get_printer_attributes``, ``guess_backend``,
``_tcp_reachable``) is mocked so the tests are deterministic and never touch the
network. ``get_printer_attributes`` is patched in the printer_service namespace
because the module imports it by name.
"""

from datetime import datetime, timezone
from unittest.mock import patch

from src.services.printer_service import printer_service


def _reachable_ipp(**overrides):
    base = {
        "reachable": True,
        "make_and_model": "Brother QL-820NWB",
        "printer_state": "idle",
        "printer_state_reasons": "none",
        "current_time": datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_reachable_ipp_reports_available():
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes", return_value=_reachable_ipp()):
        result = printer_service.check_printer_status("tcp://10.50.60.20", "QL-820NWB")

    assert result["available"] is True
    details = result["details"]
    assert details["source"] == "ipp"
    assert details["printer_state"] == "idle"
    assert details["reported_model"] == "Brother QL-820NWB"
    assert "clock" in details
    assert details["clock"]["printer_time"] is not None
    assert "is idle" in result["status"]


def test_unreachable_ipp_and_tcp_reports_not_available():
    unreachable = {
        "reachable": False,
        "make_and_model": None,
        "printer_state": None,
        "printer_state_reasons": None,
        "current_time": None,
        "error": "timed out",
    }
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes", return_value=unreachable), \
         patch.object(printer_service, "_tcp_reachable", return_value=False):
        result = printer_service.check_printer_status("tcp://10.50.60.20", "QL-800")

    assert result["available"] is False
    assert result["status"] == "Printer not reachable"
    assert result["details"]["source"] == "tcp"
    assert result["details"]["error"] == "timed out"


def test_tcp_fallback_reachable_when_ipp_fails():
    unreachable = {
        "reachable": False,
        "make_and_model": None,
        "printer_state": None,
        "printer_state_reasons": None,
        "current_time": None,
    }
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes", return_value=unreachable), \
         patch.object(printer_service, "_tcp_reachable", return_value=True):
        result = printer_service.check_printer_status("tcp://10.50.60.20", "QL-800")

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
