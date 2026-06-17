---
name: knowledge
description: 从字幕中建立术语知识库，基于知识库进行第三次校对
platform: Agent (跨平台)
---

# 术语知识库 + 第三次校对

由 **用户提供的 AI Agent**（如 Claude Code）浏览英文字幕和中文翻译，建立术语知识库 `glossary.md`，然后 `translate_srt.py` 自动注入校对。

## 工作流程

### 阶段 1：Agent 建立知识库

用户触发 Agent 浏览视频文件夹下的 `.srt` + `.zh.srt`，抽取：

1. **核心主题词汇** — 该视频主题的关键词/句式
2. **专业术语** — 需统一理解的学术/技术概念
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
