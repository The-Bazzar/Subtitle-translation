# YouTube 视频下载 + AI 字幕生成 + 时间码美化

一键流水线：从 YouTube 链接直达美化后的 SRT 字幕。

## 🛠 前置依赖

### WSL (推荐)

```bash
# Python 包管理器 (用于运行 WhisperX)
curl -LsSf https://astral.sh/uv/install.sh | sh

# yt-dlp — 视频下载
sudo wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp

# FFmpeg — 音视频处理 + 场景检测
sudo apt update && sudo apt install -y ffmpeg
```

### Windows (可选)

| 工具 | 用途 |
|------|------|
| `yt-dlp` | `download.ps1` 视频下载 |
| `mpv` | `mpv-burn.ps1` 字幕硬压 |

---

## 🚀 快速开始

```bash
# 进入 WSL
wsl -u root

# 一键流水线: 下载 → 字幕 → 美化 (推荐)
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"
```

执行后会在当前目录生成：

```
视频标题/
├── 视频标题.webm           # 视频文件
├── 视频标题.srt            # 美化后的英文字幕 ✨
├── 视频标题.webp           # 封面缩略图
├── 视频标题.info.json      # 元数据
└── 视频标题.description    # 视频简介
```

---

## 📖 命令参考

### `pipeline.sh` — 一键流水线 (推荐)

串联下载 + 字幕生成 + 时间码美化，从 URL 一步到位。

```bash
# 基础用法
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx"

# 传递美化选项 (-- 之后)
./pipeline.sh "https://www.youtube.com/watch?v=xxxxx" -- --backup --scene-threshold 0.2

# 跳过某些步骤
SKIP_BEAUTIFY=1 ./pipeline.sh "url"     # 仅下载+字幕, 跳过美化
SKIP_DOWNLOAD=1 ./pipeline.sh "url"     # 仅美化已有视频
```

### `download_and_sub.sh` — 下载 + 字幕

单视频下载并生成 WhisperX 英文字幕（不做时间码美化）。

```bash
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"

# 批量下载
./download_and_sub.sh "URL1" && ./download_and_sub.sh "URL2"
```

### `beautify_srt.sh` — 字幕时间码美化

用 ffmpeg/ffprobe 检测场景切换，按 Netflix 规范将字幕时间码吸附对齐到场景变化点。自动检测视频帧率，所有帧数参数按实际 fps 换算。

```bash
# 自动查找同目录 .srt 并原位覆盖
./beautify_srt.sh video.webm

# 指定字幕 + 备份
./beautify_srt.sh video.webm subtitle.srt --backup

# 仅预览变化 (不写入)
./beautify_srt.sh video.webm --preview

# 激进对齐 (对剪辑密集的视频)
./beautify_srt.sh video.webm --scene-threshold 0.2 --snap-frames 10

# 保守对齐 (对长镜头视频)
./beautify_srt.sh video.webm --scene-threshold 0.35 --snap-frames 4
```

**算法流程**：帧率检测 → 场景检测 (≥7帧间隔) → 入点吸附到场景 → 出点吸附到场景前2帧 → 重叠/间隙修复 → 时长约束

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--scene-threshold` | `0.25` | 场景检测灵敏度 (越小越灵敏) |
| `--snap-frames` | `7` | 吸附到场景切换的最大帧数 |
| `--end-offset-frames` | `2` | 出点对齐到场景切换前 N 帧 |
| `--min-scene-interval-frames` | `7` | 场景切换最小帧间隔 |
| `--min-duration` | `1.0` | 最短字幕时长 (秒, Netflix: 1000ms) |
| `--max-duration` | `8.0` | 最长字幕时长 (秒, Netflix: 8000ms) |
| `--min-gap` | `0.083` | 字幕最小间距 (秒, Netflix: 2帧) |
| `--max-gap-merge` | `0.5` | 小于此值的间隙合并 (秒, Netflix: 500ms) |
| `--use-keyframes` | 关闭 | 启用关键帧吸附 (默认不启用) |
| `--extend` | 关闭 | 延伸字幕填充到场景切换前 |
| `--no-scene-snap` | — | 完全跳过场景吸附 |
| `--preview` | — | 仅预览, 不写入 |
| `--backup` | — | 覆盖前备份为 `.bak` |

### `download.ps1` — 仅下载 (PowerShell)

Windows 环境下仅下载视频，不生成字幕。

```powershell
.\download.ps1 "https://www.youtube.com/watch?v=xxxxx"
```

### `mpv-burn.ps1` — 字幕硬压 (PowerShell)

将 SRT 字幕硬压到视频中，输出 hevc_nvenc 编码的 mkv。

```powershell
.\mpv-burn.ps1 "C:\path\to\video.webm"
# 输出: burned.mkv (同目录, hevc_nvenc qp=20, aac 音频)
```

### 从 PowerShell 调用 WSL

```powershell
wsl -u root bash -lc "sh ./pipeline.sh 'https://www.youtube.com/watch?v=xxxxx'"
```

---

## 📂 项目结构

```
Subtitle translation/
├── pipeline.sh               # 一键流水线入口 (下载 → 字幕 → 美化)
├── download_and_sub.sh       # 下载视频 + 生成英文字幕
├── beautify_srt.sh           # 字幕时间码美化入口
├── beautify_srt.py           # 美化核心算法 (场景检测 + Netflix 帧对齐)
├── download.ps1              # PowerShell: 仅下载 (不含字幕)
├── mpv-burn.ps1              # PowerShell: 字幕硬压 (NVENC)
├── .env                      # API keys (gitignored)
├── cookies.txt               # YouTube 登录凭证 (gitignored)
└── <Video Title>/            # 每个视频独立的输出目录
    ├── <Video Title>.<ext>   # 视频文件 (webm/mp4/mkv)
    ├── <Video Title>.srt     # WhisperX 生成的美化英文字幕
    ├── <Video Title>.webp    # 封面缩略图
    ├── <Video Title>.info.json     # yt-dlp 元数据
    └── <Video Title>.description   # 视频简介
```

---

## 💡 注意事项

- **WhisperX 首次运行**：会自动下载 `large-v3` 模型（数 GB），保持网络畅通。
- **GPU 加速**：WhisperX 默认使用 float16 + GPU。无 NVIDIA 显卡时需修改脚本中的 `--compute_type` 为 `int8` 或添加 `--device cpu`。
- **cookies.txt**：包含 YouTube 登录凭证，过期后需重新导出。已 gitignored。
- **场景检测耗时**：对长视频可能较慢（~5 分钟/小时视频）。
- **字幕格式验证**：美化脚本会自动识别真正的 SRT 文件（排除 ASS/SSA 格式伪装的 `.srt`）。
- **帧率自适应**：所有帧数参数 (`--snap-frames`, `--end-offset-frames`, `--min-scene-interval-frames`) 会按实际视频帧率自动换算为秒。
- **关键帧吸附**：默认关闭，如需启用加 `--use-keyframes`（支持 H.264/H.265/VP9，三级回退策略）。
