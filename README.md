# YouTube 字幕流水线

从 YouTube 链接出发，完成：

`下载原片 + 重编码编辑版 -> WhisperX JSON -> JSON 时间轴美化 -> glossary 术语知识库 -> 整句翻译 -> 分割对轴 -> split 校对 -> 双语 ASS -> burned.mkv`

> 必须使用 PowerShell 7。旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。升级命令：`winget install Microsoft.PowerShell`

## 项目结构

```text
├── pipeline.ps1
├── pipeline.sh
├── download.ps1
├── download.sh
├── whisper.ps1
├── whisper.sh
├── merge_ass.ps1
├── merge_ass.sh
├── translate_srt.ps1
├── translate_srt.sh
├── translate_srt.py
├── ffmpeg-burn.ps1
├── ffmpeg-burn.sh
├── mpv-burn.ps1
├── mpv-burn.sh
├── setup.ps1
├── setup.sh
├── .env.ps1
├── template.ass.example
├── .env.example
├── providers.example.json
├── web_search.example.json
├── tavily_domains.example.json
├── glossary_prompt.example.md
├── translate_prompt.example.md
├── proofread_prompt.example.md
└── split_prompt.example.md
```

时间轴美化和 glossary 生成已集中到 `translate_srt.py`。主链路不再使用 SRT，WhisperX `.json` 是唯一字幕输入。`glossary_prompt.md` / `split_prompt.md` 可作为本地风格微调文件使用，`tavily_domains.json` 可维护题材相关站点，`web_search.json` 可维护联网 provider、统一结果数和阶段预算；这些本地文件不提交，仓库只提交对应 example。

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

### 完成通知

`pipeline.ps1` / `pipeline.sh` 独立运行时，任务成功响成功铃，错误退出响错误铃。批处理中的 child pipeline 保持静默，由 `batch.ps1` / `batch.py` 在每个失败结果返回时各响一次错误铃，最后再按整体结果响一次；任一任务失败时批处理退出码为 `1`。帮助和 dry-run 不响铃。Linux/WSL 使用终端 BEL，是否有声音取决于终端的响铃设置。

## 主流程

1. `download.ps1/.sh` 下载原片、封面、`.info.json`、`.description`、`.tags.txt`，然后把原片改名为 `<name>.original.<ext>`，并统一重编码出编辑用 `<name>.mkv`
2. `whisper.ps1/.sh` 对编辑版 `<name>.mkv` 调用 `whisperx --output_format json`，输出 `<name>.json`
3. `translate_srt.py --only-beautify` 美化 JSON 里的 word 时间轴并回写 segment，输出 `<name>.beautified.json`、`<name>.scenes.json`、`<name>.scenechange.txt`
4. `translate_srt.py --only-glossary` 读取整句 transcript 和元数据，重新生成并覆盖 `glossary.md`
5. `translate_srt.py` 使用整句 JSON 翻译
6. AI 分割后用每个源语言 split 的首尾 word 匹配美化后的 `words[]` 回填时间，再对 split events 做最终校对，输出 `.split.<source>.srt` / `.split.<target>.srt` 和最终 ASS；显式 `--no-split` 时也继续输出 ASS
7. `ffmpeg-burn.ps1/.sh` 使用双语 `.ass` 硬压字幕；pipeline 默认回到 `<name>.original.<ext>` 原片压制

成果物链：

```text
<name>.original.<ext> + <name>.mkv -> <name>.json -> <name>.beautified.json
-> <name>.web_evidence.json + glossary.md
-> <name>.human-review.json + <name>.proofread-report.md
-> <source>.proofread.ass / <target>.ass / <source>-<target>.ass -> burned.mkv
```

## translate_srt.py

入口只接受 WhisperX JSON。推荐使用项目包装器，不需要用户设置 PATH，也不要求从仓库目录运行：

```powershell
& "<repo>\translate_srt.ps1" "video.json" --video "video.mp4"
& "<repo>\translate_srt.ps1" "video.json" --video "video.mp4" --only-beautify
& "<repo>\translate_srt.ps1" "video.beautified.json" --video "video.mp4" --source-lang en --target-lang ja
& "<repo>\translate_srt.ps1" "video.beautified.json" --video "video.mp4" -o "custom.en-ja.ass"
```

```bash
bash "<repo>/translate_srt.sh" "video.json" --video "video.mp4"
bash "<repo>/translate_srt.sh" "video.json" --video "video.mp4" --only-beautify
bash "<repo>/translate_srt.sh" "video.beautified.json" --video "video.mp4" --source-lang en --target-lang ja
bash "<repo>/translate_srt.sh" "video.beautified.json" --video "video.mp4" -o "custom.en-ja.ass"
```

