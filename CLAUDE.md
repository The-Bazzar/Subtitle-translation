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
├── beautify_srt.py           # Python: 场景检测 + 关键帧对齐 → 美化 SRT 时间码
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
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.25

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

# 激进对齐
./beautify_srt.sh video.webm --scene-threshold 0.25 --snap-distance 0.25

# 保守对齐
./beautify_srt.sh video.webm --scene-threshold 0.4 --snap-distance 0.12

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

1. **场景检测** — `ffmpeg -vf "select='gt(scene,0.3)',showinfo"` 检测视频中的硬切场景切换
2. **关键帧提取** — `ffprobe` 从视频流中提取 I-frame 时间戳 (兼容 VP9/H.264/H.265)
3. **吸附对齐** — 字幕起始时间优先吸附到前一个场景切换，结束时间吸附到后一个场景切换
4. **关键帧微调** — 在场景切换基础上微调到最近的关键帧
5. **延伸填充** — 若字幕结束靠近下一个场景切换，自动延伸到该切换点
6. **重叠修复** — 检测并修复字幕重叠，合并过小间隙
7. **时长约束** — 强制最短/最长字幕时长

## Important Notes

- `.env` 中的 API keys 用于翻译相关任务 (OpenRouter/DeepSeek/Gemini)，不要提交。`.gitignore` 已配置忽略。
- `cookies.txt` 包含 YouTube 登录凭证，已 gitignored。过期后需要重新导出。
- WhisperX 首次运行会自动下载 `large-v3` 模型 (数 GB)，需要保持网络畅通。
- 无 NVIDIA GPU 时需将 `--compute_type float16` 改为 `int8`，或添加 `--device cpu`。
- 每个视频目录名即为 `yt-dlp --get-title` 的结果 (特殊字符替换为 `_`)。
- `beautify_srt.sh` 运行在 WSL 中，会自动识别真正的 SRT 文件（排除 ASS/SSA 格式伪装的 `.srt`）。
- 场景检测对长视频可能耗时较久（~5 分钟/小时视频），关键帧提取有三级回退策略确保兼容性。
