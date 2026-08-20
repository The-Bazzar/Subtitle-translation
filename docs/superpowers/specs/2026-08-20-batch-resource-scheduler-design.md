# Batch Resource Scheduler Design

Status: approved design
Branch: `feat/batch-resource-scheduler`
Scope: 方案 A，仅协调当前 batch 进程及其子进程

## 1. Principle

系统资源由用户掌控。调度器只管理当前 `batch` 进程创建的任务和子进程，不检测、不暂停、不终止用户在 batch 外启动的 GPU、LLM、embedding 或其他程序。

调度器的责任是保证本 batch 内部不会自行制造已知的 GPU 竞争：

- 编辑版视频 NVENC 重编码最多 4 路
- WhisperX ASR 全局串行
- WhisperX alignment 全局串行
- Whisper worker 存活或模型驻留期间禁止 NVENC
- 最终硬字幕 NVENC 渲染最多 4 路
- CPU/IO 并发自动设为 `max(1, logical_cpu_threads // 4)`

不新增并发环境变量，也不允许用户通过 `jobs` 参数绕过这些边界。

## 2. Design Alignment

保持不变的设计约束：

- WhisperX `.json` 仍是唯一主字幕输入
- `.beautified.json`、`split_status` 和现有输出命名保持不变
- 本地工具继续负责下载、ASR、时间轴和硬压
- PowerShell 与 bash 入口保持行为一致
- batch 失败不会修改或回滚用户已有产物

本方案把编辑版重编码从 `download` 中拆为显式 `prepare-video` 阶段，并改变 batch 的 pipeline 执行方式，触及 `DISCIPLINE.md` 的 D2 与 D8。进入实现前必须建立 `[design-change]` Issue，并在 PR 中附 Design Change Report。

## 3. Process Architecture

```text
batch.ps1 / batch.sh
        |
py_launcher.ps1 / py_launcher.sh
        |
      batch.py
        |
        +-- CPU/IO executor
        +-- NVENC executor, capacity 4
        +-- Whisper worker process, capacity 1
        +-- serialized terminal event queue
```

`batch.py` 是唯一调度权威。PowerShell 不再维护 RunspacePool 或重复实现并发、状态和退出码逻辑。

### 3.1 Shared Python launcher

新增严格白名单 launcher：

```text
py_launcher.ps1 translate_srt [args...]
py_launcher.ps1 merge_ass [args...]
py_launcher.ps1 batch [args...]

py_launcher.sh translate_srt [args...]
py_launcher.sh merge_ass [args...]
py_launcher.sh batch [args...]
```

目标枚举固定为：

```text
translate_srt -> translate_srt.py
merge_ass     -> merge_ass.py
batch         -> batch.py
```

launcher 负责：

- 从自身目录定位项目 `.venv`
- 读取 `PYTHON_PATH_WIN` 或 `PYTHON_PATH_LINUX`
- 拒绝白名单外的目标
- 原样转发后续参数
- 原样返回 Python 退出码

`translate_srt.ps1/.sh`、`merge_ass.ps1/.sh`、`batch.ps1/.sh` 保留为薄包装，继续兼容用户现有命令。

## 4. Script Decomposition

### 4.1 Download

`download.ps1/.sh` 只负责：

- yt-dlp 下载原片
- 下载封面和 metadata
- 保存 `.info.json`、`.description` 和 `.tags.txt`
- 保留 SponsorBlock 行为

成功契约：

```text
OUTPUT_RENDER_VIDEO=<base>.original.<ext>
```

`download` 不再创建编辑版 `.mkv`，也不再隐式调用重编码。

### 4.2 Prepare edit video

新增：

```text
prepare-video.ps1 <original-video>
prepare-video.sh <original-video>
```

它只负责原片时间戳抚平和编辑版重编码，并复用现有编码器优选与成功判定。

成功契约：

```text
OUTPUT_VIDEO=<base>.mkv
```

该阶段不读取 URL、cookies 或 metadata，不自行猜测原片路径。

### 4.3 Audio extraction

mono 16kHz WAV 提取属于 CPU/IO 阶段，不占 Whisper worker。WAV 准备完成后，任务才可进入 ASR 队列。

独立运行 `whisper.ps1/.sh` 时仍保留“提取 WAV后执行 Whisper”的用户入口，但 batch 内部将两者分开调度。

## 5. Resource Classes

### 5.1 CPU/IO

```text
capacity = max(1, os.cpu_count() // 4)
```

适用于：

- 网络下载
- WAV 提取
- 普通文件处理
- beautify、glossary、翻译、分割和校对入口
- 不使用 NVENC 的外部命令

所有 `jobs` 配置全部删除：

- 删除 PowerShell `MaxJobs`
- 删除 `-j`
- 删除 `--jobs`
- 不新增 `--io-jobs`

