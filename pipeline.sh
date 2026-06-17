#!/bin/bash
# =============================================================================
# pipeline.sh — 一键流水线: 下载视频 → 字幕 → 美化 → 翻译 [+ 硬压]
#
# 串联 download.sh → whisper.sh → beautify_srt.py → translate_srt.py → (ffmpeg-burn.sh)
# 从 YouTube 链接直达双语 .zh-en.ass 字幕 / burned.mkv 硬字幕。
#
# 成果物链 (每步输出作为下一步输入):
#   VIDEO_PATH (步骤1) → BEAUTIFIED_SRT (步骤2) → ASS_PATH (步骤3) → burned.mkv (步骤4)
#
# 用法:
#   ./pipeline.sh <YouTube URL> [-- <beautify选项>]
#
# 示例:
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --preview
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2
#   TRANSLATE_PROVIDER=deepseek ./pipeline.sh "url"
#   BURN=0 ./pipeline.sh "url"   # 跳过硬压
#
# 环境变量:
#   SKIP_DOWNLOAD=1       跳过下载 (仅处理已有视频)
#   SKIP_BEAUTIFY=1       跳过美化
#   SKIP_TRANSLATE=1      跳过翻译
#   TRANSLATE_PROVIDER    翻译后端: openrouter | deepseek | gemini (默认: openrouter)
#   TRANSLATE_MODEL       翻译模型 (默认: 后端内置默认)
#   PROOFREAD=0            关闭中英校对 (默认开启)
#   PROOFREAD_PROVIDER     校对专用后端 (默认: 同翻译)
#   PROOFREAD_MODEL        校对专用模型 (默认: 同翻译)
#   EXISTING_SRT          已有美化后 SRT 路径 (跳过美化步骤, 直接用于翻译)
#   EXISTING_ASS          已有 .zh-en.ass 路径 (跳过翻译步骤, 直接用于压制)
#   BURN=0                跳过字幕硬压 (默认启用)
#   BURN_OVC / BURN_OVCOPTS / BURN_OAC / BURN_RES  压制参数
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_SCRIPT="$SCRIPT_DIR/download.sh"
WHISPER_SCRIPT="$SCRIPT_DIR/whisper.sh"
BEAUTIFY_SCRIPT="$SCRIPT_DIR/beautify_srt.py"
TRANSLATE_SCRIPT="$SCRIPT_DIR/translate_srt.py"
BURN_SCRIPT="$SCRIPT_DIR/ffmpeg-burn.sh"

# ── 从 .env 读取默认配置 (环境变量优先, .env 次之, 硬编码兜底) ──────────────────

ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    # tr -d '\r' 先去掉 Windows 换行符, 避免值末尾带 \r
    while IFS='=' read -r key value; do
        # 跳过空行和注释
        case "$key" in ''|'#'*) continue ;; esac
        # 去除首尾空白和 \r
        key="${key# }"; key="${key% }"; key="${key%$'\r'}"
        value="${value# }"; value="${value% }"; value="${value%$'\r'}"
        [ -z "$key" ] && continue
        # 仅在环境变量未设置时从 .env 取值
        case "$key" in
            TRANSLATE_PROVIDER) TRANSLATE_PROVIDER="${TRANSLATE_PROVIDER:-$value}" ;;
            TRANSLATE_MODEL)    TRANSLATE_MODEL="${TRANSLATE_MODEL:-$value}" ;;
        esac
    done < "$ENV_FILE"
fi

# 硬编码兜底 (环境变量 > .env > 此处默认值)
TRANSLATE_PROVIDER="${TRANSLATE_PROVIDER:-openrouter}"
TRANSLATE_MODEL="${TRANSLATE_MODEL:-}"

# 压制参数
BURN_OVC="${BURN_OVC:-hevc_nvenc}"
BURN_OVCOPTS="${BURN_OVCOPTS:-qp=20}"
BURN_OAC="${BURN_OAC:-aac}"
BURN_RES="${BURN_RES:-}"

# ── 帮助 ──────────────────────────────────────────────────────────────────────

