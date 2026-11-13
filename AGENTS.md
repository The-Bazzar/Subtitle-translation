# AGENTS.md

本文件是项目技术行为、代码路径和协作约束的唯一权威文档。代码以当前仓库为准；不要引用历史脚本或实际视频项目路径。

## Overview

项目现在由可安装的 Python CLI 统一实现：

```text
download(original) -> prepare-video(edit mkv) -> whisper(json)
-> scene detection(sidecars only) -> glossary -> translate(bilingual semantic events) -> target proofread
-> ass -> burn
```

WhisperX `.json` 是唯一主字幕输入，SRT 只作为检查输出。远程 LLM 只负责 glossary、翻译、分割和校对；下载、ASR、时间轴、文件和硬压由本地 Python stage 负责。

### Experimental Post-Translation MFA

MFA_ENABLED 是 translate/pipeline/batch 的唯一用户启用入口，包内模板默认 1，显式 0 可关闭；不再提供 --mfa/--no-mfa。已有配置不由 setup/init 覆盖，完全缺失该键保留旧关闭回退，setup 会追加缺失键。内部配置快照传递已解析的阶段选择，缺失 runtime/媒体时仍报错、不自动安装或静默回退。受管理工具支持 Windows x86_64 原生、Linux x86_64 或显式选择的 Windows/WSL。

MFA_ENABLED=1 时为 glossary -> translate（联合双语语义断句）-> 窗口 corpus MFA -> scene detection -> 可选纯译文 proofread -> ASS。窗口规划在 windowed_mfa.py，来源粗时间合并跨段/共享来源窗口，上限 60 秒/12 单元、零额外 padding；固定一路 CPU，总预算 max(300 秒, 4 * 音频秒数)。文本翻译检查点独立于最终定时缓存，pending 事件不能导出。场景检测只产出 sidecar，不改双语事件或词轴。词时长超过 2 秒或词间空洞超过 3 秒保守降级，这些是待校准的本地声学保护，不是给 LLM 的数学断句要求。关闭 MFA 时仍投影 WhisperX 词轴，但也移除独立 split；batch generation/锁/发布不变，多引擎暂缓。详见[当前架构](docs/v2.1-architecture.md)与[联合翻译协议](docs/superpowers/specs/2026-11-13-semantic-bilingual-translation.md)。

Windows 主机必须使用 PowerShell 7。旧版 Windows PowerShell 5.x 会导致 `.ps1` 报错：

```powershell
winget install Microsoft.PowerShell
```

## CLI

最新 CLI 契约见 `docs/cli-and-env.md`：新增 subtitle-translation help 打印全部真实子命令帮助。命令行仅保留输入/输出路径、URL、语言、配置根和 --only-* 模式，--help/--version 元信息保留；开关及调优参数全部移至 .env，旧 --mfa/--no-mfa、proofread/no-proofread、skip/quiet/force/dry-run、batch-size、模型/设备/编码器和场景小参数不再解析。下方早期命令行覆盖规则由此取代。translate 使用 TRANSLATE_BATCH_SIZE、TRANSLATE_CONTEXT_WINDOW、SPLIT_ENABLED、SCENE_THRESHOLD、SCENE_MIN_INTERVAL_FRAMES、LOG_LEVEL、FORCE_REBUILD 等；pipeline/batch/burn 的 DRY_RUN 也从配置读取。内部阶段通过配置快照调用，不拼接隐藏开关。only 模式显式执行所选阶段。

安装后推荐使用 console script：

```text
subtitle-translation pipeline "https://www.youtube.com/watch?v=*"
subtitle-translation batch "https://www.youtube.com/watch?v=1" "https://www.youtube.com/watch?v=2"
subtitle-translation translate "path/to/video.json"
```

也可以在仓库虚拟环境未激活时使用：

```powershell
& "<repo>\.venv\Scripts\python.exe" -m subtitle_translation pipeline "URL"
```

```bash
"<repo>/.venv/bin/python" -m subtitle_translation pipeline "URL"
```

`pipeline`、`batch`、`translate`、`merge-ass`、`download`、`prepare-video`、`whisper`、`burn` 和 `setup-mfa` 都是正式子命令。`subtitle_translation.cli` 只负责参数分派；业务逻辑位于 `core/` 下的 package、`translate_srt.py` 和 batch scheduler。

## Repository Layout