### 5.2 NVENC

```text
capacity = 4
```

编辑版 `prepare-video` 和最终 burn 都使用相同的 NVENC slot 语义：一个 ffmpeg 编码进程占一个 slot。

### 5.3 Whisper

```text
ASR capacity = 1
alignment capacity = 1
```

ASR 和 alignment 由同一个 batch 私有 worker 子进程执行，不与 NVENC 同时存在。

## 6. Batch Stage Graph

```text
download ──> prepare-video ──> extract-audio
                                  |
                                  v
                         ASR wave, serial
                                  |
                                  v
                    alignment wave, serial
                                  |
                                  v
               beautify/glossary/translate
                                  |
                                  v
                         burn, max 4
```

### 6.1 Acquisition wave

- download 使用 CPU/IO executor。
- 某个原片下载完成后可立即进入 prepare 队列，无需等待其他下载。
- prepare 最多 4 路，并可与其他任务的网络下载重叠。
- prepare 完成后可立即提取 WAV。
- 只有所有已接纳任务的 download、prepare 和 WAV 提取均到达终态，才允许加载 Whisper ASR 模型。
- 下载或 prepare 失败的任务被隔离，不阻止其他成功任务进入 ASR。

### 6.2 ASR wave

1. worker 加载一次 `large-v3-turbo`。
2. 串行处理全部有效 WAV。
3. 每个任务原子写入 `<base>.asr.json`。
4. 全部 ASR 任务到达终态后，只卸载一次 ASR 模型。

ASR wave 期间禁止 prepare 和 burn NVENC。模型空闲但仍驻留显存时同样禁止 NVENC。

### 6.3 Alignment wave

1. 使用 ASR 检出的源语言对成功任务分组。
2. 每次只加载一个语言的 alignment model。
3. 串行读取 `.asr.json` 并生成正式 `<base>.json`。
4. 某个任务 alignment 完成后，可立即进入后续 CPU/IO 阶段。
5. 全部 alignment 任务到达终态后卸载模型并关闭 worker。

### 6.4 Post-processing pipeline

任务完成 alignment 后，可在其他任务继续 alignment 时并行执行：

```text
beautify -> glossary -> translate -> split -> proofread -> ASS
```

这些阶段受 CPU/IO capacity 控制。单个任务失败只终止该任务，其他任务继续。

### 6.5 Burn wave

- 只有 Whisper worker 已关闭且全部模型已释放，才允许任何 burn 开始。
- 已生成最终 ASS 的任务进入 burn 队列，最多 4 路。
- 尚在翻译的任务完成后可继续加入 burn，不要求等待整个翻译队列清空。
- 跳过 burn 的任务直接完成。

## 7. GPU Ordering Invariant

GPU 工作负载的全局顺序固定为：

```text
priority 1: prepare-video NVENC
priority 2: ASR + alignment
priority 3: final subtitle burn NVENC
```

这里的 priority 表示严格阶段顺序，不是允许抢占的普通优先级队列。

始终满足：

```text
0 <= prepare_nvenc_running <= 4
0 <= burn_nvenc_running <= 4
asr_running in {0, 1}
alignment_running in {0, 1}

whisper_worker_alive => prepare_nvenc_running == 0
whisper_worker_alive => burn_nvenc_running == 0
burn_nvenc_running > 0 => whisper_worker_alive == false
```

## 8. Whisper Worker Protocol

worker 使用 Python `multiprocessing` 独立进程：

- Windows 与 Linux 都强制使用 `spawn`
- parent 使用 `Queue` 或 `Pipe` 发送结构化命令
- IPC 只传任务 ID、路径、语言和模型参数
- 大型转录结果只通过 JSON 文件交换
- worker 日志以结构化事件回传 parent

建议命令：

```text
LOAD_ASR
TRANSCRIBE
UNLOAD_ASR
LOAD_ALIGN
ALIGN
UNLOAD_ALIGN
SHUTDOWN
```

单个任务抛出的可捕获异常由 worker 返回为任务失败，worker 继续处理其他任务。worker 进程意外退出属于 batch 基础设施故障，不自动重启。

## 9. ASR Recovery Sidecar

中间缓存：

```text
<base>.asr.json    # ASR 完成但尚未 word alignment
<base>.json        # 正式 WhisperX 词级 JSON
```

`.asr.json` 必须包含足以判断缓存有效性的 fingerprint：

- 编辑版视频路径、大小和修改时间
- Whisper model、compute type 和 source language
- 影响 ASR 结果的关键参数

规则：

- fingerprint 有效时可跳过 ASR，直接恢复 alignment。
- 正式 `.json` 成功写入后删除 `.asr.json`。
- 异常退出时保留 `.asr.json`。
- 所有 JSON 使用临时文件加原子替换，禁止留下半写入的有效缓存。

