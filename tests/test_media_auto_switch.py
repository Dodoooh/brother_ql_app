"""
Tests for resolving an ambiguous medium and for following the printer.

Detection stops at "these are the identifiers the report is consistent with".
Automatic switching needs one, and everything here is about where that extra bit
of information comes from -- because it must never come from a coin flip. Three
sources are consulted in a fixed order, each of them something the *user* said:
what was last used on this medium, what they own, and the documented plain
variant. When none of them settles it, nothing is chosen; that case has its own
tests, because it is the property that makes the feature defensible.

Everything runs offline: the media dicts come from the payloads captured from a
QL-820NWB (``tests/media_payloads.py``), and the printer, the settings file and
the clock are all mocked.
"""

import json
from unittest.mock import patch

import pytest

import media_payloads

from src.config.default_settings import DEFAULT_SETTINGS, medium_key
from src.services.ipp_client import EMPTY_MEDIA, _parse_attributes, extract_media
from src.services.printer_service import (
    MEDIA_RESOLVED_BY_DEFAULT,
    MEDIA_RESOLVED_BY_DETECTION,
    MEDIA_RESOLVED_BY_MEMORY,
    MEDIA_RESOLVED_BY_OWNED,
    MEDIA_SWITCH_AMBIGUOUS,
    MEDIA_SWITCH_APPLY,
    MEDIA_SWITCH_NONE,
    media_memory_key,
    owned_media,
    printer_service,
    resolve_media_label,
)
from src.services.settings_service import SettingsService


# The three media a printer cannot pin down, plain variant first. The plain
# variant is the documented default and the key the memory is stored under.
AMBIGUOUS_PAIRS = [("62", "62red"), ("12", "12+17"), ("103", "104")]

# A pair the catalogue never produces together. It stands in for an ambiguity
# nobody has documented -- a future catalogue could create one -- and exercises
# the real code path for "two candidates, no group, no default".
UNDOCUMENTED_PAIR = ("62x29", "60x86")


def _media_for(state_name):
    return extract_media(_parse_attributes(media_payloads.PAYLOADS[state_name]()))


def _settings(**overrides):
    """Saved settings as the app ships them, with the overrides applied."""
    base = {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-820NWB",
        "label_size": "62",
        "media_auto_switch": DEFAULT_SETTINGS["media_auto_switch"],
        "owned_media": list(DEFAULT_SETTINGS["owned_media"]),
        "media_memory": dict(DEFAULT_SETTINGS["media_memory"]),
    }
    base.update(overrides)
    return base


# --- the shipped defaults ----------------------------------------------------

def test_automatic_switching_is_off_by_default():
    """It changes a stored setting without the user acting, so it has to be
    chosen rather than inherited from a default."""
    assert DEFAULT_SETTINGS["media_auto_switch"] is False


def test_nothing_is_owned_or_remembered_by_default():
    assert DEFAULT_SETTINGS["owned_media"] == []
    assert DEFAULT_SETTINGS["media_memory"] == {}


# --- the memory key ----------------------------------------------------------

@pytest.mark.parametrize("pair", AMBIGUOUS_PAIRS)
def test_the_memory_is_keyed_on_the_medium_not_the_candidate_list(pair):
    """The key is the plain variant, which names the physical roll. It does not
    move when the catalogue does."""
    assert media_memory_key(pair) == pair[0]
    # Same key whichever end of the pair is asked about, and whatever order the
    # candidates arrive in -- the list's order is not part of the key.
    assert media_memory_key(tuple(reversed(pair))) == pair[0]
    for variant in pair:
        assert medium_key(variant) == pair[0]


def test_an_unambiguous_medium_is_keyed_on_itself():
    assert media_memory_key(("d24",)) == "d24"
    assert media_memory_key(("62x29",)) == "62x29"


def test_a_key_survives_a_catalogue_that_grows_a_variant():
    """The property the key exists for: adding a third way of addressing the
    same roll leaves every remembered choice pointing at the same medium."""
    from src.config import default_settings

    grown = ((("62", "62red", "62blue"), "hypothetical third variant"),)
    with patch.object(default_settings, "MEDIA_EQUIVALENTS", grown):
        assert medium_key("62blue") == "62"
        assert medium_key("62red") == "62"
        assert media_memory_key(("62", "62red", "62blue")) == "62"


