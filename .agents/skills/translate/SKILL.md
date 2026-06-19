---
name: translate
description: LLM 翻译 WhisperX JSON，输出 .<source>.proofread.ass / .<target>.ass / .<source>-<target>.ass
platform: Win + Linux
---

# 翻译与校对

目标：以 WhisperX `.json` 为唯一字幕输入，完成 JSON 时间轴美化、glossary、整句翻译、同步分割、词级对轴、split event 校对和 ASS 导出。

## 输入

- 优先：`.beautified.json`
- 回退：原始 `.json`
- 可选上下文：
  - `.description`
  - `.tags.txt`
  - `.info.json`
  - `glossary.md`

## 输出

- `.beautified.json`
- `.split.<source>.srt`
- `.split.<target>.srt`
- `.<source>.proofread.ass`
- `.<target>.ass`
- `.<source>-<target>.ass`
- `.<target>.description`

## 当前实现

- 核心脚本：`translate_srt.py`

## 流程

1. 读取 WhisperX JSON 到 dataclass transcript
2. 词源级时间轴美化，输出 `.beautified.json`
3. 若 `glossary.md` 不存在，读取整句 transcript 和元数据生成 glossary
4. 使用 JSON 整句 segment 翻译
5. 对未校对整句源/目标语言文本同步分割；显式 `--no-split` 时跳过分割但继续导出 ASS
6. 用每个源语言分割片段的首尾 word 顺序匹配美化后的 `words[]`，回填 split event 起止时间
7. 对已分割、已对轴的 split events 做最终双语校对，不改变时间轴或事件数量
8. 输出 split 级 `.split.<source>.srt`、`.split.<target>.srt` 和最终 `.<source>.proofread.ass`、`.<target>.ass`、`.<source>-<target>.ass`
9. 若存在 `.description`，同时生成 `.<target>.description`

## Prompt 来源

- `translate_prompt.md` 或 `translate_prompt.example.md`
- `proofread_prompt.md` 或 `proofread_prompt.example.md`
- `split_prompt.md` 或 `split_prompt.example.md` 仅用于微调分割风格；`translate_srt.py` 会在其后追加内置 `_SPLIT_FORMAT`
- prompt 文件可使用 `${SOURCE_LANG}`、`${TARGET_LANG}`、`${SOURCE_LANG_CODE}`、`${TARGET_LANG_CODE}` 模板变量

## 关键点

- 不再读取或生成 SRT 缓存
- `.beautified.json` 是主缓存，保存 `translation` / `proofread_text` / `split_events`
- `SOURCE_LANG` / `TARGET_LANG` 可写 ISO 代码、BCP-47 标签或语言名；输出文件语种后缀通过 `langcodes` 规范为 ISO 639 代码
- `.<source>-<target>.ass` 使用校对后的源语言文本
- AI 分割结果必须保留 `id` 和源/目标 ISO 639 语言代码 key，例如 `id/en/zh`。源/目标段数必须一致，并且源语言片段能还原未校对整句、首尾 word 能对齐 `words[]`；否则整句回退到 beautified 时间轴
- token normalize 仅用于匹配，不改显示文本；词内 dash/hyphen 会被忽略，带空格 dash 仍作为分隔
- 源语言校对发生在 split event 上，只做 ASR 纠错和轻量可读性修正，不改变事件数量或时间轴；目标语言翻译可以按目标语言表达自然调整
- 禁止本地强切 fallback，宁可不分割也不把源/目标文本硬切错位
- glossary 推荐在翻译前建立，这样正式校对就能直接受术语约束
