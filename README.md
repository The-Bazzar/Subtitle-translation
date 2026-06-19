# YouTube 字幕流水线

从 YouTube 链接出发，完成：

`下载视频 -> WhisperX JSON -> JSON 时间轴美化 -> glossary 术语知识库 -> 整句翻译 -> 分割对轴 -> split 校对 -> 双语 ASS -> burned.mkv`

> 必须使用 PowerShell 7。旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。升级命令：`winget install Microsoft.PowerShell`

## 项目结构

```text
├── pipeline.ps1
├── pipeline.sh
├── download.ps1
├── download.sh
├── whisper.ps1
├── whisper.sh
├── translate_srt.py
├── ffmpeg-burn.ps1
├── ffmpeg-burn.sh
├── mpv-burn.ps1
├── mpv-burn.sh
├── setup.ps1
├── setup.sh
├── .env.ps1
├── template.ass
├── .env.example
├── providers.example.json
├── translate_prompt.example.md
├── proofread_prompt.example.md
└── split_prompt.example.md
```

时间轴美化和 glossary 生成已集中到 `translate_srt.py`。主链路不再使用 SRT，WhisperX `.json` 是唯一字幕输入。`split_prompt.md` 可作为本地分割风格微调文件使用，但不提交；仓库只提交 `split_prompt.example.md`。

## 快速使用

### PowerShell

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx" -SkipBurn
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx" -SkipKnowledge
```

### Linux / WSL

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
SKIP_BURN=1 ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --scene-threshold 0.12 --snap-frames 10
```

## 主流程

1. `download.ps1/.sh` 下载视频、封面、`.info.json`、`.description`、`.tags.txt`
2. `whisper.ps1/.sh` 调用 `whisperx --output_format json`，输出 `<name>.json`
3. `translate_srt.py --only-beautify` 美化 JSON 里的 word 时间轴并回写 segment，输出 `<name>.beautified.json`
4. `translate_srt.py --only-glossary` 读取整句 transcript 和元数据，生成 `glossary.md`
5. `translate_srt.py` 使用整句 JSON 翻译
6. AI 分割后用每个源语言 split 的首尾 word 匹配美化后的 `words[]` 回填时间，再对 split events 做最终校对，输出 `.split.<source>.srt` / `.split.<target>.srt` 和最终 ASS；显式 `--no-split` 时也继续输出 ASS
7. `ffmpeg-burn.ps1/.sh` 使用双语 `.ass` 硬压字幕

成果物链：

```text
video -> json -> beautified.json -> glossary.md -> <source>.proofread.ass / <target>.ass / <source>-<target>.ass -> burned.mkv
```

## translate_srt.py

入口只接受 WhisperX JSON：

```bash
python3 translate_srt.py video.json --video video.webm
python3 translate_srt.py video.json --video video.webm --only-beautify
python3 translate_srt.py video.beautified.json --video video.webm --source-lang en --target-lang ja
python3 translate_srt.py video.beautified.json --video video.webm -o custom.en-ja.ass
```

输出：

- `<name>.beautified.json`：主缓存，保存 `translation`、`proofread_text`、`split_events`
- `<name>.split.<source>.srt`：分割后、最终校对后的源语言 SRT 检查稿
- `<name>.split.<target>.srt`：分割后、最终校对后的目标语言 SRT 检查稿
- `<name>.<source>.proofread.ass`：最终校对源语言 ASS
- `<name>.<target>.ass`：目标语言 ASS
- `<name>.<source>-<target>.ass`：双语 ASS
- `<name>.<target>.description`：目标语言简介

`SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名，例如 `en`、`en-US`、`Japanese`、`Chinese Simplified`。输出文件后缀会通过 `langcodes` 规范为 ISO 639 代码，例如 `English -> en`、`Japanese -> ja`。未显式设置 `SOURCE_LANG` 时，脚本使用 WhisperX JSON 中的 `language`；`TARGET_LANG` 默认 `zh`。

翻译、分割、校对按顺序执行：先用整句 JSON 翻译保留语义，再用未校对源语言文本分割并对齐词源时间轴，最后对已分割的 subtitle events 做双语校对。分割完成后，脚本用每个源语言 split 的首尾 word 顺序匹配美化后的 `words[]`，对齐每条显示字幕的起止时间。分割 AI 必须返回脚本内置协议要求的 JSON 数组；如果缺标号、源/目标段数不齐、源语言片段无法还原未校对整句或首尾 word 无法对齐词级时间轴，脚本会丢弃该分割结果并回退到整句 beautified 时间轴，不做本地强切。

`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 可以使用 `${SOURCE_LANG}`、`${TARGET_LANG}`、`${SOURCE_LANG_CODE}`、`${TARGET_LANG_CODE}` 模板变量；加载时由 `translate_srt.py` 替换。`split_prompt.md` 只用于微调分割风格，输出格式由 `translate_srt.py` 固定注入。

## 配置

复制 `.env.example` 为 `.env`，至少配置：

```ini
TRANSLATE_PROVIDER=deepseek
DEEPSEEK_API_KEY=
```

常用变量：

| 变量 | 说明 |
|---|---|
| `WHISPER_MODEL` | WhisperX 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | 对齐模型，空则自动选择 |
| `WHISPER_DEVICE` | `cuda` / `cpu` |
| `SOURCE_LANG` | 源语言标签；空则使用 WhisperX JSON language |
| `TARGET_LANG` | 目标语言标签，默认 `zh` |
| `TRANSLATE_PROVIDER` | 翻译后端，必填 |
| `TRANSLATE_MODEL` | 翻译模型，空则用 provider 默认 |
| `PROOFREAD` | `1/0` 控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider |
| `PROOFREAD_MODEL` | 校对专用模型 |
| `TAVILY_API_KEY` | glossary 联网搜索 |
| `TAVILY_MAX_RESULTS` | Tavily 搜索结果上限 |
| `PIPELINE_SKIP_*` | 流水线阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |

`providers.json` 使用 OpenAI SDK 兼容配置，仓库只提交 `providers.example.json`。

## 依赖

| 工具 | 用途 |
|---|---|
| `yt-dlp` | YouTube 视频/元数据下载 |
| `uv` | 安装 WhisperX |
| `whisperx` | 语音识别 + 词级对齐 JSON |
| `ffmpeg` / `ffprobe` | 音频提取、场景检测、字幕硬压 |
| `python3` | 运行 `translate_srt.py` |
| `openai` Python 包 | LLM 调用 |
| `langcodes[data]` Python 包 | 语言名/标签规范为 ISO 639 输出后缀 |
| `TAVILY_API_KEY` | glossary 可选联网搜索；脚本直接调用 Tavily HTTP API |

## 注意事项

- `.env`、`providers.json`、`cookies.txt`、`split_prompt.md` 已 gitignored
- `cookies.txt` 通过相对路径引用，请在仓库根目录运行脚本
- `TRANSLATE_PROVIDER` 必须配置，否则翻译和 glossary 会报错
- WhisperX 首次运行会下载模型
- 默认不硬压，推荐先人工校对 ASS，再决定是否压制
- `.srt` 已退出主流程；不要再把 SRT 当作翻译输入
