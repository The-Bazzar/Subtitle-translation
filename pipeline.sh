#!/bin/bash
# =============================================================================
# pipeline.sh — 一键流水线: 下载 → JSON → 美化 → 术语库 → 翻译 → 硬压
#
# 串联 download.sh → whisper.sh → translate_srt.py → (ffmpeg-burn.sh)
# 从 YouTube 链接直达双语 .<source>-<target>.ass 字幕 / burned.mkv 硬字幕。
#
# 成果物链:
#   VIDEO_PATH → JSON_PATH → BEAUTIFIED_JSON → glossary.md → ASS_PATH → burned.mkv
#
# 用法:
#   ./pipeline.sh <YouTube URL> [-- <beautify选项>]
#
# 示例:
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
#   ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --scene-threshold 0.2
#   TRANSLATE_PROVIDER=deepseek ./pipeline.sh "url"
#   BURN=0 ./pipeline.sh "url"   # 跳过硬压
#
# 环境变量:
#   SKIP_DOWNLOAD=1       跳过下载 (仅处理已有视频)
#   SKIP_WHISPER=1        跳过 WhisperX
#   SKIP_BEAUTIFY=1       跳过美化
#   SKIP_KNOWLEDGE=1      跳过术语知识库
#   SKIP_TRANSLATE=1      跳过翻译
#   TRANSLATE_PROVIDER    翻译后端: openrouter | deepseek | gemini (必填)
#   TRANSLATE_MODEL       翻译模型 (默认: 后端内置默认)
#   PROOFREAD=0           关闭双语校对 (默认开启)
#   PROOFREAD_PROVIDER    校对专用后端 (默认: 同翻译)
#   PROOFREAD_MODEL       校对专用模型 (默认: 同翻译)
#   SOURCE_LANG           源语言提示词标签；空则使用 WhisperX JSON language
#   TARGET_LANG           目标语言输出后缀/提示词标签 (默认 zh)
#   EXISTING_ASS          已有双语 .ass 路径 (跳过翻译步骤, 直接用于压制)
#   BURN=0                跳过字幕硬压 (默认启用)
#   BURN_OVC / BURN_OVCOPTS / BURN_OAC / BURN_RES  压制参数
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOAD_SCRIPT="$SCRIPT_DIR/download.sh"
WHISPER_SCRIPT="$SCRIPT_DIR/whisper.sh"
TRANSLATE_SCRIPT="$SCRIPT_DIR/translate_srt.py"
BURN_SCRIPT="$SCRIPT_DIR/ffmpeg-burn.sh"

# ── 从 .env 读取默认配置 (环境变量优先, .env 次之) ────────────────────────────

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
            TRANSLATE_PROVIDER)       TRANSLATE_PROVIDER="${TRANSLATE_PROVIDER:-$value}" ;;
            TRANSLATE_MODEL)          TRANSLATE_MODEL="${TRANSLATE_MODEL:-$value}" ;;
            SOURCE_LANG)              SOURCE_LANG="${SOURCE_LANG:-$value}" ;;
            TARGET_LANG)              TARGET_LANG="${TARGET_LANG:-$value}" ;;
            PYTHON_PATH_LINUX)        PYTHON_PATH_LINUX="${PYTHON_PATH_LINUX:-$value}" ;;
            PIPELINE_SKIP_DOWNLOAD)   PIPELINE_SKIP_DOWNLOAD="${PIPELINE_SKIP_DOWNLOAD:-$value}" ;;
            PIPELINE_SKIP_WHISPER)    PIPELINE_SKIP_WHISPER="${PIPELINE_SKIP_WHISPER:-$value}" ;;
            PIPELINE_SKIP_BEAUTIFY)   PIPELINE_SKIP_BEAUTIFY="${PIPELINE_SKIP_BEAUTIFY:-$value}" ;;
            PIPELINE_SKIP_TRANSLATE)  PIPELINE_SKIP_TRANSLATE="${PIPELINE_SKIP_TRANSLATE:-$value}" ;;
            PIPELINE_SKIP_BURN)       PIPELINE_SKIP_BURN="${PIPELINE_SKIP_BURN:-$value}" ;;
            PIPELINE_SKIP_KNOWLEDGE)  PIPELINE_SKIP_KNOWLEDGE="${PIPELINE_SKIP_KNOWLEDGE:-$value}" ;;
        esac
    done < "$ENV_FILE"
