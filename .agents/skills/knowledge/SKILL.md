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

模型配置：

- `GLOSSARY_PROVIDER` / `GLOSSARY_MODEL` 专用于 glossary agent、Tavily tool calling 和最终术语定稿
- 留空则回退到 `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL`
- 术语知识库会影响后续翻译和校对记忆，用户应使用可用范围内最顶级模型，不要用弱模型省成本

联网搜索：

- 如果配置了 `TAVILY_API_KEY`，优先联网搜索
- `TAVILY_MAX_RESULTS` 控制每次搜索结果上限，默认 20
- glossary 默认在同一个 ChatSession 中使用 `tavily_search` tool calls；脚本执行 Tavily 后把 tool result 回喂同一 session
- `TAVILY_MAX_QUERIES` 是统一搜索预算：tool-call 路径下表示最多执行多少次 Tavily 查询，fallback 路径下表示单一语言 query 上限；设为 0 可禁用 Tavily
- glossary 阶段会强制移除 provider 的 JSON mode `response_format`，避免干扰 tool calling；最终仍要求返回 JSON object
- 第一轮会把合并后的 `tavily_domains.json` 域名偏好喂给模型
- `tavily_domains.json` 维护全局百科域名和视频题材相关站点；脚本会根据 metadata、模型给出的 query / `topic_hints` 选择站点，先用 `include_domains` 搜索，结果不足再普通搜索
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
4. 联网搜索由 glossary 模型通过 `tavily_search` tool calls 发起；query 应精简、纠正 ASR 错误，并偏向百科、wiki、fandom、Bangumi、萌娘百科等题材相关站点
5. 联网搜索结果应作为 glossary 的优先证据来源，用于校正 transcript 中可能的 ASR 人名、标题、引文和术语错误
6. 如果当前没有 web_search / MCP，则离线总结，不要因此中断
7. 只保留对当前视频真正重要的术语、概念、态度和核心论点

## glossary 内容建议

- 背景
- 核心术语
- 态度基调
- 关键论点

## 重要约束

- 普通 pipeline 中已存在且非空的 `glossary.md` 不需要重新总结
- 手动运行 `--only-glossary` 时忽略已有缓存，重新生成并覆盖 `glossary.md`
- 不要把“离线生成”“联网生成”之类元信息写进 glossary
- glossary 是后续翻译、校对和视频简介翻译的常驻 system prompt 硬规则，不会因为启用 embedding 而省略
- embedding / Chroma 召回的 `retrieved_context` 只是逐条动态补充记忆，不能替代完整 glossary
- 不确定的术语宁可少写，也不要硬猜一堆