def test_candidates_that_are_not_one_medium_get_no_key():
    """No key means no memory and no default, which is what keeps an
    undocumented ambiguity from being resolved by accident."""
    assert media_memory_key(UNDOCUMENTED_PAIR) is None
    assert media_memory_key(()) is None


# --- step 1: detection settles it on its own ---------------------------------

def test_a_single_candidate_needs_no_resolving():
    result = resolve_media_label(("d24",), _settings())
    assert result.label_size == "d24"
    assert result.resolved_by == MEDIA_RESOLVED_BY_DETECTION
    assert "only label type" in result.reason


def test_nothing_detected_resolves_to_nothing():
    result = resolve_media_label((), _settings())
    assert result.label_size is None
    assert result.resolved_by is None
    assert result.resolved is False


# --- step 2: memory ----------------------------------------------------------

@pytest.mark.parametrize("pair", AMBIGUOUS_PAIRS)
def test_memory_resolves_an_ambiguous_medium(pair):
    plain, other = pair
    result = resolve_media_label(pair, _settings(media_memory={plain: other}))
    assert result.label_size == other
    assert result.resolved_by == MEDIA_RESOLVED_BY_MEMORY
    assert other in result.reason


def test_the_user_story_a_62mm_roll_returns_to_62red():
    """62red was in use last time a 62 mm roll was loaded, so a 62 mm roll comes
    back as 62red rather than as the plain default."""
    media = _media_for("continuous-62mm")
    settings = _settings(label_size="12", media_memory={"62": "62red"})
    result = printer_service._media_report(media, "12", settings=settings)

    assert result["candidates"] == ["62", "62red"]
    assert result["ambiguous"] is True
    assert result["resolution"]["label_size"] == "62red"
    assert result["resolution"]["resolved_by"] == "memory"


def test_a_remembered_label_the_loaded_medium_cannot_be_is_ignored():
    """Stale memory -- the roll was changed, or the catalogue moved under it --
    is skipped rather than trusted."""
    result = resolve_media_label(("62", "62red"), _settings(media_memory={"62": "d24"}))
    assert result.label_size == "62"
    assert result.resolved_by == MEDIA_RESOLVED_BY_DEFAULT


def test_a_malformed_memory_never_costs_a_resolution():
    for memory in ("62red", ["62red"], {"62": 62}, {"62": ""}):
        result = resolve_media_label(("62", "62red"), _settings(media_memory=memory))
        assert result.label_size == "62"
        assert result.resolved_by == MEDIA_RESOLVED_BY_DEFAULT


# --- step 3: owned media -----------------------------------------------------

@pytest.mark.parametrize("pair", AMBIGUOUS_PAIRS)
def test_owning_exactly_one_candidate_resolves_it(pair):
    plain, other = pair
    result = resolve_media_label(pair, _settings(owned_media=[other, "d24"]))
    assert result.label_size == other
    assert result.resolved_by == MEDIA_RESOLVED_BY_OWNED
    assert "owned media list" in result.reason


def test_owning_both_candidates_settles_nothing_and_falls_through():
    result = resolve_media_label(("62", "62red"),
                                 _settings(owned_media=["62", "62red"]))
    assert result.label_size == "62"
    assert result.resolved_by == MEDIA_RESOLVED_BY_DEFAULT


def test_owning_neither_candidate_falls_through():
    result = resolve_media_label(("62", "62red"), _settings(owned_media=["d24"]))
    assert result.label_size == "62"
    assert result.resolved_by == MEDIA_RESOLVED_BY_DEFAULT


def test_ownership_can_resolve_a_medium_that_has_no_documented_default():
    """Ownership does not need the group table, so it still works where the
    plain-variant default does not exist."""
    result = resolve_media_label(UNDOCUMENTED_PAIR,
                                 _settings(owned_media=["60x86"]))
    assert result.label_size == "60x86"
    assert result.resolved_by == MEDIA_RESOLVED_BY_OWNED


def test_a_malformed_owned_list_is_ignored_rather_than_fatal():
    assert owned_media({"owned_media": "62red"}) == ()
    assert owned_media({"owned_media": ["62red", 62, "", None]}) == ("62red",)
    assert owned_media(None) == ()


# --- step 4: the documented plain-variant default ----------------------------

@pytest.mark.parametrize("plain, other", AMBIGUOUS_PAIRS)
def test_the_plain_variant_is_the_default_for_every_known_pair(plain, other):
    result = resolve_media_label((plain, other), _settings())
    assert result.label_size == plain
    assert result.resolved_by == MEDIA_RESOLVED_BY_DEFAULT
    assert other in result.reason  # says what would have to be chosen deliberately