fi

# 翻译后端必须显式配置
TRANSLATE_PROVIDER="${TRANSLATE_PROVIDER:-}"
TRANSLATE_MODEL="${TRANSLATE_MODEL:-}"
SOURCE_LANG="${SOURCE_LANG:-}"
TARGET_LANG="${TARGET_LANG:-zh}"
LANG_ARGS=(--target-lang "$TARGET_LANG")
if [ -n "$SOURCE_LANG" ]; then
    LANG_ARGS=(--source-lang "$SOURCE_LANG" "${LANG_ARGS[@]}")
fi
if [ -z "$TRANSLATE_PROVIDER" ]; then
    echo "Error: TRANSLATE_PROVIDER is not set. Please configure it in .env or environment." >&2
    exit 1
fi

if [ -n "${PYTHON_PATH_LINUX:-}" ]; then
    PYTHON_BIN="$PYTHON_PATH_LINUX"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
else
    echo "Error: Python venv not found. Run ./setup.sh first, or set PYTHON_PATH_LINUX." >&2
    exit 1
fi

# 阶段跳过: 运行时 SKIP_* > .env PIPELINE_SKIP_* > 默认 0
SKIP_DOWNLOAD="${SKIP_DOWNLOAD:-${PIPELINE_SKIP_DOWNLOAD:-0}}"
SKIP_WHISPER="${SKIP_WHISPER:-${PIPELINE_SKIP_WHISPER:-0}}"
SKIP_BEAUTIFY="${SKIP_BEAUTIFY:-${PIPELINE_SKIP_BEAUTIFY:-0}}"
SKIP_KNOWLEDGE="${SKIP_KNOWLEDGE:-${PIPELINE_SKIP_KNOWLEDGE:-0}}"
SKIP_TRANSLATE="${SKIP_TRANSLATE:-${PIPELINE_SKIP_TRANSLATE:-0}}"
SKIP_BURN="${SKIP_BURN:-${PIPELINE_SKIP_BURN:-0}}"

# 压制参数
BURN_OVC="${BURN_OVC:-hevc_nvenc}"
BURN_OVCOPTS="${BURN_OVCOPTS:-source-bitrate}"
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
  2. WhisperX large-v3-turbo 生成词级 JSON
  3. ffmpeg 场景检测 → JSON 时间轴吸附对齐 → .beautified.json
  4. translate_srt.py 从整句 JSON 生成 glossary.md
  5. 整句翻译 + 校对 + 分割 + 词级对轴 → .<source>-<target>.ass + .<target>.ass + .<source>.proofread.ass
  6. ffmpeg 硬压字幕 → burned.mkv (默认启用, BURN=0 跳过)

成果物链: VIDEO_PATH → JSON_PATH → BEAUTIFIED_JSON → glossary.md → ASS_PATH → burned.mkv
  - 已存在的中间产物自动跳过 (跳过美化/跳过翻译)
  - 使用 EXISTING_ASS 指定已有双语 ASS

示例:
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --scene-threshold 0.1
  ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --aggressive  # 激进模式
  TRANSLATE_PROVIDER=deepseek ./pipeline.sh "url"
  BURN=0 ./pipeline.sh "url"   # 跳过硬压

JSON beautify 选项 (在 -- 之后):
  --scene-threshold N           场景检测灵敏度 (默认 0.15, 越低越敏感)
  --snap-frames N               吸附到场景切换的最大帧数 (默认 7)
  --end-offset-frames N         出点对齐到场景前 N 帧 (默认 2)
  --min-scene-interval-frames N 场景切换最小帧间隔 (默认 2, Netflix: 7)
  --min-duration N              最短字幕时长秒 (默认 1.0)
  --min-gap N                   字幕最小间距秒 (默认 0.083)
  --max-gap-merge N             小于此值的间隙合并秒 (默认 0.5)
  --aggressive                  激进模式 (threshold=0.08, snap=12帧, end-offset=0, min-interval=1)
  --no-scene-snap               完全跳过场景吸附