show_help() {
    cat << 'EOF'
pipeline.sh — 一键流水线: 下载视频 + WhisperX 字幕 + 时间码美化 + LLM 翻译 [+ 硬压]

用法:
  ./pipeline.sh <YouTube URL> [-- <beautify选项>]

流程:
  1. yt-dlp 下载视频 + 元数据 (SponsorBlock 去广告)
  2. WhisperX large-v3 生成英文字幕 (.srt)
  3. ffmpeg/ffprobe 场景检测 → 字幕时间码吸附对齐 → .beautified.srt (Netflix 规范)
  4. LLM API 翻译英文 → 双语 .zh-en.ass (bi-en + bi-zh)
  5. ffmpeg 硬压字幕 → burned.mkv (默认启用, BURN=0 跳过)

成果物链: VIDEO_PATH → BEAUTIFIED_SRT → ASS_PATH → (burned.mkv)
  - 已存在的中间产物自动跳过 (跳过美化/跳过翻译)
  - 使用 EXISTING_SRT / EXISTING_ASS 指定已有文件

示例:
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --preview
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2
  TRANSLATE_PROVIDER=deepseek ./pipeline.sh "url"
  BURN=0 ./pipeline.sh "url"   # 跳过硬压

beautify 选项 (在 -- 之后, 默认值遵循 Netflix 规范):
  --scene-threshold N           场景检测灵敏度 (默认 0.25)
  --snap-frames N               吸附到场景切换的最大帧数 (默认 7)
  --end-offset-frames N         出点对齐到场景前 N 帧 (默认 2)
  --min-scene-interval-frames N 场景切换最小帧间隔 (默认 7)
  --min-duration N              最短字幕时长秒 (默认 1.0)
  --max-duration N              最长字幕时长秒 (默认 8.0)
  --min-gap N                   字幕最小间距秒 (默认 0.083)
  --max-gap-merge N             小于此值的间隙合并秒 (默认 0.5)
  --use-keyframes               启用关键帧吸附 (默认关闭)
  --extend                      延伸字幕填充间隙 (默认不启用)
  --no-scene-snap               完全跳过场景吸附
  --preview                     仅预览, 不写入
  --backup                      覆盖前备份原文件

环境变量:
  SKIP_DOWNLOAD=1         跳过下载 (仅处理已有视频)
  SKIP_BEAUTIFY=1         跳过美化
  SKIP_TRANSLATE=1        跳过 LLM 翻译
  EXISTING_SRT            已有美化后 SRT 路径 (跳过美化步骤)
  EXISTING_ASS            已有 .zh-en.ass 路径 (跳过翻译步骤)
  TRANSLATE_PROVIDER      翻译后端: openrouter | deepseek | gemini (默认: openrouter)
  TRANSLATE_MODEL         翻译模型 (默认: 后端内置默认)
  PROOFREAD=0              关闭中英校对 (默认开启)
  PROOFREAD_PROVIDER       校对专用后端 (默认: 同翻译)
  PROOFREAD_MODEL          校对专用模型 (默认: 同翻译)
  BURN=0                  跳过字幕硬压 (默认启用)
  BURN_OVC                视频编码器 (默认: hevc_nvenc)
  BURN_OVCOPTS            编码器参数 (默认: qp=20)
  BURN_OAC                音频编码器 (默认: aac)
EOF
    exit 0
}

# ── 参数解析 ──────────────────────────────────────────────────────────────────