# --- the order ---------------------------------------------------------------

def test_memory_beats_ownership():
    settings = _settings(media_memory={"62": "62"}, owned_media=["62red"])
    result = resolve_media_label(("62", "62red"), settings)
    assert (result.label_size, result.resolved_by) == ("62", MEDIA_RESOLVED_BY_MEMORY)


def test_memory_beats_the_default():
    settings = _settings(media_memory={"12": "12+17"})
    result = resolve_media_label(("12", "12+17"), settings)
    assert (result.label_size, result.resolved_by) == ("12+17", MEDIA_RESOLVED_BY_MEMORY)


def test_ownership_beats_the_default():
    settings = _settings(owned_media=["104"])
    result = resolve_media_label(("103", "104"), settings)
    assert (result.label_size, result.resolved_by) == ("104", MEDIA_RESOLVED_BY_OWNED)


def test_all_three_steps_agree_when_they_all_apply():
    """Nothing pathological about the steps overlapping: the earliest one is
    reported, and the answer is the same."""
    settings = _settings(media_memory={"62": "62"}, owned_media=["62"])
    result = resolve_media_label(("62", "62red"), settings)
    assert (result.label_size, result.resolved_by) == ("62", MEDIA_RESOLVED_BY_MEMORY)


# --- when nothing resolves ---------------------------------------------------

def test_an_ambiguity_nothing_resolves_chooses_nothing():
    """The property the whole feature rests on. Two candidates, no memory, no
    ownership and no documented default: the app declines to pick."""
    result = resolve_media_label(UNDOCUMENTED_PAIR, _settings())

    assert result.label_size is None
    assert result.resolved_by is None
    assert result.resolved is False
    assert "cannot be told apart" in result.reason


def test_an_unresolvable_ambiguity_leaves_the_setting_alone():
    settings = _settings(media_auto_switch=True, label_size="d24")
    unresolved = resolve_media_label(UNDOCUMENTED_PAIR, settings)
    assert unresolved.label_size is None

    # Reported through the ordinary status path, with a medium that today's
    # catalogue cannot produce standing in for one a future catalogue could.
    with patch("src.services.printer_service.resolve_media_label",
               return_value=unresolved):
        report = printer_service._media_report(_media_for("continuous-62mm"),
                                               "d24", settings=settings)

    assert report["auto_switch"]["action"] == MEDIA_SWITCH_AMBIGUOUS
    assert report["auto_switch"]["to"] is None
    assert report["resolution"]["label_size"] is None
    # The medium is still fully reported -- the app not knowing which of two
    # labels is loaded is not a reason to stop saying what is.
    assert report["detected"] is True
    assert report["width_mm"] == 62.0


# --- ownership narrows, it never censors -------------------------------------

def test_a_medium_the_user_does_not_own_is_still_reported_and_identified():
    """The list narrows ambiguity; it does not decide what may be seen. A roll
    the user forgot to list is still in the machine."""
    settings = _settings(owned_media=["d24"], media_auto_switch=True)
    report = printer_service._media_report(_media_for("continuous-62mm"), "d24",
                                           settings=settings)

    assert report["detected"] is True
    assert report["detection"] == "ok"
    assert report["width_mm"] == 62.0
    assert report["media_type"] == "roll"
    assert report["candidates"] == ["62", "62red"]
    assert report["matches_label_size"] is False
    # And it is still acted on: the unowned roll resolves to its plain variant.
    assert report["resolution"]["label_size"] == "62"
    assert report["auto_switch"]["action"] == MEDIA_SWITCH_APPLY
    assert report["auto_switch"]["to"] == "62"


def test_an_owned_list_naming_none_of_the_candidates_reports_the_die_cut_too():
    settings = _settings(owned_media=["62"])
    report = printer_service._media_report(_media_for("die-cut-24mm-round"), "62",
                                           settings=settings)

    assert report["candidates"] == ["d24"]
    assert report["is_round"] is True
    assert report["resolution"]["label_size"] == "d24"
    assert report["resolution"]["resolved_by"] == MEDIA_RESOLVED_BY_DETECTION


# --- the auto_switch block ---------------------------------------------------

def _status(ipp, settings, tcp=False, **kwargs):
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.get_printer_attributes", return_value=ipp), \
         patch("src.services.printer_service.settings_service.get_settings",
               return_value=settings), \
         patch.object(printer_service, "_tcp_reachable", return_value=tcp):
        return printer_service.check_printer_status(
            "tcp://192.168.1.100", "QL-820NWB", **kwargs)


