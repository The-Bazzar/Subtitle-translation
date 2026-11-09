# ASR 兼容层、glossary 原文校对与 MFA 对齐完整设计

状态：完整设计草案，供本轮开发实施和评审使用；相关功能尚未实现或验收。
设计基线：2026-11-09。首版仅验证 en -> zh，保留多语言接口。

本文是目标行为的主设计文档。[研发路线图](2026-08-25-v2.1.0-asr-provider-design.md)只维护交付顺序，[Provider 输出考据](2026-11-07-asr-provider-output-contracts.md)维护官方源码依据和原生格式。当前实际行为仍以 AGENTS.md 和代码为准。本文新增的配置、字段、参数和命令均是待实现契约，不能当作当前版本已经可用的功能。

## 1. 已确定的产品原则

1. setup 默认预选 WhisperX，可替换为任意已支持单引擎或增设多引擎；各 Provider 平等。
2. ASR 只识别并保存候选，单源直接透传，多源保留实际结果和确定性兜底稿；不调用语义 LLM，不涉及 glossary。
3. 文本优选与字幕编辑统一放在 glossary 阶段，与术语整理结合候选和语境完成；单来源同样可处理，不声称已验证实际发音。
4. glossary 必须输出完整实际源文及术语资料，失败或无法确定时输出实际文本 fallback，不只留下疑点。
5. MFA 是唯一正式对齐器，位于 glossary 之后，对完整修订稿一次性对齐，包括不等于任何 ASR 候选的文字。
6. 保留独立 LLM 源文断句，移到 MFA/beautify 之后、翻译之前；结合定稿语义和词轴选择边界，本地校验并预填英文事件。MFA 只定位时间。
7. translate 和 proofreading 不再纠错源文，不回调 glossary、ASR 或 MFA。
8. proofreading 若保留，只校对译文；失败保留实际译文，正文不插存疑问号、括号说明或待确认标签。
9. 正常问句和原内容括号保留，未解决疑点进入非展示记录，最终人工处理。
10. 移除 proofreading 仍为收益评估项，保留既有显式跳过能力，本次不直接删除功能。
11. translate 在同一次请求/响应内先完成整句翻译，再依据 split_events.en 将整句译文拆分填入 split_events.zh；不另发译文断句请求，不增删事件或修改源文和时间。
12. MFA 失败不撤销修订，保留文字和粗时间；词轴不可用部分不自动切分，整段翻译后交最终人工定时和断句。

首版验证 en -> zh，保留多语言入口。混说、流式、外部听录导入、自动模型路由不在验收范围。MFA 发音词典是对齐资源，不等于语义 glossary；对齐成功不是文字正确的证据。

## 2. 完整流程与职责边界

```text
download -> prepare-video -> canonical audio
  -> ASR：所选 Provider 识别，保存不可变候选和实际兜底原文，无 LLM
  -> glossary：术语整理 + 文本优选与字幕编辑，输出实际采用稿
       -> 失败时输出实际文本 fallback 和可用术语资料
  -> MFA：完整修订稿的长音频准备、批量对齐和词轴校验
       -> 失败部分保留文本和粗时间，最终人工
  -> beautify -> 独立 LLM 源文断句 -> 本地校验/对轴，预填 split_events.en
  -> translate：单回合整句翻译 + 译文分配，按事件 ID 填充 split_events.zh
  -> 可选纯译文 proofread -> ASS -> 可选 burn
```

正式逐词 beautify 移到 MFA 之后；场景检测可独立缓存，不能提前修改尚未形成的正式词轴。glossary -> MFA 是单向流程，不是后置校对返回上游。

| 阶段 | 职责 |
|---|---|
| ASR | 转录、原生格式转换、来源/粗范围、实际兜底文稿，不读 glossary 或做 LLM 裁决 |
| glossary | 使用候选、metadata、术语及上下文，统一整理术语、优选文本和编辑字幕，不承担最终字幕断句 |
| MFA | 为实际采用稿定位时间，不改文、不裁决术语，不验证听录真实性 |
| LLM 源文断句 + 本地事件规划 | LLM 根据语义、词轴和约束选边界；本地校验并生成事件 ID、源文和时间 |
| translate | 单回合完成整句翻译及译文分配，按固定事件 ID 填入译文，不修改源文、事件数量或时间 |
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

