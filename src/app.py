import os
import json
import secrets
import connexion
import logging
import structlog
from flask import request, jsonify
from flask_cors import CORS
from pathlib import Path

# Application version (surfaced by the /health liveness endpoint)
APP_VERSION = os.environ.get("APP_VERSION", "4.0.0-dev")

from src.utils.error_handlers import build_error_body, register_error_handlers
from src.utils.pillow_patch import apply_pillow_patch
from src.utils.auth import auth_enabled, is_valid_api_key, API_KEY_HEADER
from src.services.printer_service import printer_service
from src.services.settings_service import settings_service
from src.services.queue_service import print_queue
from src.services.relay_service import relay_service

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Set root logger level to DEBUG to capture all messages
#logging.basicConfig(level=logging.DEBUG, format='%(message)s') # Basic config for root logger

# Create logger
logger = structlog.get_logger()

# Constants
# Upload folder resolution mirrors PrinterService: prefer the UPLOAD_FOLDER env
# var, otherwise fall back to the historical code-relative default so behaviour
# is unchanged when the env var is unset. Both must agree so app.config and
# printer_service.upload_folder point at the same place.
UPLOAD_FOLDER = os.environ.get(
    "UPLOAD_FOLDER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def create_app():
    """Create and configure the Flask application with Connexion."""
    # Ensure INFO-level logs are emitted under any server. The __main__ block
    # (Flask dev server) calls logging.basicConfig, but gunicorn imports this
    # module without running __main__. Without a configured root handler Python
    # falls back to logging.lastResort (WARNING-only), which suppressed our
    # keep-alive/status INFO logs. basicConfig installs a StreamHandler at the
    # desired level (no-op if a handler is already configured).
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))

    # Run startup tasks here (not just under __main__) so the app behaves the
    # same when started by a WSGI server (gunicorn/uwsgi) as via `python app.py`.
    apply_pillow_patch()
    init_config()

    # Create the connexion application
    connexion_app = connexion.App(__name__, specification_dir='api/')

    # Get the underlying Flask app
    app = connexion_app.app

    # Resolve the Flask SECRET_KEY. Never fall back to a hard-coded default
    # ('dev'): if the env var is missing we generate a random key. Sessions then
    # do not survive a restart, which is acceptable because the app currently
    # uses no server-side sessions.
    secret_key = os.environ.get('SECRET_KEY')
    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.warning("SECRET_KEY not set – generated an ephemeral random key "
                       "(sessions will not survive a restart)")

    # Configure the app
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        UPLOAD_FOLDER=UPLOAD_FOLDER,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB max upload
    )

    # Configure CORS. We no longer expose a wildcard ('*'): when CORS_ORIGINS is
    # set (comma-separated) only those origins are allowed; otherwise we fall
    # back to same-origin only. The bundled UI is served same-origin, so it
    # keeps working without any CORS configuration.
    cors_origins_env = os.environ.get("CORS_ORIGINS")
    if cors_origins_env:
        allowed_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        CORS(app, origins=allowed_origins)
        logger.info("CORS restricted to configured origins", origins=allowed_origins)
    else:
        # No origins configured -> same-origin only (no cross-origin allowed).
        CORS(app, origins=[])
        logger.info("CORS_ORIGINS not set – allowing same-origin requests only")

    # Configure opt-in API-key authentication via a before_request hook.
    register_auth(app)

    # Add the OpenAPI specification.
    # The Swagger UI is ON by default (it is living, self-documenting API docs).
    # The docs page itself is always reachable; the protected endpoints it calls
    # require the X-API-Key when API_KEY is set (Swagger's "Authorize" button
    # injects the header). Set ENABLE_SWAGGER_UI=false to turn the UI off.
    enable_swagger_ui_env = os.environ.get("ENABLE_SWAGGER_UI")
    if enable_swagger_ui_env is not None:
        swagger_ui = enable_swagger_ui_env.lower() == "true"
    else:
        swagger_ui = True
    logger.info("Swagger UI configured", swagger_ui=swagger_ui)

    # Load the spec so we can conditionally enable API-key enforcement. connexion
    # enforces ANY declared `security`, so we only inject it when API_KEY is set;
    # otherwise the API stays open by default. When enforced, the ApiKeyAuth
    # scheme gets an x-apikeyInfoFunc and the Swagger UI shows an "Authorize"
    # button. The Swagger UI page and the OpenAPI spec themselves stay reachable
    # either way (connexion does not gate its own UI routes).
    import yaml
    spec_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'api', 'openapi.yaml')
    with open(spec_path, encoding='utf-8') as spec_file:
        spec = yaml.safe_load(spec_file)
    if auth_enabled():
        spec['security'] = [{'ApiKeyAuth': []}]
        spec['components']['securitySchemes']['ApiKeyAuth']['x-apikeyInfoFunc'] = 'src.utils.auth.apikey_info'
        logger.info("API-key authentication enforced on documented operations")
    else:
        spec.pop('security', None)
    connexion_app.add_api(spec,
                         validate_responses=True,
                         options={"swagger_ui": swagger_ui})

    # Register error handlers
    register_error_handlers(app)

    # Register routes
    register_routes(app)

    # Relay power control: let a job that arrives at a switched-off printer wait
    # in the queue while the relay is closed, instead of failing. The gate is a
    # no-op when the feature is disabled, which is the default.
    print_queue.set_pre_job_gate(relay_service.ensure_printer_powered)

    # Start the in-process print-queue worker thread (idempotent).
    print_queue.start()

    # Start the relay turn-off scheduler. It ticks once immediately, which is
    # what recovers a turn-off moment that fell due while the app was down --
    # without it, a restart mid-window would leave the relay on forever.
    relay_service.start()

    # Start the keep-alive feature (if enabled in settings)
    init_keep_alive()

    logger.info("Application initialized successfully")

    return connexion_app

