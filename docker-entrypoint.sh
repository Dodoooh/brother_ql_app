#!/bin/bash
set -e

echo "Starting Brother QL Printer App container..."

# Define the persistent data directory
DATA_DIR="/app/data"
SETTINGS_FILE="$DATA_DIR/settings.json"
SETTINGS_BACKUP_FILE="$DATA_DIR/settings.json.backup"
INIT_FLAG_FILE="$DATA_DIR/.initialized"

# Ensure the data directory exists (Docker volume mount should handle this, but belt-and-suspenders)
mkdir -p "$DATA_DIR"
# Permissions on the host volume mount point are more critical.
# We assume the container user can write to the mounted volume.

# Function to check if a JSON file contains default settings
is_default_settings() {
    local file=$1
    # Check if the file exists before trying to grep it
    if [ ! -f "$file" ]; then
        echo "File $file does not exist, assuming default."
        return 0 # True, it's default (or non-existent)
    fi
    # Check for a specific default value
    if grep -q '"printer_uri": "tcp://192.168.1.100"' "$file"; then
        echo "File $file contains default printer_uri"
        return 0  # True, it's default
    else
        echo "File $file contains custom printer_uri"
        return 1  # False, it's not default
    fi
}

# No default settings.json is written here on purpose. The application creates
# its own defaults in memory when the file is absent: settings_service._load_
# settings() returns a deep copy of DEFAULT_SETTINGS when the file does not
# exist, and the first PUT /settings persists a real file. A hand-maintained
# copy of the defaults in this shell script only drifts from DEFAULT_SETTINGS
# (it did -- it was missing every setting added since) while changing nothing at
# runtime, because _load_settings overlays the file onto DEFAULT_SETTINGS and
# fills in whatever the file omits. So the single source of truth stays in
# src/config/default_settings.py and the first start with an empty data
# directory just runs on the in-memory defaults.

# --- Settings Initialization Logic ---

# Check if settings.json exists in the data directory
if [ ! -f "$SETTINGS_FILE" ]; then
    # Settings file doesn't exist
    if [ -f "$SETTINGS_BACKUP_FILE" ]; then
        # Backup exists, restore from it
        echo "$SETTINGS_FILE missing but backup exists. Restoring from $SETTINGS_BACKUP_FILE..."
        cp "$SETTINGS_BACKUP_FILE" "$SETTINGS_FILE"
        chmod 666 "$SETTINGS_FILE"
        echo "Restored settings from backup."
    else
        # No settings file and no backup: leave it absent. The application runs
        # on its in-memory DEFAULT_SETTINGS until the first PUT /settings writes
        # a real file (see the note above create_default_settings removal).
        echo "$SETTINGS_FILE not found and no backup exists. The app will run on in-memory defaults."
    fi
else
    # Settings file exists, check if it contains default settings
    if is_default_settings "$SETTINGS_FILE"; then
        # Contains default settings
        if [ -f "$SETTINGS_BACKUP_FILE" ]; then
            # Backup exists, restore from it (overwriting defaults)
            echo "Found default settings in $SETTINGS_FILE but backup exists. Restoring from $SETTINGS_BACKUP_FILE..."
            cp "$SETTINGS_BACKUP_FILE" "$SETTINGS_FILE"
            chmod 666 "$SETTINGS_FILE"
            echo "Restored settings from backup."
        else
            # Contains default settings, no backup exists. Do nothing, keep defaults.
            echo "$SETTINGS_FILE contains default values and no backup exists. Keeping defaults."
        fi
    else
        # Contains custom settings, create/update the backup
        echo "$SETTINGS_FILE contains custom settings. Creating/updating backup..."
        cp "$SETTINGS_FILE" "$SETTINGS_BACKUP_FILE"
        chmod 666 "$SETTINGS_BACKUP_FILE"
        echo "Created/Updated settings backup at $SETTINGS_BACKUP_FILE."
    fi
fi

# Create a flag file in the data directory to indicate initialization
touch "$INIT_FLAG_FILE"
chmod 666 "$INIT_FLAG_FILE"
echo "Created initialization flag: $INIT_FLAG_FILE"

# List the contents of the data directory for debugging
echo "Contents of data directory ($DATA_DIR):"
ls -la "$DATA_DIR"

# Display the current settings.json content for debugging
if [ -f "$SETTINGS_FILE" ]; then
    echo "Current $SETTINGS_FILE content:"
    cat "$SETTINGS_FILE"
fi

# Run the application - SKIP_INIT_CONFIG might still be useful if app.py has overlapping logic
echo "Starting application with SKIP_INIT_CONFIG=true"
export SKIP_INIT_CONFIG=true

# Local development convenience: FLASK_ENV=development keeps the Flask dev server
# (single-process, easier debugging). Everything else runs the production WSGI
# server (gunicorn).
if [ "$FLASK_ENV" = "development" ]; then
    echo "FLASK_ENV=development -> starting Flask development server"
    exec python /app/src/app.py
fi

# Production: gunicorn.
#   --workers 1  : the keep-alive feature, the print-queue worker and the relay
#                  power scheduler are single-process singletons (printer_service,
#                  queue_service, relay_service) that each own a background
#                  thread. A second worker starts a second copy of every one of
#                  them: verified with --workers 2 the log shows two "Print queue
#                  worker started" and two "Relay power scheduler started" lines,
#                  the relay webhook fires twice, and GET /jobs alternates between
#                  the two workers' private in-memory job lists. --workers 1 is a
#                  correctness condition, not a convenience.
#   uvicorn      : Connexion 3 is ASGI. The operation handlers are ordinary
#                  synchronous functions, so Connexion runs each one in the
#                  worker's thread pool rather than on the event loop; that is
#                  where concurrency comes from now, and it is why gunicorn's
#                  --threads is gone rather than merely unused.
#                  What --timeout does NOT do here: the arbiter heartbeat comes
#                  from the event loop, not from the request, so a call stuck in
#                  CPU-bound C code (Pillow) never trips it -- it just holds its
#                  thread until it finishes. The cure is upstream and unchanged:
#                  every render path is bounded (MAX_UPLOAD_IMAGE_PIXELS caps the
#                  decode, MAX_PDF_PAGES caps the rasterise) and the compose
#                  mem_limit turns an over-large job into an OOM the worker is
#                  restarted from -- measured under the old server: four
#                  abandoned heavy previews and a 1.2M-char text each left the
#                  service responding, none wedged it.
#   no --preload : create_app() (and thus init_keep_alive, the queue worker and
#                  the relay scheduler) must run inside the worker process,
#                  otherwise those threads would be started before the fork and
#                  would not survive it.
echo "Starting application with gunicorn + uvicorn worker (workers=1)"
exec gunicorn \
    --workers 1 \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:5000 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile - \
    asgi:application
