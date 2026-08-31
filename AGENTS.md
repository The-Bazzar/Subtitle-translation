# AGENTS.md

本文件是项目技术行为、代码路径和协作约束的唯一权威文档。代码以当前仓库为准；不要引用历史脚本或实际视频项目路径。

## Overview

项目现在由可安装的 Python CLI 统一实现：

```text
download(original) -> prepare-video(edit mkv) -> whisper(json)
-> beautify(json words) -> glossary -> translate -> split -> proofread
-> ass -> burn
```

WhisperX `.json` 是唯一主字幕输入，SRT 只作为检查输出。远程 LLM 只负责 glossary、翻译、分割和校对；下载、ASR、时间轴、文件和硬压由本地 Python stage 负责。

Windows 主机必须使用 PowerShell 7。旧版 Windows PowerShell 5.x 会导致 `.ps1` 报错：

```powershell
winget install Microsoft.PowerShell
```

## CLI

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

`pipeline`、`batch`、`translate`、`merge-ass`、`download`、`prepare-video`、`whisper` 和 `burn` 都是正式子命令。`subtitle_translation.cli` 只负责参数分派；业务逻辑位于 `core/` 下的 package、`translate_srt.py` 和 batch scheduler。

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
│   ├── translate_srt.py       # JSON beautify + glossary + LLM stages + ASS export
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

## Pipeline

`pipeline` 的固定顺序是 `download -> prepare-video -> whisper -> translate`，翻译函数内部继续执行 `beautify -> glossary -> translate -> split -> proofread -> ASS`。默认随后使用原片和双语 ASS 进入 burn；`--skip-burn` 只跳过硬压。

- download 保留 `<base>.original.<ext>`，并保存封面、`.info.json`、`.description`、`.tags.txt`。
- prepare 生成编辑用 `<base>.mkv`；保持 CPU decode，优先 NVENC，失败时按现有策略回退 CPU 编码。
- whisper 只输出 `<base>.json`；`segments[].words[]` 是后续 split 对轴的唯一词源。
- beautify 输入 JSON，处理每个 word 的场景吸附，再用首尾有效 word 回写 segment，输出 `.beautified.json`、`.scenes.json`、`.scenechange.txt`。
- glossary 位于翻译之前；存在非空 `glossary.md` 时普通 pipeline 复用，`--only-glossary` 强制覆盖。网页证据另存 `.web_evidence.json`。
- 翻译使用整句，split 使用未校对源语言文本，proofread 处理 split event。split 只用源片段首尾 token 对齐 words；失败时整句 fallback，禁止本地强切。
- 输出为 `.split.<lang>.srt`、`.<source>.proofread.ass`、`.<target>.ass`、`.<source>-<target>.ass` 和目标语言 `.description`。

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
- batch 与 pipeline 都只提供 `--skip-burn`。优先级：显式 `--skip-burn` > `PIPELINE_SKIP_BURN` > 默认启用。
- batch 必须读取 `PIPELINE_SKIP_BEAUTIFY`、`PIPELINE_SKIP_KNOWLEDGE` 和 `PIPELINE_SKIP_TRANSLATE`；skip translate 要求项目目录已有约定命名的双语 ASS。URL batch 无法表达 per-task 现有输入，`PIPELINE_SKIP_DOWNLOAD` / `PIPELINE_SKIP_WHISPER` 为 `1` 时必须启动前报错。
- batch glossary 与 pipeline 使用相同缓存语义；只有用户显式执行普通 `--only-glossary` 时才强制重建。

`.prepare.json`、`.asr.json`、`.asr.lock` 和 generation candidate 是恢复协调文件。cache fingerprint、media generation、alignment generation、锁和原子 publish 语义不得改变；损坏、旧版或 fingerprint 不匹配必须视为 cache miss。

## LLM and Data Contracts

- 所有 user prompt 都是 JSON object，顶层 `items` array；返回必须是 JSON object，item 使用 `id` 与源/目标 ISO 639 语言代码。
- `glossary.md` 是完整常驻的全局硬规则；`retrieved_context` 只能动态补充，不得截断或替代 glossary。
- 语言可为任意有效源/目标组合；代码通过 `langcodes` 规范文件后缀。
- `.beautified.json` 保存 `translation`、`proofread_text`、`split_events`、`split_status` 和原因字段。
- sidecar/web evidence/embedding 只能按当前项目与 generation 检索，不得污染另一个项目。

## Configuration and Local Files

`scripts/setup.ps1` / `scripts/setup.sh` 会从 `core/subtitle_translation/examples/` 创建缺失的 `.env`、provider、domain、prompt 和 template 文件；同一目录也作为 wheel package data，是 example 的唯一权威。旧 `.env` 只追加缺失变量，不覆盖已有值。setup 使用 `uv` 创建并清空项目 `.venv`，同步 pyproject 依赖和用户选定的 torch backend，并安装一个转发到项目 `.venv` 的全局 `subtitle-translation` shim。不能要求用户手动设置 PATH，也不能为 CLI 复制第二套 Python 环境。

本地文件和生成物禁止提交：`.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、本地 prompt、`template.ass`、视频、字幕、glossary、sidecar、`chroma_db` 和 batch report。

`cookies.txt` 仍按项目根目录相对位置读取；从任意工作目录调用 CLI 时，使用 `--project-dir` 或在目标项目目录运行，让配置根明确可见。

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
