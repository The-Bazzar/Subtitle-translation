<h1 align="center">🎬 Subtitle Translation</h1>

<p align="center">
  以 WhisperX JSON 为核心的视频字幕下载、识别、翻译、校对与硬压流水线
</p>

<p align="center">
  <img alt="Python 3.10-3.13" src="https://img.shields.io/badge/Python-3.10--3.13-3776AB?logo=python&logoColor=white">
  <img alt="PowerShell 7" src="https://img.shields.io/badge/PowerShell-7-5391FE?logo=powershell&logoColor=white">
  <img alt="Windows Linux WSL" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20WSL-555555">
  <img alt="Version 2.0 preview" src="https://img.shields.io/badge/Version-2.0.0--preview-orange">
  <a href="LICENSE"><img alt="GPL-3.0 license" src="https://img.shields.io/badge/License-GPL--3.0-2ea44f"></a>
</p>

---

## ✨ 核心特性

- 🎯 **JSON-first**：WhisperX `.json` 是唯一字幕主输入，保留 word 级时间轴。
- 🧠 **上下文翻译**：glossary、网页证据和 Chroma 检索共同维护术语一致性。
- 🎞️ **可靠对轴**：场景吸附与源语言 word 首尾匹配失败时整句回退，不做本地强切。
- ⚙️ **资源调度**：batch 分离 CPU/IO、NVENC 与 WhisperX 阶段，避免 GPU 任务互相争抢。
- 🌍 **多语言输出**：源语言和目标语言使用 ISO 639 代码生成统一文件名。
- 🖥️ **跨平台入口**：Python CLI 同时服务 Windows、Linux 和 WSL。

<details>
<summary><strong>📚 目录</strong></summary>