详细字段与已核对的固定官方提交见 [输出考据](2026-11-07-asr-provider-output-contracts.md)。源码核对与真实推理验收分开记录。原生 Transformers、官方 wrapper、vLLM 和云 API 不是同一 parser，本轮不自动兼容未验证接口。

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

glossary.md 仅保存术语及表达规则，不兼任整篇原文。另输出 source_review.json，保存每个单元的实际采用文字、来源、修改依据、fallback 原因及全文 hash。两者通过不可变 glossary_generation manifest 关联，不允许半份缓存被认作完成。最终字幕断句由 MFA/beautify 后的独立 LLM 阶段承担，不合并进 glossary。

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

--skip-knowledge / PIPELINE_SKIP_KNOWLEDGE 同时跳过术语生成和源文纠错，实际输入稿 passthrough 后继续 MFA；已有术语用于后续翻译的规则仍遵循显式参数和现有配置。--only-glossary 生成 glossary 和 source_review 后结束，不调用 MFA 或翻译。

### 6.4 预算与完整性

工程初值：每次请求 120 秒、最多重试 1 次、单批最多 8 个源文单元、最多 4 路并行。长输入缩小批次，不截掉真实文稿，不叠加无限重试。

单来源也可进行 glossary 语义校对，候选一致不再是跳过条件；只有双产物缓存命中或显式 skip 才旁路请求。所有有效单元必须输出实际 Text。完全没有实际听录但存在语音属于上游识别失败，不能造占位句。

### 6.5 文本优选与可读性编辑边界

候选一致不证明听录准确，文字异常也不证明 speaker 没有这样说。glossary 在候选和上下文证据下输出本轮采用稿，不先把每处文字分类为“确实有错”或“准确无误”。不设置自动音频复核工具、真实性评分或复核回路；MFA 对齐结果不能反向充当采用稿正确的证明。

字幕允许不逐字复现 speaker，可为读者理解做最小语法编辑，例如介词、单复数和时态，但不改变观点、否定、数量、实体或表达意图。保留有表达作用的玩梗、双关、引用、重复强调及非标准语言风格；不能确定属于无意失误还是刻意表达时优先保留，不把非标准表达一律标准化。

不可变候选与最终采用稿分别保存；修改记录区分候选比较、上下文推断和可读性编辑，保留前后文字及依据。不另立一份声称已还原实际发音的权威稿。语法编辑可能产生与实际发音不同的文字，不能承诺都能精确逐词对齐；无法可靠定位时按词轴不可用规则降级，而非撤销编辑或伪造时间。

## 7. Glossary 后的 MFA 全文对齐

### 7.1 完整稿与实际执行粒度

把 glossary 优选、编辑或 fallback 的所有单元按音频和文字顺序汇总为完整采用稿。记录工作单元和候选字符位置到完整稿的映射，来源未匹配任何完整候选不影响对齐资格；不要求预先证明全文与实际发音一致。

逻辑输入为完整音频和完整稿，实际执行不把任意长度视频当成一个超长 utterance。首版采用 MFA 的有文本长音频准备流程 mfa segment 形成 TextGrid/utterance 输入，再批量运行 mfa align；具体命令签名由已锁定的 MFA profile 提供。

mfa segment 使用 VAD/能量等声音边界辅助长音频准备，不是字幕语义断句器。MFA 的 utterance、word tiers 和停顿可以辅助定位与检查，但不直接决定最终字幕事件；项目尚无其字幕断句质量或吞吐实测，不作性能承诺。

准备结果必须检查全文词序、覆盖、重复及音频范围。公共识别窗口和成功锚点可用于诊断与合并，不直接作为不可跨越的 MFA 硬边界。仅有 VAD 区间不能按字数或时长比例把全文分配进去。

初始准备策略以约 30 秒处理块为目标，60 秒为一般上限；更长块需正确的文字对应再拆分。语义句可以跨块，块也可以含多句。无法可靠准备的范围保留文字及粗时间，转人工；不强行在错误边界对齐，不换其他对齐器。

上述流程必须通过目标 MFA 版本的真实长音频试验。若 mfa segment 无法满足完整性，发布该 profile 前需修正 MFA 输入准备实现，而不是把未知结果标成自动成功。

### 7.2 模型、词典和输出解析

默认采用所锁版本中相互兼容的英语声学模型和英语发音词典；示例 english_mfa / english_us_mfa 需在安装 profile 中验证格式和版本，不能随运行隐式升级。

