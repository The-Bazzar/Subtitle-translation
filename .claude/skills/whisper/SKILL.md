---
name: whisper
description: WhisperX 语音识别 — 从视频生成英文字幕 (.srt)
---

# 语音识别

使用 WhisperX large-v3 模型从视频文件中生成英文 SRT 字幕。

## 前置

需要已下载的视频文件。由 [[download]] skill 提供。

## 执行方式

```bash
# 自动检测 .srt 已存在则跳过
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"

# 或单独运行 (需先有视频文件)
uvx whisperx video.webm \
    --lang en \
    --model large-v3 \
    --output_dir . \
    --output_format srt \
    --compute_type float16
```

> `download_and_sub.sh` 会自动从 `.info.json` 读取视频语言传给 `--lang`。

## 输出

```
视频目录/
└── 视频标题.srt            # 英文 SRT 字幕 ✨
```

## GPU 加速

```bash
# 安装 CUDA Toolkit (WSL2)
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update && sudo apt install -y cuda-toolkit-12-8

# 安装 WhisperX
pip install whisperx
    # 验证 CUDA + torch 是否可用
    uv run --with torch python -c "import torch; print(torch.cuda.is_available())"
```

> `uvx whisperx` 检测到 CUDA 后自动装 GPU 版 torch。

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--lang` | `en` | 视频语言 (ISO 639-1) |
| `--model` | `large-v3` | ASR 模型 |
| `--compute_type` | `float16` | GPU: float16, CPU: int8 |
| `--device` | `cuda` | cuda / cpu |
| `--align_model` | 空 | 对齐模型 (留空则 WhisperX 按语言自动选择) |

### Align 模型选择

WhisperX 的 `--align_model` 用于词级时间戳对齐，留空时根据语言自动匹配默认模型。手动指定可覆盖：

| 值 | 说明 |
|------|------|
| 留空 (默认) | WhisperX 按语言自动匹配 |
| `facebook/mms-1b-fl102` | 手动指定 (通用模型, 主流语言均适用) |

通过环境变量指定：`WHISPER_ALIGN_MODEL=facebook/mms-1b-fl102 ./download_and_sub.sh "url"`

## 注意事项

- 首次运行需下载 `large-v3` 模型 (~3GB)
- GPU VRAM 需求: large-v3 + float16 ≈ 6-8GB
- 无 GPU: `--device cpu --compute_type int8` (已内置在 download_and_sub.sh)
- `.srt` 已存在时自动跳过