环境变量 (优先级: 运行时 > .env > 默认值):
  SKIP_DOWNLOAD=1 / PIPELINE_SKIP_DOWNLOAD=1     跳过下载
  SKIP_WHISPER=1 / PIPELINE_SKIP_WHISPER=1       跳过 WhisperX
  SKIP_BEAUTIFY=1 / PIPELINE_SKIP_BEAUTIFY=1     跳过美化
  SKIP_KNOWLEDGE=1 / PIPELINE_SKIP_KNOWLEDGE=1   跳过术语知识库
  SKIP_TRANSLATE=1 / PIPELINE_SKIP_TRANSLATE=1   跳过 LLM 翻译
  SKIP_BURN=1 / PIPELINE_SKIP_BURN=1             跳过字幕硬压
  SOURCE_LANG             源语言提示词标签；空则使用 WhisperX JSON language
  TARGET_LANG             目标语言输出后缀/提示词标签 (默认: zh)
  EXISTING_ASS            已有双语 .ass 路径 (跳过翻译步骤)
  TRANSLATE_PROVIDER      翻译后端: openrouter | deepseek | gemini (必填)
  TRANSLATE_MODEL         翻译模型 (默认: 后端内置默认)
  PROOFREAD=0             关闭双语校对 (默认开启)
  PROOFREAD_PROVIDER      校对专用后端 (默认: 同翻译)
  PROOFREAD_MODEL         校对专用模型 (默认: 同翻译)
  BURN_OVC                视频编码器 (默认: hevc_nvenc)
  BURN_OVCOPTS            编码器参数 (默认: source-bitrate, 自动接近源视频码率)
  BURN_OAC                音频编码器 (默认: aac)
  BURN_RES                输出分辨率 (默认: 原始)
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
echo "Languages:        ${SOURCE_LANG:-"(JSON language)"} → $TARGET_LANG"
[ "${BURN:-1}" != "0" ] && echo "Burn:             enabled (BURN=0 to skip)"
echo "============================================="
echo ""

# ── 步骤 1: 下载 ──────────────────────────────────────────────────────────────

