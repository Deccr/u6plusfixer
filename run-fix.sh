#!/usr/bin/env bash
# run-fix.sh - console version of the U6+ eMMC recovery tool.
# Usage: ./run-fix.sh [--mode Fix|Reset] [--com-port /dev/ttyUSB0] [--adapter eth0]
#                     [--factory-reset] [--skip-backup] [--speed auto|fast|slow]
#
# Examples:
#   ./run-fix.sh --mode Fix --com-port /dev/ttyUSB0 --adapter eth0
#   ./run-fix.sh --mode Reset --skip-backup
#   ./run-fix.sh --mode Fix --speed slow

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

# Check for pyserial
if ! $PYTHON -c "import serial" 2>/dev/null; then
    echo "ERROR: pyserial is required. Install it with: pip3 install pyserial"
    exit 1
fi

cd "$SCRIPT_DIR"
$PYTHON u6plus_fixer.py "$@"
