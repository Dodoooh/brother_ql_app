"""Validate a ``settings`` object against the spec, on the multipart paths.

Why this exists
---------------
The JSON print endpoints hand their whole body to Connexion, which validates it
against ``PrintSettings`` in ``openapi.yaml`` before any controller sees it. The
multipart endpoints cannot: a file upload carries ``settings`` as a *string* in
a form field, and a string is all the spec can describe. So everything inside
that string went unchecked, and the two ways of printing the same label
disagreed about what was allowed::

    copies: 101   ->  /text/print   400, "greater than the maximum of 100"
                  ->  /image/print  200, queued, and failed minutes later

The second answer is the damaging one. The caller is told the job is on its way,
the failure surfaces only if it goes looking, and the limits the spec publishes
turn out to hold on one route and not the other.

This module closes that by validating the parsed object against the *same*
schema, read from ``openapi.yaml`` at first use. Deliberately not a second
hand-written copy of the rules: a copy drifts, and the drift is invisible until
somebody hits it.
"""

import json
import os
from typing import Any, Dict, Optional

import structlog

from src.utils.exceptions import ValidationError

logger = structlog.get_logger()

# Resolved once, then reused. None means "not loaded yet"; False means "tried
# and could not", which is remembered so a broken or missing spec costs one
# warning rather than one per request.
_VALIDATOR: Optional[Any] = None

_SPEC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "api", "openapi.yaml")


def _build_validator() -> Optional[Any]:
    """Load ``PrintSettings`` from the spec and wrap it in a validator.

    Returns None when the spec, the schema or the validation library is not
    available. Validation is then skipped, which is exactly the behaviour these
    endpoints had before this module existed -- a missing spec must not make
    printing impossible.
    """
    try:
        import jsonschema
        import yaml
    except ImportError as e:  # pragma: no cover - both ship with connexion
        logger.warning("No JSON schema validation available for multipart settings",
                       error=str(e))
        return None

    try:
        with open(_SPEC_PATH, encoding="utf-8") as handle:
            spec = yaml.safe_load(handle)
        schema = spec["components"]["schemas"]["PrintSettings"]
    except Exception as e:  # noqa: BLE001 - any failure means "cannot validate"
        logger.warning("Could not read PrintSettings from the API specification",
                       path=_SPEC_PATH, error=str(e))
        return None

    # The schema references others ($ref into components), so the validator is
    # given the whole document to resolve against rather than the fragment.
    resolver = jsonschema.RefResolver.from_schema(spec)
    return jsonschema.Draft4Validator(schema, resolver=resolver)


def _describe(error: Any) -> str:
    """Render one schema violation the way the JSON endpoints render theirs."""
    where = ".".join(str(part) for part in error.absolute_path)
    field = f"settings.{where}" if where else "settings"
    return f"{error.message} - '{field}'"


def validate_print_settings(settings: Dict[str, Any]) -> None:
    """Check a parsed multipart ``settings`` object against the specification.

    Args:
        settings: The object parsed out of the ``settings`` form field, before
            it is merged with the saved configuration. Merged values are the
            app's own and are not the caller's to be told about.

    Raises:
        ValidationError: On the first violation, naming the field and the rule
            it broke, in the same shape the JSON endpoints produce.
    """
    global _VALIDATOR
    if _VALIDATOR is None:
        _VALIDATOR = _build_validator() or False
    if _VALIDATOR is False:
        return
    if not isinstance(settings, dict):
        raise ValidationError("settings must be an object", "settings")

    errors = sorted(_VALIDATOR.iter_errors(settings), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    # One message, like Connexion's: the first violation in document order,
    # with the rest counted rather than concatenated into something unreadable.
    first = _describe(errors[0])
    if len(errors) > 1:
        first = f"{first} (and {len(errors) - 1} more)"
    raise ValidationError(first, "settings")


def parse_and_validate_settings(raw: Optional[str]) -> Dict[str, Any]:
    """Parse the ``settings`` form field and validate it.

    Args:
        raw: The raw form field, or None when it was not sent at all (which is
            allowed: everything is then inherited from the saved settings).

    Returns:
        The parsed object, unmerged.

    Raises:
        ValidationError: When the field is not JSON, is not an object, or
            breaks the published schema.
    """
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ValidationError("Invalid settings JSON", "settings") from None
    if not isinstance(parsed, dict):
        raise ValidationError("settings must be an object", "settings")
    validate_print_settings(parsed)
    return parsed
