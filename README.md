# YouTube 字幕流水线

从 YouTube 链接出发，完成：

`下载视频 → WhisperX 英文字幕 → 时间码美化 → glossary 术语知识库 → LLM 翻译/校对 → 双语 ASS → ffmpeg 硬字幕 burned.mkv`

项目同时提供：

- Windows / PowerShell 超级流水线：[`pipeline.ps1`](pipeline.ps1)
- Linux / WSL bash 流水线：[`pipeline.sh`](pipeline.sh)
- 分步脚本：下载、WhisperX、美化、翻译、知识库、硬压


> ⚠️ **必须使用 PowerShell 7** — 旧版 5.x 不支持 `-Encoding UTF8`（无 BOM）等现代语法，所有 `.ps1` 脚本均会报错。升级：`winget install Microsoft.PowerShell`

## 项目结构

```text
├── pipeline.ps1
├── pipeline.sh
├── download.ps1
├── download.sh
├── whisper.ps1
├── whisper.sh
├── beautify_srt.py
├── glossary_builder.py
├── translate_srt.py
├── ffmpeg-burn.ps1
├── ffmpeg-burn.sh
├── mpv-burn.ps1
├── mpv-burn.sh
├── template.ass
├── .env.example
├── providers.example.json
├── translate_prompt.example.md
├── proofread_prompt.example.md
```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

只生成字幕，不压制：

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx" -SkipBurn
```

跳过知识库步骤：

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx" -SkipKnowledge
```

### Linux / WSL

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
```

跳过硬压：

```bash
SKIP_BURN=1 ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
```

给 beautify 透传参数：

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --scene-threshold 0.12 --snap-frames 10
```

## 环境要求

### 通用依赖

| 工具 | 用途 |
|---|---|
| `yt-dlp` | 下载视频、标签、简介、元数据 |
| `ffmpeg` / `ffprobe` | 音频提取、场景检测、硬字幕压制 |
| `python3` | `beautify_srt.py` / `glossary_builder.py` / `translate_srt.py` |
| `openai` Python 包 | LLM 调用 |
| `whisperx` | 英文字幕和词级时间码 |

### WhisperX 运行方式

#### CPU

Windows / PowerShell：

```powershell
whisperx audio.mp3 --device cpu
```

Linux / WSL：

```bash
whisperx audio.mp3 --device cpu
```

#### CUDA 12.8

WhisperX 这套配置要求你本地 CUDA 运行时和 `torch` 版本对齐。

安装：

Windows / PowerShell：

```powershell
uv tool install git+https://github.com/m-bain/whisperx.git `
  --with "torch==2.8.0+cu128" `
  --with "torchaudio==2.8.0+cu128"
```

Linux / WSL：

```bash
uv tool install git+https://github.com/m-bain/whisperx.git \
  --with "torch==2.8.0+cu128" \
  --with "torchaudio==2.8.0+cu128"
```

运行：

Windows / PowerShell：

```powershell
& { $env:TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD="1"; whisperx audio.mp3 --device cuda }
```

Linux / WSL：

```bash
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 whisperx audio.mp3 --device cuda
```

检测 CUDA 是否可用：

```powershell
uv run --with torch python -c "import torch; print(torch.cuda.is_available())"
```

```bash
uv run --with torch python -c "import torch; print(torch.cuda.is_available())"
```

### WhisperX 对齐模型

通过 `WHISPER_ALIGN_MODEL` 控制：

| 值 | 说明 |
|---|---|
| 留空 | WhisperX 按语言自动选择 |
| `facebook/wav2vec2-large-960h-lv60-self` | 英文顶级对齐模型 |
| `facebook/wav2vec2-base-960h` | 英文次选 |
| `facebook/wav2vec2-large-xlsr-53` | 多语言通用 |
| `facebook/mms-1b-fl102` | 更通用的自动对齐模型 |

## 配置文件

### `.env`

复制 [` .env.example`](.env.example) 为 `.env`。

当前脚本会读取这些关键变量：