# This function is now imported from utils.error_handlers

# Path prefix protected by API-key auth (matches the OpenAPI server base path).
API_PREFIX = "/api/v1"

# Path prefixes that are ALWAYS exempt from auth, even when API_KEY is set. This
# covers the bundled UI, static assets, the health probes and the Swagger UI /
# spec so they keep working unauthenticated.
AUTH_EXEMPT_PREFIXES = (
    "/api/v1/ui",            # Swagger UI (and its static assets)
    "/api/v1/openapi.json",  # OpenAPI spec served to the Swagger UI
    "/css/",
    "/js/",
)

AUTH_EXEMPT_EXACT = (
    "/",
    "/health",
    "/health/printer",
)


def _is_auth_exempt(path):
    """Return True when the request path must never require authentication."""
    if path in AUTH_EXEMPT_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in AUTH_EXEMPT_PREFIXES)


def register_auth(app):
    """Wire up the opt-in API-key authentication before_request hook."""
    if not auth_enabled():
        # Warn exactly once at startup so operators know the API is open.
        logger.warning("API_KEY not set – API is unauthenticated")
        return

    logger.info("API_KEY set – enforcing %s authentication on %s/*",
                API_KEY_HEADER, API_PREFIX)

    @app.before_request
    def enforce_api_key():
        path = request.path

        # Only guard the versioned API surface; everything else (UI, static
        # assets, health) stays open.
        if not path.startswith(API_PREFIX):
            return None

        if _is_auth_exempt(path):
            return None

        provided = request.headers.get(API_KEY_HEADER)
        if not is_valid_api_key(provided):
            # Answered in the same Error shape as everything else. This used to
            # be `{"error": "unauthorized"}` -- a fifth wheel that no client
            # could parse with the same code path as the other four responses.
            # Naming the header is safe (it is in the published specification)
            # and saves the caller a round of guessing; whether a key was sent
            # at all, or was merely wrong, is deliberately not distinguished.
            return jsonify(build_error_body(
                "UNAUTHORIZED",
                "A valid API key is required for this endpoint",
                {"header": API_KEY_HEADER},
            )), 401

        return None


