---
name: whisper
description: WhisperX 语音识别 — 从视频生成英文字幕 (.srt)
platform: Win + Linux
---

# 语音识别 (Win + Linux)

使用 WhisperX large-v3-turbo 从视频生成英文 SRT 字幕。

输入来自 [[download]] skill 的视频文件。

## Win — PowerShell

```powershell
.\whisper.ps1 "C:\path\to\video.webm"
```

## Linux — Bash

```bash
./whisper.sh "/path/to/video.webm"
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WHISPER_MODEL` | `large-v3-turbo` | ASR 模型 |
| `WHISPER_ALIGN_MODEL` | 空 | 对齐模型 (空=按语言自动匹配) |
| `WHISPER_COMPUTE` | `float16` | GPU: float16, CPU: int8 |

## 输出

```
视频目录/
└── 视频标题.srt            # 英文 SRT 字幕 ✨
```

## GPU 加速

需安装 CUDA Toolkit：

```bash
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update && sudo apt install -y cuda-toolkit-12-8
pip install whisperx

# 验证 CUDA
uv run --with torch python -c "import torch; print(torch.cuda.is_available())"
```

## 注意事项

- 首次运行需下载 `large-v3-turbo` 模型 (~1.5GB)
- `.srt` 已存在时自动跳过
- 语言自动从 `.info.json` 读取，fallback `en`
