"""
What a client is told when something goes wrong.

Two questions are pinned down here, one per half of the file:

1. **Shape.** Every error -- ours, Connexion's request validation, Werkzeug's
   404/405/413, the API-key check -- comes back as the ``Error`` schema the
   specification declares: ``code``, ``message``, ``details`` (an object), as
   ``application/json``. Before, four different shapes were in circulation and
   three of them were undeclared, so a client had to recognise all four.

2. **Content.** A message that helps the caller survives ("printer_uri is a
   required property", "label_size is not in the list", the printer's own
   refusal). A message that only describes the inside of the server does not:
   no absolute paths, no exception text, no library wording. What disappears
   from the response appears in the log instead, tied to it by ``error_id``.

The tests drive the real application through its test client, so they exercise
the actual handler registration -- which matters, because Connexion registers a
handler per status code and Flask prefers those over a class-based one. A test
that called the handler functions directly would pass while 404 and 405 still
came back as RFC-7807 problem documents.
"""

import importlib
import json
import os
import tempfile
from unittest.mock import patch

import pytest

# Keep the app's import-time upload folder out of the working tree, and skip the
# /app/data fallback the Docker entrypoint normally owns.
os.environ.setdefault("UPLOAD_FOLDER", tempfile.mkdtemp(prefix="bql-error-tests-"))
os.environ.setdefault("SKIP_INIT_CONFIG", "true")

# The whole file needs a real Flask/Connexion stack; conftest's stand-ins are not
# enough to build an application.
pytest.importorskip("connexion")
pytest.importorskip("flask")

from src.utils.error_handlers import build_error_body  # noqa: E402
from src.utils.exceptions import PrinterError  # noqa: E402

API = "/api/v1"

# A body that satisfies the PrinterStatusRequest schema, so the request reaches
# the controller and the *controller's* error is what gets tested.
VALID_STATUS_BODY = {"printer_uri": "tcp://10.20.30.40", "printer_model": "QL-800"}


@pytest.fixture(scope="module")
def flask_app():
    """The real application, with Flask's error handling left switched on.

    ``TESTING`` alone would make Flask re-raise exceptions instead of routing
    them through the handlers -- convenient for a stack trace, useless for a
    test about what the client receives -- so propagation is turned off
    explicitly.
    """
    from src.app import create_app

    app = create_app().app
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    return app


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture()
def printer_controller(flask_app):
    """The controller module Connexion actually bound to the route.

    The specification names operations ``api.printer_controller.…`` while the
    test suite imports ``src.api.printer_controller``; both exist as separate
    module objects because ``.`` and ``src`` are both on the path. Patching the
    wrong one patches nothing the request will ever reach.
    """
    return importlib.import_module("api.printer_controller")


def assert_error_shape(response, status, code):
    """Assert the common Error shape and return the parsed body."""
    assert response.status_code == status
    # Not application/problem+json: 404/405/413 used to come back as RFC-7807.
    assert response.mimetype == "application/json"

    body = response.get_json()
    assert set(body) == {"code", "message", "details"}, body
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"].strip(), \
        "a message a human can read is part of the contract"
    assert isinstance(body["details"], dict), \
        "details is an object in the schema; it used to be a bare string"
    return body


# --------------------------------------------------------------------------- #
# One shape for every kind of failure
# --------------------------------------------------------------------------- #

def test_a_missing_required_field_is_reported_in_the_common_shape(client):
    """Connexion's own validation error, in our clothes.

    This response used to be ``{"code": "CONNEXION_400", "message": "",
    "details": "'printer_uri' is a required property"}``: the message empty, the
    explanation hidden in a field the schema declares as an object. The text is
    now where every client already looks for it.
    """
    response = client.post(f"{API}/printers/status", json={})

    body = assert_error_shape(response, 400, "VALIDATION_ERROR")
    assert "printer_uri" in body["message"], \
        "the caller must be told which field is missing"


def test_an_unknown_path_is_reported_in_the_common_shape(client):
    response = client.get(f"{API}/there-is-no-such-endpoint")

    assert_error_shape(response, 404, "RESOURCE_NOT_FOUND")


def test_a_wrong_method_is_reported_in_the_common_shape(client):
    """/printers/status is a POST; asking for it with GET is a 405, not a problem document."""
    response = client.get(f"{API}/printers/status")

    assert_error_shape(response, 405, "METHOD_NOT_ALLOWED")


def test_an_oversized_upload_is_reported_in_the_common_shape(client):
    """Past MAX_CONTENT_LENGTH (16 MB) Werkzeug aborts the request itself."""
    too_much = b"x" * (17 * 1024 * 1024)
    response = client.post(f"{API}/image/print", data=too_much,
                           content_type="multipart/form-data; boundary=nope")

    assert_error_shape(response, 413, "PAYLOAD_TOO_LARGE")


def test_a_missing_api_key_is_reported_in_the_common_shape(monkeypatch):
    """The fifth shape: the key check used to answer ``{"error": "unauthorized"}``."""
    monkeypatch.setenv("API_KEY", "s3cret-key-for-this-test")

    from src.app import create_app

    app = create_app().app
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False

    response = app.test_client().post(f"{API}/printers/status",
                                      json=VALID_STATUS_BODY)

    body = assert_error_shape(response, 401, "UNAUTHORIZED")
    assert "s3cret" not in json.dumps(body), "the expected key is never echoed"


# --------------------------------------------------------------------------- #
# Messages that help the caller survive
# --------------------------------------------------------------------------- #