首版词典外词策略：使用已锁定词典及用户明确提供的补充发音；未覆盖或不可信的词记录为对齐问题并转人工。不在任务中自动生成未经检查的 G2P 词典，不因发音缺失改写源文。后续可独立增加经验证的发音生成能力。

正式输出优先读取 MFA TextGrid 的 word tiers。由 MFA 工具环境中的现成 praatio 解析库导出项目需要的 JSON-safe 区间，禁止用正则猜测 TextGrid。空白、silence 和 phone tiers 不当作实际源文词。

MFA 的规范化词必须通过 LanguagePolicy 映射回完整稿字符范围。允许保留大小写、标点等展示差异；缩写或数字拆分需要明确的一对多/多对一映射，无法映射则该范围词轴不可用。不存在统一可比较的 word score 时不填造 score。

### 7.3 词轴校验与最终句段

每个采纳的词区间必须满足：
- 时间有限、非负，start <= end，位于音频有效范围。
- 按文字顺序映射，保留语音间隙，不出现无法解释的逆序。
- 词内容与最终稿在声明的语言策略下对应，不能只检验时间单调。
- 局部时间加回准确的音频 sample 偏移，同一时间不能重复偏移。
- 未覆盖、无法映射、未知词占位或质量异常有明确失败范围，不静默插值。

完成后合并全局词轴。正式翻译上下文从完整稿的语句和段落重建，不复刻 Provider segments 或 MFA blocks；可使用保留字符位置的 pySBD 和明确段落边界组织完整句段。字幕事件另按第 8.3 节由独立 LLM 源文断句生成，不能把上下文批次或对齐处理块当作最终字幕。

词轴可用性按最终文字范围判定。一个最终句段包含必要词缺失时，首版保守地将该句段标为词轴不可用；不依赖部分词轴自动切分未知范围。

### 7.4 失败后的实际输出

对齐失败不改动已经确定的文字。保留最终源文及后续译文，words 为空，记录原因和待人工定时/断句。

粗时间选择顺序：
1. 仍可信的 MFA 输入准备区间。
2. 两侧已验证词区间夹出的范围，且顺序合法。
3. 能覆盖该文字来源的已知输入范围。
4. 只能确认整片音频范围时，使用整片范围并明确标记定位不确定。

以上都是粗定位，不平均分配文字时间，不伪造词轴，不宣称边界正确。人工报告保留文本、译文、粗时间和来源、音频引用、字符范围及失败原因。

MFA 每个项目执行预算初始为 max(300 秒, 4 * 音频秒数)，超时终止该工具进程树，将尚未完成的文字范围转人工，不丢文本。该工程预算需用 CPU 基准校准。

## 8. 正式 JSON 与工作源文

### 8.1 AlignedTranscript v2

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
      "segment_timing": {"quality": "coarse", "basis": "prepared_interval"},
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

唯一自动修订稿存于 glossary 的 source_review.json，MFA words 的 text/token hash 绑定该稿。规范化发布后，segment.text 在事件规划、translate 和 proofreading 中只读。

历史 proofread_text 不能自动继续作为源文覆盖项；载入旧工作缓存时迁移或 miss，防止旧校对指令再次纠错。源文修改记录存于 source_review，不再分散到后置源文校对字段。

segment.translation 保存 translate 单回合输出的整句译文，事件译文保存该整句译文的有序拆分；两者共同构成同一完整句段的翻译产物，校验后原子保存，不能由本地猜测拆分或混用不同响应。导出读取事件译文，缓存完整性同时检查整句与事件身份。发现新源文疑点只记非展示审阅信息，不改源文、不回调 glossary/ASR/MFA。

### 8.3 翻译前独立 LLM 源文断句与事件规划

保留独立 LLM split 阶段，但移到 MFA/beautify 之后、translate 之前，只处理源文。输入包括完整定稿句段及相邻上下文、带稳定 ID 的词序列/字符映射、实际词时间/停顿、字幕长度与显示时长约束，以及有依据的不可拆表达范围。LLM 结合语义与时间信息选择边界，不修改定稿，不生成译文或时间戳。

请求/响应使用顶层 items 的 JSON object；每项带句段 id 和实际源语言 key，响应在该语言 key 下返回有序边界 ID。边界 ID 由本地预先生成并绑定文字 hash、词映射和语言策略，表示词间或句段首尾的合法字符位置，不允许切开词或无法分离的映射。模型只引用允许的 ID，不重新抄写分段原文。

