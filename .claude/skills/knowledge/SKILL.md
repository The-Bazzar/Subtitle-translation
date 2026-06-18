---
name: knowledge
description: 从字幕中建立术语知识库，基于知识库进行第三次校对
platform: Agent (跨平台)
---

# 术语知识库 + 第三次校对

由 **用户提供的 AI Agent**（如 Claude Code）浏览英文字幕和中文翻译，建立术语知识库 `glossary.md`，然后 `translate_srt.py` 自动注入校对。

## 工作流程

### 阶段 1：Agent 建立知识库

用户触发 Agent 浏览视频文件夹下的 `.srt` + `.zh.srt`，同时读取 `.description`、`.tags.txt`、`.info.json` 获取上下文。

**Agent 必须优先联网搜索**，提高术语准确度：

1. 从 `.tags.txt` 提取标签，结合标题/频道名，用 `WebSearch` 或 MCP 搜索术语的标准译法和视频主题背景
2. 如有 `WebFetch`，抓取搜索结果中的权威页面（维基百科、官方文档）验证术语含义
3. **如果 WebSearch / MCP 均不可用**，静默回退到离线模式，用模型自身知识 + 字幕上下文推断

抽取内容：

1. **核心主题词汇** — 该视频主题的关键词/句式
2. **专业术语** — 需统一理解的学术/技术概念（联网验证标准译法）
3. **作者态度基调** — 感情倾向和观点立场
4. **核心观点** — 全文反复出现的关键论点

Agent 输出 `glossary.md` 到视频目录。

### 阶段 2：翻译脚本自动注入

```bash
python translate_srt.py video.beautified.srt
# 自动检测同目录 glossary.md → 注入校对 prompt → 术语一致性校对
```

## 注意事项

- **Agent 由用户提供**，项目脚本不自动生成 glossary
- 知识库质量取决于 Agent 模型能力，越强的模型抽取越准
- 建议人工复核 `glossary.md`
- 每个视频独立的知识库，不跨视频复用