def _reachable(media_state="continuous-62mm", **overrides):
    base = {
        "reachable": True,
        "make_and_model": "Brother QL-820NWB",
        "printer_state": "idle",
        "printer_state_reasons": "none",
        "current_time": None,
        "media": _media_for(media_state),
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_media_cache():
    printer_service._media_cache.clear()
    yield
    printer_service._media_cache.clear()


def test_a_mismatch_is_switched_when_automatic_mode_is_on():
    settings = _settings(media_auto_switch=True, label_size="d24")
    result = _status(_reachable(), settings)
    switch = result["media"]["auto_switch"]

    assert switch["enabled"] is True
    assert switch["action"] == MEDIA_SWITCH_APPLY
    assert switch["from"] == "d24"
    assert switch["to"] == "62"
    assert result["media"]["resolution"]["resolved_by"] == MEDIA_RESOLVED_BY_DEFAULT


def test_the_memory_decides_which_candidate_is_switched_to():
    settings = _settings(media_auto_switch=True, label_size="d24",
                         media_memory={"62": "62red"})
    switch = _status(_reachable(), settings)["media"]["auto_switch"]

    assert switch["action"] == MEDIA_SWITCH_APPLY
    assert switch["to"] == "62red"
    assert "last used" in switch["reason"]


def test_a_label_type_already_consistent_with_the_roll_is_left_alone():
    """A user printing on 62red is not moved to 62 for turning this on: 62red is
    one of the things a 62 mm roll can be."""
    settings = _settings(media_auto_switch=True, label_size="62red")
    result = _status(_reachable(), settings)

    assert result["media"]["matches_label_size"] is True
    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_NONE
    assert result["media"]["auto_switch"]["to"] is None
    # The resolution is still reported; it is just not acted on.
    assert result["media"]["resolution"]["label_size"] == "62"


def test_an_empty_bay_switches_nothing():
    settings = _settings(media_auto_switch=True, label_size="62")
    result = _status(_reachable(media_state="no-media",
                                printer_state_reasons="media-empty-report"), settings)

    assert result["media"]["detection"] == "no-media"
    assert result["media"]["resolution"]["label_size"] is None
    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_NONE


def test_an_unreachable_printer_switches_nothing():
    settings = _settings(media_auto_switch=True, label_size="62")
    unreachable = {"reachable": False, "make_and_model": None, "printer_state": None,
                   "printer_state_reasons": None, "current_time": None,
                   "media": dict(EMPTY_MEDIA)}
    result = _status(unreachable, settings)

    assert result["media"]["detection"] == "unreachable"
    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_NONE


def test_media_this_app_does_not_support_switches_nothing():
    settings = _settings(media_auto_switch=True, label_size="62")
    unknown = {"width_mm": 40.0, "length_mm": 0.0, "media_type": "roll",
               "media_name": "40mm", "is_round": False, "source": "media-col-ready"}
    result = _status(_reachable(media=unknown), settings)

    assert result["media"]["detection"] == "unidentified"
    assert result["media"]["resolution"]["label_size"] is None
    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_NONE


def test_a_usb_printer_reports_no_switch():
    class _Backend:
        def __init__(self, uri):
            pass

        def dispose(self):
            pass

    with patch("src.services.printer_service.guess_backend", return_value="pyusb"), \
         patch("src.services.printer_service.settings_service.get_settings",
               return_value=_settings(media_auto_switch=True)), \
         patch("src.services.printer_service.backend_factory",
               return_value={"backend_class": _Backend}):
        result = printer_service.check_printer_status("usb://0x04f9:0x209b", "QL-820NWB")

    assert result["media"]["detection"] == "unsupported"
    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_NONE


# --- automatic mode off means nothing changes --------------------------------

def test_with_automatic_mode_off_the_status_response_behaves_exactly_as_before():
    """Every field the endpoint carried before this feature, with the values it
    carried, and not one instruction to change anything."""
    result = _status(_reachable(), _settings(label_size="d24"))
    media = result["media"]

    assert result["available"] is True
    assert result["reachable"] is True
    assert result["state"] == "ready"
    assert media["detected"] is True
    assert media["detection"] == "ok"
    assert media["width_mm"] == 62.0
    assert media["media_type"] == "roll"
    assert media["candidates"] == ["62", "62red"]
    assert media["ambiguous"] is True
    assert media["label_size"] == "d24"
    assert media["matches_label_size"] is False

    assert media["auto_switch"]["enabled"] is False
    assert media["auto_switch"]["action"] == MEDIA_SWITCH_NONE
    assert media["auto_switch"]["to"] is None
    assert "off" in media["auto_switch"]["reason"]


def test_with_automatic_mode_off_a_mismatch_is_reported_and_not_acted_on():
    for label_size in ("62", "62red", "d24", "12"):
        media = _status(_reachable(), _settings(label_size=label_size))["media"]
        assert media["auto_switch"]["action"] == MEDIA_SWITCH_NONE
        assert media["auto_switch"]["to"] is None


def test_a_status_check_never_writes_the_settings():
    """The server reports the resolution; the client owns label_size. Two
    writers for one value is a race, and a read that edits the configuration is
    a surprise."""
    settings = _settings(media_auto_switch=True, label_size="d24")
    with patch("src.services.printer_service.settings_service.update_settings") as update, \
         patch("src.services.printer_service.settings_service.save_settings") as save:
        result = _status(_reachable(), settings)

    assert result["media"]["auto_switch"]["action"] == MEDIA_SWITCH_APPLY
    update.assert_not_called()
    save.assert_not_called()


def test_a_status_check_with_the_feature_off_still_costs_one_ipp_request():
    with patch("src.services.printer_service.guess_backend", return_value="network"), \
         patch("src.services.printer_service.settings_service.get_settings",
               return_value=_settings()), \
         patch("src.services.printer_service.get_printer_attributes",
               return_value=_reachable()) as ipp, \
         patch("src.services.printer_service.get_media_ready") as media_read:
        printer_service.check_printer_status("tcp://192.168.1.100", "QL-820NWB")

    assert ipp.call_count == 1
    media_read.assert_not_called()


# --- recording what the user settled on --------------------------------------

def _record(new_settings, previous_settings, media_state="continuous-62mm"):
    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for(media_state)) as loaded:
        result = printer_service.record_label_choice(new_settings, previous_settings)
    return result, loaded


