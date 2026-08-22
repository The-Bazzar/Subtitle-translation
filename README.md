# YouTube 字幕流水线

本项目现在是一个 JSON-first 的 Python CLI。WhisperX `.json` 是字幕主输入：

```text
下载原片 -> 准备编辑版 -> WhisperX JSON -> 时间轴美化 -> glossary
-> 整句翻译 -> AI 分割对轴 -> split 校对 -> ASS -> 硬压
```

Windows 必须使用 PowerShell 7。升级命令：

```powershell
winget install Microsoft.PowerShell
```

## 安装

```powershell
.\setup.ps1
```

```bash
./setup.sh
```

setup 会创建项目 `.venv`，从 example 补齐 `.env` 和本地配置，并按照 `TORCH_BACKEND` 安装 WhisperX 与 CPU/CUDA PyTorch。用户不需要把 Python 或项目目录加入 PATH。

## CLI 用法

安装完成后：

```text
subtitle-translation pipeline "https://www.youtube.com/watch?v=*"
subtitle-translation batch "https://www.youtube.com/watch?v=1" "https://www.youtube.com/watch?v=2"
subtitle-translation translate "G:/project/video.json"
subtitle-translation merge-ass "video.zh.ass" "video.en.ass"
```

也可使用项目 Python：

```powershell
& "<repo>\.venv\Scripts\python.exe" -m subtitle_translation pipeline "URL" -SkipBurn
```

```bash
"<repo>/.venv/bin/python" -m subtitle_translation pipeline "URL" --skip-burn
```

`pipeline` 支持 `--skip-burn`、`--skip-download --video`、`--skip-whisper --json`、`--source-lang`、`--target-lang`、`--model`、`--device`、`--ovc`、`--ovcopts`、`--oac`、`--res`，以及 scene/split 参数。运行 `subtitle-translation <command> --help` 查看完整选项。

## 兼容脚本

旧入口仍保留为薄包装器，方便已有快捷方式迁移：

```powershell
.\pipeline.ps1 "URL"
.\batch.ps1 "URL1" "URL2"
.\translate_srt.ps1 "video.json" --no-split
.\ffmpeg-burn.ps1 "video.original.mkv" -SubFile "video.en-zh.ass"
```

```bash
./pipeline.sh "URL"
./batch.sh "URL1" "URL2"
./translate_srt.sh "video.json" --no-split
./ffmpeg-burn.sh "video.original.mkv" --sub-file "video.en-zh.ass"
```

`py_launcher.ps1/.sh` 继续接受 `translate_srt`、`merge_ass`、`batch` 等旧 target，但只做命令映射，不再寻找或直接运行散落的 Python 文件。wrapper 会从自身目录定位 `.venv`，因此可以从任意工作目录调用。

## Batch 资源模型

batch 是 Python scheduler，不再并行启动多条完整 pipeline，也不接受手工 jobs 参数。CPU/IO 并发自动为 `max(1, (os.cpu_count() or 1) // 4)`；prepare 与 burn 共用固定 4 路 NVENC；NVENC 与 WhisperX 不重叠；WhisperX 全局串行并在 worker 内复用模型。失败任务只停止自己的后续阶段，聚合退出码为 `1`；worker crash 会立即阻止后续 worker-stage 任务。第一次 Ctrl+C 停止接纳，第二次强制终止活动进程并返回 `130`。

batch 输出文本报告和同基名 JSON machine report。独立 pipeline 或 batch 会使用两种终端 BEL：成功一短响，错误三响；帮助和 dry-run 不响。

## 输出与缓存

主输入是 WhisperX `<base>.json`，不会把 SRT 当作输入缓存。典型产物：

```text
<base>.original.<ext>
<base>.mkv
<base>.json
<base>.beautified.json
<base>.scenes.json
<base>.scenechange.txt
<base>.web_evidence.json
<base>.split.<source>.srt
<base>.split.<target>.srt
<base>.<source>.proofread.ass
<base>.<target>.ass
<base>.<source>-<target>.ass
<base>.<target>.description
```

glossary 位于翻译前。普通 pipeline 复用已有非空 `glossary.md`；`--only-glossary` 会重新生成覆盖。联网得到的网页证据独立写入 `.web_evidence.json`，embedding 使用 Chroma 时作为动态检索记忆。

## 配置提醒

`SOURCE_LANG` / `TARGET_LANG` 支持 ISO 639、BCP-47 或语言名，输出后缀统一规范为 ISO 639。`cookies.txt` 仍位于项目根目录，按相对路径读取；需要从其他目录执行时使用 `--project-dir` 或切换到项目目录。

glossary、翻译、分割和校对均使用 JSON 请求/响应协议。glossary 是全局硬规则，retrieved context 只是补充；split 只按源语言首尾 word 匹配，无法可靠对齐时整句回退，不做本地强切。

禁止提交 `.env`、真实 provider、cookies、本地 prompt、`template.ass`、glossary、视频、ASS、sidecar、`chroma_db` 和 batch reports。prompt example 文案归 `The-Bazzar/prompt` 仓库维护。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

```bash
./.venv/bin/python -m unittest discover -s tests
```

所有外部服务和媒体工具都应在测试中 mock；真实视频、LLM、网络和 CUDA 不属于 smoke 覆盖范围。详细迁移信息见 [MIGRATION.md](MIGRATION.md)，方案说明见 `docs/superpowers/specs/2026-08-21-python-cli-rewrite-design.md`。
