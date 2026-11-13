# ASR 兼容层、glossary 原文校对与 MFA 对齐完整设计

> 历史目标参考，多 ASR 未在 2.1 实现。本文旧命令、阶段和 schema 不作为当前功能承诺；当前流程见 [架构与发布范围](../../v2.1-architecture.md)，后续入口见 [路线图](../../superpowers/specs/2026-08-25-v2.1.0-asr-provider-design.md)。

状态：完整目标设计，供分步实施和评审使用；显式 MFA 链路已接通短样本和缓存，默认启用与完整质量基准尚未完成。
设计基线：2026-11-13。首版仅验证 en -> zh，保留多语言接口。

最新目标为“文本断句/翻译 -> 窗口化 MFA -> beautify”，显式 --mfa 已接通，并以单一项目 corpus 批量 align，取代逐窗口重复启动。详见[目标设计](2026-11-13-post-translation-windowed-mfa-design.md)、[实际初值/性能边界](2026-11-13-windowed-mfa-implementation.md)和[批量执行契约](../../superpowers/specs/2026-11-13-windowed-mfa-corpus-batching.md)；padding、跨语料性能与完整质量基准仍待验收。

本文是目标行为的主设计文档。[研发路线图](../../superpowers/specs/2026-08-25-v2.1.0-asr-provider-design.md)只维护交付顺序，[Provider 输出考据](../../superpowers/specs/2026-11-07-asr-provider-output-contracts.md)维护官方源码依据和原生格式。当前实际行为仍以 AGENTS.md 和代码为准。未在阶段实施说明中明确完成的配置、字段、参数和命令仍是待实现契约，不能当作当前版本已经可用的功能。

当前实施优先单一 WhisperX，多引擎接入暂缓。已落地的源文断句前移、单回合翻译分配及纯译文校对边界见[第一阶段实施说明](2026-11-09-whisperx-source-first-implementation.md)；这一阶段仍使用 WhisperX 词轴和仅术语 glossary，不代表完整 MFA 方案已实现。本文其余目标契约在相应模块落地并验收前不能视为当前能力。

显式 only-glossary 的采用稿产物见[基础模块说明](2026-11-09-source-review-mfa-foundation.md)。受管理安装、跨 WSL 调用、正式文稿映射和显式 --mfa 链路见[集成实施说明](2026-11-11-managed-mfa-wsl-integration.md)；默认仍保留旧词轴模式，不将编辑稿直接套入旧轴，完整质量基准仍待完成。

## 1. 已确定的产品原则

1. setup 默认预选 WhisperX，可替换为任意已支持单引擎或增设多引擎；各 Provider 平等。
2. ASR 只识别并保存候选，单源直接透传，多源保留实际结果和确定性兜底稿；不调用语义 LLM，不涉及 glossary。
3. 文本优选与字幕编辑统一放在 glossary 阶段，与术语整理结合候选和语境完成；单来源同样可处理，不声称已验证实际发音。
4. glossary 必须输出完整实际源文及术语资料，失败或无法确定时输出实际文本 fallback，不只留下疑点。
5. MFA 是唯一正式对齐器，位于 translate 之后；使用 ASR 句段粗时间定位音频窗口，必要时合并相邻窗口，对对应完整定稿及最终事件定位时间，包括不等于任何 ASR 候选的文字。不做全片无锚点的全文推进。
6. 移除独立 LLM split。translate 在同一次响应中根据整句语义生成源/目标字幕片段，不预填英文事件，不要求 LLM 计算边界 ID、字符数或时间；本地定位原文并生成事件身份，MFA 后置对轴。
7. translate 和 proofreading 不再纠错源文，不回调 glossary、ASR 或 MFA。
8. proofreading 若保留，只校对译文；失败保留实际译文，正文不插存疑问号、括号说明或待确认标签。
9. 正常问句和原内容括号保留，未解决疑点进入非展示记录，最终人工处理。
10. 移除 proofreading 仍为收益评估项，保留既有显式跳过能力，本次不直接删除功能。
11. translate 同一次请求/响应内理解整句、翻译并填充 split_events.en/zh，不另发源文或译文 split 请求。语义断句由翻译 LLM 决定，目标片段不必严格拼回整句译文；源文只读和实际译文非空仍受保护。
12. MFA 失败不撤销修订或翻译，不伪造子事件时间；受影响语句保留完整双语内容并使用来源粗时间作整句投影，交最终人工定时/断句，不能继承漂移的 MFA 区间。

首版验证 en -> zh，保留多语言入口。混说、流式、外部听录导入、自动模型路由不在验收范围。MFA 发音词典是对齐资源，不等于语义 glossary；对齐成功不是文字正确的证据。

## 2. 完整流程与职责边界

```text
download -> prepare-video -> canonical audio
  -> ASR：所选 Provider 识别，保存不可变候选和实际兜底原文，无 LLM
  -> glossary：术语整理 + 文本优选与字幕编辑，输出实际采用稿
       -> 失败时输出实际文本 fallback 和可用术语资料
  -> translate：单回合整句理解/翻译 + 成对生成源/目标字幕片段
       -> 本地定位源文、生成 ID/范围；无法定位时保留整句双语内容
  -> MFA：ASR 粗时间定位 / 相邻窗口合并 -> 定稿对齐 -> 最终事件定时
       -> 失败范围保留实际双语内容和来源粗时间，最终人工
  -> beautify -> 可选纯译文 proofread -> ASS -> 可选 burn
```

正式时间轴 beautify 位于后置 MFA 之后，保留已翻译事件的文字、ID 和数量；场景检测可独立缓存，不能提前修改尚未形成的正式时间。MFA 接管原后置 proofreading 所在位置的对轴职责，纯译文校对是否删除独立评估。前后阶段单向推进，不因对轴失败返回上游改文。

| 阶段 | 职责 |
|---|---|
| ASR | 转录、原生格式转换、来源/粗范围、实际兜底文稿，不读 glossary 或做 LLM 裁决 |
| glossary | 使用候选、metadata、术语及上下文，统一整理术语、优选文本和编辑字幕，不承担最终字幕断句 |
| MFA | 翻译后按来源粗时间窗口及必要合并，对定稿和最终事件定位时间；不改双语文字、不验证听录真实性 |
| translate + 本地事件定位 | LLM 根据整句语义同时返回源/目标片段；本地定位原文并生成 ID/范围，不要求数学边界/拼接等式，不修改定稿 |
| proofread（可选） | 仅校对译文，不改源文、event 数量/时间、不回调前序阶段 |
| 最终人工 | 处理剩余源文/译文疑点、定时和断句 |

Provider segments、比较单元、MFA blocks、翻译句段和字幕 events 是不同对象。原始分段不能成为后续硬边界。

## 3. 配置与 setup

### 3.1 一个结构化配置文件

