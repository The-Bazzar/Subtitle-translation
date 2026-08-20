# Migration Guide

## Stage-aware Batch Entry

`batch.ps1` / `batch.sh` 现在统一委托给 `py_launcher` 的 `batch` target，由 `batch.py` 按阶段调度资源，不再并发启动整条 `pipeline.ps1` / `pipeline.sh`。

所有手动并发参数已删除：PowerShell 的 `-MaxJobs` / `-j`、Python 的 `-j` / `--jobs` 均不再接受，也不要替换为 `--io-jobs`。batch 会自动使用 `max(1, (os.cpu_count() or 1) // 4)` 个 CPU/IO 槽位和固定 `4` 个 prepare NVENC 槽位。

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

当前 stage-aware batch 只执行 `download -> prepare-video -> mono 16kHz WAV`，成功后停在 `wav_ready`，不加载 Whisper。`--skip-burn`、`--translate-provider`、`--translate-model`、`--report` 和 `--dry-run` 仍可表达；burn/provider/model 会保留到阶段环境，等待后续 scheduler 阶段接入。

## Split Download and Edit Preparation

[Issue #12](https://github.com/The-Bazzar/Subtitle-translation/issues/12) 将下载与编辑视频准备拆成两个独立步骤。这是 direct download 调用契约的 breaking change；`pipeline.ps1` / `pipeline.sh` 已自动适配，无需修改 pipeline 命令。

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
