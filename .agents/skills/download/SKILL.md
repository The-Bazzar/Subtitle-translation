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

输出 `OUTPUT_VIDEO=<编辑版 mkv 路径>` 与 `OUTPUT_RENDER_VIDEO=<原片路径>`，供 `whisper.ps1` / `pipeline.ps1` 串联。

## Linux — Bash

```bash
./download.sh "https://www.youtube.com/watch?v=xxxxx"
```

输出 `OUTPUT_VIDEO=<编辑版 mkv 路径>` 与 `OUTPUT_RENDER_VIDEO=<原片路径>`，供 `whisper.sh` / `pipeline.sh` 串联。

## 输出

```
视频目录/
├── 视频标题.original.webm  # 保留的原片 (供最终压制)
├── 视频标题.mkv            # 重编码后的编辑视频 (供 WhisperX / translate)
├── 视频标题.png            # 封面缩略图
├── 视频标题.info.json      # 元数据
├── 视频标题.description    # 简介
└── 视频标题.tags.txt       # 标签
```

## 注意事项

- 需要 `cookies.txt` (YouTube 凭证, gitignored)
- yt-dlp 从 `.env` 的 `YTDLP_PATH_WIN` / `YTDLP_PATH_LINUX` 读取
- 如果输出目录中已有 `视频标题.original.mkv`，脚本视为原片已下载，只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`，然后直接进入编辑版重编码
- 下载后固定做一次时间戳抚平重编码：优先使用 `h264_nvenc -preset p7 -cq 19` 重编码视频，未检测到可用 NVIDIA GPU 或 NVENC 编码器时回退 `libx264 -preset fast -crf 19`；音频统一用 `aresample=async=1:out_sample_fmt=s16` + `flac` 重建时间轴，并清理 metadata。若 `h264_nvenc` 返回非零退出码但已输出非 0B 文件，脚本会保留该文件并继续，不再回退重编码。
