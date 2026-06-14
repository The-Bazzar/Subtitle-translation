# YouTube 视频下载 + AI 字幕生成 + 时间码美化 + 翻译 + 硬压

一键流水线：从 YouTube 链接直达 burned.mkv 硬字幕视频。

## 🛠 前置依赖

### WSL (必需)

```bash
# Python 包管理器 (用于运行 WhisperX)
curl -LsSf https://astral.sh/uv/install.sh | sh

# yt-dlp — 视频下载
sudo wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp

# FFmpeg — 音视频处理 + 场景检测
sudo apt update && sudo apt install -y ffmpeg

# Node.js — yt-dlp YouTube 验证
sudo apt install -y nodejs
```

### Windows (可选)

| 工具 | 用途 |
|------|------|
| `yt-dlp` | `download.ps1` 仅下载 |

---

## ⚙️ 配置文件 .env

在项目根目录创建 `.env` 文件，所有脚本 (`pipeline.sh`, `pipeline.ps1`, `translate_srt.py`) 均从这里读取配置：

```ini
# ── 翻译默认配置 ──
TRANSLATE_PROVIDER=deepseek       # 翻译后端: openrouter | deepseek | gemini
TRANSLATE_MODEL=deepseek-v4-pro   # 模型名, 留空则使用后端内置默认

# ── 系统提示词 (留空使用内置 Netflix 规范默认) ──
TRANSLATE_SYSTEM_PROMPT=
PROOFREAD_SYSTEM_PROMPT=

# ── 校对专用后端/模型 (留空则与翻译共用, 可实现交叉校对) ──
PROOFREAD_PROVIDER=
PROOFREAD_MODEL=

# ── API keys (至少配置一个对应 TRANSLATE_PROVIDER 的 key) ──
OPENROUTER_API_KEY=sk-or-v1-xxx   # https://openrouter.ai/keys
DEEPSEEK_API_KEY=sk-xxx           # https://platform.deepseek.com
GEMINI_API_KEY=xxx                # https://aistudio.google.com
```

| 变量 | 必填 | 说明 |
|------|:--:|------|
| `TRANSLATE_PROVIDER` | 否 | 翻译后端，不设默认 `openrouter`。所有脚本均读取 |
| `TRANSLATE_MODEL` | 否 | 模型名，不设使用后端内置默认 |
| `TRANSLATE_SYSTEM_PROMPT` | 否 | 翻译系统提示词，留空使用内置 Netflix 规范提示词 |
| `PROOFREAD_SYSTEM_PROMPT` | 否 | 校对系统提示词，留空使用内置校对提示词 |
| `PROOFREAD_PROVIDER` | 否 | 校对专用后端，留空与翻译共用（可实现交叉校对） |
| `PROOFREAD_MODEL` | 否 | 校对专用模型，留空与翻译共用 |
| `OPENROUTER_API_KEY` | * | OpenRouter API key |
| `DEEPSEEK_API_KEY` | * | DeepSeek API key |
| `GEMINI_API_KEY` | * | Gemini API key |

> \* 至少配置一个与你选择的 `TRANSLATE_PROVIDER` 对应的 key

**读取优先级**：
| 脚本 | 优先级 (高→低) |
|------|---------------|
| `pipeline.ps1` | CLI 参数 → `.env` → 默认值 |
| `pipeline.sh` | 环境变量 → `.env` → 默认值 |
| `translate_srt.py` | CLI 参数 → `.env` → 默认值 |

`.env` 已 gitignored，不要提交。换行符支持 LF / CRLF（脚本自动处理 `\r`）。

---

## 🚀 快速开始

### 超级流水线 (PowerShell, 推荐)

```powershell
# 一键: YouTube URL → burned.mkv
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"

# 仅翻译不压制
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
```

### WSL 流水线

```bash
# 一键: 下载 → 字幕 → 美化 → 翻译 → 硬压
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 跳过硬压
BURN=0 ./pipeline.sh "url"
```

执行后在视频目录生成：

```
视频标题/
├── 视频标题.webm           # 视频文件
├── 视频标题.srt            # 原始英文字幕 (WhisperX)
├── 视频标题.beautified.srt # 美化后的英文字幕 (Netflix 规范)
├── 视频标题.zh.srt         # 中文 SRT 翻译缓存 (二次运行跳过 LLM)
├── 视频标题.zh.ass         # 仅中文 ASS (style=zh)
├── 视频标题.zh-en.ass      # 双语 ASS (bi-en + bi-zh, 硬压用) ✨
├── 视频标题.png            # 封面缩略图 (PNG)
├── 视频标题.info.json      # yt-dlp 元数据
└── 视频标题.description    # 视频简介
```

