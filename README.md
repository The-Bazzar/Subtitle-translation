# YouTube 字幕流水线

从 YouTube 链接出发，完成：

`下载原片 -> 准备编辑版 -> WhisperX JSON -> JSON 时间轴美化 -> glossary 术语知识库 -> 整句翻译 -> 分割对轴 -> split 校对 -> 双语 ASS -> burned.mkv`

> 必须使用 PowerShell 7。旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。升级命令：`winget install Microsoft.PowerShell`

## 项目结构

```text
├── pipeline.ps1
├── pipeline.sh
├── download.ps1
├── download.sh
├── prepare-video.ps1
├── prepare-video.sh
├── whisper.ps1
├── whisper.sh
├── py_launcher.ps1
├── py_launcher.sh
├── merge_ass.ps1
├── merge_ass.sh
├── translate_srt.ps1
├── translate_srt.sh
├── translate_srt.py
├── ffmpeg-burn.ps1
├── ffmpeg-burn.sh
├── mpv-burn.ps1
├── mpv-burn.sh
├── batch.ps1
├── batch.sh
├── batch.py
├── batch_scheduler.py
├── batch_cache.py
├── whisper_worker.py
├── setup.ps1
├── setup.sh
├── .env.ps1
├── template.ass.example
├── .env.example
├── providers.example.json
├── tavily_domains.example.json
├── glossary_prompt.example.md
├── translate_prompt.example.md
├── proofread_prompt.example.md
├── split_prompt.example.md
└── MIGRATION.md
```

时间轴美化和 glossary 生成已集中到 `translate_srt.py`。主链路不再使用 SRT，WhisperX `.json` 是唯一字幕输入。`glossary_prompt.md` / `split_prompt.md` 可作为本地风格微调文件使用，`tavily_domains.json` 可维护题材相关站点；这些本地文件不提交，仓库只提交对应 example。

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

### Stage-aware 批处理

```powershell
.\batch.ps1 "URL1" "URL2"
.\batch.ps1 --skip-burn --translate-provider deepseek --translate-model deepseek-chat "URL1" "URL2"
.\batch.ps1 --dry-run --report "batch-result.txt" "URL1" "URL2"
```

```bash
./batch.sh "URL1" "URL2"
./batch.sh --skip-burn --translate-provider deepseek --translate-model deepseek-chat "URL1" "URL2"
./batch.sh --dry-run --report batch-result.txt "URL1" "URL2"
```

`batch.ps1` / `batch.sh` 都是 `py_launcher` 的参数透传包装器，实际调度由 `batch.py` / `batch_scheduler.py` 完成。容量自动检测：CPU/IO 槽位为 `max(1, (os.cpu_count() or 1) // 4)`，prepare 与最终 burn 共用固定 `4` 路 NVENC capacity，不提供 `-j`、`--jobs`、`--io-jobs` 或 `MaxJobs`。batch 会直接读取项目 `.env`；进程环境和显式 CLI 优先于 `.env`，`FFMPEG_PATH_WIN` / `FFMPEG_PATH_LINUX` 为空或缺失时使用 `ffmpeg`。

当前 stage-aware 入口先并行流水执行 `download -> prepare-video -> mono 16kHz WAV`，等待 acquisition 全部到达终态后，再启动一个 `spawn` worker。prepare 与 WAV 提取/替换都在同一媒体的 `<name>.asr.lock` 内完成；prepare 成功后原子写 `<name>.prepare.json`，用 UUID `generation` 绑定原片和编辑版各自的 resolved path、size、mtime，准备前后原片快照不一致时拒绝认证该编辑版。worker 加载一次 WhisperX ASR 模型并串行识别所有未缓存 WAV；每个 WAV 绑定到不可变 media generation，ASR 写 sidecar 前会在同一锁内重新验证 prepare state、编辑版和 WAV 快照。