- [快速开始](#quick-start)
- [常用命令](#commands)
- [项目结构](#layout)
- [Batch 资源模型](#batch-resources)
- [输出与缓存](#outputs)
- [配置提醒](#configuration)
- [测试与文档](#testing)

</details>

## 🔄 工作流

```text
下载原片 -> 准备编辑版 -> WhisperX JSON -> 时间轴美化 -> glossary
-> 整句翻译 -> AI 分割对轴 -> split 校对 -> ASS -> 硬压
```

> [!IMPORTANT]
> ⚠️ Windows 必须使用 **PowerShell 7**。旧版 Windows PowerShell 5.x 会导致 `.ps1` 脚本报错。
>
> ```powershell
> winget install Microsoft.PowerShell
> ```

<a id="quick-start"></a>

## 🚀 快速开始

### 🪟 Windows

```powershell
.\scripts\setup.ps1
subtitle-translation pipeline "https://www.youtube.com/watch?v=*"
```

### 🐧 Linux / WSL

```bash
./scripts/setup.sh
subtitle-translation pipeline "https://www.youtube.com/watch?v=*"
```

setup 会完成以下工作：

- 创建项目 `.venv`。
- 从 example 创建缺失的本地配置。
- 安装项目、WhisperX 和所选 CPU/CUDA PyTorch 后端。
- 安装并验证全局 `subtitle-translation` 命令 shim。

shim 始终转发到当前项目 `.venv`，不创建第二套 Python 环境。用户不需要手动配置 PATH。

<a id="commands"></a>

## 🧰 常用命令

激活项目 `.venv` 后，可以使用下面的简写命令：

```text
# 处理一个视频
subtitle-translation pipeline "URL"

# 批量处理多个视频
subtitle-translation batch "URL1" "URL2"

# 翻译已有 WhisperX JSON
subtitle-translation translate "G:/project/video.json"

# 合并两条已校对 ASS
subtitle-translation merge-ass "video.zh.ass" "video.en.ass"
```

未激活虚拟环境时，也可以直接使用项目 Python：

```powershell
& "<repo>\.venv\Scripts\python.exe" -m subtitle_translation pipeline "URL" --skip-burn
```

```bash
"<repo>/.venv/bin/python" -m subtitle_translation pipeline "URL" --skip-burn
```

运行以下命令查看某个子命令的完整参数：

```text
subtitle-translation <command> --help
```

`pipeline` 常用选项：

| 选项 | 用途 |
|---|---|
| `--skip-burn` | 生成字幕后跳过硬压 |
| `--skip-download --video PATH` | 使用已有原片 |
| `--skip-whisper --json PATH` | 使用已有 WhisperX JSON |
| `--source-lang CODE` | 指定源语言 |
| `--target-lang CODE` | 指定目标语言 |
| `--model` / `--device` | 覆盖 WhisperX 模型或设备 |
| `--ovc` / `--ovcopts` / `--oac` / `--res` | 调整硬压参数 |

<a id="layout"></a>

## 🗂️ 项目结构

```text
core/          Python package 与核心运行模块
scripts/       setup 和平台兼容启动脚本
tests/         自动化测试
docs/          架构与设计文档
```

正式功能由 `subtitle-translation` CLI 统一提供。`scripts/` 中除 setup 外的文件仅用于旧入口迁移，不承载业务逻辑。

<a id="batch-resources"></a>

## ⚙️ Batch 资源模型

batch 使用 Python scheduler 按阶段调度任务，不会并行启动多条完整 pipeline，也不接受手工 jobs 参数。

| 资源或事件 | 调度规则 |
|---|---|
| CPU / IO | 自动使用 `max(1, (os.cpu_count() or 1) // 4)` 路并发 |
| NVENC | prepare 与 burn 共用固定 4 路容量 |
| WhisperX | 全局串行，在一个 worker 内复用 ASR/alignment 模型 |
| GPU 隔离 | NVENC 与 WhisperX 不同时运行 |
| 单任务失败 | 只停止该任务的后续阶段，其他任务继续 |
| worker crash | 阻止后续 worker-stage 任务并记录失败报告 |
| 第一次 `Ctrl+C` | 停止接纳和推进新阶段，等待当前阶段 |
| 第二次 `Ctrl+C` | 终止活动进程树，以退出码 `130` 结束 |

batch 会同时输出文本报告和同基名 JSON machine report。

通知音效使用终端 BEL：

- 成功：一组短响。
- 错误：一组错误响铃。
- help 和 dry-run：静默。

<a id="outputs"></a>

## 📦 输出与缓存

SRT 不作为输入缓存。典型文件链如下：

```text
原片与编辑版
  <base>.original.<ext>
  <base>.mkv

识别与时间轴
  <base>.json
  <base>.beautified.json
  <base>.scenes.json
  <base>.scenechange.txt

知识与证据
  glossary.md
  <base>.web_evidence.json

字幕成果
  <base>.split.<source>.srt
  <base>.split.<target>.srt
  <base>.<source>.proofread.ass
  <base>.<target>.ass
  <base>.<source>-<target>.ass
  <base>.<target>.description
```

glossary 位于翻译之前：

- 普通 pipeline 会复用已有的非空 `glossary.md`。
- `--only-glossary` 会重新生成并覆盖它。
- 网页证据单独写入 `.web_evidence.json`。
- 启用 Chroma embedding 后，网页证据可作为动态检索记忆。

<a id="configuration"></a>

## 🔧 配置提醒

### 🌐 语言

`SOURCE_LANG` / `TARGET_LANG` 支持 ISO 639、BCP-47 或语言名。输出文件后缀会通过 `langcodes` 规范为 ISO 639 代码。

### 📁 项目目录

`cookies.txt` 从项目根目录读取。从其他目录运行时，请在主命令前指定配置根：

```text
subtitle-translation --project-dir "G:/Subtitle translation/.code" pipeline "URL"
```

### 🤖 LLM 数据约束

- glossary、翻译、分割和校对使用 JSON 请求/响应协议。
- `glossary.md` 是完整常驻的全局硬规则。
- retrieved context 只提供动态补充，不替代 glossary。
- split 只按源语言首尾 word 对轴；匹配失败时整句回退，不做本地强切。

### 🔒 不应提交的文件

请勿提交 `.env`、真实 provider、cookies、本地 prompt、`template.ass`、glossary、视频、字幕、sidecar、`chroma_db` 或 batch reports。

`*_prompt.example.md` 的文案由 `The-Bazzar/prompt` 仓库维护。

<a id="testing"></a>

## ✅ 测试与文档

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

```bash
./.venv/bin/python -m unittest discover -s tests
```

测试必须 mock 网络、LLM、yt-dlp、ffmpeg 和 WhisperX。真实媒体、CUDA 与远程 API 仍需在实际环境中验证。

### 📖 更多资料

- [技术与协作约束](AGENTS.md)
- [迁移说明](MIGRATION.md)
- [Python CLI 重构设计](docs/superpowers/specs/2026-08-21-python-cli-rewrite-design.md)
- [v2.1.0 ASR Provider 候选设计](docs/superpowers/specs/2026-08-25-v2.1.0-asr-provider-design.md)
- [GPL-3.0 许可证](LICENSE)
