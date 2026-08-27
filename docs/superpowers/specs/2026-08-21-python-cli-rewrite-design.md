# Python CLI 大重构设计

## Summary

将项目从“PowerShell/bash 编排 + Python 工具”迁移为可安装的 Python CLI。最终用户通过
`subtitle-translation <command>` 调用完整流水线、批处理和各个独立阶段；PowerShell/bash
只保留兼容薄包装器，不再承载业务编排、marker 解析或平台分支逻辑。

## Goals

- 提供统一命令：`pipeline`、`batch`、`translate`、`merge-ass`、`download`、
  `prepare-video`、`whisper`、`burn`、`init`。
- 保留 WhisperX JSON 作为唯一字幕主输入，以及现有阶段顺序和缓存语义。
- 让 pipeline 与 batch 复用相同的 Python stage runner、配置、日志、退出码和资源调度。
- Windows 与 Linux/WSL 使用同一套 Python 参数和阶段契约。
- 使用参数数组调用 `yt-dlp`、`ffmpeg`、`mpv` 和 WhisperX，避免 shell 字符串拼接。
- 支持 `uv tool install .` 安装，不要求用户手动设置 Python PATH。

## Non-goals

- 不重新实现 ffmpeg、yt-dlp 或 WhisperX 的媒体和识别能力。
- 不改变 `.beautified.json`、`.asr.json`、split event、glossary 或 ASS 输出格式。
- 不改变翻译、分割、校对的 LLM JSON 协议和执行顺序。
- 不改变 CPU/IO、NVENC、ASR worker 和 alignment 的资源边界。

## Architecture

```text
core/
├── subtitle_translation/
│   ├── cli.py              # 统一 argparse 入口和 command dispatch
│   ├── config.py           # CLI > process env > .env > defaults
│   ├── process.py          # 参数数组、实时输出、活动进程注册和进程树终止
│   ├── notifications.py    # success/error bell
│   ├── stages.py           # download/prepare/whisper/burn stage runner
│   └── pipeline.py         # pipeline 阶段编排
├── batch_runtime.py        # 复用 batch scheduler，调用 Python stage runner
├── batch_scheduler.py      # 任务状态和资源调度
├── whisper_worker.py       # spawn worker 与 WhisperX 生命周期
├── translate_srt.py        # JSON 美化、glossary、翻译、分割、校对、ASS
└── merge_ass.py            # ASS 合并

scripts/                    # setup 与兼容包装器
```

`subtitle_translation.cli` 是唯一公开 console entry point。旧的根目录 Python 模块在迁移
期间继续作为兼容 import 入口，并由 package 配置一起打包；新代码不得再依赖包装脚本解析
`OUTPUT_*` 文本 marker。

## Commands

```text
subtitle-translation pipeline URL [pipeline options]
subtitle-translation batch URL... [batch options]
subtitle-translation translate JSON [translate options]
subtitle-translation merge-ass ZH_ASS EN_ASS [merge options]
subtitle-translation download URL [download options]
subtitle-translation prepare-video ORIGINAL_VIDEO [options]
subtitle-translation whisper VIDEO [options]
subtitle-translation burn VIDEO ASS [burn options]
subtitle-translation init [--directory PATH]
```

每个 command 使用独立的 argparse parser。未识别参数立即返回 CLI 错误，不把参数静默传给
错误阶段。旧脚本的常用参数保持同名语义；PowerShell 风格参数只在兼容脚本层转换一次，
Python CLI 使用标准的 kebab-case 长选项。

## Stage contracts

每个阶段返回不可变的 `StageResult`，包含 `success`、`exit_code`、`outputs`、`diagnostics`
和 `command`。pipeline 只消费结构化结果，不读取 stdout 查找路径。

- download 返回 `render_video`，并继续保存 info、description、tags 和 thumbnail。
- prepare-video 返回 `edit_video`，保持 CPU decode、NVENC/libx264 优选、FLAC 和时间戳抚平。
- whisper 返回最终词级 JSON；batch worker 继续使用已有 `.asr.json` recovery contract。
- translate 返回 ASS、split SRT、beautified JSON、glossary 和 description 路径。
- burn 返回非空输出视频路径，并验证输出存在。

stdout/stderr 仍由活动子进程实时继承到当前终端；stdout 不再承担进程间协议。

## Pipeline and batch

`pipeline` 依次调用 Python stage functions：

```text
download -> prepare-video -> whisper -> translate_srt -> burn
```

`batch` 继续使用当前 scheduler 的 acquisition、ASR wave、alignment wave、postprocess 和
burn wave。它将 `batch_runtime` 中调用 `.ps1/.sh` 的部分替换为同一进程内的 Python stage
runner；WhisperX 仍只在 spawn worker 内 import。所有 worker、lock、generation、cancel 和
recovery 语义保持不变。

## Configuration and packaging

`pyproject.toml` 增加：

```toml
[project.scripts]
subtitle-translation = "subtitle_translation.cli:main"
```

配置优先级固定为：显式 CLI > 进程环境 > 当前项目 `.env` > 内置默认值。默认配置根目录为
当前工作目录，可用 `--project-dir` 覆盖。`init` 负责从 example 创建缺失配置并追加新增
`.env` 变量，不覆盖用户已有内容。

`ffmpeg`、`yt-dlp`、`mpv` 和 PowerShell 的路径通过已有配置或 `shutil.which` 解析；找不到
时给出明确的依赖错误。CLI 不依赖用户修改系统 PATH。

## Compatibility and migration

- `scripts/` 下的 pipeline、batch、translate、merge-ass、download、prepare-video、whisper 和 burn 脚本暂时保留为
  薄包装器，内部只调用 Python CLI。
- 包装器不再包含独立业务逻辑；下一次 major release 才考虑删除。
- `py_launcher` 标记为 deprecated，迁移文档提供新旧命令对照。
- 旧的输出文件、缓存、`.env` 和 provider 配置保持可读，不要求重新生成视频或字幕。
- 退出码统一为 `0` 成功、`1` 阶段/任务失败、`2` CLI 或配置错误、`130` 用户中断。

## Testing

- CLI parser 测试覆盖所有 command、帮助、未知参数和退出码。
- stage runner 使用 mock subprocess，验证参数数组、实时输出、失败透传和非空输出检查。
- pipeline contract 测试验证阶段顺序、结构化 output 传递和 skip 行为。
- batch smoke 通过 `subtitle_translation batch` 的 dry-run 和 direct runner，scheduler 测试继续覆盖资源容量、worker 协议和报告。
- Windows PowerShell 与 WSL wrapper parity 测试只验证兼容包装器调用同一 CLI。
- 网络、LLM、ffmpeg、yt-dlp 和 WhisperX 均不得在 unittest 中真实调用。

## Design alignment

本重构保持 D1-D7、D9-D12 不变；D8 的平台对齐从“分别维护两套脚本逻辑”收紧为“同一
Python 实现 + 两个薄包装器”。这是 CLI 入口和实现边界的 breaking change，但通过兼容包装
器、MIGRATION.md 和完整回归测试提供迁移路径。