本地验证边界覆盖、唯一性、顺序、不可拆范围及时间约束，从定稿切片并映射词轴，生成 start、end、稳定事件 id、source_range 和源文，提前填充 split_events.en；随后 translate 一次请求完成整句翻译及 split_events.zh 分配。本地仅负责执行和校验语义边界，不用规则程序替代 LLM 断句。en/zh 为历史字段名，序列化通过语言策略映射实际源/目标 ISO 代码，保留多语言入口。

输入工作单元、Provider 分段和 MFA block 均非强制字幕边界，允许事件跨越原识别窗口。恢复分隔空白后，事件源文必须与定稿逐字符一致，不以忽略标点/大小写的 token 相等代替完整性校验。

事件 ID 绑定源文 generation、字符范围和事件规划版本，与请求批次、重试次数及译文无关。相同规划可重试填译文；源文或边界变化时重新生成事件身份，禁止误用旧目标文本。

源文切片与 word 的字符映射决定事件首尾时间，不再依赖翻译后的首尾 token 字符串匹配。所有必要词均须有效，时间有限、范围合法、不逆序；不切开无法分离的一对多映射，不平均插值。停顿可辅助检查，不自动触发按时长硬切。无法映射的范围保留整段并记录人工任务，不回退修订前文字。

缺项、未知/重复 ID、越界或切开不可拆范围时，对受影响完整句段最多重试 1 次；仍失败则保留完整句段及已有有效时间，不用本地句界库继续强行字幕分割，记录人工任务。不改源文，不回调 glossary/MFA。pySBD 仅组织完整上下文，不作为最终字幕语义断句替代器。glossary fallback 或 skip knowledge 不自动跳过本阶段，词轴可用时仍正常断句。

split_status 保留 ok（LLM 边界通过并完成定位）、fallback（请求/边界校验失败后整段降级）、unsplit（合法决定保留整句）语义，新增 skipped 表示词轴不可用、不发断句请求，reason 为 word_timing_unavailable。规划状态与 translation_status 分开：只有英文事件不表示翻译完成。

翻译前工作结构示意（人工构造；不能作为已完成双语字幕导出）：

```json
{
  "event_plan_manifest": "<source-event-plan-ref>",
  "split_status": "ok",
  "split_events": [
    {
      "id": "evt-<stable-source-range-key>",
      "source_range": [0, 6],
      "start": 1.0,
      "end": 1.6,
      "en": "Hello.",
      "zh": "",
      "translation_status": "pending"
    }
  ]
}
```

source_range 是最终完整稿上的左闭右开字符范围。translate 返回整句译文，并填充对应 zh 和翻译状态；完整句段的整句/事件译文均通过第 9 节校验后，才满足双语导出完整性条件。

无轴范围忽略细分建议，保留完整源文、粗时间和一个待翻译事件，之后输出整段译文并交人工；不借边界建议伪造时间。所有事件字符范围不重叠、不丢失有效文字；粗时间可能重叠，必须标记定位不确定，不能为了消除重叠伪造精确边界。后置纯译文校对不改变源文、event 数量或时间，也不触发重新断句。

## 9. 翻译与可选纯译文 proofreading

新顺序为 glossary/source review -> MFA -> beautify -> 独立 LLM 源文断句及本地事件规划 -> translate（单回合整句翻译 + 译文分配）-> 可选 proofread -> ASS。MFA 使用统一资源管理的本地服务，不由模块自行创建无限进程池。

translate 和 proofreading 不再原文纠错，均以 glossary 修订稿为只读源文。正常翻译、术语一致性及目标语言表达检查继续；新增源文疑点仅写非展示记录，交最终人工。

### 9.1 单回合整句翻译与译文分配

翻译请求仍为顶层 items 的 JSON object；每项是完整句段，使用句段 id、实际源语言 key 和其全部预生成 split_events（事件 ID、源文、范围/时间），携带邻接上下文及常驻 glossary。同一句段的兄弟事件不能拆到不同请求中孤立翻译，可在一次请求中批量处理多个完整句段。

提示词要求在同一请求/响应内完成两步：先按完整句意生成自然的整句译文，再依据 split_events.en 的语义和顺序，将这份整句译文拆分填入对应 split_events.zh。不是逐事件独立翻译后拼接，也不是先请求整句译文、再请求一次译文断句；正常路径只有一次模型响应，不要求输出推理过程。