```text
├── pyproject.toml
├── core/
│   ├── subtitle_translation/
│   │   ├── cli.py             # console entry point and dispatch
│   │   ├── config.py          # .env/project configuration
│   │   ├── process.py         # argv process execution and active process registry
│   │   ├── stages.py          # download, prepare, whisper, burn
│   │   ├── pipeline.py        # single URL orchestration
│   │   ├── notifications.py   # dependency-free bells
│   │   └── examples/          # packaged env, provider, prompt and ASS examples
│   ├── translate_srt.py       # JSON working copy + scene detection + glossary + LLM stages + ASS export
│   ├── merge_ass.py           # ASS merge implementation
│   ├── batch.py               # compatibility Python import entry point
│   ├── batch_runtime.py       # batch CLI and direct Python stage runners
│   ├── batch_scheduler.py     # resource and ASR wave scheduler
│   ├── batch_cache.py         # ASR fingerprint and recovery sidecars
│   └── whisper_worker.py      # spawned WhisperX worker
├── scripts/                   # all PowerShell/bash setup and compatibility wrappers
├── docs/
└── tests/
```

所有 PowerShell/bash 文件必须位于 `scripts/`。它们只负责安装、定位项目 `.venv`、转发参数和返回退出码，不得重新实现 stage、解析 marker 或启动整条 pipeline。不能要求用户设置 PATH，也不能在脚本中调用全局 Python。
旧 ffmpeg-burn/mpv-burn 别名只转发 burn，不注入已删除的 --backend，统一读取 BURN_BACKEND。

## Pipeline

时间轴美化/吸附及相关参数已删除，仅保留只读场景检测。--only-scenes 显式检测，PIPELINE_SKIP_SCENE_DETECTION 控制是否跳过；SCENE_THRESHOLD/SCENE_MIN_INTERVAL_FRAMES 在 .env 调优。检测不改时间/文字/事件，不失效有效译文。ASR/MFA 时间源、粗时间重叠协调及旧词轴清理不变，.beautified.json 仅保留命名兼容。

pipeline 固定 download -> prepare-video -> whisper -> translate；默认内部为场景检测 -> glossary -> 联合双语翻译/断句 -> ASR 词轴投影 -> 可选纯译文校对 -> ASS。无独立 split，是否 burn 仅由 PIPELINE_SKIP_BURN 控制。

