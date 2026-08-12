# AGENTS.md

本文件是项目的唯一权威文档。代码路径、行为、配置以当前仓库为准；不要引用历史脚本或本地视频项目路径。

## Overview

`download(original + edit mkv) -> whisper(json) -> beautify(json words) -> glossary -> translate -> split -> proofread -> ass -> burn`

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
├── template.ass.example      # ASS 模板示例；setup 时复制为 template.ass
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

本地文件 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md`、`template.ass` 和生成产物均不应提交。

## Pipeline Flow

### Windows `pipeline.ps1`

1. `download.ps1` 下载原片、封面、`.info.json`、`.description`、`.tags.txt`，把原片改名为 `<base>.original.<ext>`，并统一重编码出编辑用 `<base>.mkv`
2. `whisper.ps1` 对编辑版 `<base>.mkv` 调用 WhisperX，只输出 `<base>.json`
3. `translate_srt.py --only-beautify` 美化 JSON 中的 word 时间轴，输出 `<base>.beautified.json`
4. `translate_srt.py --only-glossary` 在翻译前强制重新生成并覆盖 `glossary.md`
5. `translate_srt.py` 整句翻译、AI 分割、词级对轴、split event 校对，输出最终字幕
6. `ffmpeg-burn.ps1` 可选硬压双语 ASS 到 `burned.mkv`

### Linux/WSL `pipeline.sh`

流程与 Windows 对齐，使用 `download.sh`、`whisper.sh`、`translate_srt.py`、`ffmpeg-burn.sh`。两个 pipeline 都实时透传各步骤输出。

### Output Chain

```text
<base>.original.<ext> + <base>.mkv -> json -> beautified.json -> web_evidence.json + glossary.md
      -> split.<source>.srt / split.<target>.srt
      -> <source>.proofread.ass / <target>.ass / <source>-<target>.ass
      -> burned.mkv