响应示意（人工构造，实际语言 key 随源/目标语言变化）：

```json
{
  "items": [
    {
      "id": "s0001",
      "zh": "虽然它很贵，我还是买了。",
      "split_events": [
        {"id": "evt-0001", "zh": "虽然它很贵，"},
        {"id": "evt-0002", "zh": "我还是买了。"}
      ]
    }
  ]
}
```

该例输入整句为 Although it was expensive, I bought it.，两个英文事件依次是 Although it was expensive, 和 I bought it.。源文事件固定，中文可在保持整句含义、事件顺序及整体对应关系的前提下做自然的信息分配，不强求逐词直译；不能把否定、数字、实体等关键内容移到错误语义范围或造成重复/遗漏。

### 9.2 完整性、重试与缓存提交

响应必须逐句段及逐事件 ID 校验覆盖、唯一性和非空目标文本；按输入事件顺序归位，不能新增、合并或删去事件，也不采纳源文或时间变动。按版本化目标语言拼接规则恢复分隔符后，事件译文必须完整还原本次整句译文，不允许忽略标点、删词或本地二次改写来强行通过。该检查保证响应内部一致性，不等于证明语义翻译无误。

整句译文和该句段全部事件译文为同一原子产物。缺项、重复/未知 ID、空译文或拼接不一致时，有界重试整个受影响句段，并再次提供完整源文与全部事件；不能只补一个事件后混合不同响应的整句译文，也不增加独立译文 split 请求。单次响应中其他完整且有效的句段可保留。只有源文、上下文、事件规划、术语及配置身份均匹配的完整既有产物可作为显式 fallback。

仍无完整有效译文则记录翻译失败并沿用任务失败处理，不能把英文、空串或存疑标签当作已完成中文导出。待翻译事件可持久化用于恢复，但不能命中完成缓存。当前 translate_segments 清空 split_events 的逻辑必须迁移，不能仅提前写 en 后沿用旧翻译路径。

### 9.3 译后检查与可选校对

译后检查源/目标行长、事件显示时长、阅读速度和文字完整性；阈值由版本化语言/字幕策略定义并在实测中校准，不把仅源文满足约束等同于中文可读。过长或跨事件表达不自然时写人工任务，不自动截断、不强切、不改时间或重新调用 split；未通过质量检查的字幕不得计作自动质量成功。

proofreading 若启用，仅校对译文。可靠改进可采纳；失败或无法确定则保留当前实际译文。不改源文、event 数量/时间，不回调前序阶段。正文不追加存疑问号、括号注释或待确认标签，正常内容标点保留。

纯译文校对的采纳也以完整句段为提交单元；更新事件译文时按相同拼接规则同步整句译文和校对身份，不保留已过期的整句字段，不混合校对前后的不完整事件集合。

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

MFA 默认使用 CPU，按项目 corpus 批量处理。调度器原子申请 r 个 CPU/IO 资源份额，MFA num_jobs=r，r 不超过 4 和当前预算。不能每个外层任务各自再起一个完整进程池，也不能持有一个份额后无限等待额外份额造成死锁。

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
| MFA | source_review 实际全文、corpus、分块、MFA 发音词典、声学模型、工具和自适应配置 | 实际文字及对齐依赖相同 |
| 正式对齐 JSON | glossary/source_review 与 MFA 身份、规范化 schema | 当前 generation 完整一致 |
| LLM 源文断句 / 事件规划 | 实际源文/完整上下文、字符词映射、beautify 后词轴、语言策略/约束、split 模型/prompt、规划版本 | 边界及事件 ID、文字、时间和覆盖检查匹配 |
| 整句翻译 + 事件译文 / 纯译文校对 | 事件规划身份、完整上下文、glossary、翻译/纯译文校对参数与 prompt、目标语言拼接策略 | 完整句段的整句/全部事件译文原子匹配，不以 en 已填判为完成 |

首版 MFA 使用项目级缓存：完全相同输入直接复用，相关文字或 corpus 改变后重跑当前项目；不承诺单段增量。说话人自适应的数据依赖已计入身份，避免局部重跑与完整运行混算。

