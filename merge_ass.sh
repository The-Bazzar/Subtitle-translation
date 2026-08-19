#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIGURED_PYTHON="${PYTHON_PATH_LINUX:-}"
if [ -z "$CONFIGURED_PYTHON" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    CONFIGURED_PYTHON="$(sed -n 's/^[[:space:]]*PYTHON_PATH_LINUX[[:space:]]*=[[:space:]]*//p' "$SCRIPT_DIR/.env" | head -n 1 | tr -d '\r')"
    CONFIGURED_PYTHON="${CONFIGURED_PYTHON#\"}"
    CONFIGURED_PYTHON="${CONFIGURED_PYTHON%\"}"
fi
PYTHON_BIN="${CONFIGURED_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
SCRIPT_PATH="$SCRIPT_DIR/merge_ass.py"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: Python executable not found: $PYTHON_BIN. Run ./setup.sh first." >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"