正常重启时，download 返回原片后，只有 `<name>.prepare.json` 中的原片与编辑版身份都仍匹配才跳过 prepare；即使原片 mtime 相同或更早，只要 path/size/mtime 任一变化也会重新 prepare。模型、语言或 ASR options 变化只使 `.asr.json` cache miss，不会无意义重编码视频。ASR sidecar schema v2 同时记录 ASR generation、media generation、WAV snapshot 和 fingerprint；旧 schema 不再命中。alignment backend 始终读取绑定的 WAV snapshot path，并在调用前后复核同一 snapshot；parent 在 final promote 前再复核一次。alignment 成功后删除 WAV，删除动作仍在同一媒体锁内；失败、取消或 worker crash 则保留 WAV。

alignment candidate、最终 `<name>.json` 与 `<name>.beautified.json` 都携带 `_batch_artifact.media_generation` 和 `_batch_artifact.alignment_generation`。同一进程内同一媒体继续使用 keyed transaction lock，跨 invocation 的 prepare、WAV 替换、alignment commit 与 postprocess publish 则共享 `<name>.asr.lock`。postprocess 只写隐藏的 generation-specific beautified candidate，结束后在锁内重新验证 final generation，匹配时才原子发布为 `<name>.beautified.json`；过期或失败只清理本 generation candidate，不删除其他 invocation 已发布的新 cache。新的 alignment generation 提交前仍必须删除旧 `<name>.beautified.json`，删除失败会阻止 commit 并写入 cleanup diagnostic。

ASR wave 显式卸载模型后不关闭 worker。scheduler 用 `langcodes` 验证 sidecar 的 `result.language` 并规范为稳定 ISO 639 主语言代码；无效、未知或 `und` 会拒绝，配置有效 `SOURCE_LANG` 时可作为 fallback。任务按语言稳定排序分组并保持组内原顺序；每组只加载一次 alignment model，逐任务串行生成 WhisperX-compatible `<name>.json`，组间卸载并替换模型。parent 在 blocking thread 中持有 media lock 后才 dispatch `ALIGN`；child 只原子写隐藏的 generation candidate，绝不覆盖 final。parent 在同一锁内验证 ownership/candidate/schema，并通过 per-task commit state 将取消与 destructive commit 线性化：取消先取得状态锁时保留 sidecar 并只清 candidate；commit 先取得状态锁时不可被 abort 打断，会 durable promote、删除 owned sidecar，并继续正常 postprocess。并发 ASR writer 会阻塞到 commit 完成，随后写入的新 generation sidecar 会保留。`WHISPER_ALIGN_MODEL` 非空时覆盖自动模型，空时沿用 WhisperX 按语言自动选择。

每个任务 alignment 成功后会立即进入 CPU/IO semaphore 执行 beautify、glossary 和 translate，不等待其他语言组完成。所有 alignment 到达终态后，scheduler 才卸载 alignment model、关闭并 join worker，同时设置 `worker_released` event；任何 burn 都必须先等到该 event。启用 burn 时，任务翻译完成后使用原片和最终 `<source>-<target>.ass` 调用现有 `ffmpeg-burn.ps1/.sh`，同时校验 `OUTPUT_BURNED_VIDEO=` marker 与非空输出文件；最多 4 路并发，较晚完成翻译的任务会在 worker release 后动态加入。`--skip-burn` 任务以 `translated` 成功结束，启用 burn 的任务以 `burned` 成功结束。

active worker command 在 precommit 取消时由 controller terminate/kill，再不可中断地等待 request thread 真正结束；transaction thread 在 finally 清 candidate、释放 lock。若 destructive commit 已开始，非阻塞仲裁立即判定 commit-wins，scheduler 等 promote 和 owned sidecar 删除完成；否则 cancel-wins 保留 `.asr.json`。`worker_released` 只在全部 request/transaction thread 与 worker controller/process 清理完成后设置。heartbeat 超时或意外退出不会自动重启：立即关闭 worker admission，当前 worker task 失败，其余仍依赖 ASR/alignment 的任务进入 `blocked_by_worker_failure`；已经 alignment 成功的任务继续 postprocess，并在 worker release 后正常 burn。worker child 把 Python 输出和 native fd 1/2 写入 controller 专属临时 capture 文件，故障时读取 bounded tail，正常 close 后删除。batch 会 best-effort 原子写 invocation `Path.cwd()` 下的 `batch-worker-failure-<timestamp>.log`，记录 task/phase、队列快照、exit code、traceback，以及合并后的外部命令与 worker stdout/stderr；文本会去除 ANSI，tail 截断会显式标记。日志目录或写盘失败只追加 cleanup diagnostic，不改变首个 worker 根因、任务终态或聚合退出码。