glossary 模型、prompt、术语及证据变化可重跑 source_review，允许重试先前 fallback，但复用原始 ASR。只有实际修订文字或对齐依赖变化才重算 MFA；术语变化但文字不变时复用相同 MFA，同时更新正式产物的语义阶段来源身份。

源文 split 模型、prompt、约束或边界变化但实际文字不变时同样复用 MFA，重新校验事件规划，并使身份不匹配的完整句段翻译/校对失效。事件规划不依赖目标译文，不能形成翻译与断句的循环缓存身份。source_events 保存 LLM 源文断句及本地规划状态和来源 manifest，正式对齐 JSON 不因尚未翻译就伪装成完整双语工作缓存。

正式发布沿用锁、快照核对、唯一 candidate 和同目录原子替换：发布前确认媒体 generation、候选清单、glossary/source_review 快照、最终源文和 MFA 身份没有变化；先使不匹配的 beautified 缓存失效，再在提交锁内原子替换正式 JSON。进程失败不得留下半份正式 JSON，也不能沿用不同词轴的旧 split_events。

Manifest 中记录请求、来源、工具版本和失败原因；不写 API key、cookies 或完整认证头。只有当前项目及 generation 的资料可进入相应上下文。

## 12. 失败处理表

| 情况 | 结果 |
|---|---|
| 未注册 Provider、依赖或语言不支持 | preflight 错误，不替换为 WhisperX |
| 单一有效 ASR 来源 | 实际文字及候选透传给 glossary，不调用 ASR LLM |
| 某可选来源失败但其他有文字 | 记录降级，继续 glossary |
| 有语音而全部来源无实际文字 | ASR 失败，不造占位稿 |
| glossary 超时、坏响应、缺项或无法确定 | 源文项输出实际 fallback，术语复用或现有 fallback，继续 MFA |
| MFA 分块、词典、运行或校验失败 | 保留修订稿和粗时间，词轴不可用，最终人工 |
| 源文断句请求失败、边界缺失/越界或切开词 | 有界重试仍失败则保留整段及有效时间，不改文、不本地强切，记录人工任务 |
| 事件边界无法映射有效词轴 | 不造时间，保留整段及粗时间，翻译后交人工 |
| 翻译返回未知/重复 ID、缺项、空译文或整句/事件拼接不一致 | 按完整句段校验及有界重试，同一响应内完成翻译/分配；无有效完整产物则翻译失败 |
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

split 配置继续用于独立源文断句，但输入由源/目标双语改为定稿源文及词轴，响应改为源文边界 ID，不再使用旧双语 split 请求/完成缓存。translate prompt/响应迁移为单回合整句译文及事件分配；正式 prompt 由既定仓库维护，不在本项目直接修改其 example 文案。旧 split_events 无新规划身份时需重新源文断句并重译；仅有 segment.translation 也不足以证明每个新事件有译文。现有 split SRT 文件名保留作为兼容检查输出。

回滚时停止活动进程，恢复原代码和对应环境；原始媒体和听录候选保留。新 schema 的工作恢复缓存不能被旧版本强制复用；旧工具对无 words 的新句段不具备状态语义保证，应使用原始兼容输入或重新生成。

## 14. 验证与交付门槛

### 14.1 必须通过的确定性检查

- 任意单源/多源 ASR 都不初始化语义 LLM，候选和实际兜底稿完整进入 glossary。
- 来源缺失不因 WhisperX 身份单独失败；glossary 实际文本 fallback 在相同输入下确定。
- 有效文本工作单元有 100% 实际文字输出和来源记录，不丢项、不以疑点标签代替文本。
- ASR 不读 glossary；术语变化不重跑识别，修订文字不变时复用 MFA。
- 不匹配任何完整候选的勘误稿同样进入 MFA。
- 独立 LLM 源文断句发生在 MFA/beautify 后、translate 前，只返回边界、不修改源文；本地不替代语义决策。
- 边界绑定实际采用稿且可完整还原原文；坏边界有界重试后整段降级，词轴不可用时不发断句请求。
- 每个自动规划事件的词轴均通过文字、时间、版本校验；无词轴段绝不自动强切。
- 翻译前已填事件 ID、源文和时间；翻译不清空规划，不改变事件 ID/数量/源文/时间。
- 翻译请求含完整句段及其全部英文事件；同一次响应返回整句译文和全部事件译文，不另发译文 split 请求。
- 乱序响应按 ID 归位，重复/未知 ID、缺项、空译文、拼接不一致均校验；按完整句段重试/提交，未完成项不命中完成缓存。
- 仅边界变化不重跑 MFA，但使受影响事件译文失效；无轴范围仍有整段实际译文或明确翻译失败。
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