---

## 📖 命令参考

### `pipeline.ps1` — 超级流水线 (PowerShell)

从 YouTube URL 到硬字幕 burned.mkv。自动调用 WSL 完成下载/字幕/美化/翻译，再调用 ffmpeg 硬压（保留封面图）。

```powershell
# 基础用法
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"

# 选择翻译后端和模型
.\pipeline.ps1 "https://youtu.be/xxxxx" -TranslateProvider deepseek -TranslateModel deepseek-v4-pro

# 自定义编码 + 输出
.\pipeline.ps1 "https://youtu.be/xxxxx" -o result.mkv -Ovc libx265 -Ovcopts crf=23

# 仅翻译不压制
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn

# 仅翻译不校对
.\pipeline.ps1 "https://youtu.be/xxxxx" -NoProofread

# 使用已有双语 ASS
.\pipeline.ps1 "https://youtu.be/xxxxx" -ExistingAss path/to/existing.zh-en.ass

# 预览命令
.\pipeline.ps1 "https://youtu.be/xxxxx" -DryRun

# 透传 ffmpeg 额外参数
.\pipeline.ps1 "https://youtu.be/xxxxx" -- -preset fast
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-Url` | (必选) | YouTube 视频链接 |
| `-o, -Output` | `burned.mkv` | 输出路径 |
| `-p, -TranslateProvider` | openrouter | 翻译后端 |
| `-tm, -TranslateModel` | 后端默认 | 翻译模型 |
| `-m, -MpvPath` | mpv-lazy | mpv.com 路径 |
| `-Ovc` | `hevc_nvenc` | 视频编码器 |
| `-Ovcopts` | `qp=20` | 编码器参数 |
| `-Oac` | `aac` | 音频编码器 |
| `-SkipDownload` | — | 跳过下载 |
| `-SkipBeautify` | — | 跳过美化 |
| `-SkipTranslate` | — | 跳过翻译 |
| `-NoProofread` | — | 关闭校对 |
| `-SkipBurn` | — | 跳过压制 |
| `-ExistingAss` | — | 已有 .zh-en.ass 路径 |
| `-DryRun` | — | 仅打印命令 |

### `batch.ps1` — 批量并行流水线

多个 YouTube 链接并行执行 `pipeline.ps1`，最大化利用 CPU/GPU/网络资源。

```powershell
# 并行处理多个视频
.\batch.ps1 "URL1" "URL2" "URL3"

# 限制并行数 + 指定翻译后端
.\batch.ps1 -j 4 -p deepseek url1 url2 url3 url4 url5

# 仅出字幕不压制
.\batch.ps1 url1 url2 url3 -SkipBurn

# 预览命令
.\batch.ps1 url1 url2 -DryRun
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-Urls` | (必选) | YouTube 链接列表 |
| `-j, -MaxJobs` | CPU 核心数 | 最大并行数 |
| `-p, -TranslateProvider` | .env | 翻译后端 |
| `-tm, -TranslateModel` | .env | 翻译模型 |
| `-SkipBurn` | — | 跳过硬压 |
| `-DryRun` | — | 仅打印命令 |

### `pipeline.sh` — WSL 流水线

串联下载 → 字幕 → 美化 → 翻译 → 硬压（默认全开）。

```bash
# 基础用法
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 传递美化选项 (-- 之后)
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --preview
./pipeline.sh "url" -- --backup --scene-threshold 0.2

# 跳过某些步骤
SKIP_DOWNLOAD=1 ./pipeline.sh "url"
SKIP_BEAUTIFY=1 ./pipeline.sh "url"
SKIP_TRANSLATE=1 ./pipeline.sh "url"
BURN=0 ./pipeline.sh "url"

# 使用已有产物
EXISTING_SRT=/path/to/beautified.srt ./pipeline.sh "url"
EXISTING_ASS=/path/to/existing.zh-en.ass ./pipeline.sh "url"

# 选择翻译后端
TRANSLATE_PROVIDER=deepseek TRANSLATE_MODEL=deepseek-v4-pro ./pipeline.sh "url"

# 交叉校对
PROOFREAD_PROVIDER=openrouter PROOFREAD_MODEL=anthropic/claude-sonnet-4-6 ./pipeline.sh "url"

# 仅翻译不校对
PROOFREAD=0 ./pipeline.sh "url"
```

