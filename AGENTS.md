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
├── tavily_domains.example.json # Tavily 域名优先配置模板
├── glossary_prompt.example.md
├── translate_prompt.example.md
├── proofread_prompt.example.md
├── split_prompt.example.md
├── AGENTS.md
├── README.md
└── .agents/skills/
    ├── beautify/SKILL.md
    ├── download/SKILL.md
    ├── knowledge/SKILL.md
    ├── release/SKILL.md
    ├── translate/SKILL.md
    └── whisper/SKILL.md
```

本地文件 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 和生成产物均不应提交。

## Pipeline Flow

### Windows `pipeline.ps1`

1. `download.ps1` 下载视频、封面、`.info.json`、`.description`、`.tags.txt`
2. `whisper.ps1` 调用 WhisperX，只输出 `<base>.json`
3. `translate_srt.py --only-beautify` 美化 JSON 中的 word 时间轴，输出 `<base>.beautified.json`
4. `translate_srt.py --only-glossary` 在翻译前强制重新生成并覆盖 `glossary.md`
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
- 同步导出 `<base>.scenes.json` 和 `<base>.scenechange.txt`；txt 每行一个秒级场景切换点
- 对每个 word 做场景吸附和边界修复，再用首尾有效 word 回写 segment 起止时间
- 入点吸附到前一个场景切换，出点吸附到下一个场景切换前 `end_offset_frames`
- 只补足最短时长，不再用最大时长截断整句；长句交给 split 阶段

### glossary

- 已合并到 `translate_srt.py`
- 位于 beautify 之后、translate 之前
- 普通 pipeline 中如果 `glossary.md` 已存在且非空，直接复用，不重新总结
- 手动运行 `--only-glossary` 时忽略已有缓存，重新生成并覆盖 `glossary.md`
- 读取 transcript、`.description`、`.tags.txt`、`.info.json`
- 本地脚本会把 YouTube 原视频元信息前置写入 `glossary.md`，包括标题、作者、上传时间、原简介和标签；这部分不交给远端 LLM 合成
- 配置 `TAVILY_API_KEY` 时联网搜索，未配置时离线总结
- 联网搜索结果是 glossary 的优先证据来源；远端 LLM 应用搜索结果校正 transcript 中可能的 ASR 人名、标题、引文和术语错误
- Tavily 搜索默认由 glossary agent 在同一个 ChatSession 中通过 `tavily_search` tool calls 发起；脚本执行搜索后将 tool result 回喂同一 session
- 第一轮 glossary user JSON 会包含 metadata、transcript/retrieved context 和合并后的 `tavily_domains.json` 域名偏好
- Tavily tool 本地先按 `tavily_domains.json` 的全局百科域名和题材站点执行 `include_domains` 搜索；结果不足时再执行普通搜索；合并时优先百科/知识库域名
- 使用 `GLOSSARY_PROVIDER` / `GLOSSARY_MODEL` 指定术语知识库专用 LLM；空则回退到 `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL`
- 术语知识库阶段必须使用用户可用范围内最顶级模型，因为它负责搜索意图、网页证据判断、ASR 纠错、背景归纳和定译决策
- `glossary_prompt.md` 仅允许微调 glossary 内容策略，输出格式规则由 `translate_srt.py` 内置 `_GLOSSARY_FORMAT` 强制追加

### translate / split / proofread

- 输入 `.json` 或 `.beautified.json`
- `.beautified.json` 是主缓存，会保存 `translation`、`proofread_text`、`split_events`
- 顺序固定为：整句翻译 -> AI 分割 -> 词级对轴 -> split event 校对
- 翻译使用整句 segment，避免先分割导致上下文破碎
- 分割使用未校对源语言文本匹配 WhisperX words，校对发生在 split event 上
- 分割请求默认附带前后各 1 条 `context_before` / `context_after`，只供远端理解语义和节奏；远端必须只返回 pending item 本身
- `split_status` 明确记录分割缓存状态：`ok`=有效分割，`fallback`=AI 分割失败后整句回退且可重试，`unsplit`=低于阈值或合法保留整句；`split_reason` 是枚举原因码，`split_reason_detail` 是具体诊断文本
- 默认 ASS 模板按 1080p 双语观看调校：`bi-zh` / `bg-bi-zh` 字号 68，`bi-en` / `bg-bi-en` 字号 44；默认 AI 分割阈值是源文超过 72 字符或 3.8 秒
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

`setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`tavily_domains.json`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md`。旧版本升级时，setup 会把 `.env.example` 中新增但本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。PowerShell 入口通过 `.env.ps1` 读取，bash 入口自行读取。

| 变量 | 说明 |
|------|------|
| `WHISPER_MODEL` | WhisperX ASR 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | WhisperX 对齐模型，空则自动 |
| `WHISPER_DEVICE` | `cuda` / `cpu`；留空则跟随 `TORCH_BACKEND` 自动推导 |
| `HF_TOKEN` | Hugging Face token；用于提高 WhisperX/对齐模型下载速率限制，可留空 |
| `SOURCE_LANG` | 源语言标签；空则使用 WhisperX JSON language |
| `TARGET_LANG` | 目标语言标签，默认 `zh` |
| `TRANSLATE_PROVIDER` | 翻译后端：`openai` / `llama` / `openrouter` / `deepseek` / `gemini` |
| `TRANSLATE_MODEL` | 翻译模型，空则用 provider 默认 |
| `GLOSSARY_PROVIDER` | glossary 专用 provider；强烈建议配置为可用范围内最顶级模型对应 provider，空则复用翻译 provider |
| `GLOSSARY_MODEL` | glossary 专用模型；负责搜索意图、ASR 纠错、背景归纳和术语定译，空则复用翻译模型或 provider 默认 |
| `EMBEDDING_ENABLED` | `1` / `0` 控制是否用 LangChain + Chroma 构建 embedding 索引并注入 glossary/translate/proofread 上下文 |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | OpenAI SDK 兼容 embedding provider 和模型，可指向本地 llama.cpp / Ollama / OpenAI-compatible 服务 |
| `EMBEDDING_STORE` / `EMBEDDING_CHROMA_DIR` | 当前仅支持 `chroma`；目录空则使用项目目录下 `chroma_db` |
| `EMBEDDING_TOP_K` / `EMBEDDING_CHUNK_CHARS` / `EMBEDDING_BATCH_SIZE` | embedding 检索、切块和批量调用参数 |
| `PROOFREAD` | `1` / `0` 控制 split event 校对 |
| `PROOFREAD_PROVIDER` | 校对 provider，空则复用翻译 provider |
| `PROOFREAD_MODEL` | 校对模型，空则复用翻译模型 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |
| `OPENAI_API_KEY` / `OLLAMA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | LLM / embedding API keys |
| `TAVILY_API_KEY` / `TAVILY_MAX_RESULTS` / `TAVILY_MAX_QUERIES` | glossary 联网搜索配置；`TAVILY_MAX_QUERIES` 在 tool-call 路径下是最大 Tavily tool 查询次数，在 fallback 路径下是单一语言 query 上限，`0` 禁用 Tavily |

