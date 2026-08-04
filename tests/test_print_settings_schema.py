"""The multipart routes are held to the same settings schema as the JSON ones.

A file upload carries ``settings`` as a string in a form field, so the OpenAPI
specification can describe the field but not what is inside it. That gap meant
the two ways of printing the same label disagreed about what was allowed:
``copies: 101`` was refused with a 400 on ``/text/print`` and accepted with a
200 and a job id on ``/image/print``, only to fail in the worker minutes later.

These tests hold the two routes to the same answer, and hold the validator to
reading its rules from the specification rather than from a second copy of them.
"""

import json

import pytest

from src.utils.exceptions import ValidationError
from src.utils.print_settings_schema import (
    parse_and_validate_settings,
    validate_print_settings,
)


# --------------------------------------------------------------------------- #
# What must pass
# --------------------------------------------------------------------------- #

def test_an_empty_object_is_valid():
    """Everything is optional: the saved configuration fills the rest in."""
    assert parse_and_validate_settings("{}") == {}


def test_a_missing_field_is_valid():
    """Not sending settings at all is the ordinary case, not an error."""
    assert parse_and_validate_settings(None) == {}


def test_ordinary_settings_pass():
    raw = json.dumps({"label_size": "62", "copies": 2, "threshold": 70.0,
                      "cut_mode": "each", "dither": True})
    assert parse_and_validate_settings(raw)["copies"] == 2


def test_keys_the_schema_does_not_name_are_left_alone():
    """Layout hints and the like ride along; the schema is not a whitelist.

    ``resolve_print_settings`` documents that extra keys are preserved, and
    several endpoints rely on it, so validation must not start rejecting them.
    """
    raw = json.dumps({"label_size": "62", "qr_position": "side", "some_hint": 3})
    parsed = parse_and_validate_settings(raw)
    assert parsed["qr_position"] == "side"
    assert parsed["some_hint"] == 3


# --------------------------------------------------------------------------- #
# What must not
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("settings, expected", [
    ({"copies": 101}, "maximum"),
    ({"copies": 0}, "minimum"),
    ({"copies": "viele"}, "not of type 'integer'"),
    ({"label_size": "gibtsnicht"}, "is not one of"),
    ({"cut_mode": "sometimes"}, "is not one of"),
])
def test_a_violation_is_refused_and_named(settings, expected):
    with pytest.raises(ValidationError) as exc:
        validate_print_settings(settings)
    message = str(exc.value)
    assert expected in message
    assert "settings." in message, "the offending field is not named"


def test_several_violations_are_counted_rather_than_concatenated():
    """One readable sentence beats five glued together."""
    with pytest.raises(ValidationError) as exc:
        validate_print_settings({"copies": 101, "label_size": "gibtsnicht"})
    assert "and 1 more" in str(exc.value)


def test_a_field_that_is_not_json_is_refused():
    with pytest.raises(ValidationError) as exc:
        parse_and_validate_settings("{not json")
    assert "Invalid settings JSON" in str(exc.value)


@pytest.mark.parametrize("raw", ['"just a string"', "[1, 2, 3]", "42", "null"])
def test_a_field_that_is_not_an_object_is_refused(raw):
    with pytest.raises(ValidationError):
        parse_and_validate_settings(raw)


# --------------------------------------------------------------------------- #
# Where the rules come from
# --------------------------------------------------------------------------- #

def test_the_rules_are_read_from_the_specification():
    """Not a second hand-written copy, which would drift out of sight.

    The check: a limit that exists only in openapi.yaml is enforced here. If
    somebody widens `copies` in the specification, this module follows without
    being touched -- and if it ever stops reading the file, this fails.
    """
    import yaml

    from src.utils.print_settings_schema import _SPEC_PATH

    with open(_SPEC_PATH, encoding="utf-8") as handle:
        spec = yaml.safe_load(handle)
    maximum = spec["components"]["schemas"]["PrintSettings"]["properties"]["copies"]["maximum"]

    validate_print_settings({"copies": maximum})          # the limit itself is fine
    with pytest.raises(ValidationError):
        validate_print_settings({"copies": maximum + 1})  # one past it is not


def test_validation_that_cannot_run_lets_the_print_through(monkeypatch):
    """A specification that cannot be read must not make printing impossible.

    This module exists to close a gap, not to become a new way for the app to
    stop working. With no validator, behaviour falls back to what these
    endpoints did before it existed.
    """
    from src.utils import print_settings_schema as module

    monkeypatch.setattr(module, "_VALIDATOR", False)
    validate_print_settings({"copies": 99999})  # must not raise
