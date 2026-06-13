# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

YouTube 视频下载 + WhisperX AI 字幕生成流水线。项目维护在一个 Windows 主机上，核心脚本在 WSL 中运行。

## Architecture

```
Subtitle translation/
├── pipeline.sh               # WSL/bash: 一键流水线入口 (download → beautify)
├── download_and_sub.sh       # 主流程: 下载视频 → 生成英文字幕
├── beautify_srt.sh           # 字幕时间码美化入口 (调用 beautify_srt.py)
├── beautify_srt.py           # Python: 场景检测 + 帧率自适应 → 美化 SRT 时间码 (Netflix 规范)
├── download.ps1              # Windows PowerShell 备用: 仅下载 (不含字幕生成)
├── mpv-burn.ps1              # Windows PowerShell: mpv 字幕硬压 (NVENC)
├── .env                      # API keys — gitignored
├── cookies.txt               # YouTube cookies — gitignored
└── <Video Title>/            # 每个视频独立的输出目录
    ├── <Video Title>.<ext>   # 视频文件 (webm/mp4/mkv)
    ├── <Video Title>.srt     # WhisperX 生成的英文字幕
    ├── <Video Title>.webp    # 封面缩略图
    ├── <Video Title>.info.json     # yt-dlp 元数据
    └── <Video Title>.description   # 视频简介文本
```

## Key Commands

### 主流程 (WSL 中运行)

```bash
# 一键流水线: 下载 + 字幕 + 美化 (推荐)
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 传递美化选项
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2

# 仅下载 + 字幕 (不美化)
SKIP_BEAUTIFY=1 ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 分步执行
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"
./download_and_sub.sh "URL1" && ./download_and_sub.sh "URL2"
```

### 从 PowerShell 调用 WSL

```powershell
wsl -u root bash -lc "sh ./download_and_sub.sh https://www.youtube.com/watch?v=xxxxx"
```

### 仅下载 (PowerShell, 无字幕)

```powershell
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

### 字幕硬压到视频 (PowerShell)

```powershell
.\mpv-burn.ps1 "C:\path\to\video.webm"
# 输出: burned.mkv (同目录, hevc_nvenc qp=20, aac音频)
```

### 字幕时间码美化 (WSL 中运行)

```bash
# 自动查找同目录 .srt 并原位覆盖
./beautify_srt.sh "path/to/video.webm"

# 指定字幕文件 + 备份
./beautify_srt.sh video.webm subtitle.srt --backup

# 仅预览变化 (不写入)
./beautify_srt.sh video.webm --preview

# 激进对齐 (剪辑密集)
./beautify_srt.sh video.webm --scene-threshold 0.2 --snap-frames 10

# 保守对齐 (长镜头)
./beautify_srt.sh video.webm --scene-threshold 0.35 --snap-frames 4

# 完整选项列表
./beautify_srt.sh --help
```

## Pipeline Steps (pipeline.sh)

1. **下载 + 字幕** — 调用 `download_and_sub.sh`，捕获输出的 `OUTPUT_VIDEO` 路径
2. **时间码美化** — 调用 `beautify_srt.sh` 对齐到场景切换 & 关键帧
3. 支持通过 `SKIP_DOWNLOAD=1` `/` `SKIP_BEAUTIFY=1` 跳过指定阶段
4. 通过 `--` 分隔符向 beautify 传递自定义参数

## Pipeline Steps (download_and_sub.sh)

1. **获取标题** — `yt-dlp --get-title` 获取视频标题，过滤文件名非法字符后创建目录
2. **下载** — `yt-dlp` 下载视频/缩略图/元数据/简介，自动移除 sponsor 和 selfpromo 片段 (SponsorBlock)
3. **定位视频文件** — 在目录中查找 `.mp4/.mkv/.webm/.flv/.avi` 文件
4. **生成字幕** — `uvx whisperx` 使用 `large-v3` 模型 + `float16` 计算生成英文 SRT 字幕
5. **输出路径** — 打印 `OUTPUT_VIDEO=<绝对路径>` 供下游脚本解析

## Dependencies

| Tool | Purpose |
|------|---------|
| `yt-dlp` | YouTube 视频下载 |
| `uvx` (uv) | 运行 WhisperX，无需手动配置 Python 环境 |
| `whisperx` (large-v3) | AI 语音识别生成英文字幕 |
| `ffmpeg` | 音视频处理底层依赖 |
| `ffprobe` | 场景切换检测 + 关键帧提取 (beautify_srt) |
| `python3` | beautify_srt.py 运行环境 |
| `mpv` (Windows) | 字幕硬压 (mpv-burn.ps1) |

## Pipeline Steps (beautify_srt.sh)

1. **帧率检测** — `ffprobe` 检测视频帧率, 所有帧数参数按实际 fps 换算为秒
2. **场景检测** — `ffmpeg -vf "select='gt(scene,0.25)',showinfo"` 检测硬切场景切换 (Netflix: 7帧最小间隔)
3. **入点吸附** — 字幕起始时间吸附到前一个场景切换 (7帧以内)
4. **出点吸附** — 字幕结束时间吸附到下一个场景切换前 2 帧 (7帧以内, Netflix 规范)
5. **重叠修复** — 检测并修复字幕重叠, 间距 <500ms 自动合并
6. **时长约束** — 强制最短 1000ms / 最长 8000ms 字幕时长
7. **(可选) 关键帧微调** — `--use-keyframes` 启用, ffprobe 3 级回退提取 I-frame

## Important Notes

- `.env` 中的 API keys 用于翻译相关任务 (OpenRouter/DeepSeek/Gemini)，不要提交。`.gitignore` 已配置忽略。
- `cookies.txt` 包含 YouTube 登录凭证，已 gitignored。过期后需要重新导出。
- WhisperX 首次运行会自动下载 `large-v3` 模型 (数 GB)，需要保持网络畅通。
- 无 NVIDIA GPU 时需将 `--compute_type float16` 改为 `int8`，或添加 `--device cpu`。
- 每个视频目录名即为 `yt-dlp --get-title` 的结果 (特殊字符替换为 `_`)。
- `beautify_srt.sh` 运行在 WSL 中，会自动识别真正的 SRT 文件（排除 ASS/SSA 格式伪装的 `.srt`）。
- 场景检测对长视频可能耗时较久（~5 分钟/小时视频）。
- 所有帧数参数 (`--snap-frames`, `--end-offset-frames`, `--min-scene-interval-frames`) 会按实际视频帧率自动换算为秒。
- 关键帧吸附默认关闭 (`--use-keyframes` 启用)，因各视频编码/帧率差异大，场景吸附已足够。