- download 保留 `<base>.original.<ext>`，并保存封面、`.info.json`、`.description`、`.tags.txt`。
- prepare 生成编辑用 `<base>.mkv`；保持 CPU decode，优先 NVENC，失败时按现有策略回退 CPU 编码。
- whisper 输出原始 JSON，兼容 ASR 词轴路径使用 segments[].words[]；MFA_ENABLED=1 时不依赖 ASR 原生词轴。
- 场景检测只读取视频，输出 .scenes.json/.scenechange.txt，不修改字幕句段、词或事件时间。.beautified.json 保留工作文件名兼容；时间源仅为原始 ASR 或后置 MFA。
- glossary 位于翻译之前；存在非空 `glossary.md` 时普通 pipeline 复用，`--only-glossary` 默认重建，GLOSSARY_REUSE=1 时复用。网页证据另存 `.web_evidence.json`。显式 `--only-glossary` 还生成 `<base>.source_review.json` 指针及 `.asr_work/source_review/` 下的不可变术语/采用稿快照；原文审阅使用既有 glossary Provider，最多 8 项/批、120 秒/次、最多重试一次，失败逐项保留实际原文。该采用稿暂存但不覆盖 WhisperX JSON/词轴。默认 ASR 词轴路径只整理术语，不将未对齐的编辑稿套入旧轴；MFA 模式才在翻译前采用 source_review。
- 默认和 MFA 模式均由 translate 同回合返回整句译文与成对源/目标字幕片段，所有句段均参与，不提供边界/数学任务。SPLIT_ENABLED=0 保留整句；旧分段参数全部移除。
- bilingual_translation.py 的阶段提示词以语义和可读性为主，本地才生成 ID/源文范围并对轴。不检验目标片段拼接与整句译文逐字符相等，空格/标点差异不再取消分割。源文不可定位或片段结构无效但整句译文有效时回退整句，原因仅日志，不写 split_reason/detail。完全缺少实际整句译文仍有界重试/失败，不导出空字幕。旧 split helper 已删除，split_prompt 不再加载为运行阶段。
- translate 的每次实际 LLM 对话轮次在 stderr 即时记录项目、轮次、尝试、句段 ID、字符数、耗时及校验/分割汇总。校验后逐句段/event 记录编号、稳定 ID、split/unsplit/whole_fallback 和当前时间（MFA 前为 pending），失败句段显示 retry/failed。逐 event 始终完整输出采用的原文/译文，仅按 LOG_LEVEL 过滤，字符串控制字符经 JSON 转义；正常 split/unsplit 为 INFO，回退/重试/失败等异常状态为 WARNING，任务级错误仍为 ERROR。重试独立计轮次，异常仅记录类型，取消继续传播。缓存命中也逐 event 输出并注明 cache=hit，不伪造 LLM 轮次；不输出 API key、请求头、prompt、glossary、检索上下文或未采用原始回复，不影响 stdout marker。日志不参与 prompt、缓存身份或字幕 JSON。 统一 Loguru 格式包含时间/时区、级别、项目与阶段，LOG_LEVEL 默认 INFO，统一控制终端和文件阈值。单项目日志为视频目录 subtitle-translation.log（--video 优先，否则 JSON 目录），batch 全部集中于执行目录 batch-result.log，包含 worker 故障诊断。每轮覆盖一次、内部阶段复用，不轮转；不得使用配置根或让 batch 各项目另建日志。详见 [运行日志](docs/logging.md)。
- 纯译文 proofread 仅更新目标语言，整句与事件译文同一回合保存，但不要求严格拼接相等；不改源文、ID/事件数量/时间，不回调上游。旧 proofread_text 不再覆盖源文。prompt example 文案保持不变，代码级阶段协议优先。
- MFA_ENABLED=0 为兼容 ASR 词轴路径；模板默认 1 为后置窗口 corpus MFA，仅支持英文源文。缺少 runtime/媒体时在语义请求前报错，不静默回退；跳过 knowledge 透传原稿后继续，关闭校对不跳过 MFA。
- MFA 路径在翻译前用 pySBD 组织完整语句上下文，translate 同回合生成原样源文/目标字幕片段，不以 ASR 分段/词轴为语义边界。事件时间缺省且 pending，后置按字符映射取首尾词时间，加回裁剪 sample 偏移一次；不可分离词映射不插值。失败保留整句双语内容与来源粗时间，不使用漂移区间、不撤销定稿或重发翻译；粗时间事件不场景吸附，原 batch artifact 保留。
- 相邻定时输出重叠且至少一个为 coarse 时，合并为一条完整双语粗时间事件，保留连续定稿、整句译文、来源与包络，不猜词时间，不改文本检查点。所有派生字幕 JSON 去除根级旧 word_segments，仅 segments[].words[] 为正式工作词轴；旧兼容检查点清理该字段不丢有效翻译，原始 ASR 文件不改。
- 不再提供显示留白/吸附/时长与间隔参数；coarse-output-scene-only-v3 恢复对齐原时间，只失效旧显示投影，语义和原生 MFA 缓存可复用。场景检测设置只更新其独立 sidecar。
- MFA 桥接只执行窗口 corpus，旧单窗口/全文及 partial 解析入口已删除；CLI 将当前项目全部有效窗口组成一个 corpus，一次 align 和一次批量导出，固定 num_jobs=1，不为窗口启动独立 MFA。身份为 direct-window-corpus-align-v1，绑定完整有序 corpus、窗口 ID/音频/定稿、模型/环境和共享自适应；不能混用旧单窗口或全文缓存。命中只做一次环境探测并验证导出/结果 digest；单个缺失或无效 TextGrid 只使对应窗口降级，命令/全局协议失败按 corpus 失败处理。文本规划/翻译身份不变，不为此重发 LLM。详见 `docs/superpowers/specs/2026-11-13-windowed-mfa-corpus-batching.md`。
- 输出为 `.split.<lang>.srt`、`.<source>.proofread.ass`、`.<target>.ass`、`.<source>-<target>.ass` 和目标语言 `.description`。
- ASS/SRT 序列化先按各自精度取整总时间，再分解时/分/秒，正确处理小数进位；这不是时间美化，不修改内部词轴。
- 元数据翻译从当前项目同名 `<base>.original.mkv` 的 ffprobe JSON 读取内嵌章节，不回退到编辑版/其他项目。章节标题与标题、简介、标签在同一次请求翻译，使用完整 glossary；按本地章节 ID 恢复时间顺序，时间仅由原片提供，向下取整后以 `HH:MM:SS 标题` 写入目标 description 的“章节”区块。ffprobe 优先使用所配置 ffmpeg 同目录工具，探测超时 30 秒；无原简介但有同名原片也可触发元数据翻译。提取失败不阻塞其他简介内容，缺失/无效/重复 ID 的章节译名逐项使用原标题。该功能不改媒体、词轴或 batch generation。

## Batch Resource Rules

batch 不接受 `-j`、`--jobs`、`--io-jobs` 或 `MaxJobs`，容量自动计算：

