---
name: knowledge
description: 在美化后、翻译前建立 glossary.md，提升术语一致性和校对准确度
platform: Agent + Script
---

# 术语知识库

当前项目有两种建立 glossary 的方式：

1. 自动脚本：[`glossary_builder.py`](/G:/Subtitle%20translation/glossary_builder.py)
2. 外部 agent：由用户自己提供更强模型，人工审阅后落地 `glossary.md`

## 推荐时机

`beautify -> glossary -> translate`

这样 glossary 能同时参与翻译后的校对阶段，通常一轮正式校对就够。

## 自动脚本行为

输入：

- 优先 `.beautified.srt`
- 回退 `.srt`

附加上下文：

- `.description`
- `.tags.txt`
- `.info.json`

联网搜索：

- 如果配置了 `TAVILY_API_KEY`，优先联网搜索
- `TAVILY_MAX_RESULTS` 控制每次搜索结果上限，默认 10
- 如果没有 web search 能力，则回退离线总结

输出：

- `glossary.md`

## 外部 agent 工作要求

1. 先读字幕主线内容，理解视频主题
2. 再结合 `.description`、`.tags.txt`、`.info.json`
3. 如果可联网，优先搜索术语标准译法、背景概念、作者涉及的领域知识
4. 如果当前没有 web_search / MCP，则离线总结，不要因此中断
5. 只保留对当前视频真正重要的术语、概念、态度和核心论点

## glossary 内容建议

- 背景
- 核心术语
- 态度基调
- 关键论点

## 重要约束

- 不要把“离线生成”“联网生成”之类元信息写进 glossary
- glossary 是给后续翻译/校对 prompt 直接注入的
- 不确定的术语宁可少写，也不要硬猜一堆