```

默认 `.env.example` 设置 `PIPELINE_SKIP_BURN=1`，推荐先人工校对字幕，再决定是否硬压。

## Step Behavior

### download

- 输出目录名和视频基名相同，视频路径形如 `<video_dir>/<video_dir>.<ext>`
- 下载后会保留原片为 `<video_dir>/<video_dir>.original.<ext>`，并始终重编码生成编辑用 `<video_dir>/<video_dir>.mkv`
- 如果 `<video_dir>/<video_dir>.original.mkv` 已存在，download 脚本视为原片已下载，只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`，然后直接进入编辑版重编码
- 脚本输出 `OUTPUT_VIDEO=<编辑版 mkv>` 和 `OUTPUT_RENDER_VIDEO=<原片>`；pipeline 前半段使用前者，burn 使用后者
- 同步保存 `.png` 封面、`.info.json` 元数据、`.description` 简介、`.tags.txt` 标签
- SponsorBlock 移除 `sponsor,selfpromo`
- 下载后固定做一次时间戳抚平重编码：优先使用 `h264_nvenc -cq 12` 重编码视频，未检测到可用 NVIDIA GPU 或 NVENC 编码器时回退 `libx264 -crf 12`；音频统一用 `aresample=async=1:out_sample_fmt=s16` + `flac` 重建时间轴，并清理 metadata。若 `h264_nvenc` 返回非零退出码但已输出非 0B 文件，脚本会保留该文件并继续，不再回退重编码。
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
- 如果 `glossary.md` 已缓存但 `<base>.web_evidence.json` 缺失，且 Tavily 或 Exa 可用，会补建 sidecar 并融合新证据
- 手动运行 `--only-glossary` 时忽略已有缓存，重新生成并覆盖 `glossary.md`
- 读取 transcript、`.description`、`.tags.txt`、`.info.json`
- 本地脚本会把 YouTube 原视频元信息前置写入 `glossary.md`，包括标题、作者、上传时间、原简介和标签；这部分不交给远端 LLM 合成
- 配置 `TAVILY_API_KEY` 或 `EXA_API_KEY` 时联网搜索，均未配置时离线总结
- 联网搜索结果是 glossary 的优先证据来源；远端 LLM 应用搜索结果校正 transcript 中可能的 ASR 人名、标题、引文和术语错误
- 仅配置 Tavily 时 glossary 保留 `tavily_search` 工具；配置 Exa 或多个来源时使用 provider-neutral 的 `web_search`，规范化结果交给无工具 finalizer
- 网页证据统一写入 `<base>.web_evidence.json`：原始记录保存 provider、阶段和关联字幕 id；经真实证据 URL 校验的标准译名另存为 `confirmed_terms`，供 proofread 确定性约束，不依赖 embedding 命中
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
- 校对返回只包含最终 source/target 与真正不确定项的 `review`；旧 Provider 多余返回的 `edit` 仅在兼容解析层忽略，不参与修改准入
- `proofread_prompt.md` 是中文自然度、翻译腔、本地化、人物声音、语气、节奏、修辞和改写幅度的唯一控制面；主模型只返回最终文本与真正不确定项的 review，KEEP/EDIT 状态由程序比较生成
- 主流水线对目标译文采用明确回归拦截：普通自然度、本地化、搭配、语气和表达优化默认生效，仅在可确定检测到源文支持的否定、排他性、程度、情态等语义锚点丢失，或术语、证据、跨事件连续性回归时回滚；源文/ASR 修改仍要求结构化术语或 retrieved ASR 证据
- 非 quiet 运行逐 item 输出 `KEEP_BY_MODEL`、`REVIEW_BY_MODEL`、`EDIT_APPLIED`、`EDIT_PARTIALLY_APPLIED` 或 `EDIT_ROLLED_BACK`；安全回滚同时列出 semantic anchor、confirmed term、evidence conflict、ASR 范围或跨 event 等原因
- sentence group 是 batch、retry、rollback 和提交的最小原子单位；任一 child 被 safety gate 回滚时整组无新搜索 retry 一次，仍失败则逐 child 恢复自身 snapshot，禁止恢复 parent 整句 target
- `PROOFREAD_BATCH_SIZE` 与 `PROOFREAD_CONCURRENCY` 独立；worker 不写 transcript/report/review/evidence，主线程按原 event 顺序确定性合并
- 每次运行确定性输出 `<base>.proofread-report.md`，记录首次 proposal/decision/gate reason、可选 retry proposal/result 及 final target；报告不进入 transcript cache 或 human-review sidecar
- 每个 split event 都注入同一原始 segment 的完整 `sentence_context`，不受邻居窗口或 batch 边界影响；本地拒绝在非末尾事件中无依据新增句末闭合标点
- 当前字幕命中 `confirmed_terms` 时，校对请求注入高优先级 `terminology_constraints`，本地后处理禁止模型用新音译、同义词或风格变体覆盖已确认译名；冲突项不选边，进入 human review
- 自然化必须保留信息、逻辑、语气、程度、指代和修辞；不把所有源语言痕迹视为错误，有表达价值的异质化、文学化、重复或歧义可保留
- proofread 联网查询的空结果、Provider 错误和预算耗尽按 item id 保存在运行时未解决状态，并确定性合并到 event review；既有 event review 后续不得被空 review 覆盖
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
- pipeline 正常下载模式下，字幕编辑链路使用 `<base>.mkv`，最终硬压回到 `<base>.original.<ext>`
- `-SkipBurn` / `SKIP_BURN=1` / `PIPELINE_SKIP_BURN=1` 会跳过硬压
- `BURN_RES` 指定输出分辨率时保持宽高比并补黑边
- `ExistingAss` / `EXISTING_ASS` 可指定已有双语 ASS 跳过翻译，直接用于硬压

## Config

`setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`tavily_domains.json`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 和 `template.ass`。旧版本升级时，setup 会把 `.env.example` 中新增但本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。PowerShell 入口通过 `.env.ps1` 读取，bash 入口自行读取。

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
| `EMBEDDING_ENABLED` | `1` / `0` 控制是否用 LangChain + Chroma 构建 embedding 索引，为 glossary/description/translate/proofread 提供动态 `retrieved_context` |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | OpenAI SDK 兼容 embedding provider 和模型，可指向本地 llama.cpp / Ollama / OpenAI-compatible 服务 |
| `EMBEDDING_STORE` / `EMBEDDING_CHROMA_DIR` | 当前仅支持 `chroma`；目录空则使用项目目录下 `chroma_db` |
| `EMBEDDING_TOP_K` / `EMBEDDING_CHUNK_CHARS` / `EMBEDDING_BATCH_SIZE` | embedding 检索、切块和批量调用参数 |
| `PROOFREAD` | `1` / `0` 控制 split event 校对 |
| `PROOFREAD_PROVIDER` | 校对 provider，空则复用翻译 provider |
| `PROOFREAD_MODEL` | 校对模型，空则复用翻译模型；仅显式设置时启用增强校对和按需联网 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_CONCURRENCY` | 校对并发任务数，默认 `1`；worker 只收集模型结果，主线程按序提交 |
| `PROOFREAD_THINKING` / `PROOFREAD_REASONING_EFFORT` / `PROOFREAD_MAX_TOKENS` | 校对专用推理与 token 参数，不影响其它阶段 |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |
| `OPENAI_API_KEY` / `OLLAMA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | LLM / embedding API keys |
| `WEB_SEARCH_PROVIDER` / `WEB_SEARCH_TIMEOUT` | 搜索来源选择与超时；`auto` / `all` / `tavily` / `exa` |
| `TAVILY_API_KEY` / `TAVILY_MAX_RESULTS` | 可选 Tavily 搜索来源 |
| `EXA_API_KEY` / `EXA_MAX_RESULTS` | 可选 Exa 搜索来源 |
| `GLOSSARY_SEARCH_MAX_QUERIES` / `PROOFREAD_SEARCH_MAX_QUERIES` | glossary 与增强校对阶段各自的共享搜索预算 |

`BURN_OVCOPTS=source-bitrate` 是默认硬压策略：burn 脚本用 `ffprobe` 读取源视频码率，生成 VBR 的 `b/maxrate/bufsize` 参数，让输出尽量接近源码率；显式 `qp=20`、`crf=23` 等会覆盖自动模式。`BURN_OAC` 默认 `aac`，兼容 ffmpeg 和 mpv 的硬字幕压制。

glossary 搜索使用两段式 tool calling，并支持 Tavily/Exa 任一来源。`auto` 优先 Tavily、无结果时回退 Exa，`all` 在共享预算内查询两者。显式配置 `PROOFREAD_MODEL` 后，proofread 可按需调用同一 `web_search` 证据层；普通润色不得搜索，失败或冲突时必须保守完成并写 human review。

glossary tool 阶段会强制移除 provider `request_kwargs.response_format` 中的 JSON mode 参数，以免干扰 tool calling；finalizer 首选返回 `{"markdown": "..."}` JSON object，若 provider 无法稳定输出 JSON，可返回 `<GLOSSARY_MARKDOWN>...</GLOSSARY_MARKDOWN>` 标签块。普通散文和伪 tool call 文本都会被拒绝并重试。

`glossary.md` 是全局硬规则：一旦存在，会完整常驻注入后续翻译、校对和视频简介翻译的 system prompt。`web_evidence.json` 的 `confirmed_terms` 是逐条确定性硬证据层，必须引用 sidecar 中实际存在的 URL；弱证据、不确定项或冲突项不得升级。其余 `web_evidence:*` 由规范化 Tavily/Exa 结果构建并保留 provider、阶段、字幕 id、query、URL 与摘要，继续通过检索复用。

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
.\whisper.ps1 "video.mp4"
.\.venv\Scripts\python.exe translate_srt.py video.json --video video.mp4 --only-beautify
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.mp4 --only-glossary --skip-beautify
.\.venv\Scripts\python.exe translate_srt.py video.beautified.json --video video.mp4 --source-lang en --target-lang zh
.\ffmpeg-burn.ps1 "video.original.webm" -SubFile "video.en-zh.ass"
```

```bash
./download.sh "URL"
./whisper.sh "video.mp4"
./.venv/bin/python translate_srt.py video.json --video video.mp4 --only-beautify
./.venv/bin/python translate_srt.py video.beautified.json --video video.mp4 --only-glossary --skip-beautify
./.venv/bin/python translate_srt.py video.beautified.json --video video.mp4 --source-lang en --target-lang zh
./ffmpeg-burn.sh "video.original.webm" --sub-file "video.en-zh.ass"
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
| `tavily-python` | 可选 Tavily SDK；未安装时联网能力按配置降级 |
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## Working Notes

- 更新文档时以实际脚本参数和文件名为准，不保留历史 SRT 流程
- 保持 PowerShell 和 bash 入口行为对齐
- 不要提交 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 或生成产物
- 不要回退用户本地数据或未请求的工作区改动
- `README.md` 面向用户快速使用；`AGENTS.md` 面向维护和自动化代理；`.agents/skills/*` 面向分步骤执行