首版建议新增项目本地 asr_providers.json，唯一 example 位于包内 examples/asr_providers.example.json。该文件不存 API key；LLM 连接信息仍使用既有 providers.json 和环境变量。

示意配置：

```json
{
  "schema_version": 1,
  "enabled": ["whisperx"],
  "providers": {
    "whisperx": {"backend": "whisperx", "model": ""},
    "parakeet": {"backend": "nemo", "model": "nvidia/parakeet-tdt-0.6b-v3"},
    "qwen_asr": {"backend": "qwen_asr_transformers", "model": "Qwen/Qwen3-ASR-0.6B"}
  },
  "mfa": {
    "acoustic_model": "english_mfa",
    "dictionary": "english_us_mfa",
    "supplemental_dictionary": ""
  }
}
```

模型名只表示候选配置，不代表目标环境已经验证可用；实际依赖、模型 revision、工具版本和模型资源格式由通过验证的安装 profile 锁定。

- 默认 enabled 只有 whisperx，可改为只包含 parakeet、只包含 qwen_asr 或多个来源。
- providers 中存在定义不等于已经安装或启用。运行只调用 enabled 集合。
- CLI 显式参数优先于本文件；字段缺省使用包内默认值。CLI 不自动安装依赖。
- 每个引擎的非空 model 优先；WhisperX model 留空时兼容非空 WHISPER_MODEL，再使用适配器默认模型。其他引擎不读取 WhisperX 模型变量。
- glossary 原文校对使用既有 GLOSSARY_PROVIDER / GLOSSARY_MODEL 规则；留空时按现有规则复用翻译 Provider/模型，不新增 ASR 专用 LLM 配置。
- ASR 不初始化语义 LLM。glossary 根据双产物缓存和 skip 策略决定是否调用 LLM，单来源不免除其后语义阶段和翻译所需配置。
- 源/目标语言继续使用现有 SOURCE_LANG、TARGET_LANG 和 CLI。新路径首版仅接受 en -> zh。

窗口、内部并发、fallback 算法等实现参数由版本化策略管理，不为每个细节新增 .env 变量。暂不增加第二个全局 embedding 或 ASR batch size 参数。

### 3.2 安装与启用分离

setup 在 PowerShell 和 bash 中提供相同选择语义：默认预选 WhisperX，允许取消、替换或增设 Provider。只安装所选运行依赖、准备所选模型资源；未选择的库不得被 eager import。

项目 CLI 继续只使用项目 .venv。MFA 使用受 setup 管理的独立工具环境。可选 NeMo / Qwen 若与 CLI 依赖冲突，使用受管理的引擎工具环境，通过同一 worker 协议调用，不复制第二套 CLI。

工具路径、实际版本和安装状态写入本地 asr_runtime.json；不要求手工设置 PATH，不调用全局 Python。配置和 runtime manifest 都需加入本地文件忽略规则。

首版采用本机受管理子进程，不自动安装 WSL，不启动常驻推理服务。某平台尚未验证的 profile 在 setup 中明确显示不可用或实验状态，不静默切换后端。首批目标是 Windows 与 Linux；支持矩阵须用真实安装测试填写。

升级保留已有模型、启用选择及本地配置。额外引擎安装失败不能悄悄改 enabled 或破坏已经可用的默认配置；通过检查后再原子更新选择。模型缓存不因重建运行环境被删除。

setup 负责准备所选模型。运行前 preflight 检查本地资源；缺失时给出明确的准备命令和原因，不能将无进度的大文件下载藏在“识别中”。

## 4. 模块划分与 Provider 契约

```text
core/subtitle_translation/asr/
  types.py        # 共享数据和 schema
  config.py       # 结构化配置、兼容变量、preflight
  registry.py     # backend/profile 注册与能力声明，按需导入
  audio.py        # 统一音频、输入区间和坐标
  worker.py       # 受管理 Provider 子进程与协议
  candidates.py   # 非语义候选对应与实际 fallback，无 LLM
  mfa.py          # MFA 工作目录、调用和结果转换
  normalize.py    # 正式 JSON、词映射和状态校验
  backends/
    whisperx.py
    parakeet.py
    qwen_asr.py
```

glossary 原文校对属于 translate_srt 的 glossary 模块，经公共本地 MFA 服务继续前进；语义逻辑不进入 ASR adapter。现有 stages.py 与 batch_runtime.py 复用同一 ASR stage。whisper_worker.py / controller 可以作为迁移 facade，调度器只处理统一命令和结构化结果，不直接解析厂商对象。

### 4.1 Provider 接口

```python
load(config) -> None
transcribe(audio_path, request) -> SourceResult
unload() -> None
```

request 至少包含 audio_id、input_id、输入音频范围、source_language、实际模型参数及取消上下文。Provider 返回的是候选，不是正式 MFA 文稿。

能力声明包括支持语言、设备、输入长度和格式、语言检测、是否返回原生分段/词轴及 backend profile 版本。不能通过直接调用一次 transcribe 推断它支持所有能力。

跨进程只传路径、请求 ID、配置和 JSON-safe 结果；不传 GPU tensor、decoder state 或未受控库对象。候选原文和相关原生字段投影不可变，未导出的重型状态字段明确记录。

### 4.2 原生格式依据

| 后端 | 真实结构依据 | 转换规则 |
|---|---|---|
| WhisperX | 候选 text、segments；识别和内部 alignment 是不同阶段 | 新主路径只取识别候选，不为最终词轴重复执行其内部 alignment |
| Parakeet / NeMo | Hypothesis.text、可选 timestamp 和 score；容器取决于调用 profile | 解包固定版本的 best hypothesis；帧 offset 显式换算，原生 score 不当跨模型概率 |
| Qwen / 官方 wrapper | list[ASRTranscription]，含 language、text、可选 time_stamps | 默认仅文字识别，不加载 Qwen ForcedAligner；English 映射为 en，无原生句段时间时保留输入范围 |

详细字段与已核对的固定官方提交见 [输出考据](../../superpowers/specs/2026-11-07-asr-provider-output-contracts.md)。源码核对与真实推理验收分开记录。原生 Transformers、官方 wrapper、vLLM 和云 API 不是同一 parser，本轮不自动兼容未验证接口。

### 4.3 SourceResult v1

候选必须包含 source_id、provider、backend、model、model_revision、runtime_version、audio_id、input_id、input_range、requested_language、实际 text、原生投影引用及 status。原生语言、句段、词轴和分数为可选字段。

输入范围来自调用方，不冒充模型识别的语音边界。原生时间统一为绝对秒并记录原单位和转换参数。所有字符范围使用 Python Unicode code point 的左闭右开区间，绑定具体文字版本；比较规范化必须保留原始字符映射。