合并已校对的双语 ASS 时，同样使用项目包装器：

```powershell
& "<repo>\merge_ass.ps1" "video.zh.ass" "video.en.ass"
```

```bash
bash "<repo>/merge_ass.sh" "video.zh.ass" "video.en.ass"
```

输出：

- `<name>.beautified.json`：主缓存，保存 `translation`、`proofread_text`、`split_events`
- `<name>.scenes.json`：场景切换 sidecar，包含 fps、threshold、frame、timecode 等调试信息
- `<name>.scenechange.txt`：每行一个秒级场景切换点，例如 `12.345000`
- `<name>.web_evidence.json`：联网证据 sidecar，保存规范化 query、provider、域名、URL、标题和证据摘要，用于 embedding 检索
- `<name>.human-review.json`：需要人工核验的 translation / proofread 标记，不污染字幕文本
- `<name>.proofread-report.md`：串行校对的初始决策、safety retry、最终提交或回滚报告
- `<name>.split.<source>.srt`：分割后、最终校对后的源语言 SRT 检查稿
- `<name>.split.<target>.srt`：分割后、最终校对后的目标语言 SRT 检查稿
- `<name>.<source>.proofread.ass`：最终校对源语言 ASS
- `<name>.<target>.ass`：目标语言 ASS
- `<name>.<source>-<target>.ass`：双语 ASS
- `<name>.<target>.description`：目标语言简介

`SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名，例如 `en`、`en-US`、`Japanese`、`Chinese Simplified`。输出文件后缀会通过 `langcodes` 规范为 ISO 639 代码，例如 `English -> en`、`Japanese -> ja`。未显式设置 `SOURCE_LANG` 时，脚本使用 WhisperX JSON 中的 `language`；`TARGET_LANG` 默认 `zh`。

翻译、分割、校对按顺序执行：先用整句 JSON 翻译保留语义，再用未校对源语言文本分割并对齐词源时间轴，最后对已分割的 subtitle events 做双语校对。所有批量 LLM 阶段的 user prompt 都是 JSON object，顶层包含 `items` array，返回也必须是同形态 JSON object；`items` 内只使用 `id` 和源/目标 ISO 639 语言代码 key，例如 `en`、`zh`。分割阶段默认给 pending segment 附带前后各 1 条 `context_before` / `context_after`，仅用于理解语义和节奏，远端只返回 pending item 本身；可用 `--split-context-window` 调整。分割完成后，脚本用每个源语言 split 的首尾 word 顺序匹配美化后的 `words[]`，对齐每条显示字幕的起止时间。如果缺标号、源/目标段数不齐、源语言片段无法还原未校对整句或首尾 word 无法对齐词级时间轴，脚本会丢弃该分割结果并回退到整句 beautified 时间轴，不做本地强切。`.beautified.json` 会用 `split_status` 记录状态：`ok` 为有效分割，`fallback` 为分割失败后整句回退且可重试，`unsplit` 为低于阈值或合法保留整句；`split_reason` 保存原因码，`split_reason_detail` 保存具体诊断文本。

默认模板以 1080p 双语观看为基准：`bi-zh` / `bg-bi-zh` 字号为 68，`bi-en` / `bg-bi-en` 字号为 44；AI 分割默认在源文超过 72 字符或 3.8 秒时触发。beautify 只负责词级时间轴吸附和边界修复，不再提供本地硬截整句参数。

`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 可以使用语言模板变量。配置 Tavily 或 Exa 时，glossary 会依据网页结果校正疑似 ASR、专名和术语；原始证据另存到 `<name>.web_evidence.json`，供后续检索和人工复核。

## 配置

