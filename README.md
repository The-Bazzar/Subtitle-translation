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
├── glossary_prompt.example.md
├── translate_prompt.example.md
├── proofread_prompt.example.md
└── split_prompt.example.md
```

时间轴美化和 glossary 生成已集中到 `translate_srt.py`。主链路不再使用 SRT，WhisperX `.json` 是唯一字幕输入。`glossary_prompt.md` / `split_prompt.md` 可作为本地风格微调文件使用，但不提交；仓库只提交对应 example。

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
3. `translate_srt.py --only-beautify` 美化 JSON 里的 word 时间轴并回写 segment，输出 `<name>.beautified.json`、`<name>.scenes.json`、`<name>.scenechange.txt`
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

```powershell
.\.venv\Scripts\python.exe translate_srt.py video.json --video video.webm
.\.venv\Scripts\python.exe translate_srt.py video.json --video video.webm --only-beautify
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.webm --source-lang en --target-lang ja
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.webm -o custom.en-ja.ass
```

```bash
./.venv/bin/python translate_srt.py video.json --video video.webm
./.venv/bin/python translate_srt.py video.json --video video.webm --only-beautify
./.venv/bin/python translate_srt.py video.beautified.json --video video.webm --source-lang en --target-lang ja
./.venv/bin/python translate_srt.py video.beautified.json --video video.webm -o custom.en-ja.ass
```

输出：

- `<name>.beautified.json`：主缓存，保存 `translation`、`proofread_text`、`split_events`
- `<name>.scenes.json`：场景切换 sidecar，包含 fps、threshold、frame、timecode 等调试信息
- `<name>.scenechange.txt`：每行一个秒级场景切换点，例如 `12.345000`
- `<name>.split.<source>.srt`：分割后、最终校对后的源语言 SRT 检查稿
- `<name>.split.<target>.srt`：分割后、最终校对后的目标语言 SRT 检查稿
- `<name>.<source>.proofread.ass`：最终校对源语言 ASS
- `<name>.<target>.ass`：目标语言 ASS
- `<name>.<source>-<target>.ass`：双语 ASS
- `<name>.<target>.description`：目标语言简介

`SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名，例如 `en`、`en-US`、`Japanese`、`Chinese Simplified`。输出文件后缀会通过 `langcodes` 规范为 ISO 639 代码，例如 `English -> en`、`Japanese -> ja`。未显式设置 `SOURCE_LANG` 时，脚本使用 WhisperX JSON 中的 `language`；`TARGET_LANG` 默认 `zh`。

翻译、分割、校对按顺序执行：先用整句 JSON 翻译保留语义，再用未校对源语言文本分割并对齐词源时间轴，最后对已分割的 subtitle events 做双语校对。所有批量 LLM 阶段的 user prompt 都是 JSON object，顶层包含 `items` array，返回也必须是同形态 JSON object；`items` 内只使用 `id` 和源/目标 ISO 639 语言代码 key，例如 `en`、`zh`。分割阶段默认给 pending segment 附带前后各 1 条 `context_before` / `context_after`，仅用于理解语义和节奏，远端只返回 pending item 本身；可用 `--split-context-window` 调整。分割完成后，脚本用每个源语言 split 的首尾 word 顺序匹配美化后的 `words[]`，对齐每条显示字幕的起止时间。如果缺标号、源/目标段数不齐、源语言片段无法还原未校对整句或首尾 word 无法对齐词级时间轴，脚本会丢弃该分割结果并回退到整句 beautified 时间轴，不做本地强切。`.beautified.json` 会用 `split_status` 记录状态：`ok` 为有效分割，`fallback` 为分割失败后整句回退且可重试，`unsplit` 为低于阈值或合法保留整句；`split_reason` 保存原因码，`split_reason_detail` 保存具体诊断文本。

默认模板以 1080p 双语观看为基准：`bi-zh` / `bg-bi-zh` 字号为 68，`bi-en` / `bg-bi-en` 字号为 44；AI 分割默认在源文超过 72 字符或 3.8 秒时触发。beautify 只负责词级时间轴吸附和边界修复，不再提供本地硬截整句参数。