```ini
MPV_PATH_WIN=
MPV_PATH_LINUX=
FFMPEG_PATH_WIN=
FFMPEG_PATH_LINUX=
YTDLP_PATH_WIN=
YTDLP_PATH_LINUX=

WHISPER_MODEL=large-v3-turbo
WHISPER_ALIGN_MODEL=facebook/mms-1b-fl102
WHISPER_DEVICE=cuda

TRANSLATE_PROVIDER=deepseek
TRANSLATE_MODEL=deepseek-v4-pro
PROOFREAD=1
PROOFREAD_PROVIDER=
PROOFREAD_MODEL=

PIPELINE_SKIP_DOWNLOAD=0
PIPELINE_SKIP_WHISPER=0
PIPELINE_SKIP_BEAUTIFY=0
PIPELINE_SKIP_KNOWLEDGE=0
PIPELINE_SKIP_TRANSLATE=0
PIPELINE_SKIP_BURN=0

BURN_OVC=hevc_nvenc
BURN_OVCOPTS=qp=20
BURN_OAC=aac
BURN_RES=

OPENROUTER_API_KEY=
DEEPSEEK_API_KEY=
GEMINI_API_KEY=
TAVILY_API_KEY=
TAVILY_MAX_RESULTS=10
```

说明：

| 变量 | 说明 |
|---|---|
| `TRANSLATE_PROVIDER` | 必填。不再默认回退到 `openrouter` |
| `TRANSLATE_MODEL` | 翻译模型，留空走 provider 默认值 |
| `PROOFREAD` | `1/0`，控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider，留空则复用翻译 provider |
| `PROOFREAD_MODEL` | 校对专用模型，留空则复用翻译模型 |
| `TAVILY_API_KEY` | glossary 联网搜索所需 |
| `TAVILY_MAX_RESULTS` | Tavily 搜索结果上限，默认 10 |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `WHISPER_DEVICE` | `cuda` 或 `cpu` |

### `providers.json`

复制 [`providers.example.json`](providers.example.json) 为 `providers.json`。

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

注意：这里的 `url` 需要是 OpenAI SDK 的 `base_url`，不要再写到 `/chat/completions` 这一层。

## 各脚本说明

### `download.ps1` / `download.sh`

下载：

- 视频本体
- `.png` 封面
- `.info.json`
- `.description`
- `.tags.txt`

视频文件名固定为：

`<文件夹名>/<文件夹名>.<ext>`

这样后续脚本能可靠推导成果物路径。

### `whisper.ps1` / `whisper.sh`

当前逻辑：

1. 如果 `.srt` 已存在则跳过
2. 从视频提取 `16kHz mono wav`
3. 调 `whisperx --output_format all`
4. 输出 `.srt + .json + .txt/.tsv/.vtt`
5. 删除临时 `.wav`

`.json` 里的词级时间码会被 `translate_srt.py` 用于长句自然分句后的重新对轴。

### `beautify_srt.py`

当前默认行为：

- 不覆盖原始 `.srt`
- 输出 `<name>.beautified.srt`

主要规则：

- `scene_threshold=0.15`
- `snap_frames=7`
- `end_offset_frames=2`
- `min_scene_interval_frames=2`
- `min_duration=1.0`
- `max_duration=8.0`
- `min_gap=0.083`
- `max_gap_merge=0.5`

流程：

1. `ffprobe` 取 fps
2. `ffmpeg` / `ffprobe` 找场景切换
3. 入点吸附到场景变化点
4. 出点吸附到下一场景前 2 帧
5. 修复重叠和小间隙
6. 限制最短/最长时长

### `glossary_builder.py`

这一步现在已经集成到两条流水线里，位置固定在：

`beautify 之后 → translate 之前`

输入会优先选：

1. `.beautified.srt`
2. 不存在时回退 `.srt`

它会读取：

- 字幕
- `.description`
- `.tags.txt`
- `.info.json`

如果配置了 `TAVILY_API_KEY`，会额外联网搜索，再调用翻译模型生成 `glossary.md`。

如果没有 `TAVILY_API_KEY`，则离线生成 glossary。

### `translate_srt.py`

当前输出：

- `.split.srt`
- `.zh.srt`
- `.proofread.srt`
- `.zh.ass`
- `.zh-en.ass`
- `.zh.description`

流程：