status 区分 ok、empty 和 failed。空文本不单独证明静音，不补写占位句子。多个输入的返回数量、顺序及 ID 必须校验，未知返回结构作为该来源的协议错误处理。

## 5. 统一音频、比较单元与全文组织

### 5.1 单一音频坐标

所有来源使用同一解码音频：16 kHz、单声道 PCM，时间原点为原视频零点。以实际 PCM 采样内容和采样率形成 audio_id，裁剪区间先按整数 sample 记录，再转换秒时间。

音频只做必要解码/重采样，不由不同 Provider 分别改变速度或静音结构。音频提取与原媒体 generation 绑定，所有识别和 MFA 完成前保持输入快照可用。

### 5.2 首版比较策略

首版采用共同输入区间，避免依赖 Qwen 等接口不存在的原生句段时间：

1. 复用现成 VAD 实现生成公共识别窗口；不从任一 ASR 的语义分段决定全局窗口。
2. 各 Provider 对相同 input_id 的音频范围生成文字。引擎内部如何分句不改变 input_id。
3. 一次输入产生多个原生文本块时，adapter 按顺序组成该来源文字并保留原块映射，不编造细粒度时间。
4. 工作单元可以合并相邻公共窗口；LLM 可取得相邻上下文，但只输出当前工作单元的文字，不重复输出上下文。
5. 每个有效输入文字只归属一个输出工作单元；单元合并后必须更新输入清单，防止重复和遗漏。

初始策略建议目标窗口 20 秒，优先在停顿处结束，30 秒为一般窗口上限；强制截断语音时记录边界疑点，允许把相邻窗口并为比较单元。比较单元建议不超过 3 个窗口或约 200 个英文词，超长内容继续按可追溯文字范围分配，不截掉未处理文本。这些是工程初值，不是模型最大长度声明。

原生分段和时间可帮助诊断边界，不能成为 Provider 权威。出现 1:N、N:1 对应时在共享输入范围内处理。对不同长度候选的 token 对应使用现成编辑距离/序列库，例如 RapidFuzz，并保留字符位置；不手写一套 ASR 解码或强制对齐算法。

语义一句跨窗口不意味着两段必须成为两个最终字幕。后续完整稿按文字位置重新组句，窗口只是处理坐标。

### 5.3 相同文字与分歧

LanguagePolicy(en) 对比较副本统一 Unicode 引号、空白及大小写，生成带原字符位置的 token；不把不同数字、专名或漏词当成等价。

候选比较只记录相同/分歧及准备 fallback，不在 ASR 中调用 LLM。全部实际候选交给 glossary；即使候选一致，仍可在语义阶段发现术语、近音词或上下文问题。重复词仍是有效 token，不能以去重方式消除真实口头重复。

同一模型/权重的重复运行只算一个识别来源。Source Result 的数量不等于独立证据数量。

## 6. Glossary 术语与唯一原文校对阶段

### 6.1 双输出契约

输入为 CandidateBundle、实际兜底文稿、候选位置映射、metadata 及既有 glossary 阶段允许的上下文/证据。单源和多源均可处理。原文的漏听、听录差异、语义和术语疑点在本阶段统一处理，ASR 与 proofreading 不再重复纠错。

glossary.md 仅保存术语及表达规则，另输出 source_review.json 的实际采用稿、来源及修改记录；两者通过不可变 manifest 关联。字幕语义断句由 translate 同回合生成双语片段，不合并进 glossary，也不单独请求 split。

响应形状示意：

```json
{
  "glossary_markdown": "## Terms",
  "items": [
    {
      "id": "u0001",
      "en": "Hello.",
      "decision": "correct",
      "evidence_ids": ["A:u0001", "B:u0001"]
    }
  ]
}
```

源文 key 随语言变化。逐项校验 ID、非空文字、引用和范围；没有完整匹配候选不妨碍有据的修订。正式 prompt 在既定 prompt 仓库维护，当前结构不是已实现协议。

### 6.2 实际文本 fallback

medoid-v1 保留为输入准备规则，不作为 ASR 语义裁决：候选去重后，单源取实际文字；多源取到其他候选平均规范化 token 编辑距离最小的现有文稿。同分按原文 SHA-256、再按稳定 source_id 排序。不使用原生分数、固定 WhisperX 优先级或注册顺序，不凭空造文字。

进入 glossary 的每个有效单元必须已有非空 fallback_text。请求失败、空文、坏 JSON、缺项、超时或无法确定时，对受影响项输出该实际文字，其他有效项不整体回退。疑点只是附加信息，不替代文字。

术语部分失败可复用已有有效 glossary 或既有 fallback 术语骨架，记录降级；不能因未生成新术语而丢失实际原文。修订稿和实际 fallback 都进入 MFA。

### 6.3 缓存与 skip

非空 glossary.md 本身不足以证明本次源文已校对。缓存还须有匹配候选、输入稿、metadata/证据、术语快照、模型/prompt 和 schema 的 source_review manifest。只命中术语文件时可复用术语，但仍校对本次原文。

--skip-knowledge / PIPELINE_SKIP_KNOWLEDGE 同时跳过术语生成和源文纠错，实际输入稿 passthrough 后继续文本断句、翻译及后置 MFA；已有术语用于后续翻译的规则仍遵循显式参数和现有配置。--only-glossary 生成 glossary 和 source_review 后结束，不调用 MFA 或翻译。

### 6.4 预算与完整性

工程初值：每次请求 120 秒、最多重试 1 次、单批最多 8 个源文单元、最多 4 路并行。长输入缩小批次，不截掉真实文稿，不叠加无限重试。

单来源也可进行 glossary 语义校对，候选一致不再是跳过条件；只有双产物缓存命中或显式 skip 才旁路请求。所有有效单元必须输出实际 Text。完全没有实际听录但存在语音属于上游识别失败，不能造占位句。

### 6.5 文本优选与可读性编辑边界

候选一致不证明听录准确，文字异常也不证明 speaker 没有这样说。glossary 在候选和上下文证据下输出本轮采用稿，不先把每处文字分类为“确实有错”或“准确无误”。不设置自动音频复核工具、真实性评分或复核回路；MFA 对齐结果不能反向充当采用稿正确的证明。

字幕允许不逐字复现 speaker，可为读者理解做最小语法编辑，例如介词、单复数和时态，但不改变观点、否定、数量、实体或表达意图。保留有表达作用的玩梗、双关、引用、重复强调及非标准语言风格；不能确定属于无意失误还是刻意表达时优先保留，不把非标准表达一律标准化。

不可变候选与最终采用稿分别保存；修改记录区分候选比较、上下文推断和可读性编辑，保留前后文字及依据。不另立一份声称已还原实际发音的权威稿。语法编辑可能产生与实际发音不同的文字，不能承诺都能精确逐词对齐；无法可靠定位时按词轴不可用规则降级，而非撤销编辑或伪造时间。

