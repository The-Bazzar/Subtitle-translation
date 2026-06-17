---
name: beautify
description: 美化 SRT 字幕时间码 — Netflix 规范场景吸附对齐
platform: Win + Linux
---

# 字幕时间码美化 (Win + Linux)

用 ffmpeg 场景检测将 SRT 起止时间吸附对齐到场景切换点。**Python 脚本，跨平台**。

输入：视频文件 + 英文字幕 (.srt)。

## 执行

```bash
python beautify_srt.py video.webm
python beautify_srt.py video.webm subtitle.srt
python beautify_srt.py video.webm -o result.srt
python beautify_srt.py video.webm --preview
```

## 输出

- 默认不覆盖原文件 → `.beautified.srt`
- `-o same.srt` 显式指定才能覆盖

## 算法

```
帧率检测 → 场景检测 (7帧间隔)
→ 入点吸附到场景 → 出点吸附到场景前2帧
→ 重叠/间隙修复 → 时长约束 (1s~8s)
```

## 主要参数

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `.beautified.srt` | 输出路径 |
| `--scene-threshold` | `0.25` | 场景灵敏度 |
| `--snap-frames` | `7` | 吸附帧数 |
| `--preview` | — | 仅预览 |
