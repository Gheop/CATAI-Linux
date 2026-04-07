#!/bin/bash
set -e

echo "CATAI-Linux - Virtual Desktop Pet Cats"
echo "======================================="

# Check dependencies
echo "Checking dependencies..."
MISSING=""
python3 -c "import gi; gi.require_version('Gtk', '4.0')" 2>/dev/null || MISSING="$MISSING python3-gobject"
python3 -c "from PIL import Image" 2>/dev/null || MISSING="$MISSING python3-pillow"
python3 -c "import httpx" 2>/dev/null || MISSING="$MISSING python3-httpx"
command -v xdotool &>/dev/null || MISSING="$MISSING xdotool"
command -v wmctrl &>/dev/null || MISSING="$MISSING wmctrl"

if [ -n "$MISSING" ]; then
    echo "Missing packages:$MISSING"
    echo "Install with: sudo dnf install$MISSING"
    exit 1
fi
echo "All dependencies OK."

# Check assets
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$SCRIPT_DIR/cute_orange_cat" ]; then
    echo "ERROR: cute_orange_cat/ directory not found."
    echo "Make sure the sprite assets are in the same directory as this script."
    exit 1
fi
echo "Assets found."

echo ""
echo "Launching CATAI..."
exec python3 "$SCRIPT_DIR/catai.py" "$@"
