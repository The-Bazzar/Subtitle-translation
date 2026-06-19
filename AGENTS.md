# AGENTS.md

本文件是项目的唯一权威文档。代码路径、行为、配置以此为准。

## Overview

`download -> whisper -> beautify -> glossary -> translate/proofread -> burn`

Windows 主机，PowerShell 7 为必需运行环境（旧版 PS 5.x 会导致脚本报错）。升级命令：`winget install Microsoft.PowerShell`。Linux / WSL bash 脚本并行维护。
项目使用本地工具完成下载/语音识别/硬压，远程 LLM API 完成翻译和校对。

## Architecture

所有工具脚本位于仓库根目录：

```text
├── pipeline.ps1              # Windows: 超级流水线 (URL → burned.mkv)
├── pipeline.sh               # Linux/bash: 流水线 (同流程)
├── download.ps1              # Windows: 仅下载
├── download.sh               # Linux: 仅下载
├── download_and_sub.sh       # Linux: 下载 + WhisperX 字幕
├── whisper.ps1               # Windows: WhisperX 语音识别
├── whisper.sh                # Linux: WhisperX 语音识别
├── beautify_srt.py           # Python: 场景检测 + 帧率自适应 → 美化 SRT (Netflix 规范)
├── glossary_builder.py       # Python: 自动生成 glossary.md (可选 Tavily 搜索)
├── translate_srt.py          # Python: LLM 分句 + 翻译 + 校对 + ASS 导出
├── ffmpeg-burn.ps1           # Windows: ffmpeg 字幕硬压 (流水线默认)
├── ffmpeg-burn.sh            # Linux: ffmpeg 字幕硬压
├── mpv-burn.ps1              # Windows: mpv 硬压 (备选)
├── mpv-burn.sh               # Linux: mpv 硬压 (备选)
├── batch.ps1                 # Windows: 批量处理多个 URL
├── batch.py                  # Linux: 批量处理多个 URL
├── setup.ps1                 # Windows: 环境安装
├── setup.sh                  # Linux: 环境安装
├── template.ass              # ASS 字幕模板 (Style: zh / bi-en / bi-zh)
├── .env                      # API keys + 流水线配置 (gitignored)
├── .env.example              # 环境变量模板
├── providers.json            # LLM provider 配置 (gitignored)
├── providers.example.json    # provider 配置模板
├── translate_prompt.md       # 翻译 prompt (可自定义)
├── translate_prompt.example.md
├── proofread_prompt.md       # 校对 prompt (可自定义)
├── proofread_prompt.example.md
├── AGENTS.md                 # ← 本文档
├── README.md                 # 用户向文档
├── CLAUDE.md                 # 已弃用 (内容合并至此)
├── .gitignore
├── .agents/skills/
│   ├── beautify/SKILL.md
│   ├── download/SKILL.md
│   ├── knowledge/SKILL.md
│   ├── translate/SKILL.md
│   └── whisper/SKILL.md
└── cookies.txt               # YouTube cookies (gitignored)
```

## Pipeline Flow

### pipeline.sh (Linux)

1. **download + whisper** — `download_and_sub.sh` 下载视频 + WhisperX 识别 → `.srt` + `.json`
2. **beautify** — `beautify_srt.py` 场景吸附 → `.beautified.srt`
3. **glossary** — `glossary_builder.py` 术语知识库 → `glossary.md`
4. **translate** — `translate_srt.py` 分句 + 翻译 + 校对 → `.zh.srt` + `.zh.ass` + `.zh-en.ass`
5. **burn** — `ffmpeg-burn.sh` 双语 ASS 硬压 → `burned.mkv`

成果物链: `video → srt → beautified.srt → glossary.md → zh-en.ass → burned.mkv`

### pipeline.ps1 (Windows)

1. 调用 `wsl bash pipeline.sh` 完成下载+美化+翻译（成果物在 WSL 侧）
2. `wslpath -w` 转换 Linux 路径为 Windows 路径
3. 调用 `ffmpeg-burn.ps1` 将 `.zh-en.ass` 硬压到视频 → `burned.mkv`

### 跳过控制

