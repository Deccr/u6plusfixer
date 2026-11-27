#!/usr/bin/env bash
# prepare.sh - download the official firmware and build the U6+ recovery images.
# Needs Python 3.8+ and internet, once per PC.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find python3
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "Python 3 was not found. Install it with your package manager (e.g. apt install python3) and run this again."
    exit 1
fi

# Check version
PYVER=$($PYTHON -c 'import sys; print(sys.version_info[:2] >= (3, 8))')
if [ "$PYVER" != "True" ]; then
    echo "Python 3.8 or newer is required."
    exit 1
fi

# Check for pyserial (optional but recommended - u6plus_fixer.py needs it)
if ! $PYTHON -c "import serial" 2>/dev/null; then
    echo "WARNING: pyserial is not installed. The recovery tool will need it."
    echo "Install with: pip3 install pyserial"
fi

cd "$SCRIPT_DIR"
$PYTHON prepare/prepare.py "$@"
