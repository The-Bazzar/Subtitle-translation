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

target="${1:-}"
if [ "$#" -gt 0 ]; then
    shift
fi
case "$target" in
    translate_srt) script_name="translate_srt.py" ;;
    merge_ass) script_name="merge_ass.py" ;;
    *) echo "Error: unsupported Python target: $target" >&2; exit 2 ;;
esac

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: Python executable not found: $PYTHON_BIN. Run ./setup.sh first." >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/$script_name" "$@"
