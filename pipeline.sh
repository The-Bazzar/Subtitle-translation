#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_PATH_LINUX:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: project Python environment not found: $PYTHON_BIN. Run setup.sh first." >&2
    exit 127
fi
exec "$PYTHON_BIN" -m subtitle_translation pipeline "$@"
