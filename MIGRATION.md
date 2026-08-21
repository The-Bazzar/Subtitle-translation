# Migration Guide

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
download_exit=${PIPESTATUS[0]}
if [ "$download_exit" -ne 0 ]; then
    exit "$download_exit"
fi
render_video="$(awk -F= '/^OUTPUT_RENDER_VIDEO=/{print substr($0, index($0, "=") + 1)}' "$download_log" | tail -n 1)"
rm -f "$download_log"
./prepare-video.sh "$render_video"
```

Direct download 自动化如果仍从 download 输出解析 `OUTPUT_VIDEO`，升级后会取不到路径。请改为解析 `OUTPUT_RENDER_VIDEO`，调用 prepare-video，再从 prepare-video 输出解析 `OUTPUT_VIDEO`。