`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 可以使用 `${SOURCE_LANG}`、`${TARGET_LANG}`、`${SOURCE_LANG_CODE}`、`${TARGET_LANG_CODE}` 模板变量；加载时由 `translate_srt.py` 替换。`glossary_prompt.md` 只用于微调 glossary 内容策略，`split_prompt.md` 只用于微调分割风格，输出格式由 `translate_srt.py` 固定注入。

## 配置

运行 `setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md`。旧版本升级时，setup 会把 `.env.example` 中新增但你本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。

setup 后至少配置：

```ini
TRANSLATE_PROVIDER=deepseek
DEEPSEEK_API_KEY=
```

常用变量：

| 变量 | 说明 |
|---|---|
| `WHISPER_MODEL` | WhisperX 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | 对齐模型，空则自动选择 |
| `WHISPER_DEVICE` | `cuda` / `cpu`；留空则跟随 `TORCH_BACKEND` 自动推导 |
| `HF_TOKEN` | Hugging Face token；用于提高 WhisperX/对齐模型下载速率限制，可留空 |
| `SOURCE_LANG` | 源语言标签；空则使用 WhisperX JSON language |
| `TARGET_LANG` | 目标语言标签，默认 `zh` |
| `TRANSLATE_PROVIDER` | 翻译后端，必填；可用 `openai` / `llama` / `openrouter` / `deepseek` / `gemini` |
| `TRANSLATE_MODEL` | 翻译模型，空则用 provider 默认 |
| `EMBEDDING_ENABLED` | `1/0` 控制是否用 LangChain + Chroma 构建 embedding 索引，并注入 glossary/translate/proofread 上下文 |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | OpenAI SDK 兼容 embedding 后端和模型，可指向本地 llama.cpp / Ollama / OpenAI-compatible 服务 |
| `EMBEDDING_STORE` / `EMBEDDING_CHROMA_DIR` | 当前支持 `chroma`；目录空则使用项目目录下 `chroma_db` |
| `EMBEDDING_TOP_K` / `EMBEDDING_CHUNK_CHARS` / `EMBEDDING_BATCH_SIZE` | embedding 检索、切块和批量调用参数 |
| `PROOFREAD` | `1/0` 控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider |
| `PROOFREAD_MODEL` | 校对专用模型 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `TAVILY_API_KEY` | glossary 联网搜索 |
| `TAVILY_MAX_RESULTS` | Tavily 搜索结果上限 |
| `TAVILY_MAX_QUERIES` | Tavily 搜索 query 总数上限；标题、作者和有效标签优先参与检索 |
| `PIPELINE_SKIP_*` | 流水线阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |

启用 `EMBEDDING_ENABLED=1` 时，Chroma 索引会同时保存 `glossary.md` 项目知识、源文 transcript chunk 和翻译/分割后生成的双语 translation memory chunk；校对阶段会用源文+译文一起检索，以保持术语和译风一致。`glossary.md` 会由本地脚本直接前置 YouTube 原视频元信息，包括标题、作者、上传时间、简介和标签。索引会自动按 Markdown 标题切分 glossary，给 transcript chunk 保留 1 条 segment overlap，并在重建前清理当前项目旧 chunk，避免残留结果污染检索。

`providers.json` 使用 OpenAI SDK 兼容配置，仓库只提交 `providers.example.json`。

## 依赖

| 工具 | 用途 |
|---|---|
| `yt-dlp` | YouTube 视频/元数据下载 |
| `uv` | 按 `pyproject.toml` 创建 `.venv`，并按 `.env` 安装 PyTorch 后端 |
| `whisperx` | 语音识别 + 词级对齐 JSON |
| `ffmpeg` / `ffprobe` | 音频提取、场景检测、字幕硬压 |
| `python` | Windows/WSL 下由 setup 创建 `.venv` 运行 `translate_srt.py` |
| `openai` Python 包 | LLM 与 embedding 调用 |
| `langchain` / `langchain-openai` / `langchain-chroma` | RAG 检索链路和 OpenAI-compatible embedding 接入 |
| `chromadb` | 本地持久化向量库 |
| `langcodes[data]` Python 包 | 语言名/标签规范为 ISO 639 输出后缀 |
| `tavily-python` | glossary 可选联网搜索 SDK |
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## 注意事项

- `.env`、`providers.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 已 gitignored
- 不要把 Python 包安装到系统环境；Windows 运行 `.\setup.ps1`，Linux/WSL 运行 `./setup.sh`，它们会创建/更新仓库 `.venv`
- 运行 pipeline 或任一 `.py` 相关脚本前必须先完成 setup；脚本统一使用项目 `.venv`，不调用全局 `python` / `python3`
- `TORCH_BACKEND=auto` 会用 `nvidia-smi` 检测 NVIDIA GPU；NVIDIA 用户可设 `cuda128`，AMD/无独显用户设 `cpu`
- `cookies.txt` 通过相对路径引用，请在仓库根目录运行脚本
- `TRANSLATE_PROVIDER` 必须配置，否则翻译和 glossary 会报错
- WhisperX 首次运行会下载模型
- 默认不硬压，推荐先人工校对 ASS，再决定是否压制
- `.srt` 已退出主流程；不要再把 SRT 当作翻译输入