## 7. 翻译后的窗口化 MFA 对齐

### 7.1 定稿、来源窗口与合并窗口

把 glossary 优选、编辑或 fallback 的所有单元按音频和文字顺序汇总为完整采用稿。记录工作单元和候选字符位置到完整稿的映射，来源未匹配任何完整候选不影响对齐资格；不要求预先证明全文与实际发音一致。

MFA 执行在文本断句和 translate 之后，直接读取最终有效事件。以 ASR 句段粗时间定位窗口，配合对应完整定稿源文运行锁定 profile；不要求 ASR 词轴，不再把整片音频和全文交给 mfa segment 建立全局文字位置。具体契约见[窗口化对齐设计](2026-11-13-post-translation-windowed-mfa-design.md)。

ASR 段不一定是整句。定稿语句/事件跨越多个相邻 ASR 段，或处理单元共享来源而不能独立提供完整文字/音频时，合并同一项目/媒体 generation 的连续窗口，取所需粗范围包络并带齐中间定稿文字。窗口是声学处理块，不合并或重切语义事件，不直接决定字幕边界。

核心窗口可加有界前后余量，但需同时提供新增语音对应的定稿上下文，明确核心与上下文的字符归属，重叠词不得重复发布。偏移使用准确 sample 坐标且只加回一次；不混用剪辑前后时间原点，不按字数/时长比例分配文字。

合并/扩展需有版本化最大跨度、次数和预算，具体数值待实现及样本校准。不因 padding 重叠无界级联合并，不扩成全片。超长句无法满足窗口限制或来源粗时间缺失时，保留完整双语内容交人工，不强切、不换其他对齐器，不让失败移动后续窗口锚点。

需用真实长音频和跨段语句验证窗口文字完整性、位置合理性与后半段漂移；模型退出成功、词序完整及时间单调均不足以宣布对轴成功。

### 7.2 模型、词典和输出解析

默认采用所锁版本中相互兼容的英语声学模型和英语发音词典；示例 english_mfa / english_us_mfa 需在安装 profile 中验证格式和版本，不能随运行隐式升级。

首版词典外词策略：使用已锁定词典及用户明确提供的补充发音；未覆盖或不可信的词记录为对齐问题并转人工。不在任务中自动生成未经检查的 G2P 词典，不因发音缺失改写源文。后续可独立增加经验证的发音生成能力。

正式输出优先读取 MFA TextGrid 的 word tiers。由 MFA 工具环境中的现成 praatio 解析库导出项目需要的 JSON-safe 区间，禁止用正则猜测 TextGrid。空白、silence 和 phone tiers 不当作实际源文词。

MFA 的规范化词必须通过 LanguagePolicy 映射回完整稿字符范围。允许保留大小写、标点等展示差异；缩写或数字拆分需要明确的一对多/多对一映射，无法映射则该范围词轴不可用。不存在统一可比较的 word score 时不填造 score。

### 7.3 词轴校验与既定事件定时

每个采纳的词区间必须满足：
- 时间有限、非负，start <= end，位于音频有效范围。
- 按文字顺序映射，保留语音间隙，不出现无法解释的逆序。
- 词内容与最终稿在声明的语言策略下对应，不能只检验时间单调。
- 局部时间加回准确的音频 sample 偏移，同一时间不能重复偏移。
- 未覆盖、无法映射、未知词占位或质量异常有明确失败范围，不静默插值。
- 时间必须与来源粗范围和获准窗口扩展相容，检查异常词间空洞、语句跨度及重叠归属，不能把只通过结构检查当作位置正确。

完整稿语句上下文已在翻译前重建，MFA 不再重建或选择语义断点。通过词/字符映射为第 8.3 节已选事件取首尾时间，并恢复全局偏移；已发生翻译整句回退时仅对最终整句事件定时。原始 ASR、MFA 处理块和字幕事件仍是不同对象。

词轴可用性按最终文字范围判定。必要词缺失或事件边界切入不可分离映射时，不插值、不重切译文；受影响语句作完整双语粗时间投影。后置 beautify 只微调通过验收的时间，不清空事件或改变文字、ID。

### 7.4 失败后的实际输出

对齐失败不改动已经确定的文字。保留最终源文及已有整句译文，无可用词轴时对受影响完整语句作单一粗时间事件投影；不改不可变文本规划、不重发翻译，日志说明需人工定时/断句。

粗时间使用覆盖该文字来源的 ASR 输入包络，不能继续使用已发生漂移的 MFA 准备区间。没有可靠来源范围时保留双语内容交人工定位，不把整片范围或零时间冒充可正常导出的字幕时间。

以上都是粗定位，不平均分配文字时间，不伪造词轴，不宣称边界正确。人工报告保留文本、译文、粗时间和来源、音频引用、字符范围及失败原因。

MFA 每个项目执行预算初始为 max(300 秒, 4 * 音频秒数)，超时终止该工具进程树，将尚未完成的文字范围转人工，不丢文本。该工程预算需用 CPU 基准校准。

## 8. 正式 JSON 与工作源文

### 8.1 AlignedTranscript v2

本节是后置 MFA 完成后的定时输出，不是翻译前文本规划结构。尚未对轴的规划使用第 8.3 节的独立产物，不能通过补零时间提前构造可导出的 AlignedTranscript。

新元数据放在 _asr，延续 language、segments、text、words 和 start/end。与旧 batch 的 _batch_artifact 并存，不重用或覆盖其 generation 含义。示例是人工构造结构，不是真实推理：

```json
{
  "language": "en",
  "segments": [
    {
      "id": 1,
      "start": 1.0,
      "end": 1.6,
      "text": "Hello.",
      "words": [{"word": "Hello.", "start": 1.0, "end": 1.6}],
      "text_origin": "glossary_corrected",
      "source_refs": ["A:u0001", "B:u0001"],
      "word_timing": {
        "status": "available",
        "aligner": "mfa",
        "text_sha256": "<reviewed-text-hash>",
        "token_sha256": "<token-sequence-hash>",
        "reason": null
      },
      "segment_timing": {"quality": "aligned", "basis": "mfa_words"},
      "manual_review": {"required": false, "reasons": []}
    },
    {
      "id": 2,
      "start": 10.0,
      "end": 15.0,
      "text": "I have a nice day.",
      "words": [],
      "text_origin": "glossary_fallback",
      "source_refs": ["C:u0002"],
      "word_timing": {
        "status": "unavailable",
        "aligner": "mfa",
        "text_sha256": "<reviewed-text-hash>",
        "token_sha256": "<token-sequence-hash>",
        "reason": "mfa_alignment_failed"
      },
      "segment_timing": {"quality": "coarse", "basis": "source_input_range"},
      "manual_review": {"required": true, "reasons": ["manual_timing_and_split"]}
    }
  ],
  "_asr": {
    "schema_version": 2,
    "language_policy": "en-v1",
    "recognition_manifest": "<source-manifest-ref>",
    "source_review_manifest": "<glossary-source-review-ref>",
    "aligner": "mfa",
    "alignment_manifest": "<alignment-ref>",
    "final_text_sha256": "<complete-text-hash>"
  }
}
```

