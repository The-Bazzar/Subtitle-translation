#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_PATH_LINUX:-$SCRIPT_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: project Python environment not found: $PYTHON_BIN. Run setup.sh first." >&2
    exit 127
fi
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <pipeline|batch|translate|merge-ass|download|prepare-video|whisper|burn|init> [args...]" >&2
    exit 2
fi
TARGET="$1"
shift
case "$TARGET" in
    pipeline|batch|translate|download|prepare-video|whisper|burn|init)
        COMMAND="$TARGET"
        EXTRA=()
        ;;
    translate_srt|translate-srt)
        COMMAND=translate
        EXTRA=()
        ;;
    merge_ass|merge-ass)
        COMMAND=merge-ass
        EXTRA=()
        ;;
    prepare_video)
        COMMAND=prepare-video
        EXTRA=()
        ;;
    ffmpeg-burn)
        COMMAND=burn
        EXTRA=(--backend ffmpeg)
        ;;
    mpv-burn)
        COMMAND=burn
        EXTRA=(--backend mpv)
        ;;
    *)
        echo "Error: unsupported Python target: $TARGET" >&2
        exit 2
        ;;
esac
exec "$PYTHON_BIN" -m subtitle_translation "$COMMAND" "${EXTRA[@]}" "$@"