def register_routes(app):
    """Register additional routes not covered by the OpenAPI specification."""
    @app.route('/')
    def index():
        return app.send_static_file('index.html')
    
    @app.route('/css/<path:filename>')
    def serve_css(filename):
        return app.send_static_file(f'css/{filename}')
    
    @app.route('/js/<path:filename>')
    def serve_js(filename):
        return app.send_static_file(f'js/{filename}')

    # Third-party assets the interface used to fetch from a CDN. They are part
    # of the app now (see static/vendor/README.md), and they need a route of
    # their own because only the directories named here are served at all.
    @app.route('/vendor/<path:filename>')
    def serve_vendor(filename):
        return app.send_static_file(f'vendor/{filename}')

    @app.route('/health')
    def health():
        """Liveness probe: the web app process is up.

        Deliberately does NOT touch the printer, so a powered-off printer never
        marks the container unhealthy. This is the endpoint the Docker
        HEALTHCHECK targets.
        """
        payload = json.dumps({"status": "pass", "version": APP_VERSION})
        return app.response_class(payload, status=200, mimetype="application/health+json")

    @app.route('/health/printer')
    def health_printer():
        """Readiness of the configured printer (separate from app liveness).

        Returns 200 when reachable (pass/warn) and 503 when not (fail), using
        the IPP-based status check. Includes a clock-drift sub-check.

        The HTTP code follows *reachability* rather than readiness on purpose: a
        printer with its cover open or no roll in it is a supply problem for a
        human to fix, not an infrastructure failure for an orchestrator to
        restart something over. Those states report "warn" with the blocking
        reason instead, alongside the media that was detected.
        """
        settings = settings_service.get_settings()
        printer_uri = settings.get("printer_uri", "")
        printer_model = settings.get("printer_model", "")

        if not printer_uri:
            body = {"status": "warn",
                    "checks": {"printer": [{"status": "warn", "output": "No printer configured"}]}}
            return app.response_class(json.dumps(body), status=200,
                                      mimetype="application/health+json")

        status = printer_service.check_printer_status(printer_uri, printer_model)
        reachable = status.get("reachable", status.get("available", False))
        blocking = status.get("blocking_reasons") or []
        details = status.get("details", {})

        if not reachable:
            printer_status = "fail"
        elif blocking:
            printer_status = "warn"
        else:
            printer_status = "pass"

        printer_check = {"status": printer_status, "output": status.get("status")}
        if details.get("printer_state"):
            printer_check["printer_state"] = details["printer_state"]
        if blocking:
            printer_check["blocking_reasons"] = blocking

        body = {"status": printer_status,
                "checks": {"printer": [printer_check]}}

        media = status.get("media")
        if media and media.get("detection"):
            body["checks"]["media"] = [{
                "status": "pass" if media.get("matches_label_size") is not False else "warn",
                "observedValue": media.get("candidates"),
                "output": media.get("reason"),
            }]
            if media.get("matches_label_size") is False and printer_status == "pass":
                body["status"] = "warn"

        clock = details.get("clock")
        if clock:
            body["checks"]["clock"] = [{
                "status": "pass" if clock.get("in_sync") else "warn",
                "observedValue": clock.get("printer_time"),
                "output": clock.get("note"),
            }]
            if reachable and clock.get("in_sync") is False and body["status"] == "pass":
                body["status"] = "warn"

        http_status = 200 if reachable else 503
        return app.response_class(json.dumps(body), status=http_status,
                                  mimetype="application/health+json")