1. 读取原始或美化后的英文 SRT
2. 如果存在 `.split.srt`，跳过分句
3. 否则用 LLM 做长句拆分，并尽量用 WhisperX `.json` 精确对轴
4. 如果存在 `.zh.srt`，跳过初译
5. 否则执行翻译
6. 默认执行双语校对，输出校对后的英文 `.proofread.srt`
7. 自动检测 `glossary.md` 并注入翻译与校对提示词
8. 输出 `.zh.ass` 和 `.zh-en.ass`
9. 如果存在 `.description`，顺带翻译为 `.zh.description`

双语 ASS 使用：

- 英文：`bi-en`
- 中文：`bi-zh`

仅中文 ASS 使用：

- `zh`

### `ffmpeg-burn.ps1` / `ffmpeg-burn.sh`

流水线默认烧录脚本。

特点：

- 用 `ass` 滤镜硬压双语字幕
- 默认保留原视频封面图
- 可指定输出分辨率
- 指定分辨率时保持宽高比并自动补黑边，不拉伸

## 阶段跳过和缓存规则

### PowerShell

[`pipeline.ps1`](pipeline.ps1) 支持：

- `-SkipDownload`
- `-SkipWhisper`
- `-SkipBeautify`
- `-SkipKnowledge`
- `-SkipTranslate`
- `-SkipBurn`

同时也会读取 `.env` 中的：

- `PIPELINE_SKIP_DOWNLOAD`
- `PIPELINE_SKIP_WHISPER`
- `PIPELINE_SKIP_BEAUTIFY`
- `PIPELINE_SKIP_KNOWLEDGE`
- `PIPELINE_SKIP_TRANSLATE`
- `PIPELINE_SKIP_BURN`

### Bash

[`pipeline.sh`](pipeline.sh) 支持同名环境变量：

- `SKIP_DOWNLOAD`
- `SKIP_WHISPER`
- `SKIP_BEAUTIFY`
- `SKIP_KNOWLEDGE`
- `SKIP_TRANSLATE`
- `SKIP_BURN`

并从 `.env` 继承：

- `PIPELINE_SKIP_DOWNLOAD`
- `PIPELINE_SKIP_WHISPER`
- `PIPELINE_SKIP_BEAUTIFY`
- `PIPELINE_SKIP_KNOWLEDGE`
- `PIPELINE_SKIP_TRANSLATE`
- `PIPELINE_SKIP_BURN`

自动跳过规则：

- `.srt` 存在：跳过 Whisper
- `.beautified.srt` 存在：跳过美化
- `glossary.md` 存在：跳过术语知识库
- `.zh-en.ass` 存在：跳过翻译

## 术语知识库

项目内的 `knowledge` skill 和 [`glossary_builder.py`](glossary_builder.py) 是两种不同角色：

- `glossary_builder.py`：项目内可直接运行的自动生成脚本
- `knowledge` skill：给外部 AI agent 用的工作说明，适合你手动让更强模型建立知识库

推荐顺序：

`beautify → knowledge/glossary → translate`

原因是这样 glossary 能从翻译初稿开始就参与约束，后续校对只需要在此基础上做收口。

## 批处理

### Windows

```powershell
.\batch.ps1 "URL1" "URL2" "URL3"
```

### Linux / WSL

```bash
python3 batch.py "URL1" "URL2" "URL3"
```

## Skills

当前项目技能文件在：

- [` .claude/skills/download/SKILL.md`](.code/.claude/skills/download/SKILL.md)
- [` .claude/skills/whisper/SKILL.md`](.code/.claude/skills/whisper/SKILL.md)
- [` .claude/skills/beautify/SKILL.md`](.code/.claude/skills/beautify/SKILL.md)
- [` .claude/skills/knowledge/SKILL.md`](.code/.claude/skills/knowledge/SKILL.md)
- [` .claude/skills/translate/SKILL.md`](.code/.claude/skills/translate/SKILL.md)

格式已经统一为：

`<skill-dir>/SKILL.md`

## 注意事项

- `TRANSLATE_PROVIDER` 现在必须配置，否则翻译和 glossary 都会直接报错
- `.env` 和 `providers.json` 建议保持本地私有版本，仓库里只提交 example 模板
- `cookies.txt` 已 gitignored
- `whisperx` 首次运行会下载模型
- 长视频使用“先提 WAV 再 WhisperX”是为了减轻后续时间漂移
- 如果你自己用更强的外部 agent 来写 `glossary.md`，通常会比自动生成脚本更稳