- CPU/IO 并发：`max(1, (os.cpu_count() or 1) // 4)`。
- prepare 与 burn 使用同一 NVENC 资源池，固定最多 4 路。
- NVENC 与 WhisperX 不同时运行。
- WhisperX 全局串行；一个 spawned worker 复用 ASR/alignment 模型。
- 不重排用户任务，阶段优先级固定为 download/prepare、WhisperX、burn。
- worker 意外退出立即 fail-fast：关闭新 worker-stage admission，当前任务失败，等待 ASR/alignment 的任务 blocked；已完成 alignment 的任务仍可 postprocess/burn。
- 第一次 Ctrl+C 停止接纳新 stage 并等待当前 stage；第二次终止活动进程树和 worker，返回 `130`。
- 每个失败任务响一次错误铃，batch 最终再按聚合结果响一次；全部成功响成功铃。
- batch 与 pipeline 的硬压开关仅由 PIPELINE_SKIP_BURN 控制，不提供 CLI 开关，未配置时沿用启用硬压的回退。
- batch 必须读取 `PIPELINE_SKIP_SCENE_DETECTION`、`PIPELINE_SKIP_KNOWLEDGE` 和 `PIPELINE_SKIP_TRANSLATE`；skip translate 要求项目目录已有约定命名的双语 ASS。URL batch 无法表达 per-task 现有输入，`PIPELINE_SKIP_DOWNLOAD` / `PIPELINE_SKIP_WHISPER` 为 `1` 时必须启动前报错。
- batch glossary 与 pipeline 使用相同缓存语义；只有用户显式执行普通 `--only-glossary` 时才强制重建。
- 启用 MFA 的 batch 在同一现有 postprocess CPU/IO 份额内调用协调后的完整新链路，不先执行旧的独立 场景检测/glossary；MFA 固定一路 CPU job。原 WhisperX worker 和其恢复协议暂保留，输出词轴在 MFA 模式下不作为正式词源。MFA 模式的续阶段、模型请求及产物提交检查相同 admission guard，取消不能被转换成文本/对轴 fallback。

`.prepare.json`、`.asr.json`、`.asr.lock` 和 generation candidate 是恢复协调文件。cache fingerprint、media generation、alignment generation、锁和原子 publish 语义不得改变；损坏、旧版或 fingerprint 不匹配必须视为 cache miss。

## LLM and Data Contracts

- 所有 user prompt 都是 JSON object，顶层 `items` array；返回必须是 JSON object，item 使用 `id` 与源/目标 ISO 639 语言代码。
- `glossary.md` 是完整常驻的全局硬规则；`retrieved_context` 只能动态补充，不得截断或替代 glossary。
- 语言可为任意有效源/目标组合；代码通过 `langcodes` 规范文件后缀。
- `.beautified.json` 保存整句 `translation`、带稳定 `id`/segment-local `source_range` 的 `split_events`、`split_status`，以及 `event_plan_key`、`translation_key`、`proofread_key`；语义分割回退原因只写日志。英文事件已填不代表翻译完成。旧工作缓存缺少新输入/规划身份时重建；原始 WhisperX JSON、batch sidecar 和 generation 协议不改变。
- sidecar/web evidence/embedding 只能按当前项目与 generation 检索，不得污染另一个项目。
- embedding 建索引和查询对所有模型统一使用单文本字符串 `input`，多个文本按原顺序串行请求，每次响应必须恰好有一个向量。Chroma 写入批次由内部常量控制，不提供环境变量；API 重试使用 OpenAI SDK 的默认策略，失败沿用原有索引错误处理。
- MFA 桥接只接受本地明确版本的 3.4.x profile、绝对工具命令及本地声学模型/词典，解析器在 MFA 环境通过 praatio 读取 TextGrid word tiers；不在 CLI 环境导入重型 MFA 依赖。调用方负责资源 admission，桥接固定 `num_jobs=1`，不创建新并行池。受管理进程支持超时 `124` 和进程树回收，取消继续向上传递。
- 当前受管理安装锁定 MFA 3.4.3、CPU torch/torchaudio 2.8.0 和 SpeechBrain 1.0.3，预缓存 VAD；任务中禁用 HF 下载和 GPU。Windows 默认原生独立工具环境，仅显式 --distribution 时使用已有 WSL；Linux 使用本机独立工具环境。Windows torch/torchaudio 使用官方 CPU wheels，其他声学依赖使用 conda-forge；VAD 用 COPY 避免符号链接权限。不初始化 shell 或要求手工 PATH。runtime、工具、模型和样本均为本地生成物，禁止提交。

## Configuration and Local Files