def test_a_settled_choice_is_remembered_against_the_loaded_medium():
    result, _ = _record(_settings(media_auto_switch=True, label_size="62red"),
                        _settings(media_auto_switch=True, label_size="12"))
    assert result == {"media_memory": {"62": "62red"}}


def test_remembering_one_medium_leaves_the_others_alone():
    previous = _settings(media_auto_switch=True, label_size="12",
                         media_memory={"12": "12+17", "103": "104"})
    new = dict(previous, label_size="62red")
    result, _ = _record(new, previous)
    assert result == {"media_memory": {"12": "12+17", "103": "104", "62": "62red"}}


def test_a_label_size_that_did_not_change_is_not_a_choice():
    result, loaded = _record(_settings(media_auto_switch=True, label_size="62red"),
                             _settings(media_auto_switch=True, label_size="62red"))
    assert result == {}
    loaded.assert_not_called()


def test_a_choice_the_loaded_medium_cannot_be_is_not_recorded():
    """Picking a die-cut label while a 62 mm roll is loaded is preparing a job
    for a roll that is not in the machine -- it says nothing about this one."""
    result, _ = _record(_settings(media_auto_switch=True, label_size="d24"),
                        _settings(media_auto_switch=True, label_size="62"))
    assert result == {}


def test_nothing_is_recorded_when_no_medium_is_loaded():
    result, _ = _record(_settings(media_auto_switch=True, label_size="62red"),
                        _settings(media_auto_switch=True, label_size="12"),
                        media_state="no-media")
    assert result == {}


def test_a_choice_already_remembered_is_not_rewritten():
    previous = _settings(media_auto_switch=True, label_size="12",
                         media_memory={"62": "62red"})
    result, _ = _record(dict(previous, label_size="62red"), previous)
    assert result == {}


def test_a_memory_edit_carried_by_the_write_itself_is_not_clobbered():
    previous = _settings(media_auto_switch=True, label_size="12",
                         media_memory={"62": "62red", "12": "12+17"})
    new = dict(previous, label_size="62", media_memory={"103": "104"})
    result, _ = _record(new, previous)
    assert result == {"media_memory": {"103": "104", "62": "62"}}


