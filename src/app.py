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
    """Initialize configuration directories and files."""
    config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
    os.makedirs(config_dir, exist_ok=True)
    
    # Ensure settings file exists
    settings_file = os.path.join(config_dir, "settings.json")
    if not os.path.exists(settings_file):
        import json
        from src.config.default_settings import DEFAULT_SETTINGS
        with open(settings_file, 'w') as f:
            json.dump(DEFAULT_SETTINGS, f, indent=4)
        logger.info(f"Created default settings file at {settings_file}")

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
    
    app.run(host='0.0.0.0', port=5000, debug=True)
