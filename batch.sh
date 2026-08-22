#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_PATH_LINUX:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then echo "Error: run setup.sh first: $PYTHON_BIN" >&2; exit 127; fi
exec "$PYTHON_BIN" -m subtitle_translation batch "$@"
