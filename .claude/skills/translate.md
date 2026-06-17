---
name: translate
description: LLM 翻译英文字幕为中文 — 输出双语 .zh-en.ass
---

# 字幕翻译

将英文 SRT 通过 LLM API 翻译为中文，输出双语 `.zh-en.ass`（bi-en + bi-zh）用于硬压。

## 输入

**已美化的 `.beautified.srt`** 字幕文件（或原始 `.srt`）。


## 执行方式

```bash
# 基础翻译 + 校对 (默认开启)
python3 translate_srt.py video.beautified.srt

# 指定后端
python3 translate_srt.py video.srt --provider deepseek --model deepseek-v4-pro

# 交叉校对: DeepSeek 翻译 → Claude 校对
python3 translate_srt.py video.srt \
    --provider deepseek \
    --proofread-provider openrouter \
    --proofread-model anthropic/claude-sonnet-4-6
```

## 输出

```
视频目录/
├── 视频标题.zh.srt         # 中文 SRT 翻译缓存
├── 视频标题.zh.ass         # 仅中文 ASS (style=zh)
├── 视频标题.zh-en.ass      # 双语 ASS (bi-en + bi-zh) ✨
└── 视频标题.zh.description # 中文简介 (含标题/标签翻译)
```

- `.zh.srt` 存在时**自动跳过 LLM**，直接合成 ASS
- 中文按 Netflix 规范自动去除标点（仅保留 `、` `《》`），自动 `\N` 换行

## 翻译参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--provider` | openrouter | 翻译后端 |
| `--model` | 后端默认 | 翻译模型 |
| `--batch-size` | `50` | 每批翻译行数 |
| `--proofread` | 开启 | 中英校对 |
| `--proofread-provider` | 同翻译 | 校对后端 |
| `--proofread-model` | 同翻译 | 校对模型 |
| `--glossary` | 自动检测 | glossary.md 路径 (术语知识库) |
| `--title` | SRT 文件名 | 视频标题 |
| `-o, --output` | 自动 | 输出 `.zh-en.ass` |
| `-q, --quiet` | — | 静默模式 |

## 两轮校对 (+ 可选第三轮术语校对)

```
Pass 1: English → LLM → 中文初译 (.zh.srt)
Pass 2: (English + 中文初译) → LLM → 中文精校 (.zh.srt 覆盖)
Pass 3: (English + 中文精校 + glossary.md) → LLM → 术语一致性校对
```

Pass 3 需要先在视频目录下建立 `glossary.md` 术语知识库。可通过 Claude Code 的 `knowledge` skill 让 Agent 浏览字幕自动生成，也可手动编写。

`translate_srt.py` 会自动检测 SRT 同目录下的 `glossary.md`，存在则将其内容注入校对 prompt 中进行第三次术语一致性校对。也可用 `--glossary` 手动指定路径。

> **注意**：glossary 的质量取决于建立它的 Agent 能力。越强的模型抽取的术语越准确。

## 提示词配置

翻译/校对提示词从文件加载，不再硬编码在脚本或 `.env` 中：

```
translate_prompt.md   (用户自定义, gitignored)
  ↕ 不存在则回退
translate_prompt.example.md  (仓库模板)

proofread_prompt.md  (用户自定义, gitignored)
  ↕
proofread_prompt.example.md  (仓库模板)
```

使用方式：`cp translate_prompt.example.md translate_prompt.md` 后自行编辑。

## .env 配置

```ini
TRANSLATE_PROVIDER=deepseek
TRANSLATE_MODEL=deepseek-v4-pro
PROOFREAD_PROVIDER=
PROOFREAD_MODEL=
DEEPSEEK_API_KEY=sk-xxx
```

## 自定义 LLM 后端

编辑 `providers.json` 添加自定义供应商：

```json
{
    "ollama": {
        "url": "http://localhost:11434/v1/chat/completions",
        "default_model": "qwen2.5",
        "env_key": "OLLAMA_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {}
    }
}
```

## 注意事项

- 翻译消耗 API 额度，长视频 (~200 条) 约 30K-50K tokens
- `.zh.srt` 作为翻译缓存，二次运行零 API 消耗
- API key 从项目根目录 `.env` 读取 (gitignored)
- 网络不通时自动重试 3 次
