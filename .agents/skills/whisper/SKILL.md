---
name: whisper
description: WhisperX 语音识别 — 从视频生成词级 JSON
platform: Win + Linux
---

# WhisperX 语音识别

目标：从已下载的视频生成 WhisperX `.json`，作为后续所有字幕处理的唯一输入。

## 输入

- 视频文件 `<video>.<ext>`
- 可选：同目录 `<video>.info.json`，用于读取语言

## 输出

- `<video>.json`

## 当前实现

- Windows：`whisper.ps1`
- Linux：`whisper.sh`

## 关键行为

1. 若 `.json` 已存在，则直接跳过
2. 先从视频提取单声道 `16k wav`
3. 调用 `whisperx --output_format json`
4. 输出 `<video>.json`
5. 删除临时 `.wav`

## 环境变量

| 变量 | 说明 |
|---|---|
| `WHISPER_MODEL` | ASR 模型，默认 `large-v3-turbo` |
| `WHISPER_ALIGN_MODEL` | 对齐模型，留空则按语言自动匹配 |
| `WHISPER_DEVICE` | `cuda` 或 `cpu` |

## 注意

- 当前项目不再依赖 SRT
- `translate_srt.py` 以 WhisperX `.json` 为入口
- 翻译、校对、分割使用 JSON 里的整句 segment
- 分割后再用 `words[]` 对齐每条字幕事件的起止时间
