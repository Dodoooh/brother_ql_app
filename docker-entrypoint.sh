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

# Function to create default settings.json in the data directory
create_default_settings_json() {
    echo "Creating default settings.json in $DATA_DIR..."
    cat > "$SETTINGS_FILE" << 'EOF'
{
    "printer_uri": "tcp://192.168.1.100",
    "printer_model": "QL-800",
    "label_size": "62",
    "font_size": 50,
    "alignment": "left",
    "rotate": 0,
    "threshold": 70.0,
    "dither": false,
    "compress": false,
    "red": false,
    "keep_alive_enabled": false,
    "keep_alive_interval": 60,
    "printers": [
        {
            "id": "default",
            "name": "Default Printer",
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-800",
            "label_size": "62"
        }
    ]
}
EOF
    # Set permissions appropriate for the container user
    chmod 666 "$SETTINGS_FILE"
    echo "Created $SETTINGS_FILE with permissions 666"
}

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
        # No settings file and no backup, create default settings
        echo "$SETTINGS_FILE not found and no backup exists. Creating default settings."
        create_default_settings_json
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
exec python /app/src/app.py
