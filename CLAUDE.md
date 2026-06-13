# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

YouTube 视频下载 + WhisperX AI 字幕生成流水线。项目维护在一个 Windows 主机上，核心脚本在 WSL 中运行。

## Architecture

```
Subtitle translation/
├── pipeline.ps1              # Windows/PowerShell: 超级流水线 (URL → burned.mkv)
├── pipeline.sh               # WSL/bash: 流水线 (download → beautify → translate)
├── download_and_sub.sh       # 主流程: 下载视频 → 生成英文字幕
├── beautify_srt.sh           # 字幕时间码美化入口 (调用 beautify_srt.py)
├── beautify_srt.py           # Python: 场景检测 + 帧率自适应 → 美化 SRT 时间码 (Netflix 规范)
├── translate_srt.py          # Python: LLM 翻译 → .zh.srt + .zh.ass + .zh-en.ass (双语硬压)
├── mpv-burn.sh               # WSL/bash: mpv 字幕硬压 (调用 Windows mpv.com)
├── template.ass              # ASS 字幕模板 (Style: zh 定义中文字幕样式)
├── download.ps1              # Windows PowerShell 备用: 仅下载 (不含字幕生成)
├── mpv-burn.ps1              # Windows PowerShell: mpv 字幕硬压 (NVENC)
├── .env                      # API keys + 翻译默认配置 (OpenRouter/DeepSeek/Gemini + TRANSLATE_PROVIDER/MODEL)
├── cookies.txt               # YouTube cookies — gitignored
└── <Video Title>/            # 每个视频独立的输出目录
    ├── <Video Title>.<ext>   # 视频文件 (webm/mp4/mkv)
    ├── <Video Title>.srt     # 原始英文字幕 (WhisperX 生成)
    ├── <Video Title>.beautified.srt  # 美化后的英文字幕 (beautify_srt 输出)
    ├── <Video Title>.zh.srt  # 中文 SRT 翻译缓存
    ├── <Video Title>.zh.ass  # 仅中文 ASS (style=zh)
    ├── <Video Title>.zh-en.ass  # 双语 ASS (bi-en + bi-zh, 硬压用)
    ├── <Video Title>.webp    # 封面缩略图
    ├── <Video Title>.info.json     # yt-dlp 元数据
    └── <Video Title>.description   # 视频简介文本
```

## Key Commands

### 超级流水线 (PowerShell, 推荐)

```powershell
# 一键: YouTube URL → burned.mkv 硬字幕视频
.\pipeline.ps1 "https://www.youtube.com/watch?v=xxxxx"

# 选择翻译后端 + 自定义编码
.\pipeline.ps1 "https://youtu.be/xxxxx" -TranslateProvider deepseek -Ovc libx265 -Ovcopts crf=23

# 只出中文 ASS, 跳过压制
.\pipeline.ps1 "https://youtu.be/xxxxx" -SkipBurn
```

### 主流程 (WSL 中运行)

```bash
# 一键流水线: 下载 + 字幕 + 美化 (推荐)
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 传递美化选项
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2

# 仅下载 + 字幕 (不美化)
SKIP_BEAUTIFY=1 ./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 分步执行
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"
./download_and_sub.sh "URL1" && ./download_and_sub.sh "URL2"
```

### 从 PowerShell 调用 WSL

```powershell
wsl -u root bash -lc "sh ./download_and_sub.sh https://www.youtube.com/watch?v=xxxxx"
```

### 仅下载 (PowerShell, 无字幕)

```powershell
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

### 字幕硬压到视频 (PowerShell)

```powershell
.\mpv-burn.ps1 "C:\path\to\video.webm"
# 输出: burned.mkv (同目录, hevc_nvenc qp=20, aac音频)
```

### 字幕硬压到视频 (WSL)

```bash
# 基础用法 (默认 hevc_nvenc qp=20)
./mpv-burn.sh "path/to/video.webm"

# 指定输出
./mpv-burn.sh video.webm -o result.mkv

# 自定义编码参数
./mpv-burn.sh video.webm --ovc libx265 --ovcopts crf=23 --slang=en,zh

# 自定义 mpv 路径
./mpv-burn.sh video.webm --mpv-path /mnt/c/Apps/mpv.com

