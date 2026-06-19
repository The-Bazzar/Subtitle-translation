---
name: translate
description: LLM 翻译英文字幕为中文，输出 .zh.srt / .proofread.srt / .zh.ass / .zh-en.ass
platform: Win + Linux
---

# 翻译与校对

目标：把英文字幕转换成双语成果物，并把术语知识库注入到翻译和校对阶段。

## 输入

- 优先：`.beautified.srt`
- 回退：原始 `.srt`
- 可选上下文：
  - `.json`
  - `.description`
  - `.tags.txt`
  - `glossary.md`

## 输出

- `.split.srt`
- `.zh.srt`
- `.proofread.srt`
- `.zh.ass`
- `.zh-en.ass`
- `.zh.description`

## 当前实现

- 核心脚本：[`translate_srt.py`](/G:/Subtitle%20translation/translate_srt.py)

## 流程

1. 读取英文 SRT
2. 若存在 `.split.srt`，跳过分句
3. 否则用 LLM 做长句自然拆分，并尽量用 WhisperX `.json` 词级时间码精确对轴
4. 若存在 `.zh.srt`，跳过初译
5. 否则执行翻译
6. 默认执行中英双语校对
7. 若存在 `glossary.md`，自动注入翻译和校对提示词
8. 输出 `.proofread.srt`、`.zh.ass`、`.zh-en.ass`
9. 若存在 `.description`，同时生成 `.zh.description`

## 配置来源

当前脚本依赖 `.env`：

- `TRANSLATE_PROVIDER`
- `TRANSLATE_MODEL`
- `PROOFREAD`
- `PROOFREAD_PROVIDER`
- `PROOFREAD_MODEL`

如果没有配置 `TRANSLATE_PROVIDER`，脚本会直接报错。

## Prompt 来源

- `translate_prompt.md` 或 `translate_prompt.example.md`
- `proofread_prompt.md` 或 `proofread_prompt.example.md`

代码本身还会强制要求编号格式，避免用户自定义 prompt 时把输出结构搞乱。

## 关键点

- `.proofread.srt` 保存校对后的英文
- `.zh-en.ass` 使用校对后的英文，而不是原始 ASR 英文
- glossary 推荐在翻译前建立，这样一轮正式校对就够用