`BURN_OVCOPTS=source-bitrate` 是默认硬压策略：burn 脚本用 `ffprobe` 读取源视频码率，生成 VBR 的 `b/maxrate/bufsize` 参数，让输出尽量接近源码率；显式 `qp=20`、`crf=23` 等会覆盖自动模式。`BURN_OAC` 默认 `aac`，兼容 ffmpeg 和 mpv 的硬字幕压制。

配置 `TAVILY_API_KEY` 时，glossary 阶段默认使用同一个 LLM session 执行 tool calling：脚本第一轮把 metadata、transcript/retrieved context 和 `tavily_domains.json` 域名偏好一起交给 glossary 模型；模型按需请求 `tavily_search`，脚本执行 Tavily 后把结果作为 tool message 喂回同一 session，最后由模型返回 glossary JSON。tool-call 路径下，`TAVILY_MAX_QUERIES` 控制最多执行多少次 Tavily 查询；fallback query-agent 路径下，它仍表示每种语言最多生成多少条 query。Tavily tool 会结合 metadata、模型给出的 `topic_hints` 和 `tavily_domains.json` 做域名优先搜索，并在最终合并时给百科/知识库域名加权。该阶段使用 `GLOSSARY_PROVIDER` / `GLOSSARY_MODEL`，不要为了省成本使用弱模型。

glossary 阶段会强制移除 provider `request_kwargs.response_format` 中的 JSON mode 参数，以免干扰 tool calling；输出格式仍由内置 prompt 要求返回 JSON object。

启用 `EMBEDDING_ENABLED=1` 时，Chroma 索引同时包含 `glossary:*` 项目知识 chunk、`transcript:*` 源文 chunk 和翻译/分割后生成的双语 `translation_memory:*` chunk；proofread 阶段用源文+译文 query 检索，优先获得历史译法和术语一致性参考。`glossary:*` 包含本地组合的视频元信息和 glossary 内容，并按 Markdown 标题切分；`transcript:*` 使用干净字幕文本建向量，retrieved context 返回带时间码的字幕行，并按字符数、时间跨度、segment 数量切块，按末尾时间窗口自动 overlap；每次重建索引前会清理当前项目旧 chunk，避免残留向量污染检索。

`providers.json` 是 OpenAI SDK 兼容配置，`url` 是 SDK `base_url`，不包含 `/chat/completions`。`request_kwargs` 会原样合并进 `chat.completions.create(**kwargs)`，用于 DeepSeek JSON mode、Gemini Google Search 等 provider 专用参数；Gemini 内置联网需要 Gemini 3 或更新模型。

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
| `openai` | LLM 与 embedding 调用 |
| `langchain` / `langchain-openai` / `langchain-chroma` | RAG 检索链路和 OpenAI-compatible embedding 接入 |
| `chromadb` | 本地持久化向量库 |
| `langcodes[data]` | 语言名/标签规范为 ISO 639 输出后缀 |
| `tavily-python` | glossary 可选联网搜索 SDK |
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## Working Notes

- 更新文档时以实际脚本参数和文件名为准，不保留历史 SRT 流程
- 保持 PowerShell 和 bash 入口行为对齐
- 不要提交 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 或生成产物
- 不要回退用户本地数据或未请求的工作区改动
- `README.md` 面向用户快速使用；`AGENTS.md` 面向维护和自动化代理；`.agents/skills/*` 面向分步骤执行
