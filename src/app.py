import os
import connexion
import logging
import structlog
from flask_cors import CORS
from pathlib import Path

from src.utils.error_handlers import register_error_handlers
from src.utils.pillow_patch import apply_pillow_patch
from src.services.printer_service import printer_service
from src.services.settings_service import settings_service

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
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def create_app():
    """Create and configure the Flask application with Connexion."""
    # Create the connexion application
    connexion_app = connexion.App(__name__, specification_dir='api/')
    
    # Get the underlying Flask app
    app = connexion_app.app
    
    # Configure the app
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev'),
        UPLOAD_FOLDER=UPLOAD_FOLDER,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16 MB max upload
    )
    
    # Enable CORS
    CORS(app)
    
    # Add the OpenAPI specification
    connexion_app.add_api('openapi.yaml', 
                         validate_responses=True,
                         options={"swagger_ui": True})
    
    # Register error handlers
    register_error_handlers(app)
    
    # Register routes
    register_routes(app)
    
    logger.info("Application initialized successfully")
    
    return connexion_app

# This function is now imported from utils.error_handlers

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
    
    # Apply Pillow patch for compatibility with newer versions
    apply_pillow_patch()
    
    # Initialize configuration
    init_config()
    
    # Create and run the application
    app = create_app()
    
    # Initialize keep alive feature
    init_keep_alive()
    
    # Use Flask's debug setting, which might influence logging levels too
    # Determine debug mode based on environment variables
    debug_mode = os.environ.get('FLASK_ENV') == 'development' or os.environ.get('FLASK_DEBUG') == '1'
    # Disable the reloader explicitly to prevent state issues with singletons during development
    use_reloader = False
    logger.info("Starting Flask app", debug_mode=debug_mode, use_reloader=use_reloader)
    app.run(host='0.0.0.0', port=5000, debug=debug_mode, use_reloader=use_reloader)
