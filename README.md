# YouTube 视频下载与 AI 字幕生成工具

## 🛠 前置依赖安装

在运行脚本之前，请确保你的系统已安装以下必要的开发工具和运行环境。

### 1. Python 环境与 UV (推荐)
脚本使用 `uvx` 来运行 WhisperX，它可以省去手动配置 Python 虚拟环境的麻烦。
* **安装 UV**：
```bash
curl -lsSf https://astral.sh/uv/install.sh | sh
```

### 2. JavaScript / TypeScript 运行时 (Node.js & Deno)

* **Node.js** (建议LTS)：
```bash
# 使用 nvm 或 系统的包管理器安装
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt-get install -y nodejs
```

* **Deno** (现代安全 JS/TS 运行时)：
```bash
curl -fsSL https://deno.land/x/install/install.sh | sh
```

### 3. 多媒体工具 (yt-dlp & FFmpeg)

* **yt-dlp**：用于下载视频。
```bash
sudo wget https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -O /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```

* **FFmpeg**：`yt-dlp` 和 `WhisperX` 处理视音频的核心底层依赖。
```bash
# Ubuntu/Debian
sudo apt update && sudo apt install -y ffmpeg
# macOS
brew install ffmpeg
```


---

## 📦 准备工作

1. **获取脚本**：将脚本内容保存为 `download_and_sub.sh`。
2. **赋予执行权限**：
```bash
chmod +x download_and_sub.sh
```

3. **Cookie 凭证（可选）**：
如果有需要请在脚本同级目录下放置一份 `cookies.txt` 文件。
---

## 📖 使用方法

进入wsl虚拟机：

``` bath
wsl -u root
```

直接在终端中运行脚本，并将 YouTube 视频链接作为第一个参数传入：

```bash
./download_and_sub.sh "https://www.youtube.com/watch?v=xxxxx"
```

使用`&&`🔗多条命令：

```bash
./download_and_sub.sh "https://www.youtube.com/watch?v=xxx1" \
    && ./download_and_sub.sh "https://www.youtube.com/watch?v=xxx2" \
    && ./download_and_sub.sh "https://www.youtube.com/watch?v=xxx3"
```


或者直接用`powershell`操作：
``` powershell
wsl -u root bash -lc "sh ./download_and_sub.sh https://www.youtube.com/watch?v=xxxxx"
```

### ⏳ 运行流程

1. **步骤 1**：抓取视频标题并创建独立文件夹。
2. **步骤 2**：调用 `yt-dlp` 下载视频、元数据及封面（自动剔除赞助广告）。
3. **步骤 3**：在文件夹中自动检索下载完成的视频文件。
4. **步骤 4**：使用 `uvx whisperx` 挂载 `large-v3` 模型生成 `.srt` 字幕文件。

---

## 📂 目录输出结构

执行成功后，会在当前目录下生成类似如下的结构：

```text
├── download_and_sub.sh
├── cookies.txt (可选)
└── 视频标题_文件夹/
    ├── 视频标题.mp4            # 视频文件
    ├── 视频标题.jpg            # 视频封面
    ├── 视频标题.info.json      # 视频元数据
    ├── 视频标题.description    # 视频简介
    └── 视频标题.srt            # AI 生成的英文副标题

```

---

## 💡 注意事项

> ⚠️ **首次运行提示**：由于脚本中配置的 WhisperX 模型为 `large-v3`，首次运行时 `uvx` 会自动下载该模型权重（通常需要几 GB 的空间），请保持网络畅通。
> ⚙️ **硬件加速**：WhisperX 默认会尝试使用 GPU 加速。如果你的设备没有 NVIDIA 显卡或未配置好 CUDA，可能需要在脚本中将 `--compute_type float16` 调整为 `int8` 或指定 `--device cpu` 以确保兼容性。
