#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source <(tr -d '\r' < "$SCRIPT_DIR/.env") && set +a
FFMPEG="${FFMPEG_PATH_LINUX:-ffmpeg}"

if ! command -v "$FFMPEG" >/dev/null 2>&1; then
    echo "Error: ffmpeg command not found: $FFMPEG" >&2
    exit 1
fi

if [ "$#" -ne 1 ]; then
    echo "用法: $0 <original-video>" >&2
    exit 1
fi

ORIGINAL_VIDEO="$1"
if [ ! -f "$ORIGINAL_VIDEO" ]; then
    echo "错误: 原片不存在: $ORIGINAL_VIDEO" >&2
    exit 1
fi

resolve_lexical_abs_path() {
    local candidate="$1"
    if [[ "$candidate" != /* ]]; then
        candidate="$PWD/$candidate"
    fi
    realpath -s -- "$candidate" 2>/dev/null || readlink -m -- "$candidate" 2>/dev/null || printf '%s\n' "$candidate"
}

remove_temporary_artifact() {
    local path="$1"
    if [ -e "$path" ] || [ -L "$path" ]; then
        rm -rf -- "$path"
    fi
}

is_nonempty_regular_file() {
    local path="$1"
    [ -f "$path" ] && [ ! -L "$path" ] && [ -s "$path" ]
}

print_native_cmd() {
    local label="$1"
    shift
    {
        printf '%s' "$label"
        printf ' %q' "$@"
        printf '\n'
    } >&2
}

ffmpeg_encoder_available() {
    "$FFMPEG" -hide_banner -encoders 2>/dev/null | grep -Fq "$1"
}

nvidia_available() {
    command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1
}

run_edit_reencode() {
    local input_path="$1"
    local output_path="$2"
    local temporary_path="$3"

    echo "============================================="
    echo "prepare-video: 重编码生成编辑视频"
    echo "============================================="
    echo "原片: $input_path"
    echo "编辑: $output_path"
    echo "模式: 优先 h264_nvenc；不可用时回退 libx264；音频统一 aresample s16 + flac"

    run_reencode_attempt() {
        local label="$1"
        shift
        local -a video_args=("$@")
        local -a ffmpeg_cmd=(
            "$FFMPEG"
            -hide_banner
            -stats
            -i "$input_path"
            -pix_fmt yuv420p
            "${video_args[@]}"
            -filter_complex "[0:a]aresample=async=1:out_sample_fmt=s16[aout]"
            -map 0:v:0
            -map "[aout]"
            -c:a flac
            -map_metadata -1
            -movflags +faststart
            -y
            "$temporary_path"
        )

        remove_temporary_artifact "$temporary_path"
        echo "尝试: $label"
        print_native_cmd "ffmpeg cmd:" "${ffmpeg_cmd[@]}"
        local ffmpeg_exit
        if "${ffmpeg_cmd[@]}"; then
            ffmpeg_exit=0
        else
            ffmpeg_exit=$?
        fi

        local accepted=0
        if [ "$ffmpeg_exit" -eq 0 ] && is_nonempty_regular_file "$temporary_path"; then
            accepted=1
        elif [ "$label" = "h264_nvenc" ] && is_nonempty_regular_file "$temporary_path"; then
            echo "Warning: h264_nvenc 返回非零退出码，但本次已输出非 0B 文件，继续使用该文件" >&2
            accepted=1
        fi

        if [ "$accepted" -eq 1 ]; then
            local replace_exit
            if mv -fT -- "$temporary_path" "$output_path"; then
                return 0
            else
                replace_exit=$?
                echo "Error: failed to replace prepared edit video '$output_path'" >&2
                return "$replace_exit"
            fi
        fi

        remove_temporary_artifact "$temporary_path"
        echo "Warning: $label 重编码失败" >&2
        if [ "$ffmpeg_exit" -eq 0 ]; then
            ffmpeg_exit=1
        fi
        return "$ffmpeg_exit"
    }

    local last_exit=1
    if nvidia_available && ffmpeg_encoder_available "h264_nvenc"; then
        if run_reencode_attempt "h264_nvenc" -c:v h264_nvenc -cq 12; then
            return 0
        else
            last_exit=$?
        fi
    else
        echo "跳过 h264_nvenc: 未检测到可用 NVIDIA GPU 或 ffmpeg h264_nvenc 编码器" >&2
    fi

    if run_reencode_attempt "libx264" -c:v libx264 -crf 12; then
        return 0
    else
        last_exit=$?
    fi

    echo "Error: ffmpeg re-encode failed." >&2
    return "$last_exit"
}

ORIGINAL_VIDEO_ABS="$(resolve_lexical_abs_path "$ORIGINAL_VIDEO")"
ORIGINAL_DIR="$(dirname "$ORIGINAL_VIDEO_ABS")"
ORIGINAL_NAME="$(basename "$ORIGINAL_VIDEO_ABS")"
EDIT_BASE="${ORIGINAL_NAME%.*}"
EDIT_BASE="${EDIT_BASE%.original}"
EDIT_VIDEO_ABS="$ORIGINAL_DIR/$EDIT_BASE.mkv"
if [ "$ORIGINAL_VIDEO_ABS" = "$EDIT_VIDEO_ABS" ] || { [ -e "$EDIT_VIDEO_ABS" ] && [ "$ORIGINAL_VIDEO_ABS" -ef "$EDIT_VIDEO_ABS" ]; }; then
    echo "Error: Edit video path would overwrite the original: $ORIGINAL_VIDEO_ABS" >&2
    exit 1
fi
if [ -e "$EDIT_VIDEO_ABS" ] || [ -L "$EDIT_VIDEO_ABS" ]; then
    if [ -d "$EDIT_VIDEO_ABS" ]; then
        echo "Error: Edit video output path is a directory: $EDIT_VIDEO_ABS" >&2
        exit 1
    fi
    if [ -L "$EDIT_VIDEO_ABS" ] || [ ! -f "$EDIT_VIDEO_ABS" ]; then
        echo "Error: Edit video output path is not a regular file: $EDIT_VIDEO_ABS" >&2
        exit 1
    fi
fi

TEMP_EDIT_VIDEO="$(mktemp "$ORIGINAL_DIR/.$EDIT_BASE.prepare.XXXXXX.mkv")"
rm -f -- "$TEMP_EDIT_VIDEO"
cleanup_temp_edit_video() {
    if [ -n "${TEMP_EDIT_VIDEO:-}" ] && { [ -e "$TEMP_EDIT_VIDEO" ] || [ -L "$TEMP_EDIT_VIDEO" ]; }; then
        remove_temporary_artifact "$TEMP_EDIT_VIDEO" || echo "Warning: could not clean temporary edit video '$TEMP_EDIT_VIDEO'" >&2
    fi
}
trap cleanup_temp_edit_video EXIT

run_edit_reencode "$ORIGINAL_VIDEO_ABS" "$EDIT_VIDEO_ABS" "$TEMP_EDIT_VIDEO"

echo "OUTPUT_VIDEO=$EDIT_VIDEO_ABS"
