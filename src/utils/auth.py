"""API-Key authentication helpers.

Authentication is *opt-in*: it is only enforced when the ``API_KEY`` environment
variable is set. Enforcement itself lives in a Flask ``before_request`` hook in
``app.py`` (see the ``enforce_api_key`` handler) rather than in connexion
security schemes, which would be too invasive and would also gate the bundled
UI and the health probes.
"""

import os
import hmac

# Header carrying the client-supplied API key.
API_KEY_HEADER = "X-API-Key"


def get_expected_api_key():
    """Return the configured API key, or ``None`` when auth is disabled.

    Reads ``os.environ`` on every call so that the value is never cached at
    import time (relevant for tests and for WSGI workers spawned after the
    environment was prepared).
    """
    key = os.environ.get("API_KEY")
    if key is None:
        return None
    # Treat an empty / whitespace-only value as "not set" so an accidentally
    # blank env var does not silently disable every request.
    key = key.strip()
    return key or None


def auth_enabled():
    """Whether API-key authentication is enforced."""
    return get_expected_api_key() is not None


def is_valid_api_key(provided):
    """Constant-time comparison of a provided key against the expected one.

    Returns ``False`` when auth is disabled or when ``provided`` is missing.
    """
    expected = get_expected_api_key()
    if expected is None or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def apikey_info(apikey, required_scopes=None):
    """connexion ``x-apikeyInfoFunc`` callback.

    Only wired into the OpenAPI spec when API_KEY is set (see ``create_app``),
    so connexion enforces the X-API-Key header on the documented operations and
    the Swagger UI "Authorize" button works. Returns an identity dict for a
    valid key, or ``None`` (→ connexion 401) for an invalid one.
    """
    if is_valid_api_key(apikey):
        return {"sub": "apikey"}
    return None