def test_a_validation_error_keeps_the_sentence_that_helps(client, printer_controller):
    """A bad *input* is explained: the caller can only fix what it is told about."""
    complaint = "label_size 'zz' is not in the list of supported labels"
    with patch.object(printer_controller.printer_service, "check_printer_status",
                      side_effect=ValueError(complaint)):
        response = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)

    body = assert_error_shape(response, 400, "VALIDATION_ERROR")
    assert complaint in body["message"]


def test_a_printer_error_keeps_the_sentence_that_helps(client, printer_controller):
    """The device's own refusal is the whole point of the response."""
    refusal = "Error sending to printer: the cover is open"
    with patch.object(printer_controller.printer_service, "check_printer_status",
                      side_effect=PrinterError(refusal)):
        response = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)

    # PRINTERERROR is the pre-existing code AppError derives from the class
    # name; only the envelope around it changed here.
    body = assert_error_shape(response, 500, "PRINTERERROR")
    assert refusal in body["message"]


# --------------------------------------------------------------------------- #
# Messages that only describe the inside of the server do not survive
# --------------------------------------------------------------------------- #

def test_an_unexpected_exception_says_nothing_about_itself(client, printer_controller):
    """The controller used to re-raise this as ``PrinterError(f"...: {e}")``.

    That was wrong twice over: it blamed the printer for a fault of the app, and
    it copied the exception verbatim into a 500 -- module paths, library wording
    and all. What is left is a fixed sentence and a token for finding the real
    record in the log.
    """
    boom = RuntimeError(
        "Timeout in /app/src/services/printer_service.py while calling pysnmp")
    with patch.object(printer_controller.printer_service, "check_printer_status",
                      side_effect=boom):
        response = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)

    body = assert_error_shape(response, 500, "INTERNAL_SERVER_ERROR")
    assert body["message"] == "An internal error occurred"

    serialized = json.dumps(body)
    assert "pysnmp" not in serialized
    assert "printer_service.py" not in serialized
    assert "/app/" not in serialized
    assert "Traceback" not in serialized

    # ... but the response can still be tied to the log record that has it all.
    assert body["details"]["error_id"]


def test_an_internal_error_does_not_repeat_itself(client, printer_controller):
    """Two requests that fail the same way get two different correlation ids."""
    with patch.object(printer_controller.printer_service, "check_printer_status",
                      side_effect=RuntimeError("boom")):
        first = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)
        second = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)

    assert (first.get_json()["details"]["error_id"]
            != second.get_json()["details"]["error_id"])


def test_a_server_path_never_reaches_the_client(client, printer_controller):
    """The 400 that carried an absolute path.

    ``pdf_renderer`` names the temporary upload in the ValueError it raises for
    an unreadable PDF, and that ValueError legitimately becomes a 400 -- so the
    path was echoed to whoever uploaded a broken file. The explanation survives;
    the path does not.
    """
    leaky = ValueError(
        "Could not open PDF '/app/uploads/9f2c.pdf': password required")
    with patch.object(printer_controller.printer_service, "check_printer_status",
                      side_effect=leaky):
        response = client.post(f"{API}/printers/status", json=VALID_STATUS_BODY)

    body = assert_error_shape(response, 400, "VALIDATION_ERROR")
    assert "/app/uploads" not in json.dumps(body)
    assert "<path>" in body["message"]
    assert "password required" in body["message"], "the reason still helps"


# --------------------------------------------------------------------------- #
# The scrubber itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message, expected", [
    ("Could not open PDF '/app/uploads/jobs/ab.pdf': broken",
     "Could not open PDF '<path>': broken"),
    ("Failed to save settings to /app/data/settings.json",
     "Failed to save settings to <path>"),
    ("cannot identify image file '/tmp/x/y.png'",
     "cannot identify image file '<path>'"),
])
def test_the_scrubber_removes_server_paths(message, expected):
    assert build_error_body("X", message)["message"] == expected


@pytest.mark.parametrize("message", [
    # A printer URI is the user's own configuration and must stay readable --
    # this is the message that explains what a valid one looks like.
    "printer_uri must start with tcp://, usb:// or file:///dev/usb/lp0",
    # A URL is not a filesystem path, even when a segment shares a name.
    "Relay webhook http://192.168.1.42/app/relay/0 returned HTTP 502",
    # Ordinary complaints about input have nothing to redact.
    "label_size 'zz' is not in the list of supported labels",
    "copies exceeds the maximum of 100",
])
def test_the_scrubber_leaves_useful_messages_alone(message):
    assert build_error_body("X", message)["message"] == message


def test_details_are_always_an_object():
    """Connexion handed back a string here; the schema says object."""
    assert build_error_body("X", "y", "a bare string")["details"] == {}
    assert build_error_body("X", "y", None)["details"] == {}


def test_details_are_scrubbed_and_keep_their_types():
    body = build_error_body("X", "y", {"field": "settings", "copies": 25,
                                       "path": "/app/uploads/x.png"})

    assert body["details"]["field"] == "settings"
    assert body["details"]["copies"] == 25, "numbers stay numbers"
    assert body["details"]["path"] == "<path>"


def test_a_5xx_keeps_its_status_but_not_its_words():
    """A 503 stays a 503 -- an orchestrator reads the difference -- and still
    says nothing about the server beyond the id that finds the log record.

    Built on its own application because Flask refuses to register a route on
    one that has already served a request, and the shared fixture has.
    """
    from werkzeug.exceptions import ServiceUnavailable

    from src.app import create_app

    app = create_app().app
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/__test__/unavailable")
    def _unavailable():
        raise ServiceUnavailable("the SNMP socket at /app/run/snmp.sock is gone")

    response = app.test_client().get("/__test__/unavailable")

    body = assert_error_shape(response, 503, "SERVICE_UNAVAILABLE")
    assert body["message"] == "An internal error occurred"
    assert "snmp" not in json.dumps(body)
    assert body["details"]["error_id"]