运行 `setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`web_search.json`、`tavily_domains.json`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 和 `template.ass`。旧版本升级时，setup 会把 `.env.example` 中新增但你本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。

setup 后至少配置：

```ini
TRANSLATE_PROVIDER=deepseek
GLOSSARY_PROVIDER=deepseek
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
| `GLOSSARY_PROVIDER` | 术语知识库构建后端；强烈建议使用可用范围内最顶级模型，空则复用翻译 provider |
| `GLOSSARY_MODEL` | 术语知识库构建模型；负责搜索意图、ASR 纠错、背景归纳和译名决策，空则复用翻译模型或 provider 默认 |
| `EMBEDDING_ENABLED` | `1/0` 控制是否用 LangChain + Chroma 构建 embedding 索引，为 glossary/description/translate/proofread 提供动态 `retrieved_context` |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | OpenAI SDK 兼容 embedding 后端和模型，可指向本地 llama.cpp / Ollama / OpenAI-compatible 服务 |
| `EMBEDDING_STORE` / `EMBEDDING_CHROMA_DIR` | 当前支持 `chroma`；目录空则使用项目目录下 `chroma_db` |
| `EMBEDDING_TOP_K` / `EMBEDDING_CHUNK_CHARS` / `EMBEDDING_BATCH_SIZE` | embedding 检索、切块和批量调用参数 |
| `LOCAL_EVIDENCE_RETRIEVAL_ENABLED` / `LOCAL_EVIDENCE_TOP_K` | 可选的本地 lexical evidence 检索，默认 `0`；与 embedding 分开，设为 `0` 时不会注入这类动态 `retrieved_context` |
| `PROOFREAD` | `1/0` 控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider |
| `PROOFREAD_MODEL` | 校对专用模型 |
| `PROOFREAD_ENHANCED` | `1/0` 显式启用证据增强校对和按需联网搜索，默认 `0`；仅设置 provider/model 不会开启联网 |
| `PROOFREAD_THINKING` / `PROOFREAD_REASONING_EFFORT` | 已知支持的 provider（当前为 DeepSeek）留空自动使用 `enabled` / `high`；非空显式值始终覆盖。能力未知或不支持的 provider 不发送专用参数 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `TAVILY_API_KEY` / `EXA_API_KEY` | Tavily/Exa 搜索 API keys；key 为空时对应 provider 禁用 |
| `PIPELINE_SKIP_*` | 流水线阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |

`BURN_OVCOPTS=source-bitrate` 会用 `ffprobe` 读取源视频码率，并用 VBR 的 `b/maxrate/bufsize` 让硬字幕输出尽量接近源码率；显式设置 `qp=20`、`crf=23` 等会覆盖自动模式。`BURN_OAC` 默认 `aac`，兼容 ffmpeg 和 mpv 的硬字幕压制。

非敏感联网搜索配置位于本地 gitignored `web_search.json`（由 `web_search.example.json` 初始化），字段为 `provider`、统一 `max_results`、`glossary_max_queries` 和 `proofread_max_queries`。环境变量只保存 `TAVILY_API_KEY` / `EXA_API_KEY`。

配置联网 provider 时，glossary 阶段默认使用两段式 tool calling：脚本第一轮把 metadata、transcript/retrieved context 和 `tavily_domains.json` 域名偏好一起交给 glossary 模型；模型按需请求搜索，脚本执行后把结果作为 tool message 喂回同一 session。搜索完成后，脚本新建无工具 finalizer session，只喂用户 JSON、transcript/retrieved context 和已收集的 `web_evidence`，要求模型生成最终 glossary。`web_search.json` 的 `glossary_max_queries` 控制新网络查询预算；设为 `0` 时不发起新请求，但已有 sidecar evidence 仍可供 glossary 和后续阶段读取。

如果 `glossary.md` 已缓存但 `<name>.web_evidence.json` 缺失，且已配置 Tavily 或 Exa，脚本会补建 sidecar 而不重写 glossary。

证据增强校对必须通过 `PROOFREAD_ENHANCED=1` 明确开启。`PROOFREAD_PROVIDER` 和 `PROOFREAD_MODEL` 只选择校对模型，不会改变联网能力。启用后可使用 Tavily、Exa 或已有的 web evidence 缓存；`web_search.json` 的 `proofread_max_queries` 只限制实际新搜索，设为 `0` 时仍可离线复用 `<name>.web_evidence.json` 的 exact cache，缓存复用不计入预算。glossary 是主要研究阶段，但 proofread 保留针对候选 ASR/术语疑点的按需联网核验；当轮新证据会先 enrich 后再进入 candidate safety evaluation。

校对 safety 分为语言无关和语言专用两层：ID 完整性、时间轴不变、术语约束、sentence-group 原子性与证据冲突处理始终启用；当前语义锚点词表仅覆盖 English→Chinese，其他语言方向会显式跳过该语言专用 gate，仍保留全部通用检查。

同一原句分割出的 siblings 始终作为一个事务组请求和提交；任一 sibling 在最多一次定向 safety retry 后仍失败，整组回滚并写入 human review/report。输入或输出长度恢复只在完整句组边界拆分请求，不拆开 siblings。

`PROOFREAD_BATCH_SIZE` 控制单次串行校对请求包含多少字幕。未开启 thinking 时，部分模型的校对会明显趋于保守；开启 thinking 可提高问题发现与校对覆盖，但会增加 token、延迟和费用。当前仅对已知支持这组请求参数的 DeepSeek 校对自动使用 `PROOFREAD_THINKING=enabled` 与 `PROOFREAD_REASONING_EFFORT=high`；能力未知或不支持的 provider 不发送专用参数。任一非空显式环境变量始终覆盖对应自动值。thinking/reasoning 只作用于 proofreading，不影响首译或 glossary。proofread report 中的 KEEP / EDIT / REVIEW / ROLLBACK 计数仅用于诊断，不是 EDIT 数量或修改率目标。