- text_origin 允许 glossary_selected、glossary_corrected、glossary_fallback、glossary_skipped。ASR 单/多源状态另存候选 manifest。
- word_timing.status 只有 available / unavailable；只在全部必要检查通过时 available。
- reason 使用枚举常量，例如 preparation_failed、oov_unresolved、mfa_alignment_failed、mfa_timeout、word_mapping_failed、invalid_timestamps。
- manual_review 是人工任务状态，不得嵌入字幕正文；不能以 words=[] 唯一推断所有失败原因。
- text、来源和状态必须覆盖全部有效输入单元。没有语音的结果可以 segments=[]，不生成“静音”占位字幕。

### 8.2 修订源文与只读工作副本

唯一自动修订稿存于 glossary 的 source_review.json；文本规划、翻译及后置 MFA words 的 text/token hash 绑定该稿。glossary 定稿后，segment.text 在事件规划、translate、MFA 和 proofreading 中只读。

历史 proofread_text 不能自动继续作为源文覆盖项；载入旧工作缓存时迁移或 miss，防止旧校对指令再次纠错。源文修改记录存于 source_review，不再分散到后置源文校对字段。

segment.translation 保存 translate 单回合输出的整句译文，事件译文保存该整句译文的有序拆分；两者共同构成同一完整句段的翻译产物，校验后原子保存，不能由本地猜测拆分或混用不同响应。导出读取事件译文，缓存完整性同时检查整句与事件身份。发现新源文疑点只记非展示审阅信息，不改源文、不回调 glossary/ASR/MFA。

### 8.3 翻译同回合生成双语事件

独立 split 请求已移除，glossary 后直接 translate：输入完整只读定稿、邻接上下文、完整术语/检索证据，不输入预生成事件、边界 ID、字符上限、词轴或精确时间。所有有效句段均进入翻译，不以 72 字符为跳过条件。详见[联合语义协议](../../superpowers/specs/2026-11-13-semantic-bilingual-translation.md)。

请求/响应仍使用顶层 items；每项使用句段 ID 和实际语言 key，响应给出整句译文与 split_events 中成对的源/目标文本。模型选择自然语义边界，按序复制连续源文片段，不计算事件 ID、字符位置、数量、时长或拼接等式。

本地定位片段在原稿中的顺序位置，容忍空白表示差异，恢复原样源文并生成稳定 ID 和 segment-local source_range；MFA 前时间缺省且 timing_status=pending，不能导出。词轴与范围检查用于对轴保护，不要求模型执行数学。en/zh 为内部历史字段，模型协议使用实际语言 key。

输入工作单元、Provider 分段和 MFA block 均非强制字幕边界，允许事件跨越原识别窗口。恢复分隔空白后，事件源文必须与定稿逐字符一致，不以忽略标点/大小写的 token 相等代替完整性校验。

事件 ID 绑定源文 generation、字符范围和事件规划版本，与请求批次、重试次数及译文无关。相同规划可重试填译文；源文或边界变化时重新生成事件身份，禁止误用旧目标文本。

翻译后才按源文切片与 MFA 词/字符映射确定事件首尾时间。必要词必须完整有效，时间有限、范围与窗口相容且不逆序；不切开不可分离的一对多映射，不平均插值。无法定时的范围保留整句双语内容和来源粗时间，不回退修订前文字，不根据停顿强切或重新请求译文 split。

源文片段无法定位、重复/漏掉文字、片段为空或结构无效但整句译文有效时立即保留完整源句与本次整句译文，不拒收、不重试分段、不本地强切，原因仅日志。完全没有有效整句译文才有界重试/失败。pySBD 仅组织上下文，不负责字幕语义分割；glossary fallback、skip knowledge 或 ASR words 缺失不跳过翻译。

split_status 只描述文字规划：ok 表示边界校验通过，fallback 表示坏响应后的整句规划，unsplit 表示保留整句。不得再因尚无词轴而 skipped/word_timing_unavailable。规划、translation_status 与 timing_status 独立：只有英文事件不表示翻译完成，有完整双语文字也不表示可按精确时间导出。

翻译后本地事件结构示意（人工构造；在 MFA 定时前不能导出）：

```json
{
  "event_plan_manifest": "<source-event-plan-ref>",
  "split_status": "ok",
  "split_events": [
    {
      "id": "evt-<stable-source-range-key>",
      "source_range": [0, 6],
      "en": "Hello.",
      "zh": "你好",
      "translation_status": "complete",
      "timing_status": "pending"
    }
  ]
}
```

事件 source_range 相对所属 segment.text，使用 Unicode code point 左闭右开范围；segment 全稿范围和 source_map 另存。translate 填充整句/事件译文后只满足文字完整性条件；还必须经后置 MFA 定时验收或明确的来源粗时间降级，才能进入字幕导出。

所有文本事件字符范围不得重叠或丢失文字。后置 MFA 失败时，定时/导出层对受影响语句生成完整双语粗时间投影，不修改不可变文本规划，不为消除重叠伪造精确边界。后置纯译文校对不改变源文、event 数量或时间，也不触发重新断句。

## 9. 翻译与可选纯译文 proofreading

新顺序为 glossary/source review -> translate（联合双语语义断句）-> 窗口化 MFA -> beautify -> 可选纯译文 proofread -> ASS。没有独立 split 模型请求；MFA/资源 admission 与只读译文校对边界不变。

translate 和 proofreading 不再原文纠错，均以 glossary 修订稿为只读源文。正常翻译、术语一致性及目标语言表达检查继续；新增源文疑点仅写非展示记录，交最终人工。

### 9.1 单回合整句翻译与译文分配

翻译请求仍为顶层 items；每项包含完整源句、句段 ID、上下文及 glossary，不预生成 split_events。模型同一响应给出整句译文及成对的源/目标片段，不能逐碎片孤立翻译。短句同样由模型判断是否分割，不增加独立 split 请求。

阶段提示词以整句语义、自然从句/转折和可读性为主，避免长段堆积和过碎片段，保护名字、固定表达与玩梗。不要求计算长度、比例、边界 ID、时长、数量或拼接等式；不输出推理过程。代码级协议覆盖旧固定事件/独立 split 说明，外部维护的 prompt example 不改。

响应示意（人工构造，实际语言 key 随源/目标语言变化）：

