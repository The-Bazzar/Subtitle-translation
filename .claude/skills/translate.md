---
name: translate
description: 从 YouTube URL 或本地视频生成双语 .zh-en.ass 字幕（下载 → 美化 → 翻译）
---

# 字幕翻译技能

一键完成英→中字幕翻译流水线：时间码美化 + LLM 翻译 → 双语 `.zh-en.ass`。

## 输入

技能接受两种输入：

| 输入类型 | 示例 | 行为 |
|---------|------|------|
| **YouTube URL** | `https://www.youtube.com/watch?v=xxxxx` | 下载 → 美化 → 翻译 (全流程) |
| **本地视频文件** | `C:\path\to\video.webm` 或 `/mnt/c/.../video.webm` | 美化 → 翻译 (跳过下载) |

## 执行方式

### 方式 A: YouTube URL (PowerShell 超级流水线)

```powershell
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx" -SkipBurn
```

### 方式 B: YouTube URL (WSL 流水线)

```bash
BURN=0 ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
```

### 方式 C: 本地视频文件 (WSL)

```bash
# 已有视频 + 英文字幕 → 直接美化 + 翻译
SKIP_DOWNLOAD=1 BURN=0 ./pipeline.sh "dummy-url"
# 输入视频路径后自动: 美化 → 翻译
```

或直接分步：
```bash
# 1. 时间码美化 (如未美化)
./beautify_srt.sh "path/to/video.webm"

# 2. 翻译 (自动检测 .zh.srt 缓存)
python3 translate_srt.py "path/to/video.beautified.srt"
```

### 方式 D: 仅翻译已有 SRT

```bash
python3 translate_srt.py "path/to/subtitle.srt"
```

## 输出

```
视频目录/
├── 视频标题.srt            # 原始英文字幕
├── 视频标题.beautified.srt # 美化后英文字幕 (Netflix 规范)
├── 视频标题.zh.srt         # 中文 SRT 翻译缓存
├── 视频标题.zh.ass         # 仅中文 ASS
└── 视频标题.zh-en.ass      # 双语 ASS (bi-en + bi-zh) ✨
```

- `.zh.srt` 已存在时**自动跳过 LLM**，直接从缓存生成 ASS
- `.zh-en.ass` 中英文在前 (bi-en, 36px)，中文在后 (bi-zh, 72px)
- 中文自动插入 `\N` 软换行（解决 CJK 无空格无法自动换行问题）

## 工作流程

```
YouTube URL                           本地视频文件
    │                                      │
    ▼                                      │
download_and_sub.sh                        │
  ├─ yt-dlp 下载视频                        │
  ├─ WhisperX 生成英文字幕 (.srt)            │
  └─ OUTPUT_VIDEO=...                      │
    │                                      │
    ▼                                      ▼
beautify_srt.sh
  ├─ ffmpeg 场景检测
  ├─ 时间码吸附对齐 (Netflix 规范)
  └─ 输出 .beautified.srt
    │
    ▼
translate_srt.py
  ├─ Pass 1: 检测 .zh.srt 缓存 → 跳过 LLM
  ├─ 或: LLM API 分批翻译 (英→中)
  ├─ (可选) Pass 2: LLM 中英校对 (--proofread)
  ├─ 输出 .zh.srt (缓存, 校对后覆盖)
  ├─ 输出 .zh.ass (仅中文)
  └─ 输出 .zh-en.ass (双语) ✨
```

## API 后端

| 后端 | 默认模型 | 特点 |
|------|---------|------|
| `openrouter` | `anthropic/claude-sonnet-4-6` | 质量最高 |
| `deepseek` | `deepseek-chat` | 性价比高 |
| `gemini` | `gemini-2.5-pro` | 免费额度大 |

从 `.env` 读取 `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL`：
```ini
TRANSLATE_PROVIDER=deepseek
TRANSLATE_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=sk-xxx
```

## 翻译参数 (translate_srt.py)

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--provider` | openrouter | LLM 后端 |
| `--model` | 后端默认 | 模型名称 |
| `--batch-size` | `50` | 每批翻译行数 |
| `--proofread` | 开启 | 中英校对 (默认开启) |
| `--proofread-provider` | 同翻译 | 校对专用后端 (交叉校对) |
| `--proofread-model` | 同翻译 | 校对专用模型 |
| `--proofread-prompt` | 内置默认 | 自定义校对提示词 |
| `--system-prompt` | 内置默认 | 自定义翻译提示词 |
| `--title` | SRT 文件名 | 视频标题 (写入 ASS) |
| `--template` | `./template.ass` | ASS 模板 |
| `-o, --output` | 自动 | 输出 `.zh-en.ass` 路径 |
| `-q, --quiet` | — | 静默模式 |

### 两轮校对模式 (`--proofread`)

```
Pass 1: English → LLM → 中文初译 (.zh.srt)
Pass 2: (English + 中文初译) → LLM → 中文精校 (.zh.srt 覆盖)
```

两轮 LLM 调用上下文隔离，校对轮独立审校不继承翻译轮思维链。

```bash
# 开启校对
python3 translate_srt.py video.srt --proofread

# 校对轮用自定义提示词
python3 translate_srt.py video.srt --proofread --proofread-prompt "你的校对提示词"

# 交叉校对: 翻译用 DeepSeek, 校对用 OpenRouter Claude
python3 translate_srt.py video.srt --provider deepseek --proofread-provider openrouter --proofread-model anthropic/claude-sonnet-4-6
```

## .env 提示词配置

```ini
# 翻译/校对系统提示词 (留空使用内置 Netflix 规范默认)
TRANSLATE_SYSTEM_PROMPT=
PROOFREAD_SYSTEM_PROMPT=
# 校对专用后端/模型 (留空则与翻译共用, 可用于交叉校对)
PROOFREAD_PROVIDER=
PROOFREAD_MODEL=
```

优先级：`CLI 参数 > .env > 内置默认`

内置翻译提示词遵循 Netflix 中文规范：去除所有标点（仅保留 `《》`），用空格替代停顿。

## 注意事项

- 翻译消耗 API 额度，长视频 (~200 条) 约 30K-50K tokens
- `.zh.srt` 作为翻译缓存，二次运行零 API 消耗
- API key 从项目根目录 `.env` 读取 (gitignored)
- 网络不通时自动重试 3 次，间隔递增
- WhisperX 首次运行需下载 `large-v3` 模型 (数 GB)
- 本地视频需已有 `.srt` 英文字幕（或让 pipeline 先下载生成）
