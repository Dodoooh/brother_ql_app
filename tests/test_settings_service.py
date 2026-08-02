"""
Tests for src/services/settings_service.py::SettingsService.

These tests always use a temporary settings file (``tmp_path``) — never the real
``/app/data`` or ``data/settings.json``. ``brother_ql.backends.guess_backend`` is
patched where keep-alive validation is exercised so the tests behave identically
whether or not the real brother_ql package is installed (the conftest stub maps
tcp:// -> "network" and usb:// -> "pyusb", matching the patched behaviour).
"""

import json
from unittest.mock import patch

import pytest

from src.services.settings_service import SettingsService


def _valid_settings(**overrides):
    base = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "font_size": 50,
        "alignment": "left",
        "rotate": 0,
        "threshold": 70.0,
        "dither": False,
        "compress": False,
        "red": False,
        "keep_alive_enabled": False,
        "keep_alive_interval": 60,
        "ipp_port": 631,
        "printers": [
            {
                "id": "default",
                "name": "Default Printer",
                "printer_uri": "tcp://192.168.1.100",
                "printer_model": "QL-800",
                "label_size": "62",
            }
        ],
    }
    base.update(overrides)
    return base


def _make_service(tmp_path, settings=None):
    path = tmp_path / "settings.json"
    if settings is not None:
        path.write_text(json.dumps(settings), encoding="utf-8")
    return SettingsService(settings_file=str(path)), path


def _net_backend(uri):
    """Deterministic guess_backend used in keep-alive validation tests."""
    return "network" if str(uri).startswith("tcp://") else "pyusb"


# --- mtime cache -------------------------------------------------------------

def test_get_settings_uses_mtime_cache(tmp_path):
    service, _ = _make_service(tmp_path, _valid_settings())

    # The constructor already called _load_settings once; reset the counter.
    with patch.object(service, "_load_settings", wraps=service._load_settings) as load:
        first = service.get_settings()
        second = service.get_settings()
        # Same unchanged file -> only ONE disk load across both calls.
        assert load.call_count == 1

    assert first["printer_uri"] == "tcp://192.168.1.100"
    assert second["printer_uri"] == "tcp://192.168.1.100"


def test_cache_refreshes_after_save_settings(tmp_path):
    service, _ = _make_service(tmp_path, _valid_settings())
    assert service.get_settings()["font_size"] == 50

    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        assert service.save_settings(_valid_settings(font_size=99)) is True

    # Fresh value visible immediately (cache refreshed on save).
    assert service.get_settings()["font_size"] == 99


def test_update_settings_merges_and_persists(tmp_path):
    service, path = _make_service(tmp_path, _valid_settings())

    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        assert service.update_settings({"alignment": "center"}) is True

    settings = service.get_settings()
    assert settings["alignment"] == "center"
    # Untouched key preserved through the merge.
    assert settings["printer_model"] == "QL-800"
    # Persisted to disk.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["alignment"] == "center"


# --- validation --------------------------------------------------------------

def test_valid_settings_pass_validation(tmp_path):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        # Should not raise.
        service._validate_settings(_valid_settings())


def test_ipp_port_out_of_range_rejected(tmp_path):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        with pytest.raises(ValueError):
            service._validate_settings(_valid_settings(ipp_port=70000))


def test_empty_printer_uri_rejected(tmp_path):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        with pytest.raises(ValueError):
            service._validate_settings(_valid_settings(printer_uri="   "))


def test_file_scheme_uri_rejected(tmp_path):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        with pytest.raises(ValueError):
            service._validate_settings(_valid_settings(printer_uri="file:///etc/passwd"))


def test_keep_alive_with_non_network_uri_rejected(tmp_path):
    service, _ = _make_service(tmp_path)
    settings = _valid_settings(
        printer_uri="usb://0x04f9:0x209c",
        keep_alive_enabled=True,
        keep_alive_interval=60,
        printers=[
            {
                "id": "default",
                "name": "USB Printer",
                "printer_uri": "usb://0x04f9:0x209c",
                "printer_model": "QL-800",
                "label_size": "62",
            }
        ],
    )
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        with pytest.raises(ValueError):
            service._validate_settings(settings)


def test_save_settings_returns_false_on_invalid(tmp_path):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        # save_settings swallows validation errors and returns False.
        assert service.save_settings(_valid_settings(ipp_port=0)) is False


