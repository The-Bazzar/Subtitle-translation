# 文本审阅产物与 MFA 基础模块实施

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

状态：代码和 mock 测试已完成；真实 MFA 安装、转录质量和默认流水线串联尚未验收。实施分支为 codex/v2.1.0，多引擎继续暂缓。

本文件记录基础模块阶段的范围。后续已完成受管理安装、正式映射和显式 --mfa 链路的短样本接通，最新状态见[集成说明](2026-11-11-managed-mfa-wsl-integration.md)；下文的未串联说明指本阶段初始边界，不代表最新代码状态。

## 当前可用范围

显式 `translate input.json --only-glossary` 执行既有术语生成后，使用同一 glossary Provider 完成文本优选/字幕编辑，并暂存完整采用稿。该命令不调用 MFA、翻译、源文断句或硬压。普通 pipeline/batch 仍使用第一阶段的 WhisperX 词轴，不将尚未重新对齐的文字替换进去。

新增 `subtitle_translation.mfa` 为内部实验 API，可接收本地规范音频和完整采用稿调用 MFA segment/align，并导出通过检查的全局词时间。它尚未自动注册或安装，不能把此模块存在等同于当前 CLI 已执行 glossary -> MFA -> beautify。

## 采用稿契约

- 输入为不可变 WhisperX 源文单元、来源/粗范围、完整常驻 glossary、项目 metadata 和已有网页证据，不访问音频，也不新增 ASR 专用 LLM 配置。
- 保留/编辑通过 JSON 顶层 items 返回实际源语言 key、decision、basis 和 reason；不返回时间、字幕边界或译文。单来源不宣称存在其他独立候选。
- keep 必须逐字保留原文。edit 必须给出实际修改及 context/readability 依据，不把语法编辑声称为声学确认。语法修订保留原意和表达意图，意图不明时保留玩梗、引用和非标准表达。
- 每批最多 8 项，每次 SDK 请求超时 120 秒、SDK 内重试关闭，由阶段统一最多两次尝试。仅重试失败项，其余项保留。无有效响应时生成实际原文 fallback，而非空文字或疑点占位。
- 独立缓存身份包含输入文件、单元、来源、语言、媒体 generation、术语快照、metadata/证据、模型配置、完整 prompt 和策略版本；模型/配置敏感字段只进入 hash，不以原始认证信息保存。

## 双产物与发布

`<base>.source_review.json` 是指针 manifest。其 generation_dir 指向本项目 `.asr_work/source_review/<input-hash>/<generation>/`，内含不可变 glossary.md、source_review.json 和 manifest.json。采用稿包含完整实际文字、逐项原文/采用文字、来源、修改依据、fallback、全文 hash 和无认证信息的模型来源。

汇总全文时使用版本化语言分隔规则，不把每个原 ASR 单元强制变成段落。source_map 保存每个采用单元在全文中的 Unicode code point 范围，便于后续独立重建句段；原粗时间仍只作为来源定位，不是最终字幕边界。

先写完整 generation，再检查源文件与术语文件仍匹配请求快照，最后原子替换指针。失败或取消不发布半份 generation，可能留下未被引用的私有工作目录，后续另行管理清理。读缓存需核对目录范围、两个产物及 manifest、hash 和逐项文字/来源完整性；旧文件、半份缓存、损坏或路径越界均为 miss。

采用稿从不修改原始 ASR JSON、旧 words、batch sidecar 或当前字幕工作副本。batch 的内部 reuse-glossary 暂不启动审阅；完整 batch 依赖改造后再接入。

## MFA 实验适配器

runtime manifest 的 schema_version 为 1，mfa.profile 为 mfa-3.4-textgrid-v1；需要精确 3.4.x 版本、command/python_command 参数数组和两个本地模型文件。工具数组首项及模型路径必须为已存在的绝对路径，不依赖用户设置 PATH。环境 Python 应来自同一受管理 MFA 工具环境，不能使用全局 Python 或要求 CLI 环境安装 MFA。

适配器执行：规范音频/采用稿快照 -> 环境版本探测 -> mfa segment -> mfa align -> 环境 Python/praatio 解析 TextGrid -> 词序与时间校验。逻辑输入为完整采用稿，实际长音频准备由 MFA 负责，不使用旧 ASR segment 作为硬边界；未匹配原候选的文字同样进入该路径。

- 仅接受 16 kHz、单声道、16-bit PCM WAV，并流式核对音频数据完整性。规范音频提取仍由后续外层 stage 实现。
- 词典和声学模型必须已在本地，运行阶段不执行模型下载命令；HF 相关下载设为 offline，所需 VAD/语言资源也须在 profile 的安装验收中准备和验证。
- 每次调用独立工作目录和 MFA_ROOT_DIR。固定 num_jobs=1，不创建外层并行池，调用方负责已有 CPU/IO 资源 admission。
- 总命令预算 max(300 秒, 4 * 音频秒数)，版本探测最多 30 秒。受管理进程超时返回 124 并回收自身进程树，KeyboardInterrupt 向上传递，不发布 fallback alignment。
- 只从 words 或 speaker - words interval tiers 取实际词，不解析 phone tiers，不用正则猜 TextGrid。
- 检查实际工具版本、完整文字 token 序列、非空词、有限合法时间、范围和顺序；未知词、缺词、非法/重叠时间均不可采纳。该检查不是听录真实性复核。
- 当前基础 API 保守地要求整份词序完整，异常时返回完整采用文字、空 words 和 unavailable/reason；局部失败范围、最终句段重建及粗时间发布仍待外层映射实现，不能宣称已完成正式 AlignedTranscript v2。

成功缓存绑定实际音频/采用文字 hash、本地模型内容、命令/version profile 和解析器代码；命中仍重新探测实际 MFA/praatio 版本并验证缓存词序/时间。模型或文字变化重新运行，不因为 glossary 模型变化而对相同实际采用文字重复对齐。仅完整有效的 alignment 原子发布 current 指针；失败不回退文本或尝试其他对齐器。

## 设计影响与后续

本阶段补充 D3/D6/D7 的语义产物、协议、缓存发布及工具进程边界，附本说明并需 PR 评审。现有 WhisperX worker、NVENC 互斥、CPU/IO 容量、media/alignment generation 和正式 JSON 发布锁不改；内部原子文件工具复用于工作 JSON 和新产物。

下一步需完成受管理 MFA 安装/profile、规范音频 stage、采用稿到正式句段/词位置映射，将普通 pipeline 和 batch 的依赖统一为 glossary/source_review -> MFA -> beautify -> 前置源文断句 -> 单回合翻译。只有该链路与真实英文基准通过后，才启用默认采用稿替换并声明完整支持。

## 证据与验证

官方机制依据：[MFA 安装与入门](https://montreal-forced-aligner.readthedocs.io/en/latest/getting_started.html)、[有文本长音频准备](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/corpus_creation/create_segments.html)、[对齐与输出](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/workflows/alignment.html)、[MFA TextGrid 导出源码](https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner/blob/main/montreal_forced_aligner/textgrid.py)、[praatio 解析器](https://github.com/timmahrt/praatIO/blob/main/praatio/textgrid.py)。这些依据不替代固定版本的真实 profile 验收。

单元测试 mock 所有模型和外部工具，WAV/TextGrid 及响应均为人工构造。覆盖实际 fallback、双产物缓存、输入中途变化、强制重建、原文/旧词轴不变、only-glossary 停止边界、环境/profile 校验、词序/时间校验、资源变化、超时进程回收和取消。未运行真实 MFA、下载模型或评估自动字幕质量。