if [ "${SKIP_DOWNLOAD:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_DOWNLOAD=1 — 跳过下载"
    echo "============================================="
    echo ""
    echo "请提供视频文件路径:"
    read -r VIDEO_PATH
else
    echo "============================================="
    echo "pipeline — 步骤 1/6: 下载视频"
    echo "============================================="
    echo ""

    VIDEO_PATH=""
    DOWNLOAD_LOG="$(mktemp)"
    if ! bash "$DOWNLOAD_SCRIPT" "$URL" 2>&1 | tee "$DOWNLOAD_LOG"; then
        echo ""
        echo "Error: Download step failed." >&2
        rm -f "$DOWNLOAD_LOG"
        exit 1
    fi
    VIDEO_PATH="$(awk -F= '/^OUTPUT_VIDEO=/{print substr($0, index($0, "=") + 1)}' "$DOWNLOAD_LOG" | tail -n 1)"
    rm -f "$DOWNLOAD_LOG"

    if [ -z "$VIDEO_PATH" ] || [ ! -f "$VIDEO_PATH" ]; then
        echo ""
        echo "Error: Failed to locate downloaded video file." >&2
        exit 1
    fi
fi

# ── 推导所有成果物路径 ────────────────────────────────────────────────────────

VIDEO_DIR="$(dirname "$VIDEO_PATH")"
VIDEO_NAME="$(basename "$VIDEO_PATH")"
VIDEO_BASE="${VIDEO_NAME%.*}"

# 步骤 1 产物: WhisperX 生成的词级 JSON
JSON_PATH="$VIDEO_DIR/${VIDEO_BASE}.json"

# 步骤 2 产物: 美化后的 JSON
BEAUTIFIED_JSON="$VIDEO_DIR/${VIDEO_BASE}.beautified.json"

echo "OUTPUT_VIDEO=$VIDEO_PATH"

# ── 步骤 2: WhisperX 字幕 ─────────────────────────────────────────────────────

if [ "${SKIP_WHISPER:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_WHISPER=1 — 跳过 WhisperX"
    echo "============================================="
elif [ -f "$JSON_PATH" ]; then
    echo "============================================="
    echo "原始 JSON 已存在, 跳过 — $JSON_PATH"
    echo "============================================="
else
    echo ""
    echo "============================================="
    echo "pipeline — 步骤 2/6: WhisperX 生成 JSON"
    echo "============================================="
    echo ""
    if ! bash "$WHISPER_SCRIPT" "$VIDEO_PATH"; then
        echo "Error: whisper.sh failed." >&2
        exit 1
    fi
    echo ""
fi

# 双语 ASS 字幕 (EXISTING_ASS 可覆盖默认路径)
if [ -n "${EXISTING_ASS:-}" ]; then
    ASS_PATH="$EXISTING_ASS"
else
    ASS_PATH="$(
        "$PYTHON_BIN" "$TRANSLATE_SCRIPT" "$JSON_PATH" "${LANG_ARGS[@]}" --print-output-path \
        | awk -F= '/^OUTPUT_ASS=/{print $2}' \
        | tail -n 1
    )"
fi

# ── 步骤 3: JSON 时间码美化 ───────────────────────────────────────────────────

if [ "${SKIP_BEAUTIFY:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_BEAUTIFY=1 — 跳过 JSON 时间轴美化"
    echo "============================================="
elif [ -f "$BEAUTIFIED_JSON" ]; then
    echo "============================================="
    echo "美化 JSON 已存在, 跳过 — $BEAUTIFIED_JSON"
    echo "============================================="
else
    echo "============================================="
    echo "pipeline — 步骤 3/6: JSON 时间码美化 → .beautified.json"
    echo "============================================="
    echo ""

    if [ ! -f "$JSON_PATH" ]; then
        echo "Error: No JSON file found for beautify." >&2
        echo "Expected: $JSON_PATH" >&2
        exit 1
    fi

    "$PYTHON_BIN" "$TRANSLATE_SCRIPT" "$JSON_PATH" --video "$VIDEO_PATH" --only-beautify "${BEAUTIFY_ARGS[@]}"
    echo ""
fi


# ── 步骤 4: 术语知识库 (联网搜索 + LLM) ─────────────────────────────────────

# 确定给 knowledge/translate 的 JSON 输入
if [ -f "$BEAUTIFIED_JSON" ]; then
    TRANSLATE_SRC="$BEAUTIFIED_JSON"
else
    TRANSLATE_SRC="$JSON_PATH"
fi

if [ "${SKIP_KNOWLEDGE:-0}" = "1" ]; then
    echo "============================================="
    echo "SKIP_KNOWLEDGE=1 — 跳过术语知识库"
    echo "============================================="
elif [ -f "$VIDEO_DIR/glossary.md" ]; then
    echo "============================================="
    echo "glossary.md 已存在, 跳过 — $VIDEO_DIR/glossary.md"
    echo "============================================="
else
    echo "============================================="
    echo "pipeline — 步骤 4/6: AI Agent 生成术语知识库 → glossary.md"
    echo "============================================="
    echo ""
    "$PYTHON_BIN" "$TRANSLATE_SCRIPT" "$TRANSLATE_SRC" --video "$VIDEO_PATH" --only-glossary --skip-beautify
    echo ""
fi

# ── 步骤 5: LLM 翻译 ───────────────────────────────────────────────────────

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
    echo "pipeline — 步骤 5/6: LLM 翻译 (${SOURCE_LANG:-"(JSON language)"} → $TARGET_LANG) → $(basename "$ASS_PATH")"
    echo "============================================="
    echo ""

    if [ ! -f "$TRANSLATE_SRC" ]; then
        echo "Error: No JSON file found for translation." >&2
        echo "Expected: $TRANSLATE_SRC" >&2
        exit 1
    fi

    # 校对默认开启, PROOFREAD=0 关闭
    if [ "${PROOFREAD:-1}" = "0" ]; then
        export PROOFREAD=0
    fi
    "$PYTHON_BIN" "$TRANSLATE_SCRIPT" "$TRANSLATE_SRC" --video "$VIDEO_PATH" --skip-beautify --skip-knowledge \
        "${LANG_ARGS[@]}"

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
    echo "pipeline — 步骤 6/6: 字幕硬压 → burned.mkv"
    echo "============================================="
    echo ""

    if [ -z "${ASS_PATH:-}" ] || [ ! -f "$ASS_PATH" ]; then
        echo "Warning: bilingual ASS not found, skipping burn." >&2
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
if [ -n "${JSON_PATH:-}" ] && [ -f "$JSON_PATH" ]; then
    echo "JSON:     $JSON_PATH"
fi
if [ -n "${BEAUTIFIED_JSON:-}" ] && [ -f "$BEAUTIFIED_JSON" ]; then
    echo "美化JSON: $BEAUTIFIED_JSON"
fi
if [ -n "${ASS_PATH:-}" ] && [ -f "$ASS_PATH" ]; then
    echo "双语 ASS: $ASS_PATH"
fi
echo "============================================="