# 透传额外 mpv 参数
./mpv-burn.sh video.webm -- --vf-append=vapoursynth="~~/vs/MEMC_RIFE_NV.vpy"
```

### 字幕时间码美化 (WSL 中运行)

```bash
# 自动查找同目录 .srt 并输出 .beautified.srt (不覆盖原文件)
./beautify_srt.sh "path/to/video.webm"

# 指定字幕文件
./beautify_srt.sh video.webm subtitle.srt

# 覆盖原文件 (需显式指定 -o)
./beautify_srt.sh video.webm -o video.srt --backup

# 仅预览变化 (不写入)
./beautify_srt.sh video.webm --preview

# 激进对齐 (剪辑密集)
./beautify_srt.sh video.webm --scene-threshold 0.2 --snap-frames 10

# 保守对齐 (长镜头)
./beautify_srt.sh video.webm --scene-threshold 0.35 --snap-frames 4

# 完整选项列表
./beautify_srt.sh --help
```

### 字幕翻译 (WSL 中运行)

```bash
# 基础翻译 (从 .env 读取 provider/model)
python3 translate_srt.py "视频目录/视频.srt"

# 输出: .zh.srt (缓存) + .zh.ass (仅中文) + .zh-en.ass (双语)
# .zh.srt 已存在时自动跳过 LLM, 直接合成 ASS

# 使用 DeepSeek (性价比高)
python3 translate_srt.py video.srt --provider deepseek

# 使用 Gemini (免费额度大)
python3 translate_srt.py video.srt --provider gemini

