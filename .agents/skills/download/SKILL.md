---
name: download
description: 下载 YouTube 视频 + 元数据（不含字幕生成）
platform: Win + Linux
---

# 视频下载 (Win + Linux)

使用 yt-dlp 下载 YouTube 视频及相关元数据。**不包含**语音识别。

## 执行

```text
subtitle-translation download "https://www.youtube.com/watch?v=xxxxx"
```

成功输出 `OUTPUT_RENDER_VIDEO=<原片绝对路径>`。脚本在原片与元数据落盘后结束，不创建编辑版，也不运行 ffmpeg 重编码。

需要编辑版时，将该 marker 的路径显式传给独立准备脚本：

```text
subtitle-translation prepare-video "<OUTPUT_RENDER_VIDEO>"
```

`prepare-video` 成功输出 `OUTPUT_VIDEO=<编辑版 mkv 绝对路径>`，供 `subtitle-translation whisper` 使用；`subtitle-translation pipeline` 已自动串联这两个步骤。

## 输出

```
视频目录/
├── 视频标题.original.webm  # 保留的原片 (供最终压制)
├── 视频标题.png            # 封面缩略图
├── 视频标题.info.json      # 元数据
├── 视频标题.description    # 简介
└── 视频标题.tags.txt       # 标签
```

## 注意事项

- 需要 `cookies.txt` (YouTube 凭证, gitignored)
- yt-dlp 从 `.env` 的 `YTDLP_PATH_WIN` / `YTDLP_PATH_LINUX` 读取
- 如果输出目录中已有 `视频标题.original.mkv`，脚本视为原片已下载，只用 `yt-dlp --skip-download` 补充封面、`.info.json`、`.description` 和 `.tags.txt`
- `download` 不调用 `prepare-video`；direct download 用户必须按仓库根目录 `MIGRATION.md` 显式增加准备步骤
- 时间戳抚平、`h264_nvenc` / `libx264` 选择、FLAC 音频与 metadata removal 均归独立 `prepare-video` stage 负责
