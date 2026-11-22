# Subtitle Translation

以 WhisperX JSON 为主输入的字幕下载、翻译、对轴和 ASS 输出工具。
2.1.1 优先单 WhisperX、英文源文验证；多 ASR 接入延期，不属于本版功能。

## 开始使用

Windows 需要 PowerShell 7；Linux/WSL 使用 bash。

```powershell
.\scripts\setup.ps1 -InstallMfa
subtitle-translation help
```

```bash
./scripts/setup.sh --mfa
subtitle-translation help
```

setup 创建项目 .venv、安装 CLI/所选 ASR 依赖并生成缺失配置和命令 shim，
不要求手动 PATH，不复制第二套 CLI。已有配置值不覆盖。
示例的 PROOFREAD=0、MFA_ENABLED=1、PIPELINE_SKIP_BURN=1。新配置默认常开 MFA，需先完成 setup-mfa。
当前 MFA 只支持英文源文；其他源语言请设置 MFA_ENABLED=0，使用兼容 ASR 词轴路径。

## 命令与配置

```text
subtitle-translation pipeline "URL"
subtitle-translation batch "URL1" "URL2" --report "batch-result.json"
subtitle-translation translate "video.json" --video "video.mkv" --lang en --target-lang zh
subtitle-translation translate "video.json" --only-glossary
subtitle-translation translate "video.json" --video "video.mkv" --only-scenes
subtitle-translation proofread "video.beautified.json"
subtitle-translation proofread "video.beautified.json" --output-json "reviewed.beautified.json"
subtitle-translation merge-ass "video.zh.ass" "video.en.ass"
```

CLI 仅保留 URL、路径、语言、输出位置及 --only-*。
help 打印所有子命令的完整帮助；--help / --version 仍可用。
MFA、校对、分段、阶段跳过、模型/设备、批量大小、场景阈值、编码、调试和强制重建均在 .env 配置。
未知参数和已删除的开关直接报错，不静默忽略。
旧 ffmpeg-burn/mpv-burn 脚本均为 burn 别名，后端只由 BURN_BACKEND 配置选择。
完整参数/变量表见 [CLI 与环境配置](docs/cli-and-env.md)。

从其他位置运行时，可在子命令前加 --project-dir 选择配置根。
它不改变输出根；cookies.txt 始终只从命令执行目录读取。
每次 yt-dlp 业务调用前先清理其缓存，清理失败则下载阶段失败。

## 当前流程

MFA_ENABLED=0 时保留兼容路径：

```text
download original -> prepare edit video -> WhisperX JSON
  -> scene detection -> glossary -> joint bilingual translation
  -> ASR word-time projection -> optional target proofread -> ASS -> optional burn
```

模板默认 MFA_ENABLED=1：

```text
WhisperX JSON -> glossary/source_review -> joint bilingual translation
  -> source-window corpus MFA -> scene detection -> optional target proofread
  -> ASS -> optional burn
```

- glossary 是唯一自动源文编辑阶段，保留语义、否定、数量、实体和玩梗；不确定则保留实际候选。
- translate 同一次响应返回整句译文和成对源/目标字幕片段，按完整上下文语义断句。
  不独立调用 split，不要求 LLM 计算字符位置、事件 ID、时间或严格拼接等式。
- 本地只读定位源片段，生成稳定事件 ID/范围。没有有效片段但整句译文有效时保留整句双语内容；
  完全缺少实际译文时有界重试，不能导出空字幕。
- PROOFREAD 控制翻译流程中的纯译文校对，不修改源文、事件 ID/数量或时间；失败保留已有译文。
- 场景检测只写 sidecar，不吸附或改时间，不补时长/间隔，不缩放词轴。
  调整场景设置不失效有效译文或字幕时间缓存。
- PIPELINE_SKIP_BURN=0 才让模板配置下的 pipeline/batch 执行后续硬压。
  prepare-video 保留 CPU decode，优先 NVENC，按既定策略回退 CPU。

SRT 是检查输出，不是输入缓存。原始识别 JSON 不被修订稿覆盖。
主翻译按 2.0.2 的调用方式共享同项目会话：跨批保留已发送请求和译文，温度 0.3，最多三次尝试。
联合翻译/断句默认每批 20 个完整句段（TRANSLATE_BATCH_SIZE），降低单轮响应负担；校对批量未指定时仍取其一半，默认 10。已有 .env 的显式批量不自动更改。
所有文本 LLM 阶段默认查询当前 provider 的模型元数据获取窗口，并在约 80% 时压缩可移除的历史。翻译保留近期原译对；其他阶段按完整对话轮次裁剪，保留完整术语库、当前请求和工具结果链，不额外调用摘要模型。接口不提供窗口时有日志提示并使用回退预算；特殊部署可在 providers.json 的 model_info 列表中覆盖。单次正文已经过大时不会静默截断。详见[上下文预算](docs/superpowers/specs/2026-11-19-translation-context.md)。
RAG 按批检索并使用 EMBEDDING_TOP_K，完整 glossary 常驻 system；user 项恢复为 id、源文及 retrieved_context，只在响应中新增双语 split_events。
每批按检索、LLM 请求、保存检查点的顺序执行，不预取全部批次；MFA 路径的 glossary 也保留原有 embedding/RAG 输入。
纯译文校对共享独立的项目内会话，温度 0.2，按 event 数量组织完整句批次；逐 event 检索，最多三次尝试，超限先缩小批次，再尝试去除动态 RAG。
响应保留 2.0.2 的 ISO 语言键规范化及 EN/ZH/SRC/TGT 标签清理，仅清理目标文本，不修改冻结的源文片段。
详见 [翻译与校对会话契约](docs/superpowers/specs/2026-11-15-translation-session-compatibility.md)。
ASS/SRT 序列化按各自精度取整并正确进位，不改变内部词轴。