# --- owned media and the media memory ----------------------------------------
#
# Both are checked against the media catalogue, unlike calibration and bleed.
# Those may legitimately carry entries for media that is not loaded now, and an
# entry that matches nothing simply never applies; these two help *choose* a
# label size on the user's behalf, so an entry naming nothing real can only
# mislead. Rejection therefore names the offending value.

def _validate(tmp_path, **overrides):
    service, _ = _make_service(tmp_path)
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        service._validate_settings(_valid_settings(**overrides))


def test_owned_media_accepts_known_identifiers(tmp_path):
    _validate(tmp_path, owned_media=["62red", "12+17", "d24", "62x29"])


def test_owned_media_may_be_empty(tmp_path):
    _validate(tmp_path, owned_media=[])


def test_owned_media_rejects_an_unknown_identifier_by_name(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, owned_media=["62red", "62-red"])
    assert "62-red" in str(excinfo.value)


def test_owned_media_rejects_a_non_string_entry(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, owned_media=[62])
    assert "62" in str(excinfo.value)


def test_owned_media_rejects_a_non_list(tmp_path):
    with pytest.raises(ValueError):
        _validate(tmp_path, owned_media={"62red": True})


def test_media_memory_accepts_a_variant_of_its_own_medium(tmp_path):
    _validate(tmp_path, media_memory={"62": "62red", "12": "12+17", "103": "104"})


def test_media_memory_accepts_a_medium_pinned_to_its_plain_variant(tmp_path):
    _validate(tmp_path, media_memory={"62": "62"})


def test_media_memory_rejects_an_unknown_medium_by_name(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, media_memory={"64": "62red"})
    assert "64" in str(excinfo.value)


def test_media_memory_rejects_a_variant_used_as_the_key(tmp_path):
    """62red is one way of addressing the 62 mm medium, not a medium of its own.
    Keying on it would split one roll's history across two entries."""
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, media_memory={"62red": "62red"})
    message = str(excinfo.value)
    assert "62red" in message and "'62'" in message


def test_media_memory_rejects_an_unknown_label_as_the_value(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, media_memory={"62": "62-red"})
    assert "62-red" in str(excinfo.value)


def test_media_memory_rejects_a_value_from_a_different_medium(tmp_path):
    """The entry that would actually break a print: a 62 mm roll can never be a
    d24, so remembering one against the other could only ever switch to a label
    size the loaded medium cannot use."""
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, media_memory={"62": "d24"})
    message = str(excinfo.value)
    assert "d24" in message and "62red" in message  # names the offender and the options


def test_media_memory_rejects_a_non_string_value(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        _validate(tmp_path, media_memory={"62": None})
    assert "62" in str(excinfo.value)


def test_media_auto_switch_must_be_a_boolean(tmp_path):
    with pytest.raises(ValueError):
        _validate(tmp_path, media_auto_switch="yes")


def test_a_settings_file_predating_the_feature_still_loads(tmp_path):
    """An older settings file has none of the three keys; the defaults are
    merged in, off."""
    service, _ = _make_service(tmp_path, _valid_settings())
    settings = service.get_settings()

    assert settings["media_auto_switch"] is False
    assert settings["owned_media"] == []
    assert settings["media_memory"] == {}


def test_a_settings_file_predating_the_feature_still_validates(tmp_path):
    service, _ = _make_service(tmp_path)
    stored = _valid_settings()
    for key in ("media_auto_switch", "owned_media", "media_memory"):
        assert key not in stored
    with patch("src.services.settings_service.guess_backend", side_effect=_net_backend):
        service._validate_settings(stored)


# --- deepcopy isolation ------------------------------------------------------

def test_returned_settings_are_isolated_from_cache(tmp_path):
    service, _ = _make_service(tmp_path, _valid_settings())

    first = service.get_settings()
    first["printer_model"] = "MUTATED"
    first["printers"][0]["label_size"] = "MUTATED"

    second = service.get_settings()
    assert second["printer_model"] == "QL-800"
    assert second["printers"][0]["label_size"] == "62"


def test_missing_file_falls_back_to_defaults(tmp_path):
    # No file written -> defaults are returned, nothing cached.
    service, _ = _make_service(tmp_path, settings=None)
    settings = service.get_settings()
    assert settings["printer_model"] == "QL-800"
    assert "printers" in settings
