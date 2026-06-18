---
name: knowledge
description: 美化后/翻译前建立术语知识库，配合双语校对实现一轮审校
platform: Agent (跨平台)
---

# 术语知识库 + 双语校对

由 **用户提供的 AI Agent** 浏览美化/分句后的英文字幕，建立术语知识库 `glossary.md`，然后 `translate_srt.py` 自动注入实现**一轮双语审校**（英文 ASR 纠错 + 中文校对 + 术语一致性）。

## 推荐时机

```
beautify/split → knowledge (glossary.md) → translate (翻译 + 双语校对)
```

在翻译之前建立知识库，glossary 可同时注入翻译和校对两轮 prompt。

## Agent 工作流程

### 步骤 0：收集上下文

先读取视频目录下所有可用文件：

| 文件 | 用途 |
|------|------|
| `<video>.srt` / `.beautified.srt` / `.split.srt` | 英文字幕（优先美化/分句后的） |
| `<video>.zh.srt` | 中文翻译（如有），检查已有译法 |
| `<video>.description` | 视频简介，理解主题和语境 |
| `<video>.tags.txt` | 视频标签，搜索关键词直接来源 |
| `<video>.info.json` | yt-dlp 元数据（标题、频道、上传日期） |

### 步骤 1：联网搜索（优先）

**必须优先尝试联网搜索**，提高术语准确度：

1. 从 `.tags.txt` 提取标签，结合标题/频道名，用 `WebSearch` 或 MCP 搜索术语的标准译法和视频主题背景
2. 如有 `WebFetch`，抓取搜索结果中的权威页面（维基百科、官方文档）验证术语含义
3. **如果 WebSearch / MCP 均不可用**，静默回退到离线模式，用模型自身知识 + 字幕上下文推断

### 步骤 2：生成 glossary.md

综合字幕分析 + 搜索结果，输出 `glossary.md` 到视频目录：

```markdown
# 术语知识库 — <视频标题>

## 背景
<.tags.txt + .description + 搜索结果，2-3 句概括视频主题>

## 核心术语
| 英文 | 推荐译法 | 说明 |
|------|---------|------|
| transformer | Transformer 架构 | 深度学习模型，不译"变压器" |

## 态度基调
- 作者对 <X> 持批判/支持/中立态度

## 关键论点
- （全文反复出现的核心观点）
```

> **重要**：`glossary.md` 直接注入校对 prompt，**不要**在其中添加"离线生成""联网搜索"等元信息。

### 步骤 3：翻译脚本自动注入

```bash
python translate_srt.py video.beautified.srt
# 自动检测同目录 glossary.md → 注入翻译 + 校对 prompt → 双语审校
```

校对 prompt 会先检查英文 ASR 错误，再检查中文翻译质量，一轮完成。

## 注意事项

- **Agent 由用户提供**，项目脚本不自动生成 glossary
- 每个视频独立的知识库，不跨视频复用
- `glossary.md` 内容越精准越好，不确定的术语宁可省略
- 建议在美化/分句后、翻译前建立知识库
- 建议人工复核后重新运行校对
