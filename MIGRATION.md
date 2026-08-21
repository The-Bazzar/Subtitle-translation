# Migration Guide

## Stage-aware Batch Entry

`batch.ps1` / `batch.sh` 现在统一委托给 `py_launcher` 的 `batch` target，由 `batch.py` 按阶段调度资源，不再并发启动整条 `pipeline.ps1` / `pipeline.sh`。

所有手动并发参数已删除：PowerShell 的 `-MaxJobs` / `-j`、Python 的 `-j` / `--jobs` 均不再接受，也不要替换为 `--io-jobs`。

```powershell
# 旧命令
.\batch.ps1 -MaxJobs 4 "URL1" "URL2"

# 新命令
.\batch.ps1 "URL1" "URL2"
```

```bash
# 旧命令
./.venv/bin/python batch.py --jobs 4 "URL1" "URL2"

# 新命令
./batch.sh "URL1" "URL2"
```

### 资源与 GPU waves

batch 自动使用 `max(1, (os.cpu_count() or 1) // 4)` 个 CPU/IO 槽位；prepare 与最终 burn 共用固定 `4` 路 NVENC capacity。旧 batch 会并发启动多条完整 pipeline，新 batch 改为严格分阶段调度：每个任务先流水执行 `download -> prepare-video -> mono 16kHz WAV`，所有 acquisition 任务到达成功或失败终态后才创建一个 worker。worker 只加载一次 ASR，串行完成全部未缓存识别，再卸载 ASR；alignment 按规范化 ISO 语言稳定分组，同语言只加载一次 alignment model，组间卸载切换。postprocess 可在单个 alignment 成功后立即开始，但任何 burn 都必须等待 `worker_released`，随后最多 4 路动态执行。`--skip-burn` 仍让任务在 `translated` 结束。

### ASR 恢复与锁

每个 ASR 成功任务原子写 `<base>.asr.json`。其 fingerprint 包含编辑版 resolved path、size、mtime、Whisper model、compute type、源语言和 ASR options；每次写入还生成唯一 UUID generation。损坏、缺少或非法 generation、旧 schema、fingerprint 不匹配都视为 cache miss 并重跑 ASR。

正常重启时，若既有编辑版 `<base>.mkv` 与当前 sidecar fingerprint 匹配，batch 会跳过 prepare，避免重编码改变 size/mtime 后让可恢复 sidecar 失效；原片更新或任一 fingerprint 输入变化时仍走完整 prepare。新的 alignment generation 提交前先删除旧 `<base>.beautified.json`；删除失败会阻止 final JSON commit 并记录 `cleanup_diagnostics`。alignment 成功后删除 WAV，alignment 失败、取消或 worker crash 时保留 WAV。

所有 sidecar 写与 alignment commit 使用持久 `<base>.asr.lock` 跨进程互斥。parent 持锁 dispatch，child 只写 generation-specific candidate，parent 持锁复核 ownership、candidate 与 schema。取消先赢时只清 candidate 并保留 `<base>.asr.json` 供下次恢复；destructive commit 先赢时先原子 promote final `<base>.json`，再删除自己持有 generation 的 `<base>.asr.json`，然后继续 postprocess。`<base>.asr.lock` 最多 1 byte、已加入 `.gitignore`，活跃任务间不得 unlink。

### 故障与两阶段中断

worker crash 或 heartbeat timeout 不自动重启。当前 worker task 失败，仍依赖 ASR/alignment 的任务进入 `blocked_by_worker_failure`，已经 alignment 成功的任务继续 postprocess，并在 worker 释放后允许 burn。scheduler best-effort 原子写 invocation cwd 下的 `batch-worker-failure-<timestamp>.log`，包含 task/phase、资源队列快照、worker exit code、traceback 以及有界 stdout/stderr tail；日志写盘失败只追加 cleanup diagnostic，不替换首个根因。

`--report` 现在同时写文本报告和同基名 JSON 机器报告。JSON 顶层新增 `worker_failure`、`worker_failure_log`、`worker_failure_root_cause`、`worker_failure_detail`、invocation `output_directory` 和 `cleanup_diagnostics`；每个 task 也新增自己的 `output_directory`，便于自动化定位产物。指定 `.json` 路径时，文本报告写到同基名 `.txt`。

第一次 `Ctrl+C` 同步关闭 command admission 和阶段推进；已经取得 reservation 的外部命令自然结束，尚未取得 reservation 的命令不再 spawn。第二次 `Ctrl+C` 终止已注册的子进程树并 abort worker，等待真实退出后返回 `130`。precommit cancel-wins 保留未消费 recovery sidecar，commit-wins 保留已经完成的 final 输出。

