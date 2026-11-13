# WhisperX 前置源文断句第一阶段实施

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

状态：代码和 mock 回归已落地；尚未运行真实 LLM/媒体质量基准，不代表完整 MFA 方案已实现。

本文件描述不带 --mfa 的前置源文断句路径。后续显式 MFA 模式及安装/实测情况见[集成说明](2026-11-11-managed-mfa-wsl-integration.md)，不以本文件早期范围覆盖后续进展。

## 目标与范围

以[完整目标设计](2026-11-07-en-zh-multi-asr-design.md)为依据，先保留现有 WhisperX 识别、原生对齐、standalone/batch 入口及调度恢复协议，实施源文断句前移、单回合整句翻译和译文分配、纯译文校对。

```text
WhisperX JSON -> beautify -> glossary（仅术语）
-> 独立 LLM 源文断句 -> 本地事件校验和对轴
-> translate（同一回合整句翻译 + 译文分配）
-> 可选纯译文校对 -> ASS -> 可选 burn
```

后续的[审阅/MFA 基础模块](2026-11-09-source-review-mfa-foundation.md)已支持显式 only-glossary 暂存采用稿及独立实验对齐 API，但默认字幕链路尚未启用。MFA 管理环境、正式文稿映射、识别/语义/对齐的 batch 依赖拆分仍为后续实施项；多引擎接入暂缓。当前默认字幕链路不修改源文，不能在 WhisperX 词轴上提前应用尚未对齐的语法编辑。最终顺序仍是 glossary 采用稿 -> MFA -> beautify -> 源文断句 -> translate，不以本阶段的临时顺序替代目标设计。

## 阶段契约

- 源文断句：完整句段、邻接原文、实际 words、长度/时长约束和预生成边界 ID 进入 LLM。短句或 no-split 直接保留整句；长句由 LLM 选择合法边界 ID，不改字、不翻译、不造时间。
- 本地映射：检查完整词序与文字对应、必要词全部有有限合法时间且无逆序/重叠。首版仅暴露可映射词间空白处的字符边界，禁止切开一个原生 word 的多 token 表达。按选中边界切原文、读取词时间，不用字符串首尾 token 搜索重复词。
- translate：输入完整句段及其全部源文事件，同一响应返回整句目标文本和按固定事件 ID 分配的全部目标片段。zh/ja 按无分隔拼接，其他目标语言暂按单空格拼接；仅拼接不一致时本地合并为原句时间的单一事件并保留本次整句译文，不拒收，见[整句回退设计](2026-11-13-translation-whole-event-fallback.md)。复杂排版语言需后续语言策略扩展，不宣称多语言质量已验证。
- 校对：沿用专用 Provider/model 解析和开关，但只处理目标语言，完整句段提交；更新事件时同步整句字段。不接受任何源文或时间修改。
- 代码协议优先于旧风格 prompt。正式 example 文案不改，本阶段不在安装时覆盖本地 prompt。

## 状态、缓存与失败

保留旧 WhisperX 输入及历史内部 en/zh 字段。事件新增稳定 id 和 source_range；本阶段 source_range 是相对所属 segment.text 的 Unicode code point 左闭右开范围，不冒充最终全文 AlignedTranscript v2 的全局范围。后续 MFA/全文重建须升级规划版本并使不匹配工作缓存失效。

event_plan_key 绑定版本、实际文字、词轴、粗范围、邻接上下文、split 约束、模型和完整 prompt；translation_key 绑定规划、语言、完整上下文、实际检索结果及翻译模型/prompt。proofread_key 绑定完整当前译文及校对配置。英文事件已填不能命中翻译完成缓存，旧 proofread_text 不再覆盖源文。

beautified 工作缓存还需匹配原始输入、beautify 参数及视频路径身份；已有 batch media/alignment generation 保留在输入身份中，不重定义 .asr.json/.asr.lock 或发布锁。source-first 处理继续使用 batch 指定的 candidate 输出路径，正式 publish 仍由外层协调器拥有。

工作 JSON 检查点使用同目录唯一临时文件、flush/fsync 和 os.replace 写入；失败清理临时文件且不覆盖旧缓存。损坏或不匹配的 beautified 工作缓存从原始 WhisperX 输入重建，不能把恢复错误当成完成。该文件级写入保护不替代 batch 外层快照核对或正式发布锁。

- 无完整词轴：skipped/word_timing_unavailable，不请求 split，保留整句和粗时间，继续整句翻译。
- 源文边界坏响应：最多两次尝试，成功项保留，失败项整句 fallback；相同输入复用降级规划，force 显式重试。
- 翻译坏响应：仅拼接不一致且结构、ID、非空校验通过时，立即保留源句/本次整句译文并合并事件，原因仅日志显示。其他坏响应仅重试失败完整句段，不孤立补事件、不拼接不同响应；整句与全部事件校验后一起更新。已完成的其他句段写检查点；无有效完整译文则失败，CLI 不继续导出或 burn。
- 校对失败：保留已有完整译文，不撤销源文规划。
- KeyboardInterrupt 不被当作普通模型失败吞掉，不继续 fallback 请求。

字幕仍输出既有 split SRT 和 ASS 文件名。不追加存疑标点或解释，不用本地规则硬切。翻译拼接回退只保留 unsplit 状态，原因不写工作 JSON；其他既有阶段的状态和原因字段保持不变。

## 设计不变量影响

本阶段涉及 D2/D3/D6/D7 的执行顺序、工作 JSON/缓存身份、LLM 协议及职责边界，属于对原 Python CLI 重构“不改变双语 split 顺序/格式”范围的明确后续变更。变更附本说明并需 PR 评审。入口、安装、CPU/IO/NVENC、WhisperX worker、generation 锁及原子 publish 协议保持不变，不新增并行池。

旧低层 translate/split/proofread 辅助函数暂作兼容保留，CLI 使用新的 source-first 调用链。回滚代码时不得强制复用新工作缓存，原始 WhisperX JSON 可用于重建。

## 验证

单元测试 mock LLM 和媒体，覆盖边界 ID/字符范围、重复词、无轴和坏轴、不合法响应、有界重试、整句/事件原子性、只读源文、缓存及 generation 保留、取消、CLI 调用顺序和临时目录中的双语 ASS 输出。完整 unittest 回归必须通过。

真实质量验收仍需同一采用稿/词轴下比较旧双语 split 与新前置源文断句，报告双语自然度、超长/过快字幕、人工重切量和请求/token/耗时；单独评估纯译文校对收益。单元测试通过不代表 WhisperX、LLM 或 MFA 质量基准通过。
