# 受管理 MFA、WSL 与显式流水线集成

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

状态：codex/v2.1.0 开发实现；短样本与缓存接通已实测，默认行为尚未切换。不是完整质量验收或发布声明。

实现更新：[翻译后窗口化 MFA 与合并窗口](2026-11-13-post-translation-windowed-mfa-design.md)首版已接入显式 --mfa，见[当前实施边界](2026-11-13-windowed-mfa-implementation.md)。本文保留早期前置 MFA 流程及历史验收记录，不能把旧全文链路或词序结构校验当作时间位置准确的证据。

## 当前流程

```text
WhisperX candidate JSON -> glossary + source_review
-> canonical PCM WAV -> MFA segment -> MFA align -> TextGrid words
-> full-draft sentence contexts -> beautify -> LLM source split
-> single-response sentence translation / event allocation
-> optional target-only proofread -> ASS / optional burn
```

仅显式 `translate/pipeline/batch --mfa` 启用该路径。不带参数保留前一阶段的 WhisperX 词轴流程；多引擎仍暂缓。skip knowledge 透传实际原文后继续 MFA，only-glossary 不执行 MFA，only-beautify 保留已有 JSON 检查语义。

## 安装与环境

`setup-mfa` 使用项目 CLI，安装独立工具环境，不创建另一套 CLI，不修改用户 shell/PATH，不覆盖 WhisperX/PyTorch。Windows 使用已有 WSL（可指定 distribution），Linux 使用本机 x86_64 环境；不自动安装 WSL，不支持未经验证的平台/profile。两套 setup 的安装开关转发到同一 Python 安装器。

受管理组合锁定 micromamba 2.10.0、MFA 3.4.3、CPU torch/torchaudio 2.8.0、SpeechBrain 1.0.3，并限制 Hugging Face 客户端为兼容的 0.x。后两项是 MFA 长音频准备的 VAD 依赖，不是新增 ASR 来源或音频真实性复核。

英文声学模型/词典使用官方 v3.0.0 固定发布资源，检查下载大小及 archive 完整性，记录文件 hash。固定 URL 避免模型管理器枚举匿名 GitHub releases。词典使用 DictionaryModel 加载校验，避开 3.4.3 pretty-print 的 Path 类型转换问题。

安装阶段预下载并加载 speechbrain/vad-crdnn-libriparty。runtime 保存实际 HF revision 与文件指纹，正式任务使用私有缓存并禁用 HF 在线下载。Linux 快照符号链接在工具环境内校验，不依赖 Windows UNC 对符号链接的文件判断。工具、模型、验证音频、asr_runtime.json 都是本地生成物；准备失败不替换已就绪 manifest。

## 调用与资源

runtime 保存绝对参数数组和明确 WSL 发行版，路径通过 UNC/WSL path 转换，不用 shell 字符串拼接。MFA 数据库和临时目录位于 Linux 文件系统，解析器只在工具 Python 内导入 praatio。

每次调用独立 MFA_ROOT_DIR，固定 num_jobs=1，OMP/OpenBLAS/MKL 限制为 1 且隐藏 CUDA。不加入新并行池；batch 仍使用现有 postprocess CPU/IO 份额，NVENC/WhisperX 生命周期及用户任务顺序不改。

batch MFA 模式用一次协调调用替代旧的独立 beautify/glossary/translate 三次入口调用。原 WhisperX worker/alignment 恢复协议暂保留，存在冗余原生 alignment 开销，但正式词轴只取 MFA。MFA 指纹另存 _asr，不重新解释 .asr.json/.asr.lock/_batch_artifact 的 media/alignment generation；正式 candidate 仍由原外层锁与快照机制发布。

续阶段、模型请求和提交使用 admission guard，关闭 admission 后不进入新 MFA/LLM 操作。超时回收自身进程树，取消向上传递，不生成假装业务失败的 fallback。规范音频和工作 JSON 检查输入快照及有效载荷，原子替换保存。

## 文稿映射

MFA 规范化词映射回完整采用稿字符范围，保留大小写/标点。一对多、连字符或缩写拆分合并为不可拆时间单元，不能将一个时间区间均分给显示词。数字等无法可靠映射的情况仍降级，不靠时间单调性宣称成功。

pySBD 只重建语句上下文，使用无损字符范围并允许跨原 ASR 单元，不决定最终字幕断句。句界穿过不可拆映射组时合并上下文，再由 LLM 在合法词边界选择字幕断点。

工作 JSON 保存 _asr、global source_range、来源、text_origin、word_timing、segment_timing、manual_review 和原 batch artifact。split_events.source_range 仍是 segment-local 字段。无轴范围保留采用文字及粗时间，不恢复旧词轴，不参与 beautify 场景吸附/边界整理，整段翻译后交人工。

桥接按原始 utterance 完整覆盖采用稿后逐区间验证；局部缺词或 OOV 只使相交的语句上下文降级，保留其他完整词轴，结构损坏仍整体降级。解析器升级允许严格核对后复用已完成的原生 TextGrid，见[局部失败与恢复设计](2026-11-11-mfa-partial-alignment.md)。对齐成功不证明原文真实，也不反向改写或选择文本。

## 缓存与实测

成功对齐绑定实际音频/采用稿、模型、VAD revision/文件指纹、工具及安装包环境指纹。命中仍探测环境，但不再执行 segment/align。事件/翻译身份按工作 JSON 的毫秒精度绑定，避免高精度时间序列化造成虚假 cache miss。

Windows 前端 + WSL Debian 已完成官方 LibriSpeech demo 短样本：11.765 秒、27 个词时间、2 条 ASS 事件；第二次只做一次环境/资源探测，没有重复对齐和 LLM 请求。采用稿编辑及其他语义响应使用 schema fixture，MFA、音频和 TextGrid 解析为真实执行，原始 JSON 保持不变。

该结果只证明链路和缓存，不证明自然翻译、语义断句、长音频、源文真实性或发布门槛通过；Linux native 安装器及其他语言仍需独立实测。单元测试 mock 模型/网络/工具；`tests/manual_mfa_smoke.py` 为需显式指定音频/文稿的手工检查，不参与 unittest 自动运行，不调用真实付费 LLM。

## 对齐与后续

本阶段落实 D1/D2/D3/D6/D7/D10 的数据源、顺序、缓存、协议、职责与安装扩展，需 PR 评审；当前不改稳定 main 或发布标签。默认启用前还需长音频、实体/数字/口语/OOV、部分失败、完整 batch 中断/恢复及真人译文/断句基准，再评估冗余 WhisperX alignment 和纯译文校对收益。

依据：[MFA 入门](https://montreal-forced-aligner.readthedocs.io/en/latest/getting_started.html)、[micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html)、[英文模型与引用/许可](https://mfa-models.readthedocs.io/en/latest/acoustic/English/English%20MFA%20acoustic%20model%20v3_0_0.html)、[VAD 模型](https://huggingface.co/speechbrain/vad-crdnn-libriparty)、[官方推荐 demo](https://github.com/MontrealCorpusTools/librispeech-demo/tree/v1.0.0)。