`--report batch-result.txt` 除文本报告外还会生成同基名 `batch-result.json`。机器报告顶部包含 `worker_failure`、`worker_failure_log`、`worker_failure_root_cause`、`worker_failure_detail`、invocation `output_directory` 和 `cleanup_diagnostics`；每个 task 也记录自己的 `output_directory`、终态、失败阶段、耗时和输出路径。指定 `.json` 报告名时，JSON 使用指定路径，文本报告写到同基名 `.txt`。

所有外部命令的 stdout/stderr 都以 64 KiB chunk 持续排空，并按 `\n`、`\r` progress 或 EOF partial framing 后进入有界 terminal queue；超长逻辑行拆为带同一 `[02][prepare]` 前缀的 bounded continuation events，不依赖 `StreamReader` line limit。唯一 printer 保持 queue 顺序；每个 task/stream 只保留 256 KiB diagnostic tail，截断时写明 marker。第一次 `Ctrl+C` 会同步关闭 command gate；外部命令只有在此前取得 reservation 才可 spawn，并被视为当前 active command 自然结束，尚未 reservation 的命令绝不 spawn。reservation 返回是 Python scheduler 定义的线性化点，不宣称与操作系统进程创建原子。当前命令结束后不再启动下一阶段。第二次 `Ctrl+C` 强制终止已注册的子进程树并 abort worker，等待真实退出后以 `130` 结束；已完成输出和 recovery sidecar 保留。

`<name>.asr.json` 只是 batch 的恢复中间件，不是字幕主入口。`whisper.ps1` / `whisper.sh` standalone 路径仍直接输出最终词级 `<name>.json`，不会读取或写入 `.asr.json`。

`<name>.asr.lock` 是持久、最多 1 byte 且已被 Git 忽略的运行时协调文件，不是字幕或 cache；保留它可避免活跃进程因 unlink 后锁定不同 inode/handle 而失去互斥。

单任务 `pipeline.sh` 在 prepare 失败时精确透传 `prepare-video.sh 的原始退出码`，与 PowerShell pipeline 的行为一致，不再把所有 prepare 错误折叠为 `1`。

发布前的跨平台 contract smoke 位于 `tests/test_batch_smoke.py`：Windows 经 `batch.ps1 -> py_launcher.ps1 -> batch.py`，WSL/Linux 经 `batch.sh -> py_launcher.sh -> batch.py`，运行真实 argparse/main、`ResourceLimits.detect`、subprocess stage runners、marker parser 和 spawned `WhisperWorkerController` 协议，并解析 JSON machine report 断言报告契约。smoke 把 byte-identical production modules 复制到隔离目录并校验 SHA-256；只把 download、prepare、translate、burn、ffmpeg 和 child 内 import 的 WhisperX 作为 deterministic fake 外部边界。WSL smoke 由 `wsl -u root` 按测试环境 override、仓库 `.venv/bin/python`、`command -v python3` 的顺序选择 Python `>=3.10,<3.14` 且能 import `langcodes` 的现有 Linux interpreter；找不到时开发者测试精确 skip，测试不会下载或安装依赖。内部测试变量 `BATCH_SMOKE_REQUIRE_WSL=1` 用于 release gate，此时缺少 WSL root 或合格 interpreter 会失败而不是 skip；它不是项目用户配置。带时间戳的证据断言所有 acquisition 完成后才加载 ASR、ASR 与 alignment command 串行、worker shutdown 先于所有 burn，且 prepare/burn 各自峰值为 4。它不替代真实 CUDA、ffmpeg、网络、LLM 或媒体质量验证。

### 完成通知

