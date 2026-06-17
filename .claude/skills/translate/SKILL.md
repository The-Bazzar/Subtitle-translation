---
name: translate
description: LLM 翻译英文字幕为中文 — 输出双语 .zh-en.ass
platform: Win + Linux
---

# 字幕翻译 (Win + Linux)

将英文 SRT 通过 LLM API 翻译为中文，输出双语 `.zh-en.ass`。**Python 脚本，跨平台**。

输入：已美化的 `.beautified.srt`（或原始 `.srt`）。

## 执行

```bash
python translate_srt.py video.beautified.srt
python translate_srt.py video.srt --provider deepseek
# 交叉校对
python translate_srt.py video.srt --provider deepseek --proofread-provider openrouter
```

## 输出

```
视频目录/
├── 视频标题.zh.srt         # 中文缓存 (二次运行跳过 LLM)
├── 视频标题.zh.ass         # 仅中文 ASS
├── 视频标题.zh-en.ass      # 双语 ASS ✨
└── 视频标题.zh.description # 中文简介
```

## 流程

```
Pass 1: English → LLM → 中文初译 (.zh.srt)
Pass 2: (English + 初译) → LLM → 中文精校
Pass 3: (English + 精校 + glossary.md) → LLM → 术语校对 (可选)
```

## 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--provider` | openrouter | 翻译后端 |
| `--proofread-provider` | 同翻译 | 校对后端 |
| `--proofread-model` | 同翻译 | 校对模型 |
| `--glossary` | 自动检测 | blog.md 路径 |

## 提示词

翻译/校对提示词从文件加载：
- `translate_prompt.md` (用户自定义) → `translate_prompt.example.md` (模板) → 内置回退
- `proofread_prompt.md` (用户自定义) → `proofread_prompt.example.md` (模板) → 内置回退

## 自定义 LLM 后端

编辑 `providers.json`：
```json
{"my_api": {"url": "...", "default_model": "...", "env_key": "MY_KEY", "auth_header": "Bearer {api_key}", "extra_headers": {}}}
```