# 自定义标题和输出
python3 translate_srt.py video.srt --title "My Video" -o custom.zh-en.ass
```

## Pipeline Steps (pipeline.sh)

1. **下载 + 字幕** — 调用 `download_and_sub.sh`，捕获输出的 `OUTPUT_VIDEO` 路径
2. **时间码美化** — 调用 `beautify_srt.sh` 对齐到场景切换，输出 `.beautified.srt` (Netflix 规范)
3. **LLM 翻译** — 调用 `translate_srt.py` 翻译英文→中文，输出 `.zh.srt` (缓存) + `.zh.ass` + `.zh-en.ass` (双语)
4. **硬压字幕** — 默认启用，调用 `mpv-burn.sh --sub-file .zh-en.ass` 输出 burned.mkv (BURN=0 跳过)
5. 支持通过 `SKIP_DOWNLOAD=1` `SKIP_BEAUTIFY=1` `SKIP_TRANSLATE=1` `SKIP_BURN=1` 跳过指定阶段
6. 通过 `--` 分隔符向 beautify 传递自定义参数
7. `.zh.srt` 缓存存在时自动跳过 LLM，直接合成双语 ASS

**成果物链**: `VIDEO_PATH` → `BEAUTIFIED_SRT` → `ASS_PATH` → `burned.mkv`
- 每步输出作为下一步输入，已存在的中间产物自动跳过
- `EXISTING_SRT` 环境变量指定已有美化 SRT → 跳过美化
- `EXISTING_ASS` 环境变量指定已有 .zh-en.ass → 跳过翻译

## Pipeline Steps (pipeline.ps1)

1. **WSL 流水线** — 调用 `wsl bash pipeline.sh <url>` 完成下载+美化+翻译
2. **路径转换** — `wslpath -w` 将 WSL 路径转为 Windows 路径
3. **硬压字幕** — 调用 `mpv-burn.ps1` 将 .zh.ass 硬压到视频 → burned.mkv

## Pipeline Steps (download_and_sub.sh)

1. **获取标题** — `yt-dlp --get-title` 获取视频标题，过滤文件名非法字符后创建目录
2. **下载** — `yt-dlp` 下载视频/缩略图/元数据/简介，自动移除 sponsor 和 selfpromo 片段 (SponsorBlock)
3. **定位视频文件** — 在目录中查找 `.mp4/.mkv/.webm/.flv/.avi` 文件
4. **生成字幕** — `uvx whisperx` 使用 `large-v3` 模型 + `float16` 计算生成英文 SRT 字幕
5. **输出路径** — 打印 `OUTPUT_VIDEO=<绝对路径>` 供下游脚本解析

## Dependencies

| Tool | Purpose |
|------|---------|
| `yt-dlp` | YouTube 视频下载 |
| `uvx` (uv) | 运行 WhisperX，无需手动配置 Python 环境 |
| `whisperx` (large-v3) | AI 语音识别生成英文字幕 |
| `ffmpeg` | 音视频处理底层依赖 |
| `ffprobe` | 场景切换检测 + 关键帧提取 (beautify_srt) |
| `python3` | beautify_srt.py / translate_srt.py 运行环境 |
| `OpenRouter` / `DeepSeek` / `Gemini` | LLM API 翻译后端 (translate_srt.py) |
| `mpv` (Windows) | 字幕硬压 (mpv-burn.ps1) |

## Pipeline Steps (beautify_srt.sh)

1. **帧率检测** — `ffprobe` 检测视频帧率, 所有帧数参数按实际 fps 换算为秒
2. **场景检测** — `ffmpeg -vf "select='gt(scene,0.25)',showinfo"` 检测硬切场景切换 (Netflix: 7帧最小间隔)
3. **入点吸附** — 字幕起始时间吸附到前一个场景切换 (7帧以内)
4. **出点吸附** — 字幕结束时间吸附到下一个场景切换前 2 帧 (7帧以内, Netflix 规范)
5. **重叠修复** — 检测并修复字幕重叠, 间距 <500ms 自动合并
6. **时长约束** — 强制最短 1000ms / 最长 8000ms 字幕时长
7. **(可选) 关键帧微调** — `--use-keyframes` 启用, ffprobe 3 级回退提取 I-frame

## Pipeline Steps (translate_srt.py)

1. **解析 SRT** — 提取字幕文本, 忽略时间码 (节省 LLM token)
2. **分批翻译** — 每批 50 条发送给 LLM, 系统提示词约束 1:1 翻译
3. **保留标记** — 翻译中保留 `\N` 软换行标记
4. **加载模板** — 读取 `template.ass` 的 `[Script Info]` + `[V4+ Styles]`
5. **写入 ASS** — 填充 Title, 组合原始时间码 + 中文译文, Style=zh

## Important Notes

- `.env` 配置翻译后端和 API keys:
  - `TRANSLATE_PROVIDER`: 翻译后端 (openrouter/deepseek/gemini)，`pipeline.sh` 和 `pipeline.ps1` 均从 `.env` 读取
  - `TRANSLATE_MODEL`: 模型名，留空则用后端内置默认
  - `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`: 至少配一个对应 provider 的 key
  - `.env` 已 gitignored，不要提交。
- `cookies.txt` 包含 YouTube 登录凭证，已 gitignored。过期后需要重新导出。
- WhisperX 首次运行会自动下载 `large-v3` 模型 (数 GB)，需要保持网络畅通。
- 无 NVIDIA GPU 时需将 `--compute_type float16` 改为 `int8`，或添加 `--device cpu`。
- 每个视频目录名即为 `yt-dlp --get-title` 的结果 (特殊字符替换为 `_`)。
- `beautify_srt.sh` 运行在 WSL 中，会自动识别真正的 SRT 文件（排除 ASS/SSA 格式伪装的 `.srt`）。
- **美化默认不覆盖原文件** — 输出 `<原名>.beautified.srt`，需显式 `-o same.srt` 才会覆盖。
- 场景检测对长视频可能耗时较久（~5 分钟/小时视频）。
- 所有帧数参数 (`--snap-frames`, `--end-offset-frames`, `--min-scene-interval-frames`) 会按实际视频帧率自动换算为秒。
- 关键帧吸附默认关闭 (`--use-keyframes` 启用)，因各视频编码/帧率差异大，场景吸附已足够。
- 流水线自动跳过已完成的步骤：检测到 `.beautified.srt` 跳过美化，检测到 `.zh-en.ass` 跳过翻译。
- 翻译缓存：`.zh.srt`（中文 SRT）存在时自动跳过 LLM，直接合成 `.zh.ass` + `.zh-en.ass`。
- 翻译通过 LLM API 执行 (OpenRouter/DeepSeek/Gemini)，API key 在 `.env` 中配置。不消耗本地 GPU。
- 双语 `.zh-en.ass` 使用 `bi-en` (英文) / `bi-zh` (中文) 样式，仅中文 `.zh.ass` 使用 `zh` 样式。