def init_config():
    """Initialize configuration directories and files if needed."""
    # Check if initialization should be skipped (set by docker-entrypoint.sh)
    if os.environ.get('SKIP_INIT_CONFIG') == 'true':
        logger.info("SKIP_INIT_CONFIG is set, assuming entrypoint handled initialization.")
        
        # Verify the initialization flag exists in the correct data directory
        data_dir = "/app/data" # Consistent with entrypoint and settings_service
        init_flag_file = os.path.join(data_dir, ".initialized")
        
        if os.path.exists(init_flag_file):
            logger.info("Initialization flag found in data directory.")
        else:
            # This case should ideally not happen if the entrypoint runs correctly.
            logger.warning("SKIP_INIT_CONFIG is true, but initialization flag not found in data directory. Entrypoint might have failed.")
            # Optionally, create the flag here as a fallback, though it indicates an issue.
            # try:
            #     os.makedirs(data_dir, exist_ok=True)
            #     with open(init_flag_file, 'w') as f: f.write('')
            #     os.chmod(init_flag_file, 0o666)
            #     logger.info("Created missing initialization flag in data directory as fallback.")
            # except Exception as e:
            #     logger.error(f"Failed to create fallback initialization flag: {str(e)}")
        return # Skip the rest of the function

    # --- Fallback Logic (Should NOT run in standard Docker deployment) ---
    # This part is now largely redundant due to docker-entrypoint.sh handling
    # the creation of the initial settings.json in the /app/data volume.
    # Keeping it minimal or removing it entirely might be cleaner.
    # For now, just log a warning if this path is reached unexpectedly.
    logger.warning("Running fallback init_config logic. This should not happen in standard Docker deployment.")
    
    # Example: Ensure data directory exists (though entrypoint should create it)
    data_dir = "/app/data"
    if not os.path.exists(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
            logger.info(f"Created data directory at {data_dir} (fallback).")
        except Exception as e:
            logger.error(f"Failed to create data directory (fallback): {str(e)}")

def init_keep_alive():
    """Initialize keep alive feature based on settings."""
    try:
        settings = settings_service.get_settings()
        
        # Check if keep alive is enabled in settings
        if settings.get("keep_alive_enabled", False):
            printer_uri = settings.get("printer_uri")
            printer_model = settings.get("printer_model")
            interval = settings.get("keep_alive_interval", 60)
            
            # Start keep alive
            result = printer_service.start_keep_alive(printer_uri, printer_model, interval)
            
            if result.get("success", False):
                logger.info("Keep alive initialized successfully", 
                           printer_uri=printer_uri, 
                           interval=interval)
            else:
                logger.warning("Failed to initialize keep alive", 
                              message=result.get("message", "Unknown error"))
        else:
            logger.info("Keep alive is disabled in settings")
    except Exception as e:
        logger.error("Error initializing keep alive", error=str(e), exc_info=True)

if __name__ == '__main__':
    # Set up logging
    logging.basicConfig(level=logging.INFO)

    # Create and run the application (startup tasks + keep-alive run inside
    # create_app so behaviour is identical under a WSGI server).
    app = create_app()

    # Debug-Guard: the Werkzeug debugger must never be enabled in production.
    # It is only switched on when FLASK_ENV explicitly equals 'development' (and
    # is not 'production'), regardless of FLASK_DEBUG, so a stray FLASK_DEBUG=1
    # cannot accidentally expose the interactive debugger.
    flask_env = os.environ.get('FLASK_ENV')
    debug_mode = flask_env == 'development' and flask_env != 'production'
    # Disable the reloader explicitly to prevent state issues with singletons during development
    use_reloader = False
    logger.info("Starting Flask app", debug_mode=debug_mode, use_reloader=use_reloader)
    # Bind on 0.0.0.0 because we run inside a container and must be reachable
    # from outside it. Access control is NOT provided by the bind address but by
    # the opt-in API-key auth (API_KEY) and/or an upstream reverse proxy.
    app.run(host='0.0.0.0', port=5000, debug=debug_mode, use_reloader=use_reloader)
