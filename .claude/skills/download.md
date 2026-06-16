---
name: download
description: 下载 YouTube 视频 + 元数据（不含字幕生成）
---

# 视频下载

使用 yt-dlp 下载 YouTube 视频及相关元数据。

## 执行方式

```bash
# WSL: 下载视频 + 元数据 + 字幕生成 (全流程)
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"

# PowerShell: 仅下载视频 + 元数据
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

> `download_and_sub.sh` 下载后会自动调用 WhisperX，详见 [[whisper]] skill。

## 输出

```
视频目录/
├── 视频标题.webm           # 视频文件 (内嵌封面)
├── 视频标题.png            # 封面缩略图 (PNG)
├── 视频标题.info.json      # 元数据 (标题/作者/语言等)
├── 视频标题.description    # 视频简介
└── 视频标题.tags.txt       # 视频标签
```

## 流程

```
YouTube URL
  │
  ▼
yt-dlp --get-title → 文件夹名 (过滤特殊字符)
  │
  ▼
yt-dlp 下载:
  - 视频 (文件名 = 文件夹名, 可预测)
  - 缩略图 (PNG) + 内嵌封面
  - .info.json (元数据)
  - .description (简介)
  - .tags.txt (标签)
  - SponsorBlock 自动去广告
```

## 注意事项

- 需要 `cookies.txt` (YouTube 登录凭证, gitignored)
- yt-dlp 路径从 `.env` 的 `YTDLP_PATH_WIN` / `YTDLP_PATH_LINUX` 读取
- 视频文件名与文件夹名一致, 便于后续脚本定位