**流程**：yt-dlp 下载 → WhisperX 字幕 → 场景检测美化 → LLM 翻译 → ffmpeg 硬压

**成果物链**：`VIDEO_PATH` → `BEAUTIFIED_SRT` → `ASS_PATH` → `burned.mkv`
- 每步输出作为下一步输入，已存在的中间产物自动跳过
- `.zh.srt` 翻译缓存存在时自动跳过 LLM

| 环境变量 | 默认值 | 说明 |
|------|--------|------|
| `SKIP_DOWNLOAD` | 0 | 跳过下载 |
| `SKIP_BEAUTIFY` | 0 | 跳过美化 |
| `SKIP_TRANSLATE` | 0 | 跳过翻译 |
| `EXISTING_SRT` | — | 已有美化 SRT 路径 |
| `EXISTING_ASS` | — | 已有 .zh-en.ass 路径 |
| `TRANSLATE_PROVIDER` | openrouter | 翻译后端 |
| `TRANSLATE_MODEL` | 后端默认 | 翻译模型 |
| `PROOFREAD` | 1 | 0=关闭校对 |
| `PROOFREAD_PROVIDER` | 同翻译 | 校对后端 |
| `PROOFREAD_MODEL` | 同翻译 | 校对模型 |
| `BURN` | 1 | 0=跳过硬压 |
| `BURN_OVC` | hevc_nvenc | 视频编码器 |
| `BURN_OVCOPTS` | qp=20 | 编码器参数 |
| `BURN_OAC` | aac | 音频编码器 |

### `download_and_sub.sh` — 下载 + WhisperX 字幕

```bash
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"
# 输出: OUTPUT_VIDEO=<绝对路径>  (供 pipeline.sh 解析)
```

### `beautify_srt.sh` — 字幕时间码美化

```bash
# 自动查找同目录 .srt → .beautified.srt (不覆盖原文件)
./beautify_srt.sh video.webm

# 指定字幕 + 输出
./beautify_srt.sh video.webm subtitle.srt
./beautify_srt.sh video.webm -o result.srt

# 覆盖原文件 (显式指定)
./beautify_srt.sh video.webm -o video.srt --backup

# 仅预览变化
./beautify_srt.sh video.webm --preview
```

**算法流程**：帧率检测 → 场景检测 (≥7帧间隔) → 入点吸附到场景 → 出点吸附到场景前2帧 → 重叠/间隙修复 → 时长约束

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `<原名>.beautified.srt` | 输出路径 (默认不覆盖原文件) |
| `--scene-threshold` | `0.25` | 场景检测灵敏度 |
| `--snap-frames` | `7` | 吸附到场景切换的最大帧数 |
| `--end-offset-frames` | `2` | 出点对齐到场景前 N 帧 |
| `--min-scene-interval-frames` | `7` | 场景切换最小帧间隔 |
| `--min-duration` | `1.0` | 最短字幕时长 (秒) |
| `--max-duration` | `8.0` | 最长字幕时长 (秒) |
| `--min-gap` | `0.083` | 字幕最小间距 (秒) |
| `--max-gap-merge` | `0.5` | 间隙合并阈值 (秒) |
| `--use-keyframes` | 关闭 | 启用关键帧吸附 |
| `--extend` | 关闭 | 延伸字幕填充间隙 |
| `--no-scene-snap` | — | 跳过场景吸附 |
| `--preview` | — | 仅预览, 不写入 |
| `--backup` | — | 覆盖前备份 |

### `translate_srt.py` — 字幕翻译

两轮 LLM 翻译流程：翻译 (Pass 1) → 校对 (Pass 2, 默认开启)。

输出三类文件：
- `.zh.srt` — 中文翻译缓存（同目录已存在则跳过 LLM，校对后覆盖为精校版）
- `.zh.ass` — 仅中文 ASS（style=zh）
- `.zh-en.ass` — 双语 ASS（bi-en + bi-zh，硬压用）
- 中文按 Netflix 规范自动去除标点（仅保留 `《》`），自动插入 `\N` 软换行

