---
name: whisper
description: WhisperX 语音识别 — 从视频生成英文字幕 (.srt)
platform: Win + Linux
---

# 语音识别 (Win + Linux)

使用 WhisperX large-v3-turbo 从视频生成英文 SRT 字幕。

输入来自 [[download]] skill 的视频文件。

## Win — PowerShell

```powershell
.\whisper.ps1 "C:\path\to\video.webm"

# 调参 (句子太长时)
.\whisper.ps1 "video.webm" -ChunkSize 10 -SegmentResolution sentence -MaxLineWidth 36
```

## Linux — Bash

```bash
./whisper.sh "/path/to/video.webm"

# 调参 (句子太长时)
WHISPER_CHUNK_SIZE=10 WHISPER_MAX_LINE_WIDTH=36 ./whisper.sh "video.webm"
```

## 核心参数 (句子分割)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--segment_resolution` | `sentence` | **sentence**=句子级(推荐) / chunk=原始长段 |
| `--chunk_size` | `15` | 处理块大小秒 (WhisperX 原始: 30, 越小段越短) |
| `--max_line_width` | `42` | 每行最大字符数 (字幕标准, 需 alignment) |
| `--max_line_count` | `2` | 每段最大行数 (需 alignment) |
| `--condition_on_previous_text` | `false` | 关掉让每段独立 (不会连成长句) |
| `--vad_onset` | `0.5` | VAD 语音起始阈值 (通常不改) |
| `--vad_offset` | `0.363` | VAD 语音结束阈值 (通常不改) |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WHISPER_MODEL` | `large-v3-turbo` | ASR 模型 |
| `WHISPER_ALIGN_MODEL` | 空 | 对齐模型 (空=按语言自动匹配) |
| `WHISPER_SEGMENT_RESOLUTION` | `sentence` | 分割粒度 |
| `WHISPER_MAX_LINE_WIDTH` | `42` | 每行最大字符数 |
| `WHISPER_MAX_LINE_COUNT` | `2` | 每段最大行数 |
| `WHISPER_CHUNK_SIZE` | `15` | 处理块大小秒 |
| `WHISPER_CONDITION_ON_PREVIOUS` | `false` | 前文 prompt 开关 |

## 调参指南

| 问题 | 解决方案 |
|------|---------|
| **句子太长** (最常⻅) | `--segment_resolution sentence --chunk_size 10` |
| **句子太碎/太短** | `--segment_resolution chunk --chunk_size 30` |
| **字幕行溢出屏幕** | `--max_line_width 36` |
| **单字行太多** | `--max_line_width 50 --max_line_count 3` |
| **段落粘连** | `--condition_on_previous_text false` |

## 输出

```
视频目录/
└── 视频标题.srt            # 英文 SRT 字幕 ✨
```

## GPU 加速

需安装 CUDA Toolkit。首次运行需下载 `large-v3-turbo` 模型 (~1.5GB)。

## 注意事项

- `.srt` 已存在时自动跳过
- 语言自动从 `.info.json` 读取，fallback `en`
- `--max_line_width/--max_line_count` 需要 alignment (--no_align 时无效)