```json
{
  "items": [
    {
      "id": "s0001",
      "zh": "虽然它很贵，我还是买了。",
      "split_events": [
        {"en": "Although it was expensive,", "zh": "虽然它很贵"},
        {"en": "I bought it.", "zh": "我还是买了"}
      ]
    }
  ]
}
```

该例输入为完整英文句，响应同时提供两条英文/中文片段；不先固定英文事件。中文以整句理解自然表达，不强求逐词语序对应或与整句译文逐字符拼接一致；不能丢失否定、数量、实体等关键含义。

### 9.2 完整性、重试与缓存提交

响应保留合法 JSON、句段 ID 关联、实际整句译文非空与源文只读保护。本地定位源片段后才生成事件身份，不要求模型预先知道 ID/坐标，也不以目标拼接字符串不相等为失败条件。此类结构保护不证明语义翻译正确。

整句与全部双语事件为同一响应的原子产物。源片段不可定位或事件结构无效但整句译文有效时直接保留整句双语内容，原因仅日志，不写原因字段；目标空白/标点/措辞差异不触发回退。没有实际整句译文时重试受影响完整句段，不混用响应，也不额外请求 split。旧的严格拼接回退策略由[联合语义协议](../../superpowers/specs/2026-11-13-semantic-bilingual-translation.md)取代。

仍无完整有效译文则记录翻译失败并沿用任务失败处理，不能把英文、空串或存疑标签当作已完成中文导出。待翻译事件可持久化用于恢复，但不能命中完成缓存。当前 translate_segments 清空 split_events 的逻辑必须迁移，不能仅提前写 en 后沿用旧翻译路径。

### 9.3 译后检查与可选校对

译后先检查文字完整性和行长；MFA/beautify 后再检查真实事件显示时长和阅读速度，不用虚构时间提前验收。阈值由版本化策略定义并校准，不把仅源文满足约束等同于中文可读。过长或跨事件表达不自然交人工，不截断、强切或重新调用 split；未通过质量检查不计作自动质量成功。

proofreading 若启用，仅校对译文。可靠改进可采纳；失败或无法确定则保留当前实际译文。不改源文、event 数量/时间，不回调前序阶段。正文不追加存疑问号、括号注释或待确认标签，正常内容标点保留。

纯译文校对以完整句段提交，整句及事件目标字段同回合保存，保留事件 ID/源文/时间；不再校验严格拼接等式，不混合校对前后的半份响应。

保留 --no-proofread 及既有跳过路径；本次不删除功能或擅自改变默认开关。实施时分别比较旧整句翻译后 split 与新预分段上下文翻译，再对新路径比较增加 proofreading 前后的质量、人工修订量、费用和耗时；不能将两项架构变化混为同一个收益结论。

所有可导出的实际字幕正常输出，疑点进入 review_tasks，不以标签替代译文或阻塞输出。

## 10. Batch、资源和进程协议