固定同一采用稿、词轴和术语，对照旧整句翻译后双语 split 与新 LLM 源文断句后单回合整句翻译/事件分配，覆盖长句、否定、跨事件语序、专名、数字、句中换气及目标语言扩张。分别统计英文断句自然度、中文语义完整性/流畅度、超长/过快字幕比例、人工合并或重切次数、遗漏/重复、LLM 请求数、token 和总耗时。另验证正常路径每批 translate 只有一次请求/响应、不会偷偷追加译文 split；失败重试单独计数。再比较新路径有无纯译文校对，不预设阶段前移必然降低成本或改善质量。

听录保真与可读性编辑分开评估：保留采用前稿和编辑记录，WER/CER 不将有意语法编辑直接等同听录错误；最终展示稿另评意思保留、语法可读性和玩梗保留情况。人工标注仅用于离线验收，不引入运行时自动音频复核。

首版质量门槛建议：在预先固定、禁用可读性编辑的听录保真对照中，glossary 多源优选后的整体 WER 不劣于同集最佳单引擎；正常编辑路径另要求新增意思/表达意图错误数低于修正错误数，不以逐字 WER 否定允许的语法编辑。常规英文验证集可用词轴覆盖目标至少 90%，抽样词边界绝对误差中位数目标不超过 100 ms、P95 不超过 300 ms。数值属于待实测校准的候选发布标准，不是能力承诺；未达标只能记录实验状态或继续修正，不能填为已通过。

不得用排除人工处理句段后的事件规划成功率掩盖总自动完成率，也不能用后置术语修订掩盖前置 ASR 评估错误。

## 15. 实施顺序与设计对齐

1. 固定纯 ASR CandidateBundle、glossary/source_review 双输出、AlignedTranscript、LLM 源文断句及单回合整句/事件翻译契约和缓存协议。
2. 默认单源路径先识别，再 glossary 文本优选/编辑，之后 MFA、beautify、独立 LLM 源文断句及本地事件规划，最后 translate 单回合完成整句翻译与译文分配。
3. setup 适配可选单一 Parakeet/Qwen；所有 ASR 无 LLM，glossary 均可处理单源语义。
4. glossary 接收多源证据，统一完成术语和原文优选；失败仍提供实际文字。
5. batch 依赖改为识别 -> glossary/source review -> MFA -> 其余 postprocess；等待语义请求不占 GPU，MFA 仍受 CPU 预算约束。
6. 删除 translate/proofread 原文改写路径及旧缓存覆盖行为，保留纯译文校对开关，完成收益对照后再决定是否移除。

本轮正常迭代涉及 D1/D2/D3/D4/D6/D7/D10 的数据源、阶段顺序、缓存、协议、职责和安装边界，需在实现 PR 中说明并同步 AGENTS、README、MIGRATION 和 setup；prompt 由既定仓库维护。

已确定 ASR 无语义 LLM、文本优选与字幕编辑集中 glossary、MFA 位于其后、独立 LLM 源文断句前移至翻译前、translate 单回合完成整句翻译和译文分配、翻译和 proofreading 源文只读。不设置自动音频复核。仍需验证 Provider 平台/依赖、MFA 长音频准备、前置断句及单回合译文分配质量、预算和质量目标。Proofreading 删除属于评估项，本次不自动执行。

## 16. 参考与证据状态

- [Provider 原生输出考据及固定源码](2026-11-07-asr-provider-output-contracts.md)
- [研发路线图](2026-08-25-v2.1.0-asr-provider-design.md)
- [MFA 语料结构](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/corpus_structure.html)
- [MFA 有文本长音频准备](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/corpus_creation/create_segments.html)
- [MFA 对齐与输出](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/workflows/alignment.html)
- [MFA 说话人自适应](https://montreal-forced-aligner.readthedocs.io/en/latest/user_guide/concepts/speaker_adaptation.html)
- [praatio](https://github.com/timmahrt/praatIO)
- [pySBD](https://github.com/nipunsadvilkar/pySBD)

文档中的 schema 示例、工程默认和质量目标均为设计数据。它们不是已运行程序的输出，不代表已完成 setup 安装、真实模型转录或 MFA 基准。