```bash
# 基础翻译 + 校对 (默认开启)
python3 translate_srt.py video.srt

# 指定后端
python3 translate_srt.py video.srt --provider deepseek --model deepseek-v4-pro

# 仅翻译不校对
PROOFREAD=0 python3 translate_srt.py video.srt

# 交叉校对: DeepSeek 翻译 → Claude 校对
python3 translate_srt.py video.srt \
    --provider deepseek \
    --proofread-provider openrouter \
    --proofread-model anthropic/claude-sonnet-4-6

# 自定义提示词
python3 translate_srt.py video.srt \
    --system-prompt "你的翻译提示词" \
    --proofread-prompt "你的校对提示词"

# 自定义输出
python3 translate_srt.py video.srt --title "My Video" -o custom.zh-en.ass
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--provider` | openrouter | 翻译后端 |
| `--model` | 后端默认 | 翻译模型 |
| `--batch-size` | `50` | 每批翻译行数 |
| `--proofread` | 开启 | 中英校对（`PROOFREAD=0` 关闭） |
| `--proofread-provider` | 同翻译 | 校对专用后端（交叉校对） |
| `--proofread-model` | 同翻译 | 校对专用模型 |
| `--system-prompt` | 内置默认 | 自定义翻译提示词 |
| `--proofread-prompt` | 内置默认 | 自定义校对提示词 |
| `--title` | SRT 文件名 | 视频标题 (写入 ASS Title) |
| `--template` | `./template.ass` | ASS 模板路径 |
| `-o, --output` | 自动 | 输出 `.zh-en.ass` (`.zh.srt` + `.zh.ass` 同目录) |

### `ffmpeg-burn.sh` — 字幕硬压 (WSL, 默认)

使用 ffmpeg 的 `ass` 滤镜硬压双语字幕，**保留原视频封面图**。流水线默认使用此脚本。

```bash
# 基础用法
./ffmpeg-burn.sh path/to/video.webm --sub-file video.zh-en.ass

# 自定义编码器
./ffmpeg-burn.sh video.webm --sub-file sub.ass -o result.mkv --ovc libx265 --ovcopts crf=23
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `burned.mkv` | 输出路径 |
| `--sub-file` | — | 字幕文件路径 |
| `--ovc` | `hevc_nvenc` | 视频编码器 |
| `--ovcopts` | `qp=20` | 编码器参数 |
| `--oac` | `aac` | 音频编码器 |
| `--ffmpeg-path` | `ffmpeg` | ffmpeg 路径 |
| `--dry-run` | — | 仅打印命令 |

### `ffmpeg-burn.ps1` — 字幕硬压 (PowerShell, 默认)

```powershell
.\ffmpeg-burn.ps1 "C:\path\to\video.webm" -SubFile video.zh-en.ass
# 输出: burned.mkv (同目录, 保留封面图)
```

### `mpv-burn.sh` / `mpv-burn.ps1` — 字幕硬压 (高级)

mpv 编码模式，支持补帧滤镜等高级功能。仅手动使用，流水线默认用 ffmpeg-burn。

```bash
./mpv-burn.sh video.webm --sub-file sub.ass -- --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy"
```

---

## 📂 项目结构

```
Subtitle translation/
├── batch.ps1                 # 批量并行流水线 (PowerShell): 多URL并行
├── pipeline.ps1              # 超级流水线 (PowerShell): URL → burned.mkv
├── pipeline.sh               # WSL 流水线 (下载 → 美化 → 翻译 → 硬压)
├── download_and_sub.sh       # 下载视频 + 生成英文字幕
├── beautify_srt.sh           # 字幕时间码美化入口
├── beautify_srt.py           # 美化核心算法 (场景检测 + Netflix 帧对齐)
├── translate_srt.py          # 字幕翻译: LLM 英→中 → .zh.srt + .zh.ass + .zh-en.ass
├── ffmpeg-burn.sh            # WSL: 字幕硬压 (ffmpeg ass 滤镜, 默认)
├── ffmpeg-burn.ps1           # PowerShell: 字幕硬压 (ffmpeg, 默认)
├── mpv-burn.sh               # WSL: 字幕硬压 (mpv 编码, 高级)
├── mpv-burn.ps1              # PowerShell: 字幕硬压 (mpv 编码, 高级)
├── template.ass              # ASS 模板 (bi-en / bi-zh / zh 样式定义)
├── download.ps1              # PowerShell: 仅下载 (不含字幕)
├── .env                      # API keys + 翻译默认配置 (gitignored)
├── cookies.txt               # YouTube 登录凭证 (gitignored)
└── <Video Title>/             # 每个视频独立的输出目录
    ├── <Video Title>.<ext>   # 视频文件 (webm/mp4/mkv)
    ├── <Video Title>.srt     # 原始英文字幕 (WhisperX)
    ├── <Video Title>.beautified.srt  # 美化后英文字幕
    ├── <Video Title>.zh.srt  # 中文 SRT 翻译缓存
    ├── <Video Title>.zh.ass  # 仅中文 ASS
    ├── <Video Title>.zh-en.ass  # 双语 ASS (硬压用)
    ├── <Video Title>.webp    # 封面缩略图
    ├── <Video Title>.info.json     # yt-dlp 元数据
    └── <Video Title>.description   # 视频简介