保留既有资源规则：
- CPU/IO 容量 max(1, (os.cpu_count() or 1) // 4)。
- prepare/burn 共用最多 4 路 NVENC；ASR 模型波次与 NVENC 互斥。
- GPU ASR worker 串行，一个模型服务同一波次的多个任务，完成后卸载，再运行下一模型。
- 任务 ID 和用户输入顺序不变，不靠解析终端 marker 传递结果。
- 底层 worker 意外退出沿用 fail-fast；它与有结构化错误返回的 Provider 业务失败分开。

Provider 业务失败只减少有效候选集合。剩一份实际文字时 degraded passthrough；仍有多份时按有效来源比较。WhisperX 不拥有特别的失败规则。

MFA 默认使用 CPU，在现有 postprocess CPU/IO admission 内处理项目窗口 corpus，固定 num_jobs=1，不新增并行池。batch 依赖为 recognize -> glossary -> translate（联合双语语义断句）-> windowed MFA -> timed postprocess；远程语义等待不持有已完成 GPU 模型。

glossary、源文断句、translate 和可选校对请求共享受管理的有界网络容量，不保持已完成的 GPU 模型。MFA 可在资源允许时与其他 CPU/IO 工作并行，首次实现不要求流式抢占或极细粒度增量。

第一次 Ctrl+C 停止新 stage admission，允许活动阶段结束；第二次终止活动 Provider/MFA 进程树并返回 130。取消优先于 fallback，不在取消后继续发起请求、启动 MFA 或发布新产物。用户主动取消不伪装成 LLM 失败继续运行。

已有错误铃、聚合报告和失败退出码规则保留。正常单来源及有标记的人工处理不是任务失败；如果实际字幕成功导出，可返回 0 并在报告中明确 degraded/manual 数量，不能把它们算为自动处理成功。

## 11. 缓存、文件与原子发布

建议新增生成物目录，全部属于本地输出，不提交：

```text
<project>/.asr_work/<audio_id>/<run_id>/
  input_manifest.json
  sources/<source_key>.json
  source_review.json
  final_source.txt
  mfa_input/
  mfa_output/
  alignment.json
  source_events.json
  review_tasks.json
  final.candidate.json
```

既有 <base>.asr.json、.asr.lock、media/alignment generation 等继续由原 batch 协调层拥有，不把它们重新解释成某一 Provider 的裸返回。

source_id 是不透明标识，不直接作为文件名；source_key 使用 source_id 的稳定 hash，manifest 保存映射，避免冒号、斜杠或模型名称造成 Windows 路径非法。所有本地配置、runtime manifest 和生成物路径均属于忽略清单。

| 缓存层 | 关键身份 | 可复用条件 |
|---|---|---|
| 原始识别 | 音频、输入区间、Provider/backend、模型 revision、依赖/profile、语言、解码参数 | 上述内容相同 |
| glossary / 源文校对 | 候选、输入稿、metadata/术语/证据快照、fallback policy、glossary LLM/prompt、schema | glossary 与 source_review 双产物均匹配 |
| MFA 原生词轴 | 规范音频/坐标、粗时间来源、合并窗口/余量、对应定稿/上下文、模型/词典、环境及自适应 corpus | 实际音频/文字及窗口和对齐依赖相同 |
| 正式对齐 JSON / 事件定时投影 | glossary/source_review、有效文本事件、MFA 词轴、映射/时间策略、规范化 schema | 当前 generation 完整一致，无旧事件/词轴混算 |
| 联合双语翻译/语义断句 | 定稿/generation、上下文、glossary/检索、语言、模型/prompt、no-split 与协议版本 | 同回合整句/双语片段原子提交，不绑定字符/时长门槛或后置词轴 |
| 纯译文校对 | 当前双语事件、上下文、glossary/检索及模型/prompt | 源文/事件/时间只读，不要求目标严格拼接相等 |

MFA 缓存区分原生词轴与事件时间投影，首版可按完整项目窗口 corpus 复用，不承诺逐窗口增量。说话人自适应的数据依赖计入身份，避免局部和完整运行混算。旧全文 segment/align 产物不因文字相同直接复用为窗口化结果；新执行/窗口/坐标策略必须失效旧路径身份。

glossary 模型、prompt、术语及证据变化可重跑 source_review，但复用原始 ASR。术语变化而实际文字、窗口及对齐依赖不变时可复用 MFA，同时更新语义阶段来源身份。

源文边界变化时失效受影响句段译文，并按最终事件重做时间投影；仅在窗口音频、定稿和 corpus 均不变时复用原生 MFA 词轴。若跨段关系改变了合并窗口，则重算相关对齐依赖，不能承诺所有边界变化都免重跑 MFA。只改 MFA/窗口/beautify 参数不重发有效翻译。文本规划和翻译不绑定后置时间，避免循环依赖。source_events 保存独立未定时规划及来源，不能冒充可导出的双语定时 JSON。

正式发布沿用锁、快照核对、唯一 candidate 和同目录原子替换：发布前确认媒体 generation、候选清单、glossary/source_review 快照、最终源文和 MFA 身份没有变化；先使不匹配的 beautified 缓存失效，再在提交锁内原子替换正式 JSON。进程失败不得留下半份正式 JSON，也不能沿用不同词轴的旧 split_events。

Manifest 中记录请求、来源、工具版本和失败原因；不写 API key、cookies 或完整认证头。只有当前项目及 generation 的资料可进入相应上下文。

## 12. 失败处理表

| 情况 | 结果 |
|---|---|
| 未注册 Provider、依赖或语言不支持 | preflight 错误，不替换为 WhisperX |
| 单一有效 ASR 来源 | 实际文字及候选透传给 glossary，不调用 ASR LLM |
| 某可选来源失败但其他有文字 | 记录降级，继续 glossary |
| 有语音而全部来源无实际文字 | ASR 失败，不造占位稿 |
| glossary 超时、坏响应、缺项或无法确定 | 源文项输出实际 fallback，术语复用或现有 fallback，继续文本断句、翻译及后置 MFA |
| MFA 分块、词典、运行或校验失败 | 保留修订稿和粗时间，词轴不可用，最终人工 |
| 源文断句请求失败、边界缺失/越界或切开词 | 有界重试仍失败则保留整段及有效时间，不改文、不本地强切，记录人工任务 |
| 事件边界无法映射有效 MFA 词轴 | 不造时间，保留已有完整双语内容，投影为来源粗时间整句事件交人工 |
| 翻译整句/事件目标字符串拼接不一致 | 不作为失败/回退条件，保留自然目标片段与整句译文 |
| 片段源文无法定位或结构无效，但整句译文有效 | 保留完整源句和整句译文，单一事件后续对轴，原因仅日志 |
| 翻译返回未知/重复 ID、缺项或空译文 | 按完整句段校验及有界重试，同一响应内完成翻译/分配；无有效完整产物则翻译失败 |
| 译文过长或阅读速度不合格 | 保留完整实际译文，记录人工任务，不截断、强切或重排事件 |
| translate/proofread 试图改源文 | 不采纳源文变动；译文按校验及 fallback 输出 |
| proofreading 失败或禁用 | 保留实际译文并正常导出 |
| worker 意外退出 | fail-fast、回收和恢复记录 |
| 用户取消 | 停止新阶段，不伪装成请求失败继续 fallback |

基础资源缺失在 preflight 报告；已有实际文稿后的 MFA 失败保留文本，不回退原候选。

## 13. CLI、兼容和迁移

拟新增 asr 正式子命令。参数示意尚未实现：

```text
subtitle-translation asr VIDEO
subtitle-translation asr VIDEO --providers parakeet
subtitle-translation asr VIDEO --providers whisperx,parakeet
subtitle-translation pipeline URL --asr-providers qwen_asr
subtitle-translation batch URL1 URL2 --asr-providers whisperx,qwen_asr
```

CLI 覆盖启用集合只影响本次运行，不自动下载整个工具链、不覆盖用户安装选择。多引擎模型参数来自各自配置；一个通用 --model 仅在单引擎模式允许，不能把同一模型名传给所有引擎。

asr 命令只生成 CandidateBundle 和实际输入稿，不执行 glossary 或 MFA；新 translate 接受 bundle 并继续 glossary/source review、MFA 及输出。whisper 兼容入口显式选择 WhisperX，其候选/旧 JSON 迁移需明确，不能把无词轴候选冒充正式词轴 JSON。旧 --skip-whisper / PIPELINE_SKIP_WHISPER 表示跳过识别并复用指定输入，不按模型名只跳过一个来源，也不自动跳过 glossary 和 MFA；URL batch 的 skip 输入限制继续遵循既有规则。

新主路径通过 SDK adapter 和受管理 worker 识别，不执行自定义 WHISPERX_PATH_* 的旧完整识别/对齐命令。旧 WhisperX alignment 参数不能暗中控制 MFA；升级时给出迁移说明，MFA 改用独立配置。

旧 JSON 仍可直接 translate。无新 _asr 元数据的词轴按既有读入逻辑检查，可标记 legacy；缺少新 fingerprint 的恢复缓存视为 miss。新增 asr_providers.json、asr_runtime.json、.asr_work 和审阅记录加入生成物忽略清单，example 仍只有包内一份权威。

独立 split 和其请求/缓存迁移为联合 semantic-bilingual-events-v1；新响应为整句译文和成对源/目标片段，不让模型生成坐标/时间，不要求目标拼接相等。--split-max-chars/duration 弃用并忽略，--no-split 与邻接上下文选项保留。旧固定事件翻译不能冒充新协议完成，首次重译；MFA 原生身份不直接因语义协议变更失效。prompt example 不修改，现有 split SRT 文件名保留，只在定时就绪或有效粗时间降级后导出。

回滚时停止活动进程，恢复原代码和对应环境；原始媒体和听录候选保留。新 schema 的工作恢复缓存不能被旧版本强制复用；旧工具对无 words 的新句段不具备状态语义保证，应使用原始兼容输入或重新生成。

## 14. 验证与交付门槛

### 14.1 必须通过的确定性检查

- 任意单源/多源 ASR 都不初始化语义 LLM，候选和实际兜底稿完整进入 glossary。
- 来源缺失不因 WhisperX 身份单独失败；glossary 实际文本 fallback 在相同输入下确定。
- 有效文本工作单元有 100% 实际文字输出和来源记录，不丢项、不以疑点标签代替文本。
- ASR 不读 glossary；术语变化不重跑识别，修订文字不变时复用 MFA。
- 不匹配任何完整候选的勘误稿同样进入 MFA。
- translate 单次请求同时返回源/目标语义片段，无独立 split；短句和无 ASR words 的句段均参与。
- 边界绑定采用稿并逐字符完整还原；坏边界有界重试后保留单一待定时整句，不造时间。
- 翻译前已有 ID、范围和源文，没有精确时间；文字完整性、翻译完整性和定时就绪分别验证。
- 模型按语义选择分段并复制只读源文；ID/位置由本地计算，目标字符串拼接差异不取消分段。
- 翻译请求含完整句段及其全部英文事件；同一次响应返回整句译文和全部事件译文，不另发译文 split 请求。
- 句段响应按 ID 归位；无实际整句译文有界重试，源片段不可定位时整句回退且仅日志提示。空白/标点/自然措辞差异不触发目标拼接回退，未完成项不命中缓存。
- 跨 ASR 段语句触发有限窗口合并，携带完整对应定稿和上下文；上限、防级联、重叠归属、sample 偏移及异常长空洞均有回归测试。
- 边界变化使受影响译文/时间投影失效；窗口依赖不变时复用原生词轴，变化时不能误复用。只改对齐配置不重发有效翻译。
- 无轴范围保留已有完整双语内容和来源粗时间，不使用漂移 MFA 范围；后置 beautify 不清空或重建事件。
- MFA 失败不撤销 glossary 修订；translate/proofread 不改源文，不返工上游。
- 字幕正文没有程序生成的存疑标签；正常问号和括号保留。
- 缓存、并发、取消、candidate 发布及恢复测试保持通过。

### 14.2 模型和平台实测

首批考据只有官方源码和示例，Parakeet/NeMo、Qwen 官方 wrapper 尚未在本项目运行。接入时分别锁版本，采集真实输出 fixtures，验证输入对应、时间单位、空结果、长音频和取消，不把人工构造 JSON 标成真实推理。

Windows/PowerShell 7 与 Linux/bash 分别验证默认 setup、单引擎替换、可选依赖不被导入、工具路径含空格、配置升级幂等性和模型缓存保留。未通过的平台/profile 不标为已支持。

单元测试 mock 网络、ASR、LLM、MFA、ffmpeg 等外部依赖；修改核心翻译、setup、入口、缓存或并发时运行全量 unittest。真实模型和人工基准单独执行。

### 14.3 英文质量基准

建议初始集不少于 30 段、总计不少于 60 分钟，覆盖专名、数字、口音、重复/填充词、长停顿、背景声、窗口边界和真实 ASR 分歧；人工标注听录及抽样词边界。调参集和验收集分开。

分别报告原始 ASR、glossary 源文校对、MFA 和译文质量，另做 proofreading 纯译文增益对照。指标包括 WER/CER、漏听/语素错误、实体错误、修正与新增错误、fallback/degraded 比例、可用词轴覆盖、边界误差、事件规划自动完成率、最终人工数量/时长、耗时和峰值内存。

固定同一采用稿和术语，对照已实现词轴驱动前置 split 与文本规划/翻译后窗口化 MFA，覆盖长句、否定、跨事件语序、专名、数字、换气、窗口合并及目标语言扩张。报告前后半段漂移、边界误差、断句自然度、译文完整性、超长/过快字幕、人工重切量、请求/token 和总耗时。正常路径每批 translate 只一次请求，不追加译文 split，失败重试单独计数。另评纯译文校对收益，不预设流程变更必然改善质量。

听录保真与可读性编辑分开评估：保留采用前稿和编辑记录，WER/CER 不将有意语法编辑直接等同听录错误；最终展示稿另评意思保留、语法可读性和玩梗保留情况。人工标注仅用于离线验收，不引入运行时自动音频复核。

首版质量门槛建议：在预先固定、禁用可读性编辑的听录保真对照中，glossary 多源优选后的整体 WER 不劣于同集最佳单引擎；正常编辑路径另要求新增意思/表达意图错误数低于修正错误数，不以逐字 WER 否定允许的语法编辑。常规英文验证集可用词轴覆盖目标至少 90%，抽样词边界绝对误差中位数目标不超过 100 ms、P95 不超过 300 ms。数值属于待实测校准的候选发布标准，不是能力承诺；未达标只能记录实验状态或继续修正，不能填为已通过。

不得用排除人工处理句段后的事件规划成功率掩盖总自动完成率，也不能用后置术语修订掩盖前置 ASR 评估错误。

## 15. 实施顺序与设计对齐

1. 保留现有 WhisperX 识别/对齐，先落地独立源文断句前移、单回合整句/事件翻译及纯译文校对，拒绝旧源文覆盖和不匹配工作缓存。当前第一阶段已完成此代码路径和 mock 回归。
2. 实现单一 WhisperX 下的实际候选/输入稿、glossary/source_review 双输出和文本优选/编辑；与 MFA 链路一起启用，避免将新文字套入旧词轴。
3. 已接通独立文本检查点、来源窗口合并及 sample 偏移；当前零额外 padding、全部有效窗口共享一次 corpus align/批量导出，固定一路 CPU。继续完善上下文余量和跨语料性能/质量基准，不把结构检查或短样本接通当完整质量验收。
4. 验证默认单源完整流程、失败降级、缓存、取消和原子发布，运行真实英文质量对照，再独立评估是否移除纯译文校对。
5. 多引擎接入暂缓；后续再固定 Parakeet/Qwen profile 和真实 fixture，完善 setup 选择及多源证据输入，不在当前 WhisperX 实施中强制安装其他引擎。

本轮正常迭代涉及 D1/D2/D3/D4/D6/D7/D10 的数据源、阶段顺序、缓存、协议、职责和安装边界，需在实现 PR 中说明并同步 AGENTS、README、MIGRATION 和 setup；prompt 由既定仓库维护。

已确定 ASR 无语义 LLM、文本编辑集中 glossary、translate 同回合生成双语语义事件、MFA 后置窗口对齐、beautify 不改事件文字。独立 split 及 LLM 数学边界/严格拼接要求移除，时间有效性检查保留。真实语义质量仍需验收，不设置自动音频复核，proofreading 删除为独立评估项。

## 16. 参考与证据状态

- [Provider 原生输出考据及固定源码](../../superpowers/specs/2026-11-07-asr-provider-output-contracts.md)
- [研发路线图](../../superpowers/specs/2026-08-25-v2.1.0-asr-provider-design.md)
- [MFA 语料结构](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/corpus_structure.html)
- [MFA 有文本长音频准备](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/corpus_creation/create_segments.html)
- [MFA 对齐与输出](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/workflows/alignment.html)
- [MFA 说话人自适应](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/concepts/speaker_adaptation.html)
- [praatio](https://github.com/timmahrt/praatIO)
- [pySBD](https://github.com/nipunsadvilkar/pySBD)

文档中的 schema 示例、工程默认和质量目标均为设计数据。它们不是已运行程序的输出，不代表已完成 setup 安装、真实模型转录或 MFA 基准。