| 环境变量 | PowerShell 参数 | 效果 |
|---------|----------------|------|
| `SKIP_DOWNLOAD` / `PIPELINE_SKIP_DOWNLOAD` | `-SkipDownload` | 跳过下载 |
| `SKIP_WHISPER` / `PIPELINE_SKIP_WHISPER` | `-SkipWhisper` | 跳过语音识别 |
| `SKIP_BEAUTIFY` / `PIPELINE_SKIP_BEAUTIFY` | `-SkipBeautify` | 跳过时间码美化 |
| `SKIP_KNOWLEDGE` / `PIPELINE_SKIP_KNOWLEDGE` | `-SkipKnowledge` | 跳过术语知识库 |
| `SKIP_TRANSLATE` / `PIPELINE_SKIP_TRANSLATE` | `-SkipTranslate` | 跳过翻译/校对 |
| `SKIP_BURN` / `PIPELINE_SKIP_BURN` | `-SkipBurn` | 跳过硬压 |

bash 从 `.env` 继承 `PIPELINE_SKIP_*` 变量。

## Step Behavior

### download

- 视频文件名固定为 `<video_dir>/<video_dir>.<ext>`
- 同步下载缩略图 `.png`、元数据 `.info.json`、简介 `.description`、标签 `.tags.txt`
- SponsorBlock 去广告

### whisper

- 已存在 `.srt` 自动跳过
- 视频先转为 mono 16kHz WAV 再识别
- WhisperX 输出 `--output_format all`（SRT + JSON + TXT/TSV/VTT）
- `.json` 包含词级时间码，供 `translate_srt.py` 分句对轴
- `WHISPER_DEVICE` 控制 `cuda` / `cpu`

### beautify

- 不覆盖原始 `.srt`，默认输出 `.beautified.srt`
- 场景吸附代替关键帧吸附（默认关闭 `--use-keyframes`）
- 入点吸附到前一个场景切换 (7帧内)
- 出点吸附到下一个场景切换前 2 帧 (Netflix 规范)
- 重叠/间隙修复：<500ms 自动合并
- 时长约束：最短 1s / 最长 8s

### glossary

- 位于 beautify 之后、translate 之前
- 输入优先 `.beautified.srt`，回退 `.srt`
- 读取 `.description`、`.tags.txt`、`.info.json` 作为上下文
- 配置 `TAVILY_API_KEY` 时联网搜索术语标准译法
- 回退离线总结
- 需要 `TRANSLATE_PROVIDER` 配置（复用 LLM 栈）

### translate

- 输出: `.split.srt` / `.zh.srt` / `.proofread.srt` / `.zh.ass` / `.zh-en.ass` / `.zh.description`
- `.split.srt` 存在时跳过 LLM 分句
- `.zh.srt` 存在时跳过 LLM 翻译
- 长句拆分 (>60 chars 或 >3s) 用 LLM 按自然语言边界拆分，WhisperX JSON 精确对轴
- 分批翻译 (50条/批)，保留 `\N` 软换行
- 双语校对 (Pass 2)，可交叉模型
- 自动检测 `glossary.md` 注入 Pass 3 术语校对
- 双语 ASS 使用 `bi-en` / `bi-zh` 样式，仅中文 ASS 使用 `zh`

### burn

- ffmpeg ASS 滤镜硬压为流水线默认路径
- 保留原视频封面图
- 指定分辨率时保持宽高比，自动补黑边

## Config

### `.env`

| 变量 | 说明 |
|------|------|
| `WHISPER_MODEL` | 默认为 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | 对齐模型，空则自动选择 |
| `WHISPER_DEVICE` | `cuda` / `cpu` |
| `TRANSLATE_PROVIDER` | 翻译后端 (`openrouter` / `deepseek` / `gemini`) |
| `TRANSLATE_MODEL` | 翻译模型，空则用 provider 默认 |
| `PROOFREAD` | `1` / `0` 控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider，空则复用翻译 provider |
| `PROOFREAD_MODEL` | 校对专用模型 |
| `BURN_OVC` | 硬压编码器 (默认 `hevc_nvenc`) |
| `BURN_OVCOPTS` | 编码参数 (默认 `qp=20`) |
| `BURN_OAC` | 音频编码器 (默认 `aac`) |
| `BURN_RES` | 输出分辨率 (空=原分辨率) |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `GEMINI_API_KEY` | Gemini API key |
| `TAVILY_API_KEY` | Tavily 搜索 API key |
| `TAVILY_MAX_RESULTS` | 搜索结果上限 (默认 10) |

### `providers.json`

格式：

```json
{
  "my_provider": {
    "url": "https://api.example.com/v1",
    "default_model": "my-model",
    "env_key": "MY_API_KEY",
    "auth_header": "Bearer {api_key}",
    "extra_headers": {}
  }
}
```