if [ $# -eq 0 ]; then
    show_help
fi

if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    show_help
fi

URL="$1"
shift

# 收集 -- 之后的 beautify 参数
BEAUTIFY_ARGS=()
PASSTHROUGH=false
for arg in "$@"; do
    if [ "$PASSTHROUGH" = true ]; then
        BEAUTIFY_ARGS+=("$arg")
    elif [ "$arg" = "--" ]; then
        PASSTHROUGH=true
    else
        echo "Warning: Unexpected argument '$arg' (use -- to pass beautify options)" >&2
    fi
done

# ── 启动信息 ──────────────────────────────────────────────────────────────────

echo "============================================="
echo "pipeline — 一键字幕流水线"
echo "============================================="
echo "URL:              $URL"
echo "Translator:       $TRANSLATE_PROVIDER${TRANSLATE_MODEL:+ / $TRANSLATE_MODEL}"
[ "${BURN:-1}" != "0" ] && echo "Burn:             enabled (BURN=0 to skip)"
echo "============================================="
echo ""

# ── 步骤 1: 下载 + 字幕生成 ───────────────────────────────────────────────────

if [ "${SKIP_DOWNLOAD:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_DOWNLOAD=1 — 跳过下载"
    echo "============================================="
    echo ""
    echo "请提供视频文件路径:"
    read -r VIDEO_PATH
else
    echo "============================================="
    echo "pipeline — 步骤 1/5: 下载视频"
    echo "============================================="
    echo ""

    DOWNLOAD_OUTPUT=$(bash "$DOWNLOAD_SCRIPT" "$URL" 2>&1) || {
        echo "$DOWNLOAD_OUTPUT"
        echo ""
        echo "Error: download.sh failed." >&2
        exit 1
    }
    echo "$DOWNLOAD_OUTPUT"

    VIDEO_PATH=$(echo "$DOWNLOAD_OUTPUT" | grep '^OUTPUT_VIDEO=' | tail -1 | cut -d= -f2-)

    if [ -z "$VIDEO_PATH" ] || [ ! -f "$VIDEO_PATH" ]; then
        echo ""
        echo "Error: Failed to locate downloaded video file." >&2
        exit 1
    fi

    echo ""
    echo "============================================="
    echo "pipeline — 步骤 2/5: WhisperX 生成字幕"
    echo "============================================="
    echo ""

    bash "$WHISPER_SCRIPT" "$VIDEO_PATH" || {
        echo "Error: whisper.sh failed." >&2
        exit 1
    }

    echo ""
fi

# ── 推导所有成果物路径 ────────────────────────────────────────────────────────

VIDEO_DIR="$(dirname "$VIDEO_PATH")"
VIDEO_NAME="$(basename "$VIDEO_PATH")"
VIDEO_BASE="${VIDEO_NAME%.*}"

# 步骤 1 产物: WhisperX 生成的原始字幕
SRT_PATH="$VIDEO_DIR/${VIDEO_BASE}.srt"

# 步骤 2 产物: 美化后的字幕 (默认不覆盖原 SRT)
if [ -n "${EXISTING_SRT:-}" ]; then
    BEAUTIFIED_SRT="$EXISTING_SRT"
else
    BEAUTIFIED_SRT="$VIDEO_DIR/${VIDEO_BASE}.beautified.srt"
fi

# 步骤 3 产物: 双语 ASS 字幕 (EXISTING_ASS 可覆盖默认路径)
ASS_PATH="${EXISTING_ASS:-$VIDEO_DIR/${VIDEO_BASE}.zh-en.ass}"

echo "OUTPUT_VIDEO=$VIDEO_PATH"

# ── 步骤 2: 字幕时间码美化 ─────────────────────────────────────────────────────

if [ -n "${EXISTING_SRT:-}" ] && [ -f "$EXISTING_SRT" ]; then
    echo "============================================="
    echo "EXISTING_SRT — 使用已有美化字幕: $EXISTING_SRT"
    echo "============================================="
elif [ "${SKIP_BEAUTIFY:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_BEAUTIFY=1 — 跳过字幕美化"
    echo "============================================="
elif [ -f "$BEAUTIFIED_SRT" ]; then
    echo "============================================="
    echo "美化字幕已存在, 跳过 — $BEAUTIFIED_SRT"
    echo "============================================="
else
    echo "============================================="
    echo "pipeline — 步骤 3/5: 字幕时间码美化 → .beautified.srt"
    echo "============================================="
    echo ""

    if [ ! -f "$SRT_PATH" ]; then
        echo "Error: No SRT file found for beautify." >&2
        echo "Expected: $SRT_PATH" >&2
        echo "Hint: Verifying SRT existence in video directory..." >&2
        # Fallback: search for any .srt in video dir
        SRT_PATH=""
        for f in "$VIDEO_DIR"/*.srt; do
            if [ -f "$f" ]; then
                SRT_PATH="$f"
                break
            fi
        done
        if [ -z "$SRT_PATH" ]; then
            echo "Error: Still no SRT file found. Cannot beautify." >&2
            exit 1
        fi
        echo "Found SRT: $SRT_PATH" >&2
    fi

    python "$BEAUTIFY_SCRIPT" "$VIDEO_PATH" "$SRT_PATH" -o "$BEAUTIFIED_SRT" "${BEAUTIFY_ARGS[@]}"
    echo ""
fi


# ── 步骤 3: LLM 翻译 (英→中) ───────────────────────────────────────────────

# 确定翻译输入: 优先使用美化后的 SRT, 否则使用原始 SRT
if [ -f "$BEAUTIFIED_SRT" ]; then
    TRANSLATE_SRC="$BEAUTIFIED_SRT"
else
    TRANSLATE_SRC="$SRT_PATH"
fi

if [ "${SKIP_TRANSLATE:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_TRANSLATE=1 — 跳过翻译"
    echo "============================================="
elif [ -f "$ASS_PATH" ]; then
    echo "============================================="
    echo "双语 ASS 已存在, 跳过 — $ASS_PATH"
    echo "============================================="
else
    echo "============================================="
    echo "pipeline — 步骤 4/5: LLM 翻译 (英→中) → .zh-en.ass"
    echo "============================================="
    echo ""

    if [ ! -f "$TRANSLATE_SRC" ]; then
        echo "Error: No SRT file found for translation." >&2
        echo "Expected: $TRANSLATE_SRC" >&2
        exit 1
    fi

    TRANSLATE_ARGS=(--provider "$TRANSLATE_PROVIDER")
    if [ -n "$TRANSLATE_MODEL" ]; then
        TRANSLATE_ARGS+=(--model "$TRANSLATE_MODEL")
    fi
    # 校对默认开启, PROOFREAD=0 关闭
    if [ "${PROOFREAD:-1}" = "0" ]; then
        export PROOFREAD=0
    fi
    python "$TRANSLATE_SCRIPT" "$TRANSLATE_SRC" -o "$ASS_PATH" "${TRANSLATE_ARGS[@]}"

    echo ""
fi

echo "OUTPUT_ASS=$ASS_PATH"
if [ "${BURN:-1}" = "0" ]; then
    echo ""

    echo "============================================="
    echo "BURN=0 — 跳过字幕硬压"
    echo "============================================="
elif [ "${SKIP_BURN:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_BURN=1 — 跳过字幕硬压"
    echo "============================================="
else
    echo "============================================="
    echo "pipeline — 步骤 5/5: 字幕硬压 (mpv) → burned.mkv"
    echo "============================================="
    echo ""

    if [ -z "${ASS_PATH:-}" ] || [ ! -f "$ASS_PATH" ]; then
        echo "Warning: .zh-en.ass not found, skipping burn." >&2
    else
        bash "$BURN_SCRIPT" "$VIDEO_PATH" \
            --sub-file "$ASS_PATH" \
            --ovc "$BURN_OVC" \
            --ovcopts "$BURN_OVCOPTS" \
            --oac "$BURN_OAC" \
            ${BURN_RES:+--res "$BURN_RES"}
    fi
fi

# ── 完成 ──────────────────────────────────────────────────────────────────────

echo ""
echo "============================================="
echo "pipeline — 全部完成!"
echo "============================================="
echo "视频:     $VIDEO_PATH"
if [ -n "${SRT_PATH:-}" ] && [ -f "$SRT_PATH" ]; then
    echo "字幕:     $SRT_PATH"
fi
if [ -n "${BEAUTIFIED_SRT:-}" ] && [ -f "$BEAUTIFIED_SRT" ]; then
    echo "美化字幕: $BEAUTIFIED_SRT"
fi
if [ -n "${ASS_PATH:-}" ] && [ -f "$ASS_PATH" ]; then
    echo "双语 ASS: $ASS_PATH"
fi
echo "============================================="
