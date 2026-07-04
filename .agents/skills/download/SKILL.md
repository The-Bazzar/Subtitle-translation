---
name: download
description: 下载 YouTube 视频 + 元数据（不含字幕生成）
platform: Win + Linux
---

# 视频下载 (Win + Linux)

使用 yt-dlp 下载 YouTube 视频及相关元数据。**不包含**语音识别。

## Win — PowerShell

```powershell
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

输出 `OUTPUT_VIDEO=<编辑版 mp4 路径>` 与 `OUTPUT_RENDER_VIDEO=<原片路径>`，供 `whisper.ps1` / `pipeline.ps1` 串联。

## Linux — Bash

```bash
./download.sh "https://www.youtube.com/watch?v=xxxxx"
```

输出 `OUTPUT_VIDEO=<编辑版 mp4 路径>` 与 `OUTPUT_RENDER_VIDEO=<原片路径>`，供 `whisper.sh` / `pipeline.sh` 串联。

## 输出

```
视频目录/
├── 视频标题.original.webm  # 保留的原片 (供最终压制)
├── 视频标题.mp4            # 重编码后的编辑视频 (供 WhisperX / translate)
├── 视频标题.png            # 封面缩略图
├── 视频标题.info.json      # 元数据
├── 视频标题.description    # 简介
└── 视频标题.tags.txt       # 标签
```

## 注意事项

- 需要 `cookies.txt` (YouTube 凭证, gitignored)
- yt-dlp 从 `.env` 的 `YTDLP_PATH_WIN` / `YTDLP_PATH_LINUX` 读取
- 下载后固定做一次时间戳抚平重编码：先用 CPU 默认解码把画面解成 `yuv4mpegpipe` 纯净帧流，再与原音频回拼；优先尝试 `hevc_nvenc`，失败时回退 `libx264`