`url` 是 OpenAI SDK 的 `base_url`，不包含 `/chat/completions`。

## Key Commands

### PowerShell (推荐)

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
.\pipeline.ps1 "https://youtu.be/xxxxx" -TranslateProvider deepseek -Ovc libx265 -Ovcopts crf=23
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipKnowledge
.\batch.ps1 "URL1" "URL2"
```

### Linux / WSL

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
./pipeline.sh "URL" -- --scene-threshold 0.12 --snap-frames 10
SKIP_BEAUTIFY=1 ./pipeline.sh "URL"
SKIP_BURN=1 ./pipeline.sh "URL"
./download_and_sub.sh "URL"
python3 batch.py "URL1" "URL2"
```

### 仅下载

```powershell
.\download.ps1 "URL"
```

### 字幕硬压

```powershell
.\ffmpeg-burn.ps1 "video.webm"
.\mpv-burn.ps1 "C:\path\to\video.webm"
```

```bash
./mpv-burn.sh "path/to/video.webm"
./mpv-burn.sh video.webm -o result.mkv
./mpv-burn.sh video.webm --ovc libx265 --ovcopts crf=23 --slang=en,zh
```

### 时间码美化

```bash
./beautify_srt.sh "path/to/video.webm"
./beautify_srt.sh video.webm -o video.srt --backup
./beautify_srt.sh video.webm --scene-threshold 0.2 --snap-frames 10
./beautify_srt.sh --help
```

### 翻译

```bash
python3 translate_srt.py video.srt
python3 translate_srt.py video.srt -o custom.zh-en.ass
```

### 从 PowerShell 调用 Linux

```powershell
wsl -u root bash -lc "sh ./download_and_sub.sh https://www.youtube.com/watch?v=xxxxx"
```

## Skills

项目技能文件在 `.agents/skills/` 目录下，每个技能一个文件夹，格式为 `skill-dir/SKILL.md`：

- `.agents/skills/beautify/SKILL.md`
- `.agents/skills/download/SKILL.md`
- `.agents/skills/knowledge/SKILL.md`
- `.agents/skills/translate/SKILL.md`
- `.agents/skills/whisper/SKILL.md`

## Dependencies

| 工具 | 用途 |
|------|------|
| `yt-dlp` | YouTube 视频/元数据下载 |
| `uv` | 安装 WhisperX 为全局工具 |
| `whisperx` (large-v3-turbo) | AI 语音识别 + 词级对齐 |
| `ffmpeg` / `ffprobe` | 音频提取、场景检测、字幕硬压 |
| `python3` | beautify_srt.py / glossary_builder.py / translate_srt.py |
| `openai` Python 包 | LLM 调用 (分句 / 翻译 / 校对) |
| `tavily-python` | glossary 联网搜索 (可选) |

## Important Notes

- `.env` 和 `providers.json` 已 gitignored，仓库只提交 example 模板
- `cookies.txt` 已 gitignored，过期后重新导出
- `TRANSLATE_PROVIDER` 必须配置，否则翻译和 glossary 直接报错
- WhisperX 首次运行下载 `large-v3-turbo` (~1.5GB)
- WhisperX 安装: `uv tool install git+https://github.com/m-bain/whisperx.git --with "torch==2.8.0+cu128" --with "torchaudio==2.8.0+cu128"`
- `translate_srt.py` 内置 LLM 长句拆分，`--no-split` 禁用
- 美化默认不覆盖原文件，输出 `.beautified.srt`，`-o same.srt` 覆盖
- 场景检测对长视频较耗时 (~5分/小时视频)
- 帧数参数按实际帧率自动换算为秒
- 关键帧吸附默认关闭（`--use-keyframes` 启用），场景吸附已足够
- 流水线自动跳过已完成的步骤（`.beautified.srt`、`.zh-en.ass` 等存在时跳过对应阶段）
- 翻译不消耗本地 GPU，全部通过 LLM API
- 双语 `.zh-en.ass` 使用 `bi-en` / `bi-zh` 样式，仅中文 `.zh.ass` 使用 `zh`

## Working Notes

- 优先读取 `.env` 而非硬编码工具路径
- 保持 PowerShell 和 bash 入口行为对齐
- 更新文档时匹配实际代码路径和参数，而非历史版本
- 仓库可能包含用户本地的 `.env`、`providers.json`、cookies 和生成产物，不要回退用户数据


