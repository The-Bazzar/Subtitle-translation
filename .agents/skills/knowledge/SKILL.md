---
name: knowledge
description: 在 JSON 时间轴美化后、翻译前建立 glossary.md
platform: Agent + Script
---

# 术语知识库

目标：在翻译前建立 `glossary.md`，让翻译和校对共享术语、语气和背景判断。

## 推荐时机

`json beautify -> glossary -> translate`

## 自动脚本行为

当前自动 glossary 已集成到 `translate_srt.py`：

```bash
./.venv/bin/python translate_srt.py video.beautified.json --video video.webm --only-glossary --skip-beautify
```

输入：

- 优先 `.beautified.json`
- 回退 `.json`

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

Prompt：

- `glossary_prompt.md` 或 `glossary_prompt.example.md`
- 只用于微调 glossary 内容策略
- JSON 输出格式由 `translate_srt.py` 内置规则追加，不要在 prompt 文件里改返回格式

## 外部 agent 工作要求

1. 先读 JSON 里的整句 transcript，理解视频主题
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

- 已存在且非空的 `glossary.md` 不需要重新总结
- 不要把“离线生成”“联网生成”之类元信息写进 glossary
- glossary 是给后续翻译和校对 prompt 直接注入的
- 不确定的术语宁可少写，也不要硬猜一堆
