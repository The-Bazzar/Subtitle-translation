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

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WHISPER_MODEL` | `large-v3-turbo` | ASR 模型 |
| `WHISPER_ALIGN_MODEL` | 空 | 对齐模型 (空=按语言自动匹配) |
| `WHISPER_DEVICE` | `cuda` | 推理设备: cuda / cpu |

## 输出

```
视频目录/
├── 视频标题.srt            # 英文 SRT 字幕 ✨
└── 视频标题.json           # 词级时间码 (供 split_srt.py 分句用)
```

## 句子拆分

WhisperX 原生的 `--segment_resolution` 是机械切分，不认语法边界。改用 `split_srt.py` 后处理：

```bash
# LLM 辅助自然语言分句 (需要 API key)
python split_srt.py video.srt
# 输出: video.split.srt (36 条 → 96 条, 在逗号/从句/连词处拆分)
```

时间码来自 `.json` 词级时间戳，精确到微秒；无 JSON 时自动回退字符比例。

## 安装与运行

两种方式，选其一：

### 方式 1：CPU（无需 CUDA）

```powershell
# 直接运行，WhisperX 自动用 CPU
whisperx audio.mp3 --device cpu
```

### 方式 2：CUDA 12.8 加速（推荐）

**首次安装**：

```powershell
# Windows PowerShell
uv tool install git+https://github.com/m-bain/whisperx.git `
  --with "torch==2.8.0+cu128" `
  --with "torchaudio==2.8.0+cu128"
```

```bash
# Linux / WSL
uv tool install git+https://github.com/m-bain/whisperx.git \
  --with "torch==2.8.0+cu128" \
  --with "torchaudio==2.8.0+cu128"
```

**每次运行**：
```powershell
# Windows PowerShell
& { $env:TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD="1"; whisperx audio.mp3 --device cuda }
```

```bash
# Linux / WSL
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 whisperx audio.mp3 --device cuda
```

> **注意**：本地 CUDA 版本必须 ≥ 12.8，否则用方式 1。`compute_type` 由 WhisperX 自动检测。

## 注意事项

- `.srt` 已存在时自动跳过
- 语言自动从 `.info.json` 读取，fallback `en`
- `--max_line_width/--max_line_count` 需要 alignment (--no_align 时无效)
- CUDA 版本不匹配时用 `WHISPER_DEVICE=cpu` 回退
