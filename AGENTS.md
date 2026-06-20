# AGENTS.md

本文件是项目的唯一权威文档。代码路径、行为、配置以当前仓库为准；不要引用历史脚本或本地视频项目路径。

## Overview

`download -> whisper(json) -> beautify(json words) -> glossary -> translate -> split -> proofread -> ass -> burn`

Windows 主机必须使用 PowerShell 7，旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。升级命令：

```powershell
winget install Microsoft.PowerShell
```

项目使用本地工具完成下载、语音识别、时间轴处理和硬压，使用远程 LLM API 完成 glossary、翻译、分割和校对。主字幕入口是 WhisperX `.json`，SRT 不再作为输入缓存。

## Repository Layout

所有运行脚本位于仓库根目录：

```text
├── pipeline.ps1              # Windows: download -> whisper -> translate_srt.py -> ffmpeg-burn
├── pipeline.sh               # Linux/WSL: 同流程
├── download.ps1              # Windows: yt-dlp 下载视频和元数据
├── download.sh               # Linux/WSL: yt-dlp 下载视频和元数据
├── whisper.ps1               # Windows: WhisperX 生成词级 JSON
├── whisper.sh                # Linux/WSL: WhisperX 生成词级 JSON
├── translate_srt.py          # JSON 美化 + glossary + 翻译/分割/校对 + SRT/ASS 导出
├── ffmpeg-burn.ps1           # Windows: ffmpeg ASS 硬压
├── ffmpeg-burn.sh            # Linux/WSL: ffmpeg ASS 硬压
├── mpv-burn.ps1              # Windows: mpv 硬压备选
├── mpv-burn.sh               # Linux/WSL: mpv 硬压备选
├── batch.ps1                 # Windows: 多 URL 批处理
├── batch.py                  # Linux/WSL: 多 URL 批处理
├── setup.ps1                 # Windows: 安装依赖
├── setup.sh                  # Linux/WSL: 安装依赖
├── .env.ps1                  # PowerShell 读取 .env 的共享模块
├── template.ass              # ASS 模板；保留历史 Style: zh / bi-en / bi-zh
├── .env.example              # 环境变量模板
├── providers.example.json    # LLM provider 配置模板
├── translate_prompt.example.md
├── proofread_prompt.example.md
├── split_prompt.example.md
├── AGENTS.md
├── README.md
└── .agents/skills/
    ├── beautify/SKILL.md
    ├── download/SKILL.md
    ├── knowledge/SKILL.md
    ├── translate/SKILL.md
    └── whisper/SKILL.md
```

本地文件 `.env`、`providers.json`、`cookies.txt`、`split_prompt.md` 和生成产物均不应提交。

## Pipeline Flow

### Windows `pipeline.ps1`

1. `download.ps1` 下载视频、封面、`.info.json`、`.description`、`.tags.txt`
2. `whisper.ps1` 调用 WhisperX，只输出 `<base>.json`
3. `translate_srt.py --only-beautify` 美化 JSON 中的 word 时间轴，输出 `<base>.beautified.json`
4. `translate_srt.py --only-glossary` 在翻译前生成或复用 `glossary.md`
5. `translate_srt.py` 整句翻译、AI 分割、词级对轴、split event 校对，输出最终字幕
6. `ffmpeg-burn.ps1` 可选硬压双语 ASS 到 `burned.mkv`

### Linux/WSL `pipeline.sh`

流程与 Windows 对齐，使用 `download.sh`、`whisper.sh`、`translate_srt.py`、`ffmpeg-burn.sh`。两个 pipeline 都实时透传各步骤输出。

### Output Chain

```text
video -> json -> beautified.json -> glossary.md
      -> split.<source>.srt / split.<target>.srt
      -> <source>.proofread.ass / <target>.ass / <source>-<target>.ass
      -> burned.mkv
```

默认 `.env.example` 设置 `PIPELINE_SKIP_BURN=1`，推荐先人工校对字幕，再决定是否硬压。

## Step Behavior

### download

- 输出目录名和视频基名相同，视频路径形如 `<video_dir>/<video_dir>.<ext>`
- 同步保存 `.png` 封面、`.info.json` 元数据、`.description` 简介、`.tags.txt` 标签
- SponsorBlock 移除 `sponsor,selfpromo`
- `cookies.txt` 通过相对路径引用，必须在仓库根目录运行脚本
- Windows 文件夹名会做 Unicode 标点和非法字符清理，避免引号、破折号等导致跨 Windows/WSL 路径乱码

