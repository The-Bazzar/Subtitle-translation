# AGENTS.md

本文件是项目的唯一权威文档。代码路径、行为、配置以当前仓库为准；不要引用历史脚本或本地视频项目路径。

## Overview

`download(original) -> prepare-video(edit mkv) -> whisper(json) -> beautify(json words) -> glossary -> translate -> split -> proofread -> ass -> burn`

Windows 主机必须使用 PowerShell 7，旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。升级命令：

```powershell
winget install Microsoft.PowerShell
```

项目使用本地工具完成下载、语音识别、时间轴处理和硬压，使用远程 LLM API 完成 glossary、翻译、分割和校对。主字幕入口是 WhisperX `.json`，SRT 不再作为输入缓存。

## Repository Layout

所有运行脚本位于仓库根目录：

```text
├── pipeline.ps1              # Windows: download -> prepare-video -> whisper -> translate_srt.ps1 -> ffmpeg-burn
├── pipeline.sh               # Linux/WSL: 同流程
├── download.ps1              # Windows: yt-dlp 下载视频和元数据
├── download.sh               # Linux/WSL: yt-dlp 下载视频和元数据
├── prepare-video.ps1         # Windows: 原片重编码为编辑版 mkv
├── prepare-video.sh          # Linux/WSL: 原片重编码为编辑版 mkv
├── whisper.ps1               # Windows: WhisperX 生成词级 JSON
├── whisper.sh                # Linux/WSL: WhisperX 生成词级 JSON
├── py_launcher.ps1           # Windows: 白名单 Python 共享启动器
├── py_launcher.sh            # Linux/WSL: 白名单 Python 共享启动器
├── merge_ass.ps1             # Windows: merge_ass 薄包装器
├── merge_ass.sh              # Linux/WSL: merge_ass 薄包装器
├── translate_srt.ps1         # Windows: translate_srt 薄包装器
├── translate_srt.sh           # Linux/WSL: translate_srt 薄包装器
├── translate_srt.py          # JSON 美化 + glossary + 翻译/分割/校对 + SRT/ASS 导出
├── ffmpeg-burn.ps1           # Windows: ffmpeg ASS 硬压
├── ffmpeg-burn.sh            # Linux/WSL: ffmpeg ASS 硬压
├── mpv-burn.ps1              # Windows: mpv 硬压备选
├── mpv-burn.sh               # Linux/WSL: mpv 硬压备选
├── batch.ps1                 # Windows: batch.py 参数透传包装器
├── batch.sh                  # Linux/WSL: batch.py 参数透传包装器
├── batch.py                  # stage-aware batch CLI 薄入口
├── batch_runtime.py          # batch CLI 实现与平台 runner
├── batch_scheduler.py        # 任务状态、资源容量、acquisition 与 ASR wave scheduler
├── batch_cache.py            # ASR fingerprint 与原子 recovery sidecar
├── whisper_worker.py         # spawn WhisperX worker 与 parent controller
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
├── MIGRATION.md             # breaking change 迁移说明
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

1. `download.ps1` 下载原片、封面、`.info.json`、`.description`、`.tags.txt`，把原片改名为 `<base>.original.<ext>`
2. `prepare-video.ps1` 接收原片路径并统一重编码出编辑用 `<base>.mkv`
3. `whisper.ps1` 对编辑版 `<base>.mkv` 调用 WhisperX，只输出 `<base>.json`
4. `translate_srt.py --only-beautify` 美化 JSON 中的 word 时间轴，输出 `<base>.beautified.json`
5. `translate_srt.py --only-glossary` 在翻译前强制重新生成并覆盖 `glossary.md`
6. `translate_srt.py` 整句翻译、AI 分割、词级对轴、split event 校对，输出最终字幕
7. `ffmpeg-burn.ps1` 可选硬压双语 ASS 到 `burned.mkv`

### Linux/WSL `pipeline.sh`

流程与 Windows 对齐，使用 `download.sh`、`prepare-video.sh`、`whisper.sh`、`translate_srt.sh`、`ffmpeg-burn.sh`。两个 pipeline 都实时透传各步骤输出。
prepare 失败时，`pipeline.sh` 精确透传 `prepare-video.sh 的原始退出码`，与 PowerShell 行为一致。

### Stage-aware Batch ASR

- `batch.ps1` / `batch.sh` 只负责把参数透传给 `py_launcher.ps1/.sh` 的 `batch` target；`batch.py` 是只委托 `batch_runtime.main` 的薄入口，跨平台调度逻辑统一位于 `batch_runtime.py` / `batch_scheduler.py`
- CPU/IO capacity 自动设为 `max(1, (os.cpu_count() or 1) // 4)`，用于 download、WAV 提取和 postprocess；prepare 与最终 burn 共用固定 `4` 路 NVENC capacity
- 不提供 `-j`、`--jobs`、`--io-jobs` 或 `MaxJobs`，启动时打印自动检测出的 CPU/IO 和 NVENC capacity
- acquisition 按任务流水执行 `download -> prepare-video -> extract-audio`；任务完成 download 后可立即等待 prepare，完成 prepare 后可立即提取 mono 16kHz WAV。匹配有效 `.asr.json` 的现存 WAV snapshot 会在重启时直接复用，缺失或变化才重新提取。prepare 和 WAV 提取/替换都持有同一媒体的 `<base>.asr.lock`，与 alignment 校验/提交串行，但不同媒体仍独立并发
- prepare 成功后原子写 `<base>.prepare.json`，用 UUID media generation 绑定原片与编辑版各自的 resolved path、size、mtime；准备前后原片快照不一致时拒绝认证。正常重启只有两份 snapshot 都仍匹配才跳过 prepare；模型/语言/options 变化只使 ASR cache miss，不重新编码视频
- 所有 acquisition 任务到达成功或失败终态后，scheduler 才启动一个 `multiprocessing.get_context('spawn')` worker；WhisperX 只在 child 内 import
- ASR wave 只把绑定 media generation 的不可变 WAV artifact 交给 worker；同一 worker 加载一次 ASR，串行处理所有未缓存任务，wave 结束后显式 `UNLOAD_ASR`，但不 shutdown。worker 在识别前和 sidecar 写入前都复核 prepare state、编辑版和 WAV snapshot，不能把旧 WAV 的结果认证给已变化的新编辑版
- 每个 ASR 成功任务通过 `batch_cache.py` 原子写 schema v2 `<base>.asr.json`；内容包含 media generation、唯一 ASR generation、WAV snapshot 和 fingerprint，fingerprint 包含编辑版 resolved path、size、mtime、Whisper model、compute type、源语言和 ASR options；所有 sidecar 写入与 alignment commit 共享 `<base>.asr.lock` 跨进程锁（POSIX `flock` / Windows `msvcrt`），损坏、缺少/非法 generation、旧版或 fingerprint 不匹配都视为无缓存
- fingerprint 有效的 `.asr.json` 跳过 `TRANSCRIBE`；detected language 通过 `langcodes` 验证并规范为稳定 ISO 639 主语言代码，`zz` / `zzz` / `und` / `unknown` 会拒绝，只有配置了有效 `SOURCE_LANG` 时才允许作为 fallback；缓存 ASR 与新 ASR 混合时使用同一分组规则
- alignment group 按 ISO language 稳定排序，组内保持原 task order；worker 一次只加载一个语言模型，同语言复用，组间执行 `UNLOAD_ALIGN -> LOAD_ALIGN`，`WHISPER_ALIGN_MODEL` 为空时由 WhisperX 自动选择，非空时覆盖
- parent 在 blocking thread 中先取得 media cache lock，再 dispatch `ALIGN`；command 只传 `.asr.json` path、expected generation、绑定的 WAV artifact 和 parent-owned candidate path。child 的 backend 输入严格使用 `artifact.wav_snapshot.path`，并在 backend 前后复核同一 artifact；只原子写隐藏的 generation-specific candidate 并回传 candidate path/generation，绝不覆盖 `<base>.json`。parent 持锁再次复核 WAV artifact、sidecar ownership、candidate path/schema，并用 per-task commit state lock 将取消与 destructive commit 线性化：`try_request_cancel()` 只做 non-blocking acquire，cancel-wins 保留 sidecar、只清 candidate；lock busy 表示 commit-wins，不再 abort，原子 promote final、删除 owned sidecar 后继续 postprocess
- alignment candidate、最终 `<base>.json` 和 `<base>.beautified.json` 使用 `_batch_artifact.media_generation` / `_batch_artifact.alignment_generation` 绑定来源。新 alignment generation 进入 destructive commit 时先失效并删除旧 beautified cache；完整 postprocess runner 持有 `<base>.asr.lock`，使同媒体跨 invocation 串行并保护稳定 SRT/ASS/glossary。postprocess 写本 generation 的隐藏 beautified candidate，结束前重验 final generation，匹配才原子 publish。过期/失败清理只删除本 candidate，禁止删除另一 invocation 已发布的新 beautified；candidate 清理失败进入 `cleanup_diagnostics`
- alignment 成功后删除 WAV，删除动作在同一媒体锁内完成；alignment 失败、取消或 worker crash 时保留 WAV。WAV 删除失败不覆盖已成功 alignment，但会进入 `cleanup_diagnostics`
- 每个 alignment 成功任务立即在 CPU/IO semaphore 中异步执行 beautify、glossary、translate，不等待其他 alignment；`--skip-burn` 成功终态为 `translated`，启用 burn 时翻译完成的任务进入 `burn_waiting`
- 所有 alignment terminal 后 scheduler 才 `UNLOAD_ALIGN -> SHUTDOWN -> join`；只有确认 worker process 已退出才设置 `worker_released` event。任何 burn 都必须等待该 event，且通过同一个固定 `4` 槽 NVENC semaphore 调用 `ffmpeg-burn.ps1/.sh`。已 ready 的 ASS 会立即进入 burn，较晚完成翻译的任务可动态加入，不要求等待全部 translation；runner 使用原片与最终 `<source>-<target>.ass`，同时验证 `OUTPUT_BURNED_VIDEO=` marker 和非空输出，成功终态为 `burned`
- active worker command 在 precommit cancel-wins 时 abort/terminate/kill，随后用 shield loop 忽略重复取消直到 request/transaction thread 真正结束，transaction thread 在 finally 清理 candidate、释放 lock，不再排队 unload/shutdown。commit-wins 同样等待 promote + owned sidecar delete 完成，该任务继续 postprocess；`worker_released` 只能在所有 `to_thread` request/transaction 与 worker controller/process cleanup 完成后设置。candidate cleanup/abort/close 异常进入 release diagnostics 和失败报告，不替换 cancel-wins 的原始 `CancelledError`；acquisition 取消且未创建 worker 时由 `run()` 外层 finally 设置 event
- `<base>.asr.lock` 是持久、最多 1 byte 的运行时协调 artifact，加入 `.gitignore` 且不会作为字幕或 cache 输入；不得在活跃任务间 unlink，以免产生 inode/handle 锁竞态
- worker 单任务异常返回结构化失败并继续后续任务；request 使用 Queue，response 使用 parent-recv / child-send 单向 Pipe，child 的 heartbeat 线程与主线程通过同一锁发送；active command 每 5 秒发送 request-scoped heartbeat，controller 以 30 秒 heartbeat silence 和内部 24 小时 operation watchdog 检测无响应，deadline 前先有界排空当前 request 已到达的消息，超时后 terminate/kill + join，且不自动重启。每个 spawned controller 拥有临时 stdout/stderr capture 文件；child 在进入 worker target 前同时重定向 Python stream 与 native fd 1/2。异常对象保留 bounded capture tail 供 scheduler 写日志，正常 close 删除临时目录
- worker unexpected exit/hung 会立即关闭新任务和 worker-stage admission：当前 worker task 标为 `failed`，等待 ASR/alignment 的任务标为 `blocked_by_worker_failure`；已 alignment 成功的 task 继续 postprocess，worker release 后仍可 burn。scheduler best-effort 写 invocation `Path.cwd()` 下的 `batch-worker-failure-<timestamp>.log`，包含 task/phase、CPU/IO 与 NVENC 队列快照、worker exit code、traceback，以及合并后的外部命令/worker stdout、stderr，写盘前移除 ANSI。日志先写 sibling temp 再原子 replace；mkdir/write/replace 失败只在 task state/drain 已完成后追加内存 `failure_log` cleanup diagnostic，不替换首个 worker 根因
- batch 文本报告旁边同时写同基名 JSON 机器报告；顶层包含 `worker_failure`、`worker_failure_log`、`worker_failure_root_cause`、`worker_failure_detail`、invocation `output_directory` 和 `cleanup_diagnostics`，每个 task 另有自己的 `output_directory`、终态、阶段、耗时与输出路径
- 外部命令 stdout/stderr reader 使用 64 KiB `read()` chunk，按 `\n`、`\r` progress 或 EOF partial framing，不使用 `readline()`；超过 64 KiB 的逻辑行拆成 bounded continuation `LogEvent`，确保 pipe 持续实时排空。唯一 printer 保持 queue 顺序与 `[02][prepare]` 前缀。queue 有固定上限并为 sentinel 预留一格；每个 task/stream 只保留 256 KiB tail，failure report 对截断显式写 marker
- 第一次 `Ctrl+C` 同步关闭 command admission 和 stage advancement。`_run_stage_command()` 必须在首次 spawn await 前取得同步 reservation：reservation 成功返回是 scheduler 的线性化点，该 command 计为 active 并可自然结束；没有 reservation 的 command 绝不调用 OS spawn API。这里不宣称 Python 与操作系统进程创建之间存在不可能的原子性。第二次 `Ctrl+C` 终止已注册的 child process trees、abort worker 并等待真实退出。precommit interrupt 使用 Task 5 的 cancel-wins/commit-wins 仲裁，保留完成输出和未消费 `.asr.json`；batch 中断返回 `130`
- CLI 保留多 URL、`-B/--burn`、`--skip-burn`、`-r/--report`、`-n/--dry-run`、`-p/--translate-provider`、`-tm/--translate-model`；provider/model 用于 postprocess，burn 默认启用且 `--skip-burn` 可关闭
- `batch_runtime.py` 直接读取项目 `.env`，优先级为显式 CLI / 进程环境 > `.env` > 硬编码默认；`FFMPEG_PATH_WIN` / `FFMPEG_PATH_LINUX` 为空或缺失时使用 `ffmpeg`
- 任一任务失败只终止该任务的后续 acquisition 阶段，其他任务继续；存在失败时聚合退出码为 `1`
- 跨平台 release smoke 使用 `tests/test_batch_smoke.py`：分别经过 `batch.ps1 -> py_launcher.ps1 -> batch.py` 与 `batch.sh -> py_launcher.sh -> batch.py`，运行真实 argparse/main、`ResourceLimits.detect`、subprocess stage runners、marker parser 和 spawned `WhisperWorkerController` 协议，并解析 JSON machine report 断言字段契约。smoke 将 `batch.py`、`batch_runtime.py`、production modules 和对应 wrappers byte-identical 复制到隔离目录并校验 SHA-256；只 fake download、prepare、translate、burn、ffmpeg 和 child 内 import 的 WhisperX 外部边界。WSL 路径由 `wsl -u root` 按 `BATCH_SMOKE_WSL_PYTHON`、仓库 `.venv/bin/python`、`command -v python3` 的顺序选择 Python `>=3.10,<3.14` 且能 import `langcodes` 的现有 Linux interpreter；没有候选时开发者测试精确 skip，测试不得下载或安装依赖。`BATCH_SMOKE_REQUIRE_WSL=1` 是仅供 test/release gate 使用的内部变量，不是项目用户配置；启用后缺少 WSL root 或合格 interpreter 必须失败而不是 skip。跨进程 wall timestamp 断言所有 acquisition 完成后才加载 ASR、prepare/burn 不与 worker ASR/alignment lifetime 重叠、ASR 与 alignment command 串行、同语言 alignment model 复用、worker shutdown 先于所有 burn，prepare 与 burn 各自峰值不超过 4。该测试不证明真实 CUDA、ffmpeg、网络、LLM 或媒体质量

### Task Notifications

- 独立运行 `pipeline.ps1` / `pipeline.sh` 时，成功响成功铃，错误退出响错误铃
- stage-aware batch 直接运行阶段 runner，不使用 pipeline 内部静默或退出码 marker 协议；每个进入失败或中断终态的任务各响一次错误铃
- `batch_runtime.py` 在全部任务终态后按聚合结果再响一次；全部成功响成功铃，任一失败响错误铃并以退出码 `1` 结束，用户中断以错误铃和退出码 `130` 结束
- help 和 dry-run 路径保持静默；Linux/WSL 使用终端 BEL，是否可听取决于终端设置

### Output Chain

```text
<base>.original.<ext> + <base>.mkv -> [batch: asr.json ->] json -> beautified.json -> web_evidence.json + glossary.md
      -> split.<source>.srt / split.<target>.srt
      -> <source>.proofread.ass / <target>.ass / <source>-<target>.ass
      -> burned.mkv
```

默认 `.env.example` 设置 `PIPELINE_SKIP_BURN=1`，推荐先人工校对字幕，再决定是否硬压。

## Step Behavior

### download

- 输出目录名和视频基名相同，视频路径形如 `<video_dir>/<video_dir>.<ext>`
- 下载后会保留原片为 `<video_dir>/<video_dir>.original.<ext>`，不再生成编辑版
- 如果 `<video_dir>/<video_dir>.original.mkv` 已存在，download 脚本视为原片已下载，只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`
- 脚本只输出 `OUTPUT_RENDER_VIDEO=<原片>`；此契约见 [Issue #12](https://github.com/The-Bazzar/Subtitle-translation/issues/12)，direct download 迁移见 `MIGRATION.md`
- 同步保存 `.png` 封面、`.info.json` 元数据、`.description` 简介、`.tags.txt` 标签
- SponsorBlock 移除 `sponsor,selfpromo`
- `cookies.txt` 通过相对路径引用，必须在仓库根目录运行脚本
- Windows 文件夹名会做 Unicode 标点和非法字符清理，避免引号、破折号等导致跨 Windows/WSL 路径乱码

### prepare-video

- 只接受一个 original video path，输出同目录下去掉 `.original` 后缀的 `<base>.mkv`
- 成功只输出 `OUTPUT_VIDEO=<编辑版 mkv 绝对路径>`；pipeline 前半段使用编辑版，burn 继续使用 download 返回的原片
- 固定做一次时间戳抚平重编码：保持 CPU decode，优先使用 `h264_nvenc -cq 12`，未检测到可用 NVIDIA GPU 或 NVENC 编码器时回退 `libx264 -crf 12`
- 音频统一用 `aresample=async=1:out_sample_fmt=s16` + `flac` 重建时间轴，并清理 metadata
- 若 `h264_nvenc` 返回非零退出码但已输出非 0B 文件，脚本保留该文件并成功结束，不再回退重编码
- 直接调用 download 的用户必须显式串联 prepare：PowerShell 使用 `& .\prepare-video.ps1 <OUTPUT_RENDER_VIDEO>`，Linux/WSL 使用 `./prepare-video.sh <OUTPUT_RENDER_VIDEO>`

### whisper

- 已存在 `<base>.json` 时跳过
- 视频先转为 mono 16kHz WAV，再调用 WhisperX
- standalone PowerShell/bash 参数对齐：`--output_format json`、batch size `8`，CUDA 使用 `float16`、CPU 使用 `float32`
- `.info.json` 中的 `language` 会用于 WhisperX `--language`；缺省回退 `en`
- 输出 JSON 的 `segments[].words[]` 是后续分割对轴的唯一词源
- `whisper.ps1` / `whisper.sh` 始终保持 standalone 最终 JSON 行为，不读取或写入 batch `.asr.json`
- batch 先在 worker 外提取 WAV，再执行 `LOAD_ASR -> TRANSCRIBE* -> UNLOAD_ASR -> (LOAD_ALIGN -> ALIGN* -> UNLOAD_ALIGN)* -> SHUTDOWN`；`TRANSCRIBE` / `ALIGN` payload 只传 path，不通过 IPC 传模型或大型结果
- `<base>.asr.json` 只有在 fingerprint 完全匹配，且 result 包含合法字符串 `language`、list `segments` 以及每个 segment 的数值 `start` / `end` 和字符串 `text` 时才命中；写入先 fsync sibling temp 并原子替换，POSIX 再尽力 fsync parent directory
- alignment 输出保留 `language`、`segments` 和每个 segment 的 `words` list；word text 必须存在，无法对齐的合法 WhisperX word 可以没有成对的 `start` / `end`

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
- 如果 `glossary.md` 已缓存但 `<base>.web_evidence.json` 缺失，且 Tavily 可用，会补建 sidecar 而不重写 glossary
- 手动运行 `--only-glossary` 时忽略已有缓存，重新生成并覆盖 `glossary.md`
- 读取 transcript、`.description`、`.tags.txt`、`.info.json`
- 本地脚本会把 YouTube 原视频元信息前置写入 `glossary.md`，包括标题、作者、上传时间、原简介和标签；这部分不交给远端 LLM 合成
- 配置 `TAVILY_API_KEY` 时联网搜索，未配置时离线总结
- 联网搜索结果是 glossary 的优先证据来源；远端 LLM 应用搜索结果校正 transcript 中可能的 ASR 人名、标题、引文和术语错误
- Tavily 搜索默认由 glossary agent 在同一个 ChatSession 中通过 `tavily_search` tool calls 发起；脚本执行搜索后将 tool result 回喂同一 session，并把收集到的网页证据交给无工具 finalizer 生成最终 glossary
- Tavily 原始网页证据会规范化写入 `<base>.web_evidence.json` sidecar；它独立于 `glossary.md`，用于后续 embedding 检索，不作为常驻硬规则 prompt
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
- pipeline 正常下载模式下，字幕编辑链路使用 `<base>.mkv`，最终硬压回到 `<base>.original.<ext>`
- `ffmpeg-burn.ps1/.sh` 成功时都输出 `OUTPUT_BURNED_VIDEO=<绝对路径>`；batch 同时检查 marker 和非空文件
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
| `PROOFREAD_MODEL` | 校对模型，空则复用翻译模型 |
| `PROOFREAD_BATCH_SIZE` | 校对批量；空则使用 `--batch-size` 的一半，长视频建议 `2-10` |
| `PROOFREAD_RETRIEVAL_TOP_K` | 校对阶段 RAG 每条字幕检索片段数，默认 `1` |
| `PIPELINE_SKIP_*` | 各阶段默认跳过开关 |
| `BURN_OVC` / `BURN_OVCOPTS` / `BURN_OAC` / `BURN_RES` | 硬压参数 |
| `OPENAI_API_KEY` / `OLLAMA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | LLM / embedding API keys |
| `TAVILY_API_KEY` / `TAVILY_MAX_RESULTS` / `TAVILY_MAX_QUERIES` | glossary 联网搜索配置；`TAVILY_MAX_QUERIES` 在 tool-call 路径下是最大 Tavily tool 查询次数，在 fallback 路径下是单一语言 query 上限，`0` 禁用 Tavily |

`BURN_OVCOPTS=source-bitrate` 是默认硬压策略：burn 脚本用 `ffprobe` 读取源视频码率，生成 VBR 的 `b/maxrate/bufsize` 参数，让输出尽量接近源码率；显式 `qp=20`、`crf=23` 等会覆盖自动模式。`BURN_OAC` 默认 `aac`，兼容 ffmpeg 和 mpv 的硬字幕压制。

配置 `TAVILY_API_KEY` 时，glossary 阶段默认使用两段式 tool calling：脚本第一轮把 metadata、transcript/retrieved context 和 `tavily_domains.json` 域名偏好一起交给 glossary 模型；模型按需请求 `tavily_search`，脚本执行 Tavily 后把结果作为 tool message 喂回同一 session。搜索完成后，脚本新建无工具 finalizer session，只喂用户 JSON、transcript/retrieved context 和已收集的 `web_evidence`，要求模型生成最终 glossary。tool-call 路径下，`TAVILY_MAX_QUERIES` 控制最多执行多少次 Tavily 查询；fallback query-agent 路径下，它仍表示每种语言最多生成多少条 query。Tavily tool 会结合 metadata、模型给出的 `topic_hints` 和 `tavily_domains.json` 做域名优先搜索，并在最终合并时给百科/知识库域名加权。该阶段使用 `GLOSSARY_PROVIDER` / `GLOSSARY_MODEL`，不要为了省成本使用弱模型。

glossary tool 阶段会强制移除 provider `request_kwargs.response_format` 中的 JSON mode 参数，以免干扰 tool calling；finalizer 首选返回 `{"markdown": "..."}` JSON object，若 provider 无法稳定输出 JSON，可返回 `<GLOSSARY_MARKDOWN>...</GLOSSARY_MARKDOWN>` 标签块。普通散文和伪 tool call 文本都会被拒绝并重试。

`glossary.md` 是全局硬规则：一旦存在，会完整常驻注入后续翻译、校对和视频简介翻译的 system prompt，不会因为启用 embedding 而省略。启用 `EMBEDDING_ENABLED=1` 时，Chroma 索引同时包含 `glossary:*` 项目知识 chunk、`web_evidence:*` Tavily 网页证据 chunk、`transcript:*` 源文 chunk 和翻译/分割后生成的双语 `translation_memory:*` chunk；这些按当前字幕逐条召回为 `retrieved_context`，只作为动态补充记忆。proofread 阶段用源文+译文 query 检索，优先获得历史译法和术语一致性参考。`glossary:*` 包含本地组合的视频元信息和 glossary 内容，并按 Markdown 标题切分；`web_evidence:*` 由 `<base>.web_evidence.json` 中的规范化 Tavily 结果构建，保留 query、域名、标题、URL 和证据摘要；`transcript:*` 使用干净字幕文本建向量，retrieved context 返回带时间码的字幕行，并按字符数、时间跨度、segment 数量切块，按末尾时间窗口自动 overlap；每次重建索引前会清理当前项目旧 chunk，避免残留向量污染检索。

`providers.json` 是 OpenAI SDK 兼容配置，`url` 是 SDK `base_url`，不包含 `/chat/completions`。`request_kwargs` 会原样合并进 `chat.completions.create(**kwargs)`，用于 DeepSeek JSON mode、Gemini Google Search 等 provider 专用参数；Gemini 内置联网需要 Gemini 3 或更新模型。

## Key Commands

### PowerShell

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
.\pipeline.ps1 "https://youtu.be/xxxxx" -SourceLang en -TargetLang ja -SkipBurn
.\pipeline.ps1 "https://youtu.be/xxxxx" -ExistingAss "path\to\video.en-zh.ass"
.\batch.ps1 "URL1" "URL2"
.\batch.ps1 --dry-run --report "batch-result.txt" "URL1" "URL2"
```

### Linux / WSL

```bash
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
TARGET_LANG=ja SKIP_BURN=1 ./pipeline.sh "URL"
./pipeline.sh "URL" -- --scene-threshold 0.12 --snap-frames 10
./batch.sh "URL1" "URL2"
./batch.sh --skip-burn --translate-provider deepseek "URL1" "URL2"
```

### Manual Steps

```powershell
$downloadOutput = & .\download.ps1 "URL"
$renderVideo = ($downloadOutput | Where-Object { $_ -like 'OUTPUT_RENDER_VIDEO=*' } | Select-Object -Last 1) -replace '^OUTPUT_RENDER_VIDEO=', ''
$prepareOutput = & .\prepare-video.ps1 $renderVideo
$editVideo = ($prepareOutput | Where-Object { $_ -like 'OUTPUT_VIDEO=*' } | Select-Object -Last 1) -replace '^OUTPUT_VIDEO=', ''
$videoBase = Join-Path ([IO.Path]::GetDirectoryName($editVideo)) ([IO.Path]::GetFileNameWithoutExtension($editVideo))
& .\whisper.ps1 $editVideo
& .\translate_srt.ps1 "$videoBase.json" --video $editVideo --only-beautify
& .\translate_srt.ps1 "$videoBase.beautified.json" --video $editVideo --only-glossary --skip-beautify
& .\translate_srt.ps1 "$videoBase.beautified.json" --video $editVideo --source-lang en --target-lang zh
& .\ffmpeg-burn.ps1 $renderVideo -SubFile "$videoBase.en-zh.ass"
```

```bash
download_output="$(./download.sh "URL")"
render_video="$(printf '%s\n' "$download_output" | sed -n 's/^OUTPUT_RENDER_VIDEO=//p' | tail -n 1)"
prepare_output="$(./prepare-video.sh "$render_video")"
edit_video="$(printf '%s\n' "$prepare_output" | sed -n 's/^OUTPUT_VIDEO=//p' | tail -n 1)"
video_base="${edit_video%.*}"
./whisper.sh "$edit_video"
./translate_srt.sh "$video_base.json" --video "$edit_video" --only-beautify
./translate_srt.sh "$video_base.beautified.json" --video "$edit_video" --only-glossary --skip-beautify
./translate_srt.sh "$video_base.beautified.json" --video "$edit_video" --source-lang en --target-lang zh
./ffmpeg-burn.sh "$render_video" --sub-file "$video_base.en-zh.ass"
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
- 独立调用 Python 功能时使用 `translate_srt.ps1/.sh`、`merge_ass.ps1/.sh` 或 `batch.ps1/.sh` 包装器；它们委托给 `py_launcher.ps1/.sh`，共享启动器只允许 `translate_srt`、`merge_ass`、`batch`，并从自身目录定位 `.venv`，不要求用户设置系统 PATH，也不依赖当前工作目录
- 不要提交 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、`glossary_prompt.md`、`translate_prompt.md`、`proofread_prompt.md`、`split_prompt.md` 或生成产物
- 不要回退用户本地数据或未请求的工作区改动
- `README.md` 面向用户快速使用；`AGENTS.md` 面向维护和自动化代理；`.agents/skills/*` 面向分步骤执行
