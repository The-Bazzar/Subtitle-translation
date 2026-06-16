---
name: beautify
description: 美化 SRT 字幕时间码 — Netflix 规范场景吸附对齐
---

# 字幕时间码美化

用 ffmpeg 场景检测 + ffprobe 帧率检测，将 SRT 字幕起止时间吸附对齐到场景切换点。

## 输入

需要**视频文件** + **英文字幕** (.srt)。

## 执行方式

```bash
# 自动查找同目录 .srt → .beautified.srt (不覆盖原文件)
./beautify_srt.sh video.webm

# 指定字幕 + 输出
./beautify_srt.sh video.webm subtitle.srt
./beautify_srt.sh video.webm -o result.srt

# 仅预览变化
./beautify_srt.sh video.webm --preview

# 激进对齐 (剪辑密集的视频)
./beautify_srt.sh video.webm --scene-threshold 0.2 --snap-frames 10

# 保守对齐 (长镜头视频)
./beautify_srt.sh video.webm --scene-threshold 0.35 --snap-frames 4
```

## 输出

```
视频目录/
├── 视频标题.srt            # 原始字幕
└── 视频标题.beautified.srt # 美化后字幕 ✨
```

## 算法流程

```
帧率检测 (ffprobe) → 场景检测 (ffmpeg, ≥7帧间隔)
  → 入点吸附到场景切换点 (7帧内)
  → 出点吸附到场景前2帧 (7帧内)
  → 重叠/间隙修复 (间距<500ms 自动合并)
  → 时长约束 (最短1s, 最长8s)
```

## 参数 (默认值遵循 Netflix 规范)

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `.beautified.srt` | 输出路径 |
| `--scene-threshold` | `0.25` | 场景检测灵敏度 |
| `--snap-frames` | `7` | 吸附最大帧数 |
| `--end-offset-frames` | `2` | 出点偏移帧数 |
| `--min-scene-interval-frames` | `7` | 场景最小帧间隔 |
| `--min-duration` | `1.0` | 最短时长 (秒) |
| `--max-duration` | `8.0` | 最长时长 (秒) |
| `--min-gap` | `0.083` | 最小间距 (秒) |
| `--max-gap-merge` | `0.5` | 间隙合并阈值 |

## 注意事项

- 默认不覆盖原文件, 输出 `.beautified.srt`
- 所有帧数参数按视频实际 fps 自动换算为秒
- 关键帧吸附默认关闭 (`--use-keyframes` 启用)
