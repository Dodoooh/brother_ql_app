#!/bin/bash

# Create necessary directories
mkdir -p src/uploads src/config

# Check if Docker is available
if command -v docker &> /dev/null && command -v docker-compose &> /dev/null; then
    echo "Docker and Docker Compose found. Running with Docker..."
    docker-compose up -d
    echo "Application started. Access it at http://localhost:5000"
    exit 0
fi

# If Docker is not available, try to run with Python
echo "Docker not found or not running. Trying to run with Python..."

# Find Python command (python3 or python)
PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "Error: Neither python3 nor python command found. Please install Python 3.8+ to run this application."
    echo "Alternatively, you can use Docker by installing Docker and Docker Compose."
    exit 1
fi

# Check Python version
PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo $PY_VERSION | cut -d. -f1)
PY_MINOR=$(echo $PY_VERSION | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo "Error: Python 3.8+ is required. Found Python $PY_VERSION"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv venv
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create virtual environment. Please install venv package."
        echo "On Ubuntu/Debian: sudo apt-get install python3-venv"
        echo "On macOS: pip3 install virtualenv"
        exit 1
    fi
fi

# Activate virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    # For Windows
    source venv/Scripts/activate
else
    echo "Error: Virtual environment activation script not found."
    exit 1
fi

# Check if pip is available
if ! command -v pip &> /dev/null; then
    echo "Error: pip command not found in virtual environment."
    exit 1
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "Error: Failed to install dependencies."
    exit 1
fi

# Run the application
echo "Starting Brother QL Printer App..."
python src/app.py

# Deactivate virtual environment on exit
if command -v deactivate &> /dev/null; then
    deactivate
fi