### Release smoke 限制

跨平台 release smoke 经过真实 `batch.ps1/.sh -> py_launcher.ps1/.sh -> batch.py` production orchestrator，运行真实 argparse/main、自动资源检测、subprocess stage runners、marker parser 和 spawned worker protocol；复制到隔离目录的 production modules 必须通过 SHA-256 证明 byte-identical。测试只 fake download、prepare、translate、burn、ffmpeg 和 WhisperX 外部边界。Windows 使用项目 Python 与 PowerShell 7；WSL 使用 `wsl -u root`，按 `BATCH_SMOKE_WSL_PYTHON`、仓库 `.venv/bin/python`、`command -v python3` 的顺序逐一 probe，只接受 Python `>=3.10,<3.14` 且能 import `langcodes` 的现有 Linux interpreter。没有候选时开发者测试精确 skip，不下载或安装依赖。

`BATCH_SMOKE_REQUIRE_WSL=1` 是仅供 test/release gate 使用的内部变量，不是 batch 或项目用户配置。启用后缺少 WSL root 或合格 interpreter 会失败而不是 skip，因此以下 PowerShell 命令的 `OK` 结果能证明 WSL smoke 确实执行。若自动候选都不合格，先把 `BATCH_SMOKE_WSL_PYTHON` 指向一个已有的合格 WSL interpreter；不得在测试中创建环境或安装包。

```powershell
$env:BATCH_SMOKE_REQUIRE_WSL = "1"
# 可选：$env:BATCH_SMOKE_WSL_PYTHON = "/existing/venv/bin/python"
.\.venv\Scripts\python.exe -m unittest -v tests.test_batch_smoke
.\.venv\Scripts\python.exe -m unittest discover -s tests
Remove-Item Env:BATCH_SMOKE_REQUIRE_WSL -ErrorAction SilentlyContinue
Remove-Item Env:BATCH_SMOKE_WSL_PYTHON -ErrorAction SilentlyContinue
```

smoke 的时间证据只证明以下调度范围：所有 acquisition 完成后才加载 ASR；prepare 与 burn 都不和 worker 的 ASR/alignment lifetime 重叠；ASR 与 alignment command 串行且同语言 alignment model 复用；worker shutdown 先于所有 burn；prepare 与 burn 各自峰值不超过 4。另行断言 CLI/report/通知、退出码、stage order、sidecar 清理和输出路径。它不证明真实 CUDA、ffmpeg、网络、LLM 或媒体质量。

## Split Download and Edit Preparation

[Issue #12](https://github.com/The-Bazzar/Subtitle-translation/issues/12) 将下载与编辑视频准备拆成两个独立步骤。这是 direct download 调用契约的 breaking change；`pipeline.ps1` / `pipeline.sh` 已自动适配，无需修改 pipeline 命令。

Linux/WSL pipeline 在 prepare 失败时不再统一返回 `1`，而是精确透传 `prepare-video.sh 的原始退出码`，与 PowerShell 行为一致。

### 旧契约

`download.ps1` / `download.sh` 同时下载原片并重编码编辑版，成功输出：

```text
OUTPUT_VIDEO=<编辑版 mkv 绝对路径>
OUTPUT_RENDER_VIDEO=<原片绝对路径>
```

### 新契约

`download.ps1` / `download.sh` 只下载原片与元数据，成功只输出：

```text
OUTPUT_RENDER_VIDEO=<原片绝对路径>
```

编辑版由 `prepare-video.ps1` / `prepare-video.sh` 独立生成，成功只输出：

```text
OUTPUT_VIDEO=<编辑版 mkv 绝对路径>
```

### PowerShell Migration

```powershell
$downloadOutput = & .\download.ps1 "URL"
$renderVideo = ($downloadOutput | Where-Object { $_ -match '^OUTPUT_RENDER_VIDEO=' }) -replace '^OUTPUT_RENDER_VIDEO=', ''
& .\prepare-video.ps1 $renderVideo
```

### Linux / WSL Migration

```bash
download_log="$(mktemp)"
./download.sh "URL" | tee "$download_log"
render_video="$(awk -F= '/^OUTPUT_RENDER_VIDEO=/{print substr($0, index($0, "=") + 1)}' "$download_log" | tail -n 1)"
rm -f "$download_log"
./prepare-video.sh "$render_video"
```

Direct download 自动化如果仍从 download 输出解析 `OUTPUT_VIDEO`，升级后会取不到路径。请改为解析 `OUTPUT_RENDER_VIDEO`，调用 prepare-video，再从 prepare-video 输出解析 `OUTPUT_VIDEO`。