### whisper

- 已存在 `<base>.json` 时跳过
- 视频先转为 mono 16kHz WAV，再调用 WhisperX
- WhisperX 参数固定为 `--output_format json`
- `.info.json` 中的 `language` 会用于 WhisperX `--language`；缺省回退 `en`
- 输出 JSON 的 `segments[].words[]` 是后续分割对轴的唯一词源

### beautify

- 已合并到 `translate_srt.py`
- 输入 `.json`，输出 `.beautified.json`，不覆盖原始 JSON
- 对每个 word 做场景吸附和边界修复，再用首尾有效 word 回写 segment 起止时间
- 入点吸附到前一个场景切换，出点吸附到下一个场景切换前 `end_offset_frames`
- 只补足最短时长，不再用最大时长截断整句；长句交给 split 阶段

### glossary

- 已合并到 `translate_srt.py`
- 位于 beautify 之后、translate 之前
- 如果 `glossary.md` 已存在且非空，直接复用，不重新总结
- 读取 transcript、`.description`、`.tags.txt`、`.info.json`
- 配置 `TAVILY_API_KEY` 时联网搜索，未配置时离线总结
- 需要 `TRANSLATE_PROVIDER` 和对应 API key

### translate / split / proofread

- 输入 `.json` 或 `.beautified.json`
- `.beautified.json` 是主缓存，会保存 `translation`、`proofread_text`、`split_events`
- 顺序固定为：整句翻译 -> AI 分割 -> 词级对轴 -> split event 校对
- 翻译使用整句 segment，避免先分割导致上下文破碎
- 分割使用未校对源语言文本匹配 WhisperX words，校对发生在 split event 上
- 分割请求默认附带前后各 1 条 `context_before` / `context_after`，只供远端理解语义和节奏；远端必须只返回 pending item 本身
- `split_status` 明确记录分割缓存状态：`ok`=有效分割，`fallback`=AI 分割失败后整句回退且可重试，`unsplit`=低于阈值或合法保留整句；`split_reason` 是枚举原因码，`split_reason_detail` 是具体诊断文本
- 翻译、分割、校对的 user prompt 都是 JSON object，顶层包含 `items` array；glossary 和 description 的 user prompt 也是 JSON object；远端 LLM 必须只返回 JSON
- 翻译、分割、校对返回严格 JSON object，顶层 `items` array 使用 `id` 和源/目标 ISO 639 语言代码 key，例如 `id`, `en`, `zh`
- 语言代码 key 由 `${SOURCE_LANG_CODE}` / `${TARGET_LANG_CODE}` 注入；本地解析只匹配这些 ISO code，不匹配完整语言名称或 `source` / `target`
- 对轴时只用源语言 split 的首尾 token 匹配 `words[]`；匹配失败则整句回退到 beautified 时间轴，禁止本地强切
- token normalize 会忽略词内 dash/hyphen，例如 `non-existent` 与 `nonexistent` 可匹配；带空格的 dash 仍作为分隔
- `--no-split` 只跳过 AI 分割，仍会输出 SRT/ASS

输出命名：

```text
<base>.split.<source>.srt
<base>.split.<target>.srt
<base>.<source>.proofread.ass
<base>.<target>.ass
<base>.<source>-<target>.ass
<base>.<target>.description
```

`SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名。输出文件后缀通过 `langcodes` 规范为 ISO 639 代码；未显式设置 `SOURCE_LANG` 时使用 WhisperX JSON 的 `language`，`TARGET_LANG` 默认 `zh`。

Prompt 文件支持模板变量：

```text
${SOURCE_LANG}
${TARGET_LANG}
${SOURCE_LANG_CODE}
${TARGET_LANG_CODE}
```

`split_prompt.md` 仅允许微调分割风格，输出格式规则由 `translate_srt.py` 内置 `_SPLIT_FORMAT` 强制追加。

### burn

- pipeline 默认调用 ffmpeg ASS 滤镜硬压
- `-SkipBurn` / `SKIP_BURN=1` / `PIPELINE_SKIP_BURN=1` 会跳过硬压
- `BURN_RES` 指定输出分辨率时保持宽高比并补黑边
- `ExistingAss` / `EXISTING_ASS` 可指定已有双语 ASS 跳过翻译，直接用于硬压

## Config

`setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md`。旧版本升级时，setup 会把 `.env.example` 中新增但本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。PowerShell 入口通过 `.env.ps1` 读取，bash 入口自行读取。

| 变量 | 说明 |
|------|------|
| `WHISPER_MODEL` | WhisperX ASR 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | WhisperX 对齐模型，空则自动 |
| `WHISPER_DEVICE` | `cuda` / `cpu`；留空则跟随 `TORCH_BACKEND` 自动推导 |
| `SOURCE_LANG` | 源语言标签；空则使用 WhisperX JSON language |
| `TARGET_LANG` | 目标语言标签，默认 `zh` |
| `TRANSLATE_PROVIDER` | 翻译后端：`openrouter` / `deepseek` / `gemini` |
| `TRANSLATE_MODEL` | 翻译模型，空则用 provider 默认 |
| `PROOFREAD` | `1` / `0` 控制 split event 校对 |
| `PROOFREAD_PROVIDER` | 校对 provider，空则复用翻译 provider |
| `PROOFREAD_MODEL` | 校对模型，空则复用翻译模型 |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |
| `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | LLM API keys |
| `TAVILY_API_KEY` / `TAVILY_MAX_RESULTS` | glossary 联网搜索配置 |

`providers.json` 是 OpenAI SDK 兼容配置，`url` 是 SDK `base_url`，不包含 `/chat/completions`。

## Key Commands

### PowerShell

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
.\pipeline.ps1 "https://youtu.be/xxxxx" -SourceLang en -TargetLang ja -SkipBurn
.\pipeline.ps1 "https://youtu.be/xxxxx" -ExistingAss "path\to\video.en-zh.ass"
.\batch.ps1 "URL1" "URL2"
```

### Linux / WSL

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
TARGET_LANG=ja SKIP_BURN=1 ./pipeline.sh "URL"
./pipeline.sh "URL" -- --scene-threshold 0.12 --snap-frames 10
./.venv/bin/python batch.py "URL1" "URL2"
```

### Manual Steps

```powershell
.\download.ps1 "URL"
.\whisper.ps1 "video.webm"
.\.venv\Scripts\python.exe translate_srt.py video.json --video video.webm --only-beautify
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.webm --only-glossary --skip-beautify
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.webm --source-lang en --target-lang zh
.\ffmpeg-burn.ps1 "video.webm" -SubFile "video.en-zh.ass"
```

```bash
./download.sh "URL"
./whisper.sh "video.webm"
./.venv/bin/python translate_srt.py video.json --video video.webm --only-beautify
./.venv/bin/python translate_srt.py video.beautified.json --video video.webm --only-glossary --skip-beautify
./.venv/bin/python translate_srt.py video.beautified.json --video video.webm --source-lang en --target-lang zh
./ffmpeg-burn.sh "video.webm" --sub-file "video.en-zh.ass"
```

## Dependencies

| 工具 | 用途 |
|------|------|
| `yt-dlp` | YouTube 视频/元数据下载 |
| `uv` | 按 `pyproject.toml` 创建 `.venv`，并按 `.env` 安装 PyTorch 后端 |
| `whisperx` | ASR + word alignment JSON |
| `ffmpeg` / `ffprobe` | 音频提取、场景检测、硬压 |
| `python` | Windows/WSL 下由 setup 创建 `.venv` 运行 `translate_srt.py` |
| `openai` | LLM 调用 |
| `langcodes[data]` | 语言名/标签规范为 ISO 639 输出后缀 |
| `tavily-python` | glossary 可选联网搜索 SDK |
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## Working Notes

- 更新文档时以实际脚本参数和文件名为准，不保留历史 SRT 流程
- 保持 PowerShell 和 bash 入口行为对齐
- 不要提交 `.env`、`providers.json`、`cookies.txt`、`split_prompt.md` 或生成产物
- 不要回退用户本地数据或未请求的工作区改动
- `README.md` 面向用户快速使用；`AGENTS.md` 面向维护和自动化代理；`.agents/skills/*` 面向分步骤执行