## 10. Task Isolation

普通阶段错误按视频隔离：

- 当前任务进入 failed
- 不再为该任务调度下游阶段
- 其他任务继续
- batch 最终只要存在失败就返回退出码 `1`

任务状态至少包含：

```text
pending
running
succeeded
failed
canceled
blocked_by_worker_failure
```

## 11. Worker Failure

worker 进程意外退出时：

1. 立即关闭新 batch 任务接纳。
2. 写入当前工作目录下的带时间戳错误日志。
3. worker 当前任务标记失败。
4. 尚未完成 ASR/alignment 的任务标记 `blocked_by_worker_failure`。
5. 已完成 alignment、正在 beautify/翻译/校对的任务继续收尾。
6. 已生成 ASS 的任务允许进入 burn，因为 worker 已退出且 GPU 模型不再驻留。
7. 所有可收尾任务结束后，batch 返回退出码 `1`。

错误日志至少包含：

- worker 退出码
- 当前任务和阶段
- 所有任务状态
- CPU/IO 与 GPU 队列状态
- worker 最后事件
- stdout/stderr 原始尾部
- Python traceback 或进程异常信息

## 12. User Interruption

第一次 `Ctrl+C`：

- 停止接纳新任务
- 停止为现有任务推进新阶段
- 允许当前正在运行的外部命令自然结束
- 清理 worker 和 executor
- 返回标准中断退出码

第二次 `Ctrl+C`：

- 终止所有当前 batch 子进程树
- 保留已完成缓存和 `.asr.json`
- 不删除用户原片或已经成功生成的文件

## 13. Real-time Output

所有子进程 stdout/stderr 实时进入 parent 的单一终端事件队列，由 parent 串行输出：

```text
[02][download] ...
[01][prepare] ...
[03][translate] ...
```

规则：

- metadata 可用前使用稳定任务序号
- metadata 可用后可附加缩短后的视频标题
- 同一行不得被多个任务交叉写入
- 控制台保持实时输出，不等待整个子进程结束
- 错误日志保留未经终端颜色处理的原始输出

## 14. Notifications and Result

保持现有通知语义：

- 每个最终失败的视频响一次错误铃
- batch 全部完成后再按聚合结果响一次
- 全部成功响成功铃
- 任一失败响错误铃并返回 `1`
- help 和 dry-run 保持静默

报告记录每个任务的最终状态、失败阶段、耗时和输出目录。worker 基础设施故障必须在报告顶部单独标记。

## 15. Standalone Pipeline

`pipeline.ps1/.sh` 增加显式 prepare 调用：

```text
download -> prepare-video -> whisper -> beautify -> glossary -> translate -> burn
```

单任务 pipeline 不启动 batch scheduler，但必须复用同一阶段脚本和 `OUTPUT_*` 契约。它仍按顺序执行，不引入后台 broker。

## 16. Validation

CI 不使用真实网络、GPU 或视频，所有外部命令必须 mock。

必须覆盖：

- CPU/IO capacity 自动计算且最小为 1
- prepare NVENC 和 burn NVENC 分别不超过 4
- worker 存活期间 NVENC 为 0
- 所有 prepare 终态前不加载 ASR
- ASR 模型只加载和卸载一次
- ASR 全部终态前不加载 alignment
- alignment 按语言分组且严格串行
- alignment 完成的任务可立即进入后处理
- burn 等待 worker 完全退出
- 单任务失败不阻塞其他任务
- worker 意外退出不自动重启
- worker 失败后已过 worker 阶段的任务继续收尾
- `.asr.json` fingerprint、恢复和原子写入
- 第一次与第二次 `Ctrl+C` 的不同语义
- 实时日志行不会交叉
- PowerShell/bash launcher 枚举、参数和退出码一致
- 已删除所有 jobs 参数及对应帮助文本

真实 RTX 4090 验收必须记录：

- NVENC 峰值并发
- Whisper 模型加载次数
- Whisper 与 NVENC 是否发生时间重叠
- 显存释放时点
- 字幕完整性和总批次耗时

## 17. PR Decomposition

按 `DISCIPLINE.md` 拆分为独立 PR：

1. `py_launcher` 抽取及三个包装入口迁移。
2. download 与 prepare-video 解耦，保持 PowerShell/bash parity。
3. batch scheduler 核心、任务状态和 mock executor。
4. 音频提取、ASR sidecar 和常驻 Whisper worker。
5. alignment wave、语言分组和后处理流水化。
6. burn wave、实时日志、通知和失败恢复。
7. README、AGENTS、setup 与迁移文档同步。

每个 PR 必须独立可测试、可回滚，不得把脚本拆分、worker 和完整 scheduler 一次性提交。
