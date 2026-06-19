---
name: beautify
description: 美化 WhisperX JSON 时间轴 — Netflix 规范场景吸附对齐
platform: Win + Linux
---

# JSON 时间轴美化

用 ffmpeg 场景检测美化 WhisperX JSON 的 word 边界，再由 words 回写 segment 时间轴。

输入：视频文件 + WhisperX `.json`。

## 执行

```bash
./.venv/bin/python translate_srt.py video.json --video video.webm --only-beautify
./.venv/bin/python translate_srt.py video.json --video video.webm --only-beautify --scene-threshold 0.12 --snap-frames 10
./.venv/bin/python translate_srt.py video.json --video video.webm --only-beautify --aggressive
```

## 输出

- 默认不覆盖原文件
- 输出 `<name>.beautified.json`

## 算法

```
帧率检测 -> 场景检测 -> 入点吸附到前一个场景切换
-> 出点吸附到下一个场景切换前2帧
-> 重叠/间隙修复 -> 补足最短时长
-> 调整首尾 word 边界 -> 由 words 回写 segment 边界
```

## 主要参数

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--scene-threshold` | `0.15` | 场景检测灵敏度 (0.05~0.5, 越低越多切换点) |
| `--snap-frames` | `7` | 吸附最大帧数 |
| `--end-offset-frames` | `2` | 出点在场景切换前的偏移帧数 |
| `--min-scene-interval-frames` | `2` | 最小场景间隔 (Subtitle Edit 兼容, Netflix: 7) |
| `--min-duration` | `1.0` | 最短字幕时长 |
| `--max-duration` | `8.0` | 保留兼容参数；JSON 美化不再用它截断整句 |
| `--aggressive` | — | 激进模式: threshold=0.08 snap=12 min-interval=1 |
| `--no-scene-snap` | — | 跳过场景吸附 |

## 调试输出

脚本会显示 segment 数、fps 和场景切换数量。
