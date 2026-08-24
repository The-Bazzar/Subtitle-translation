# Python CLI Migration

方案 2 将项目从“脚本之间互相调用”迁移为“Python package 统一编排”。这是一项 breaking change，但保留了 PowerShell/bash 薄包装器，已有快捷方式可以逐步迁移。

## 新入口

```text
subtitle-translation pipeline URL
subtitle-translation batch URL1 URL2
subtitle-translation translate input.json
```

安装后也可以直接调用项目解释器：

```powershell
.\.venv\Scripts\python.exe -m subtitle_translation pipeline URL
```

```bash
./.venv/bin/python -m subtitle_translation pipeline URL
```

console entry point 和 `python -m subtitle_translation` 使用同一份实现，退出码约定为：`0` 成功，`1` 任务失败，`2` 参数/配置错误，`130` 用户中断。

新版 `scripts/setup.ps1` / `scripts/setup.sh` 会安装全局 `subtitle-translation` shim。shim 只转发到当前仓库 `.venv` 并自动附带 `--project-dir`，不会复制第二套 Python/WhisperX 环境，也不要求用户手动设置 PATH。切换仓库位置后重新运行 setup 即可刷新 shim。

## 旧入口

兼容 wrapper 已统一移入 `scripts/`。其中的 pipeline、batch、download、prepare-video、whisper、translate、merge-ass 和 burn 脚本都只定位项目 `.venv` 后执行 `python -m subtitle_translation ...`，不再解析 `OUTPUT_*` marker，也不再互相调用。

`py_launcher.ps1/.sh` 的旧 target 仍可用：

```powershell
.\scripts\py_launcher.ps1 translate_srt video.json
.\scripts\py_launcher.ps1 merge_ass video.zh.ass video.en.ass
.\scripts\py_launcher.ps1 batch URL1 URL2
```

推荐新代码直接使用 `subtitle-translation translate`、`subtitle-translation merge-ass` 和 `subtitle-translation batch`。

## 代码迁移

旧的 PowerShell/bash stage 调用：

```powershell
$download = & .\download.ps1 URL
$render = ($download | Select-String '^OUTPUT_RENDER_VIDEO=').ToString().Substring(21)
$prepare = & .\prepare-video.ps1 $render
```

迁移后可直接让 Python 返回结构化结果；CLI 仅在兼容脚本中打印 `OUTPUT_*`：

```python
from pathlib import Path
from subtitle_translation.config import ProjectConfig
from subtitle_translation.stages import download_video, prepare_video

config = ProjectConfig.load(Path.cwd())
download_result = download_video(url, config)
render_path = Path(download_result.outputs["render_video"])
prepare_result = prepare_video(render_path, config)
edit_path = Path(prepare_result.outputs["edit_video"])
```

batch runner 使用相同 stage functions，不再依赖平台 shell、PowerShell、marker 或共享 launcher。`core/batch_runtime.py` 只负责把 `StageResult` 转为 scheduler 的阶段结果；`core/subtitle_translation/process.py` 统一管理 argv、工具环境和活动子进程。

## 仓库布局迁移

源码布局已集中整理：

```text
core/          Python package 与核心模块
scripts/       PowerShell/bash 安装和兼容脚本
misc/examples/ 配置与模板样例
```

旧自动化若直接引用根目录脚本，需要为路径加上 `scripts/`。Python import 和 `subtitle-translation` CLI 名称保持不变。

batch 的 CPU/IO capacity 为 `max(1, (os.cpu_count() or 1) // 4)`，prepare 与 burn 共用固定 4 路 NVENC。

batch 的第一次 `Ctrl+C` 停止接纳新任务并停止推进新的阶段，正在运行的外部命令允许自然结束；第二次 `Ctrl+C` 终止活动进程树并以 `130` 退出。

## 行为保持

- WhisperX `.json` 仍是唯一字幕输入，阶段顺序与 `.beautified.json` 缓存语义不变。
- glossary 仍是翻译、校对的全局硬规则，网页证据仍使用独立 sidecar。
- 整句翻译、源语言 split、word 首尾对齐、split event 校对的顺序不变。
- batch 的 worker、generation、lock、ASR cache、CPU/IO 与 NVENC 限制不变。
- `cookies.txt` 仍从项目根目录相对路径读取；从任意目录调用请使用 `--project-dir`。

## 迁移检查

1. 运行 `scripts/setup.ps1` 或 `scripts/setup.sh`，让 uv 重建项目 `.venv` 并安装 package。
2. 把自动化中的全局 `python` / `python3` 替换为项目 CLI 或 `.venv` 解释器。
3. 删除自定义的 `OUTPUT_*` 解析和 stage 间 shell 串联；Python API 返回 `StageResult.outputs`。
4. 不再传递 `-j`、`--jobs`、`--io-jobs` 或 `MaxJobs`，并发由 scheduler 自动检测。
5. 用 `python -m unittest discover -s tests` 验证安装、CLI 和 scheduler。

真实视频、ffmpeg、WhisperX、LLM、网络和 CUDA 仍需人工验证；测试 smoke 不代替这些环境测试。