```

---

## 🎬 完整用例

### 方案 A: 超级流水线 (一条命令)

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

```
YouTube URL
  │  pipeline.ps1
  ▼
┌─────────────────────────────────────────────────────┐
│ 1. yt-dlp 下载视频 + SponsorBlock 去广告             │  WSL
│ 2. WhisperX large-v3 生成英文字幕 (.srt)             │  WSL
│ 3. ffmpeg 场景检测 → 时间码美化 → .beautified.srt    │  WSL
│ 4. LLM 翻译 + 校对 (双轮) → .zh.srt + .zh.ass + .zh-en.ass │  WSL
│ 5. ffmpeg 硬压双语字幕 → burned.mkv (保留封面图)      │  Windows
└─────────────────────────────────────────────────────┘
```

### 方案 B: WSL 分步

```bash
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"
./beautify_srt.sh "视频标题/视频标题.webm"
python3 translate_srt.py "视频标题/视频标题.srt" --provider openrouter
./ffmpeg-burn.sh "视频标题/视频标题.webm" --sub-file "视频标题/视频标题.zh-en.ass"
```

### 方案 C: 分离翻译 + 压制

```bash
# WSL 中完成所有字幕工作
TRANSLATE_PROVIDER=deepseek ./pipeline.sh "url"
# 输出: OUTPUT_VIDEO=... OUTPUT_ASS=...
```
```powershell
# Windows 端单独压制
.\ffmpeg-burn.ps1 "C:\...\video.webm" -SubFile video.zh-en.ass
```

### 环境要求

| 阶段 | 环境 | 依赖 |
|------|------|------|
| 下载 + 字幕 | WSL | `yt-dlp`, `uvx` (whisperx + large-v3) |
| 时间码美化 | WSL | `ffmpeg`, `ffprobe`, `python3` |
| LLM 翻译 | WSL | `.env` 中 API key |
| 硬压字幕 | WSL / Windows | `ffmpeg` |

---

## 💡 注意事项

- **WhisperX 首次运行**：自动下载 `large-v3` 模型（数 GB），保持网络畅通。
- **GPU 加速**：WhisperX 默认 float16 + GPU。无 NVIDIA 显卡需 `--compute_type int8` 或 `--device cpu`。
- **cookies.txt**：YouTube 登录凭证，过期后需重新导出。已 gitignored。
- **场景检测耗时**：长视频可能较慢（~5 分钟/小时视频）。
- **美化默认不覆盖**：输出 `.beautified.srt`，不修改原始字幕。流水线检测到已存在的自动跳过。
- **翻译缓存**：`.zh.srt` 存在时自动跳过 LLM，直接合成 `.zh.ass` + `.zh-en.ass`。校对后缓存覆盖为精校版。
- **两轮校对**：翻译 (Pass 1) 后默认执行中英校对 (Pass 2)，`PROOFREAD=0` 关闭。校对支持**交叉模型**（如 DeepSeek 翻译 + Claude 校对）。
- **Netflix 中文规范**：默认翻译提示词去除所有中文标点（仅保留 `《》`），用空格替代停顿。可通过 `.env` 的 `TRANSLATE_SYSTEM_PROMPT` 自定义。
- **双语字幕**：`.zh-en.ass` 先排英文 (bi-en, 36px)，后排中文 (bi-zh, 72px)，中文自动 `\N` 换行。
- **硬压默认开启**：`pipeline.sh` 默认 BURN=1，设 `BURN=0` 跳过硬压。`pipeline.ps1` 的 burn 在 Windows 端执行。
- **帧率自适应**：所有帧数参数按实际视频 fps 换算为秒。
- **关键帧吸附**：默认关闭 (`--use-keyframes` 启用)，支持 H.264/H.265/VP9。