上下文元数据补充来源为 OpenRouter 的公开 `/api/v1/models`：当前 provider 缺失窗口时匿名读取目录，查询时忽略 vendor 前缀，只精确比较 model_id；同名条目的窗口若不一致则不采用。目录结果仅是标称窗口，日志明确提示实际部署限制尚未验证；已有部署元数据或人工覆盖仍优先。不向公开目录发送项目内容、模型查询参数或 provider 密钥。未知模型首次查到窗口后，程序会将数值和来源保存到本地 providers.json 的对应 provider/model_info，后续运行直接读取。失败和估算值不写回；删除自动条目或它的 context_length 后，下次运行可重新查询。
包内 provider 示例为已核实的云端默认模型预填目录窗口。复制模板后，这些正整数按显式覆盖处理；若部署限制不同，可修改或删去对应的 `context_length` 恢复自动查询。本机模型示例只预填 ID。

## 单独校对


proofread 只接受已翻译、已定时且含稳定事件 ID/源文范围的工作 JSON，通常为 .beautified.json；不接受原始 WhisperX JSON、ASS、SRT 或 pending 文本检查点。
显式执行每次重新校对，不受 PROOFREAD=0 或 MFA_ENABLED 影响，不重跑翻译、glossary、MFA、场景或简介/章节翻译。
默认原位更新工作 JSON 并重新导出源/目标/双语 ASS 与检查 SRT；--output-json 另存工作 JSON，字幕默认跟随另存文件的目录/基名；--output 仅指定双语 ASS。
新工作 JSON 保存 subtitle_languages，独立校对优先采用其中的语言；旧文件缺失时使用 CLI/.env/ASR 默认值并警告，可显式传入 --lang/--target-lang。与已保存语言冲突时拒绝，不把校对当成重新翻译。
源文 ASS 统一为 .<source>.ass，例如 .en.ass；无论是否校对均不再生成 .en.proofread.ass，旧文件不自动删除。
详见 [独立校对与输出契约](docs/superpowers/specs/2026-11-16-standalone-proofread.md)。

## MFA 安装与边界

```text
subtitle-translation setup-mfa
subtitle-translation setup-mfa --distribution Debian
```

模板已设置 MFA_ENABLED=1；安装后运行普通 translate/pipeline/batch。已有配置仍保留原值，需要关闭时设为 0。
开关不自动安装或下载模型；缺失资源时在语义请求前报错，不暗中退回另一种词轴。
当前受管理 profile 为 MFA 3.4.3，与项目 .venv 隔离。
Windows x86_64 默认原生安装，不依赖 WSL；完整安装使用 `scripts/setup.ps1 -InstallMfa`。
显式 --distribution 才使用已有 WSL；Linux x86_64 本机，macOS 尚未接入。
VAD 优先复用完整本地快照，直接加载固定模型的 YAML/权重，不要求不存在的 custom.py；支持缓存/输出目录带空格。
MFA 工具进程隔离用户 Python 包与继承的 PYTHONPATH/PYTHONHOME，避免加载工具环境之外的依赖。

MFA 按来源 ASR 句段粗时间定位窗口，跨段/共享来源时合并相邻范围。
Windows 原生 MFA 的执行文件使用独立短目录，避免 SQLite CLI 的长路径限制；缓存仍保留完整身份哈希，不影响译文检查点。
当前每块上限 60 秒/12 源单元、零额外 padding。
全部有效窗口作为独立音频/文稿加入同一 corpus，仅一次 align 和一次批量导出，固定 num_jobs=1。
不把整片全文作为无锚点 utterance，不给每个窗口重复启动模型。

必要词、时间范围、异常空洞和不可分离映射仍由本地验收，不等于听录真实性证明。
失败保留完整双语粗时间输出；相邻粗时间重叠时协调合并，不制造词时间或重发翻译。
完整跨语料质量、边界余量和平台矩阵仍需验收，不能仅凭结构校验宣称准确。
版本、目录和资源见 [MFA 安装配置](docs/mfa-installation-preferences.md)。

## 数据与缓存