`pipeline.ps1` / `pipeline.sh` 独立运行时，任务成功响成功铃，错误退出响错误铃。stage-aware batch 直接运行阶段 runner，不再使用整条 pipeline 的内部静默协议；每个最终失败或中断任务响一次错误铃，全部任务终态后再按聚合结果响一次。任一失败时退出码为 `1`，用户中断返回 `130`；帮助和 dry-run 不响铃。Linux/WSL 使用终端 BEL，是否有声音取决于终端的响铃设置。

## 主流程

1. `download.ps1/.sh` 下载原片、封面、`.info.json`、`.description`、`.tags.txt`，然后把原片改名为 `<name>.original.<ext>`
2. `prepare-video.ps1/.sh` 接收原片路径，统一重编码出编辑用 `<name>.mkv`
3. `whisper.ps1/.sh` 对编辑版 `<name>.mkv` 调用 `whisperx --output_format json`，输出 `<name>.json`
4. `translate_srt.py --only-beautify` 美化 JSON 里的 word 时间轴并回写 segment，输出 `<name>.beautified.json`、`<name>.scenes.json`、`<name>.scenechange.txt`
5. `translate_srt.py --only-glossary` 读取整句 transcript 和元数据，重新生成并覆盖 `glossary.md`
6. `translate_srt.py` 使用整句 JSON 翻译
7. AI 分割后用每个源语言 split 的首尾 word 匹配美化后的 `words[]` 回填时间，再对 split events 做最终校对，输出 `.split.<source>.srt` / `.split.<target>.srt` 和最终 ASS；显式 `--no-split` 时也继续输出 ASS
8. `ffmpeg-burn.ps1/.sh` 使用双语 `.ass` 硬压字幕；pipeline 默认回到 `<name>.original.<ext>` 原片压制

成果物链：

```text
<name>.original.<ext> + <name>.mkv -> [batch: <name>.asr.json ->] <name>.json -> <name>.beautified.json
-> <name>.web_evidence.json + glossary.md
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
- `<name>.web_evidence.json`：Tavily 网页证据 sidecar，保存规范化 query、域名、URL、标题和证据摘要，用于 embedding 检索
- `<name>.split.<source>.srt`：分割后、最终校对后的源语言 SRT 检查稿
- `<name>.split.<target>.srt`：分割后、最终校对后的目标语言 SRT 检查稿
- `<name>.<source>.proofread.ass`：最终校对源语言 ASS
- `<name>.<target>.ass`：目标语言 ASS
- `<name>.<source>-<target>.ass`：双语 ASS
- `<name>.<target>.description`：目标语言简介

`SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名，例如 `en`、`en-US`、`Japanese`、`Chinese Simplified`。输出文件后缀会通过 `langcodes` 规范为 ISO 639 代码，例如 `English -> en`、`Japanese -> ja`。未显式设置 `SOURCE_LANG` 时，脚本使用 WhisperX JSON 中的 `language`；`TARGET_LANG` 默认 `zh`。

翻译、分割、校对按顺序执行：先用整句 JSON 翻译保留语义，再用未校对源语言文本分割并对齐词源时间轴，最后对已分割的 subtitle events 做双语校对。所有批量 LLM 阶段的 user prompt 都是 JSON object，顶层包含 `items` array，返回也必须是同形态 JSON object；`items` 内只使用 `id` 和源/目标 ISO 639 语言代码 key，例如 `en`、`zh`。分割阶段默认给 pending segment 附带前后各 1 条 `context_before` / `context_after`，仅用于理解语义和节奏，远端只返回 pending item 本身；可用 `--split-context-window` 调整。分割完成后，脚本用每个源语言 split 的首尾 word 顺序匹配美化后的 `words[]`，对齐每条显示字幕的起止时间。如果缺标号、源/目标段数不齐、源语言片段无法还原未校对整句或首尾 word 无法对齐词级时间轴，脚本会丢弃该分割结果并回退到整句 beautified 时间轴，不做本地强切。`.beautified.json` 会用 `split_status` 记录状态：`ok` 为有效分割，`fallback` 为分割失败后整句回退且可重试，`unsplit` 为低于阈值或合法保留整句；`split_reason` 保存原因码，`split_reason_detail` 保存具体诊断文本。