Windows 原生 corpus 执行与缓存目录分离：SQLite、临时特征及 TextGrid 位于 runtime.work_directory/r/<完整 UUID> 短目录，过长时选系统临时目录的 subtitle-mfa/r/<完整 UUID>；为 MFA 派生路径预留 100 字符并限定总预算 240。缓存仍使用完整 SHA-256 和原生成事务，generation/execution.json 只记录执行目录诊断。缓存命中不分配新执行目录，失败/取消不发布成功缓存；不删旧工作目录、不截断身份、不修改 Linux/WSL 路径或语义检查点。详见 [原生执行目录](docs/superpowers/specs/2026-11-13-native-mfa-short-work-directory.md)。

MFA 工具进程必须隔离用户 site-packages，并清除继承的 PYTHONPATH/PYTHONHOME；native 与 WSL 均设置 PYTHONNOUSERSITE=1，不修改用户全局环境。固定 SpeechBrain VAD 模型优先复用完整快照，使用公开 YAML/Pretrainer/VAD 接口加载权重，COPY 到输出目录，不加载或伪造不存在的 custom.py；缺失必要权重仍失败，不发布就绪 manifest。

控制台日志固定 ANSI 配色，包含按级别着色的 level/message、时间及项目/阶段色彩；不按 TTY 或 NO_COLOR 关闭，不提供颜色配置项。单项目与 batch 文件 sink 始终纯文本，不写 ANSI 控制码。

包内 `.env.example` 的 `PROOFREAD=0`，由模板生成的新配置默认关闭校对；已有 `.env` 保留原值。需要校对时显式设置 `PROOFREAD=1`，不借此更改变量缺失时的 CLI 兼容回退逻辑。

`scripts/setup.ps1` / `scripts/setup.sh` 会从 `core/subtitle_translation/examples/` 创建缺失的 `.env`、provider、domain、prompt 和 template 文件；同一目录也作为 wheel package data，是 example 的唯一权威。旧 `.env` 只追加缺失变量，不覆盖已有值。setup 使用 `uv` 创建并清空项目 `.venv`，同步 pyproject 依赖和用户选定的 torch backend，并安装一个转发到项目 `.venv` 的全局 `subtitle-translation` shim。不能要求用户手动设置 PATH，也不能为 CLI 复制第二套 Python 环境。

可独立执行 `subtitle-translation setup-mfa`，或在 setup.ps1 使用 `-InstallMfa` / setup.sh 使用 `--mfa` 准备工具环境并原子注册本地 `asr_runtime.json`。Windows 可用 `--distribution` 选择已有 WSL 发行版；不自动安装/切换 WSL。模型采用官方固定发布资源，VAD 记录实际 revision 与文件 hash；版本不匹配、下载或预加载失败不得覆盖已就绪 manifest。安装说明见 [MFA 配置](docs/mfa-installation-preferences.md)。

模型解析统一遵循 `非空 *_MODEL > 对应 provider.default_model`。glossary / proofread 只有在专用 provider 也留空时才复用翻译 provider 和 model；embedding 不得硬编码跨 provider 的默认模型。

本地文件和生成物禁止提交：`.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、本地 prompt、`template.ass`、视频、字幕、glossary、sidecar、`chroma_db` 和 batch report。

download、pipeline 和 batch 只读取命令启动时的执行目录下的 `cookies.txt`（`ProjectConfig.output_dir`），获取标题与下载/元数据刷新使用同一 cookies。缺失时不传 `--cookies`，不得回退到安装目录或 `--project-dir` 配置根。

每次 yt-dlp 获取标题、下载或刷新元数据之前，必须先使用同一 executable、同一执行目录运行 `yt-dlp --rm-cache-dir`。清理失败即返回该下载阶段失败，不执行后续 yt-dlp 操作。此缓存清理不改变 ASR、generation、glossary 或其他项目缓存协议。

`--project-dir` 只决定配置根，不得改变输出根。download/pipeline 默认把新项目创建在用户执行命令时的当前目录；batch 默认报告也写入当前目录。

## Testing and Documentation

网络、LLM、yt-dlp、ffmpeg、WhisperX 必须 mock。修改 `core/translate_srt.py`、setup、入口、缓存或并发调度时运行全量：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

```bash
./.venv/bin/python -m unittest discover -s tests
```

行为变更必须同步 README、MIGRATION 和本文件。`*_prompt.example.md` 由 `The-Bazzar/prompt` 仓库维护，本项目不得修改其文案。任何触碰 D1-D12 设计不变量的变更必须附 `docs/superpowers/specs/2026-08-21-python-cli-rewrite-design.md` 同类设计说明，并经 PR 评审。