`GLOSSARY_PROVIDER` / `GLOSSARY_MODEL` 独立控制术语知识库阶段使用的 LLM；这个阶段会决定搜索什么、相信哪些网页证据、如何修正 ASR 错误、核心术语如何定译，并会影响后续翻译和校对记忆。请优先给它配置当前可用的最强、最顶级模型，而不是为了省成本使用小模型。只运行 `--only-glossary` 时，可以只配置 `GLOSSARY_PROVIDER` 和对应 API key；完整翻译流程仍需要 `TRANSLATE_PROVIDER`。

glossary tool 阶段会强制移除 provider `request_kwargs.response_format` 中的 JSON mode 参数，以免干扰 tool calling；finalizer 首选返回 `{"markdown": "..."}` JSON object，若 provider 无法稳定输出 JSON，可返回 `<GLOSSARY_MARKDOWN>...</GLOSSARY_MARKDOWN>` 标签块。普通散文和伪 tool call 文本都会被拒绝并重试。

Tavily tool 本地仍采用域名优先策略：脚本结合模型给出的 query / `topic_hints`、metadata 与 `tavily_domains.json` 中的全局百科域名、题材关键词和站点执行 `include_domains` 搜索；如果结果不足，再执行普通 Tavily 搜索；最终合并去重时会优先保留百科/知识库域名结果。`tavily_domains.json` 由 `tavily_domains.example.json` 初始化，用户可以自行添加题材、关键词和站点。

`glossary.md` 是完整常驻的全局硬规则；retrieved context 只能补充。glossary evidence 可参与构建全局术语，proofread `confirmed_terms` 只在 sidecar 持久化为局部约束。当前自动 evidence→structured hard promotion 仅支持拉丁字母源语言到中文目标语言；其他方向保留 raw web evidence、模型判断和 human review，不生成伪确定性 hard constraint。`web_evidence:*` 来自规范化 Tavily/Exa 结果。

`providers.json` 使用 OpenAI SDK 兼容配置，仓库只提交 `providers.example.json`。`request_kwargs` 会原样合并进 `chat.completions.create(**kwargs)`，用于 DeepSeek JSON mode、Gemini Google Search 等 provider 专用参数；Gemini 内置联网需要 Gemini 3 或更新模型。

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
| `exa-py>=2.0.0` | Exa 可选联网搜索 SDK；2.0.0 起 `search(..., contents=...)` 支持当前调用协议 |
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## 注意事项

- `.env`、`providers.json`、`web_search.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 已 gitignored
- 不要把 Python 包安装到系统环境；Windows 运行 `.\setup.ps1`，Linux/WSL 运行 `./setup.sh`，它们会创建/更新仓库 `.venv`
- 运行 pipeline 或任一 Python 相关脚本前必须先完成 setup；pipeline 和 `translate_srt.ps1/.sh`、`merge_ass.ps1/.sh` 统一使用项目 `.venv`，不调用全局 `python` / `python3`，也不要求用户设置 PATH。独立脚本可从任意工作目录通过包装器路径调用
- `TORCH_BACKEND=auto` 会用 `nvidia-smi` 检测 NVIDIA GPU；NVIDIA 用户可设 `cuda128`，AMD/无独显用户设 `cpu`
- `cookies.txt` 通过相对路径引用，请在仓库根目录运行脚本
- `download.ps1/.sh` 会固定输出两条路径：`OUTPUT_VIDEO` 是编辑用 `<name>.mkv`，`OUTPUT_RENDER_VIDEO` 是保留给最终压制的 `<name>.original.<ext>`；若目录中已有 `<name>.original.mkv`，脚本视为原片已下载，只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`，然后直接进入重编码。编辑版优先使用 `h264_nvenc` 重编码视频，未检测到可用 NVIDIA GPU 或 NVENC 编码器时回退 `libx264`，并统一用 `aresample=async=1:out_sample_fmt=s16` + `flac` 重建音频时间轴。若 `h264_nvenc` 返回非零退出码但已输出非 0B 文件，脚本会保留该文件并继续，不再回退重编码。
- 完整翻译流程必须配置 `TRANSLATE_PROVIDER`；只构建 glossary 时可改用 `GLOSSARY_PROVIDER`
- WhisperX 首次运行会下载模型
- 默认不硬压，推荐先人工校对 ASS，再决定是否压制
- `.srt` 已退出主流程；不要再把 SRT 当作翻译输入