默认模板以 1080p 双语观看为基准：`bi-zh` / `bg-bi-zh` 字号为 68，`bi-en` / `bg-bi-en` 字号为 44；AI 分割默认在源文超过 72 字符或 3.8 秒时触发。beautify 只负责词级时间轴吸附和边界修复，不再提供本地硬截整句参数。

`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 可以使用 `${SOURCE_LANG}`、`${TARGET_LANG}`、`${SOURCE_LANG_CODE}`、`${TARGET_LANG_CODE}` 模板变量；加载时由 `translate_srt.py` 替换。`glossary_prompt.md` 只用于微调 glossary 内容策略，`split_prompt.md` 只用于微调分割风格，输出格式由 `translate_srt.py` 固定注入。配置 Tavily 搜索时，glossary 会优先依据网页搜索结果校正 ASR 中可能误识别的人名、标题、引文和术语；原始网页证据会另外保存到 `<name>.web_evidence.json`，供后续 embedding 检索使用，而不是直接常驻注入 prompt。

## 配置

运行 `setup.ps1` / `setup.sh` 会自动从 example 创建缺失的 `.env`、`providers.json`、`tavily_domains.json`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 和 `template.ass`。旧版本升级时，setup 会把 `.env.example` 中新增但你本地 `.env` 缺失的变量追加到 `.env` 末尾，不覆盖已有配置。

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
| `PROOFREAD` | `1/0` 控制双语校对 |
| `PROOFREAD_PROVIDER` | 校对专用 provider |
| `PROOFREAD_MODEL` | 校对专用模型 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `TAVILY_API_KEY` | glossary 联网搜索 |
| `TAVILY_MAX_RESULTS` | Tavily 搜索结果上限 |
| `TAVILY_MAX_QUERIES` | glossary 联网搜索预算；tool-call 路径下是最大 Tavily tool 查询次数，fallback 路径下是单一语言 query 上限，`0` 禁用 Tavily 搜索 |
| `PIPELINE_SKIP_*` | 流水线阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |

`BURN_OVCOPTS=source-bitrate` 会用 `ffprobe` 读取源视频码率，并用 VBR 的 `b/maxrate/bufsize` 让硬字幕输出尽量接近源码率；显式设置 `qp=20`、`crf=23` 等会覆盖自动模式。`BURN_OAC` 默认 `aac`，兼容 ffmpeg 和 mpv 的硬字幕压制。

配置 `TAVILY_API_KEY` 时，glossary 阶段默认使用两段式 tool calling：脚本第一轮把 metadata、transcript/retrieved context 和 `tavily_domains.json` 域名偏好一起交给 glossary 模型；模型按需请求 `tavily_search`，脚本执行 Tavily 后把结果作为 tool message 喂回同一 session。搜索完成后，脚本新建无工具 finalizer session，只喂用户 JSON、transcript/retrieved context 和已收集的 `web_evidence`，要求模型生成最终 glossary。tool-call 路径下，`TAVILY_MAX_QUERIES` 控制最多执行多少次 Tavily 查询；fallback query-agent 路径下，它仍表示每种语言最多生成多少条 query。

如果 `glossary.md` 已缓存但 `<name>.web_evidence.json` 缺失，且 Tavily 可用，脚本会补建 sidecar 而不重写 glossary。

`GLOSSARY_PROVIDER` / `GLOSSARY_MODEL` 独立控制术语知识库阶段使用的 LLM；这个阶段会决定搜索什么、相信哪些网页证据、如何修正 ASR 错误、核心术语如何定译，并会影响后续翻译和校对记忆。请优先给它配置当前可用的最强、最顶级模型，而不是为了省成本使用小模型。只运行 `--only-glossary` 时，可以只配置 `GLOSSARY_PROVIDER` 和对应 API key；完整翻译流程仍需要 `TRANSLATE_PROVIDER`。

glossary tool 阶段会强制移除 provider `request_kwargs.response_format` 中的 JSON mode 参数，以免干扰 tool calling；finalizer 首选返回 `{"markdown": "..."}` JSON object，若 provider 无法稳定输出 JSON，可返回 `<GLOSSARY_MARKDOWN>...</GLOSSARY_MARKDOWN>` 标签块。普通散文和伪 tool call 文本都会被拒绝并重试。

Tavily tool 本地仍采用域名优先策略：脚本结合模型给出的 query / `topic_hints`、metadata 与 `tavily_domains.json` 中的全局百科域名、题材关键词和站点执行 `include_domains` 搜索；如果结果不足，再执行普通 Tavily 搜索；最终合并去重时会优先保留百科/知识库域名结果。`tavily_domains.json` 由 `tavily_domains.example.json` 初始化，用户可以自行添加题材、关键词和站点。

`glossary.md` 是全局硬规则：一旦存在，会完整常驻注入后续翻译、校对和视频简介翻译的 system prompt，不会因为启用 embedding 而省略。启用 `EMBEDDING_ENABLED=1` 时，Chroma 索引会额外保存 `glossary.md` 项目知识、`web_evidence.json` 网页证据、源文 transcript chunk 和翻译/分割后生成的双语 translation memory chunk；这些按当前字幕逐条召回为 `retrieved_context`，只作为动态补充记忆。校对阶段会用源文+译文一起检索，以保持术语和译风一致。`glossary.md` 会由本地脚本直接前置 YouTube 原视频元信息，包括标题、作者、上传时间、简介和标签；`web_evidence:*` chunk 来自 `<name>.web_evidence.json` 中的规范化 Tavily 结果，保留 query、域名、标题、URL 和证据摘要。索引会自动按 Markdown 标题切分 glossary；transcript chunk 使用干净字幕文本建向量，但返回给 LLM 的 retrieved context 会带 segment 时间码，并按末尾时间窗口自动 overlap，避免长视频上下文断裂。重建索引前会清理当前项目旧 chunk，避免残留结果污染检索。

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
| `torch` / `torchaudio` | setup 按 `.env` 的 `TORCH_BACKEND` 安装 CUDA 12.8 或 CPU wheel |

## 注意事项

- `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 已 gitignored
- 不要把 Python 包安装到系统环境；Windows 运行 `.\setup.ps1`，Linux/WSL 运行 `./setup.sh`，它们会创建/更新仓库 `.venv`
- 运行 pipeline 或任一 Python 相关脚本前必须先完成 setup；`py_launcher.ps1/.sh` 只允许启动 `translate_srt`、`merge_ass`、`batch`，并统一使用项目 `.venv`。`translate_srt.ps1/.sh`、`merge_ass.ps1/.sh`、`batch.ps1/.sh` 是其薄包装器，不调用全局 `python` / `python3`，也不要求用户设置 PATH，可从任意工作目录调用
- `TORCH_BACKEND=auto` 会用 `nvidia-smi` 检测 NVIDIA GPU；NVIDIA 用户可设 `cuda128`，AMD/无独显用户设 `cpu`
- `cookies.txt` 通过相对路径引用，请在仓库根目录运行脚本
- [Issue #12](https://github.com/The-Bazzar/Subtitle-translation/issues/12) 起，`download.ps1/.sh` 只输出 `OUTPUT_RENDER_VIDEO=<name>.original.<ext>`，不再生成编辑版。直接调用 download 的用户必须按 [MIGRATION.md](MIGRATION.md) 把该路径传给 `prepare-video.ps1/.sh`；prepare 成功后只输出 `OUTPUT_VIDEO=<name>.mkv`。若目录中已有 `<name>.original.mkv`，download 只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`。
- `prepare-video.ps1/.sh` 优先使用 `h264_nvenc` 重编码视频，未检测到可用 NVIDIA GPU 或 NVENC 编码器时回退 `libx264`，并统一用 `aresample=async=1:out_sample_fmt=s16` + `flac` 重建音频时间轴。若 `h264_nvenc` 返回非零退出码但已输出非 0B 文件，脚本会保留该文件并继续，不再回退重编码。
- 完整翻译流程必须配置 `TRANSLATE_PROVIDER`；只构建 glossary 时可改用 `GLOSSARY_PROVIDER`
- WhisperX 首次运行会下载模型
- 默认不硬压，推荐先人工校对 ASS，再决定是否压制
- `.srt` 已退出主流程；不要再把 SRT 当作翻译输入