- .json：原始识别；.beautified.json：沿用兼容名称的派生工作 JSON，不代表时间美化。
- segments[].words[] 是派生词轴；旧根级 word_segments 不复制到工作文件。
- glossary.md / .source_review.json / .web_evidence.json：术语、采用稿及网页证据。
- .asr_work/text_events/：无精确时间的双语检查点；pending 事件不能导出。
- .scenes.json / .scenechange.txt：独立场景检测缓存。
- .split.<lang>.srt、.<source>.ass、.<target>.ass、.<source>-<target>.ass：字幕成果，不在文件名编码可选校对阶段。
- .<target>.description：标题、简介、标签及同名原片内嵌章节的目标语言内容。

语义、原生 MFA corpus、定时投影和场景缓存分别绑定实际输入。
显式执行 subtitle-translation translate 时，每次重新翻译和联合断句，并在 PROOFREAD=1 时重新校对；不读取旧 .beautified.json 或文本检查点中的译文/校对结果来跳过请求，成功后覆盖输出。
glossary/采用稿、ASR、场景和原生 MFA 声学缓存仍可复用，不需要删除；--only-* 仍只执行所选操作。
pipeline/batch 内部的续跑缓存规则不变，旧调用身份首次切换会重译。
FORCE_REBUILD 会重建工作规划/译文，使用后应恢复 0。
原生模型/profile/解析器变化可能要求重对轴，但不应因此重发输入不变的语义请求。

完整 glossary 常驻提示词，retrieved_context 只补充。
embedding 对所有模型统一逐文本串行请求，不提供 EMBEDDING_BATCH_SIZE。
每轮实际 translate 请求在 stderr 记录项目/句段 ID、耗时、响应与校验/回退摘要；
每个有效句段/event 还打印编号、稳定 ID、split/unsplit/whole_fallback 状态及当前时间（MFA 前为 pending）。
每条采用的 event 均记录完整原文/译文、ID、状态和当前时间，仅按 LOG_LEVEL 过滤。
正常 split/unsplit 使用 INFO；回退、重试、失败等异常状态使用 WARNING。任务级错误仍为 ERROR。
内部续跑缓存命中也逐 event 打印并注明 cache=hit，不伪造 LLM 请求轮次；不打印 prompt、glossary、检索上下文、原始回复或密钥。

## 运行日志

应用日志统一使用 Loguru，包含当前时间/时区、级别、项目和阶段。
控制台始终使用 ANSI 配色，按日志级别着色；文件始终为纯文本，不增加颜色配置项。
单项目固定写视频文件夹内的 `subtitle-translation.log`：优先 --video 所在目录，否则使用输入 JSON 目录，绝不使用配置根。
batch 全部集中写执行目录的 `batch-result.log`，不在各视频目录另建日志；行内项目名区分并发任务。
每次重新 run 覆盖旧文件，同一轮内部阶段共用文件，不轮转、不备份。
`.env` 的 LOG_LEVEL 默认 INFO，可选 TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL；控制台与文件统一使用这个阈值，不再提供 QUIET。
日志不改变 stdout 路径 marker。详细规则见 [运行日志](docs/logging.md)。

## Batch 资源与恢复

CPU/IO 为 max(1, (os.cpu_count() or 1) // 4)，prepare/burn 共用最多 4 路 NVENC，
NVENC 与 WhisperX 不同时运行，WhisperX worker 全局串行复用模型。
MFA 使用既有 postprocess admission，不另建并行池。
任务顺序、.prepare.json/.asr.json/.asr.lock、generation、candidate 和原子发布规则不变。

第一次 Ctrl+C 停止新阶段并等待活动任务；第二次终止进程树，返回 130。
worker 意外退出沿用 fail-fast；已完成对齐的任务仍可后处理。
batch 输出文本和 JSON 报告，不接受手动 jobs 参数。
URL batch 无法表达已有输入映射，配置 skip download/whisper 会在启动前报错。

## 验证与资料

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

自动测试 mock 网络、LLM、yt-dlp、ffmpeg、WhisperX/MFA。
真实媒体、安装/平台、模型质量和 CUDA 验收单独进行。

- [迁移说明](MIGRATION.md)
- [v2.1.2 发布说明](docs/releases/v2.1.2.md)
- [v2.1.1 发布说明](docs/releases/v2.1.1.md)
- [技术与协作约束](AGENTS.md)
- [2.1 架构与发布范围](docs/v2.1-architecture.md)
- [CLI 与环境配置](docs/cli-and-env.md)
- [MFA 安装配置](docs/mfa-installation-preferences.md)
- [Python CLI 设计不变量](docs/superpowers/specs/2026-08-21-python-cli-rewrite-design.md)
- [后续 ASR 路线图](docs/superpowers/specs/2026-08-25-v2.1.0-asr-provider-design.md)
- [许可证 GPL-3.0](LICENSE)

本地 .env、provider、cookies、prompt、模板、媒体、字幕、术语、sidecar、模型/runtime、
Chroma 和 batch 报告都不能提交。外部维护的 *_prompt.example.md 文案不在本项目修改。
