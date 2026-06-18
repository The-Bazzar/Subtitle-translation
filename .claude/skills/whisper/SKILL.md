---
name: whisper
description: WhisperX 语音识别 — 从视频生成英文字幕和词级 JSON
platform: Win + Linux
---

# WhisperX 语音识别

目标：从已下载的视频生成英文字幕 `.srt` 和词级时间码 `.json`，供后续美化和分句使用。

## 输入

- 视频文件 `<video>.<ext>`
- 可选：同目录 `<video>.info.json`，用于读取语言

## 输出

- `<video>.srt`
- `<video>.json`
- 以及 WhisperX 的 `.txt/.tsv/.vtt`

## 当前实现

- Windows：[`whisper.ps1`](/G:/Subtitle%20translation/whisper.ps1)
- Linux：[`whisper.sh`](/G:/Subtitle%20translation/whisper.sh)

## 关键行为

1. 若 `.srt` 已存在，则直接跳过
2. 先从视频提取单声道 `16k wav`
3. 调用 `whisperx --output_format all`
4. 输出 `.srt + .json`
5. 删除临时 `.wav`

## 环境变量

| 变量 | 说明 |
|---|---|
| `WHISPER_MODEL` | ASR 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | 对齐模型，留空则按语言自动匹配 |
| `WHISPER_DEVICE` | `cuda` 或 `cpu` |

## 安装

CPU:

```bash
whisperx audio.mp3 --device cpu
```

CUDA 12.8:

```bash
uv tool install git+https://github.com/m-bain/whisperx.git \
  --with "torch==2.8.0+cu128" \
  --with "torchaudio==2.8.0+cu128"
```

运行时：

```bash
TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 whisperx audio.mp3 --device cuda
```

## 对齐模型建议

- 英文顶级：`facebook/wav2vec2-large-960h-lv60-self`
- 英文次选：`facebook/wav2vec2-base-960h`
- 多语言：`facebook/wav2vec2-large-xlsr-53`
- 通用替代：`facebook/mms-1b-fl102`

## 注意

- 当前项目不再依赖 WhisperX 自己的断句参数
- 长句拆分交给 `translate_srt.py`
- `translate_srt.py` 会优先用 `.json` 的词级时间码给 `.split.srt` 重新对轴
