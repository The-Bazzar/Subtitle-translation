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
python beautify_srt.py video.webm --aggressive   # 激进: 更多场景切换点 + 更宽吸附
```

## 输出

- 默认不覆盖原文件 → `.beautified.srt`
- `-o same.srt` 显式指定才能覆盖

## 算法

```
帧率检测 → 场景检测 (ffmpeg select filter, 7帧最小间隔)
→ ffprobe 读取精确 pts_time (微秒级, 6 位小数)
→ 入点吸附到前一个场景切换 → 出点吸附到下一个场景切换前2帧
→ 重叠/间隙修复 → 时长约束 (1s~8s)
→ 场景切换太少时警告并建议 --aggressive
```

## 主要参数

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `-o, --output` | `.beautified.srt` | 输出路径 |
| `--scene-threshold` | `0.15` | 场景检测灵敏度 (0.05~0.5, 越低越多切换点) |
| `--snap-frames` | `7` | 吸附最大帧数 |
| `--min-scene-interval-frames` | `2` | 最小场景间隔 (Subtitle Edit 兼容, Netflix: 7) |
| `--aggressive` | — | 激进模式: threshold=0.08 snap=12 min-interval=1 |
| `--use-showinfo` | — | 回退到 ffmpeg showinfo (精度较低, 默认用 ffprobe) |
| `--extend` | — | 延伸字幕填充到场景切换前的间隙 |
| `--preview` | — | 仅预览变化, 不写入 |

## 调试输出

脚本会显示原始检测数和过滤后结果：

```
Raw detections: 31 scene changes
Filtered out: 26 (< 83ms interval)
Found 5 scene changes
```

如果过滤太多，降低 `--min-scene-interval-frames`。
