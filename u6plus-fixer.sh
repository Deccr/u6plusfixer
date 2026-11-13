#!/usr/bin/env bash
# u6plus-fixer.sh - GUI version of the U6+ eMMC recovery tool.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find python3
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "Python 3 was not found. Install it with your package manager."
    exit 1
fi

# Check for tkinter
if ! $PYTHON -c "import tkinter" 2>/dev/null; then
    echo "ERROR: tkinter is required for the GUI. Install it with your package manager:"
    echo "  Debian/Ubuntu: sudo apt install python3-tk"
    echo "  Fedora:        sudo dnf install python3-tkinter"
    echo "  Arch:          sudo pacman -S tk"
    exit 1
fi

# Check for pyserial
if ! $PYTHON -c "import serial" 2>/dev/null; then
    echo "ERROR: pyserial is required. Install it with: pip3 install pyserial"
    exit 1
fi

cd "$SCRIPT_DIR"
$PYTHON u6plus_fixer_gui.py "$@"