def test_a_printer_that_cannot_be_asked_costs_nothing():
    with patch.object(printer_service, "get_loaded_media",
                      side_effect=RuntimeError("no route to host")):
        result = printer_service.record_label_choice(
            _settings(media_auto_switch=True, label_size="62red"),
            _settings(media_auto_switch=True, label_size="12"))
    assert result == {}


def test_with_automatic_mode_off_nothing_is_recorded_and_the_printer_is_not_asked():
    """Off means off: no extra keys in the settings file, and no round trip on
    the wire that the app did not make before."""
    result, loaded = _record(_settings(label_size="62red"),
                             _settings(label_size="12"))
    assert result == {}
    loaded.assert_not_called()


# --- the hook, end to end ----------------------------------------------------

def _service(tmp_path, settings):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    service = SettingsService(settings_file=str(path))
    service.register_update_hook(printer_service.record_label_choice)
    return service, path


def test_a_settings_write_carries_the_memory_with_it(tmp_path):
    """One write, not two: a memory saved separately could be saved when the
    choice was not, and the two would then disagree."""
    service, path = _service(tmp_path, _settings(media_auto_switch=True, label_size="12"))

    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for("continuous-62mm")):
        assert service.update_settings({"label_size": "62red"}) is True

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["label_size"] == "62red"
    assert stored["media_memory"] == {"62": "62red"}


def test_a_settings_write_with_the_feature_off_stores_no_memory(tmp_path):
    service, path = _service(tmp_path, _settings(label_size="12"))

    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for("continuous-62mm")) as loaded:
        assert service.update_settings({"label_size": "62red"}) is True

    loaded.assert_not_called()
    assert json.loads(path.read_text(encoding="utf-8"))["media_memory"] == {}


def test_a_failing_hook_never_costs_the_user_their_change(tmp_path):
    """A hook is an enrichment. An enrichment that breaks must not lose the
    change the user actually asked for."""
    def _explode(_new_settings, _previous_settings):
        raise RuntimeError("boom")

    service, path = _service(tmp_path, _settings(media_auto_switch=True, label_size="12"))
    service.register_update_hook(_explode)

    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for("continuous-62mm")):
        assert service.update_settings({"label_size": "62red"}) is True

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["label_size"] == "62red"
    # The hook that did work still contributed.
    assert stored["media_memory"] == {"62": "62red"}


def test_the_remembered_choice_comes_back_when_the_roll_does(tmp_path):
    """The whole point, in one test: 62red is chosen on a 62 mm roll, a 12 mm
    roll goes in and is switched to, and the 62 mm roll comes back as 62red."""
    service, _ = _service(tmp_path, _settings(media_auto_switch=True, label_size="62"))

    # The user settles on 62red while the 62 mm roll is loaded.
    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for("continuous-62mm")):
        assert service.update_settings({"label_size": "62red"}) is True
    settings = service.get_settings()
    assert settings["media_memory"] == {"62": "62red"}

    # A 12 mm roll goes in: it does not match, and it resolves to its own plain
    # variant.
    twelve = printer_service._media_report(_media_for("continuous-12mm"),
                                           settings["label_size"], settings=settings)
    assert twelve["auto_switch"]["action"] == MEDIA_SWITCH_APPLY
    assert twelve["auto_switch"]["to"] == "12"

    with patch.object(printer_service, "get_loaded_media",
                      return_value=_media_for("continuous-12mm")):
        assert service.update_settings({"label_size": "12"}) is True
    settings = service.get_settings()
    assert settings["media_memory"] == {"62": "62red", "12": "12"}

    # The 62 mm roll comes back, and so does 62red -- not the plain default.
    back = printer_service._media_report(_media_for("continuous-62mm"),
                                         settings["label_size"], settings=settings)
    assert back["resolution"]["resolved_by"] == MEDIA_RESOLVED_BY_MEMORY
    assert back["auto_switch"]["action"] == MEDIA_SWITCH_APPLY
    assert back["auto_switch"]["to"] == "62red"


def test_ownership_carries_the_first_switch_before_anything_is_remembered(tmp_path):
    """A user who has never chosen anything for this medium is not guessed at if
    they said which tape they own."""
    settings = _settings(media_auto_switch=True, label_size="d24",
                         owned_media=["62red", "d24"])
    report = printer_service._media_report(_media_for("continuous-62mm"), "d24",
                                           settings=settings)

    assert report["resolution"]["resolved_by"] == MEDIA_RESOLVED_BY_OWNED
    assert report["auto_switch"]["to"] == "62red"
