---
name: translate
description: LLM 翻译英文字幕为中文 — 输出双语 .zh-en.ass
platform: Win + Linux
---

# 字幕翻译 (Win + Linux)

将英文 SRT 通过 LLM API 翻译为中文，输出双语 `.zh-en.ass`。内置 LLM 长句拆分 + 两轮校对 + 术语注入。**Python 脚本，跨平台**。

输入：WhisperX 生成的 `.srt`（或已美化的 `.beautified.srt`）。

## 执行

```bash
python translate_srt.py video.srt
python translate_srt.py video.srt --no-split          # 禁用长句拆分
python translate_srt.py video.srt --split-max-chars 50 # 更激进拆分
```

## 输出

```
视频目录/
├── 视频标题.split.srt      # LLM 分句中间成果 ✨
├── 视频标题.zh.srt         # 中文翻译缓存
├── 视频标题.zh.ass         # 仅中文 ASS
├── 视频标题.zh-en.ass      # 双语 ASS ✨
└── 视频标题.zh.description # 中文简介
```

## 流程

```
Step 0: LLM 分句 — 长句 (>60 chars 或 >3s) → 自然语言边界拆分
        优先用 WhisperX JSON 词级时间码精确对轴 → .split.srt

Step 1: Pass 1 翻译 — English → LLM → 中文初译
Step 2: Pass 2 校对 — (English + 初译) → LLM → 中文精校
Step 3: Pass 3 术语 — (English + 精校 + glossary.md) → 术语一致性校对
```

## 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--no-split` | — | 禁用长句拆分 |
| `--split-max-chars` | `60` | 拆分触发字符数 |
| `--split-max-duration` | `3.0` | 拆分触发时长 (秒) |
| `--no-proofread` | — | 禁用校对 |
| `--glossary` | 自动检测 | glossary.md 路径 |
| `--batch-size` | `50` | 每批翻译条数 |

## 提示词

翻译/校对提示词从文件加载：
- `translate_prompt.md` → `translate_prompt.example.md` → 内置回退
- `proofread_prompt.md` → `proofread_prompt.example.md` → 内置回退

## 自定义 LLM 后端

编辑 `providers.json`：
```json
{"my_api": {"url": "...", "default_model": "...", "env_key": "MY_KEY", "auth_header": "Bearer {api_key}"}}
```
