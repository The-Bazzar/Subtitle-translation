# ASR 兼容层、多源勘误与 MFA 对齐完整设计

状态：完整设计草案，供本轮开发实施和评审使用；相关功能尚未实现或验收。
设计基线：2026-11-07。首版仅验证 en -> zh，保留多语言接口。

本文是目标行为的主设计文档。[研发路线图](2026-08-25-v2.1.0-asr-provider-design.md)只维护交付顺序，[Provider 输出考据](2026-11-07-asr-provider-output-contracts.md)维护官方源码依据和原生格式。当前实际行为仍以 AGENTS.md 和代码为准。本文新增的配置、字段、参数和命令均是待实现契约，不能当作当前版本已经可用的功能。

## 1. 已确定的产品原则

1. 默认 setup 预选 WhisperX；用户可以替换为单一 Parakeet、Qwen-ASR，或增设多个已支持的 Provider。
2. 单来源是正常模式：实际文本 passthrough，完全跳过 ASR 勘误 LLM，继续 MFA。不能以 Provider 名称判断是否允许单来源。
3. 多来源中各 Provider 平等。默认安装项不拥有文字、分段、置信分数或词轴方面的裁决特权。
4. 只有多份有效候选发生听录分歧时才调用 ASR 勘误 LLM；候选一致时直接通过。
5. 每个进入文本优选阶段的有效句段都有实际文本和非空 fallback。LLM 失败、空响应或无法确定时，仍有实际 Text 进入下一阶段。
6. translate_srt 之前的多引擎识别、文本优选勘误和 MFA 只处理 ASR 任务，不生成、读取或检索 glossary。
7. MFA 是唯一正式词轴对齐器，处理完整最终稿，包括未匹配任何完整 Source Result 的文字。
8. 对齐失败不撤销勘误、不切换其他对齐方案。保留实际文本和粗时间，标记词轴不可用，跳过受影响内容的自动 split，最终由人工定时和断句。
9. 后置翻译、proofreading 可结合 glossary 和语境，按需局部校对人名、近音名词等源文疑点，并同步对应译文；不回调 ASR 或 MFA。
10. 字幕和目标语言描述正常输出，不追加表达处理疑点的问号、括号说明或待确认标签。无法解决的问题写入非展示记录，交给最终人工。

MFA 的声学模型、发音词典和已确认补充发音属于对齐资源，不是项目术语库。MFA 对齐成功不证明文字正确，也不算新的独立 ASR 来源。

首版不包含其他源语言、混说、在线流式、音频 LLM 复核、联网 ASR 纠错、外部听录导入或自动模型路由。可选 Provider 的支持状态必须按 backend 和平台验证，不能仅凭名称宣称兼容。

## 2. 完整流程与职责边界

```text
download -> prepare-video -> canonical audio
  -> 所有已启用 Provider 识别同一音频，保存不可变 Source Result
  -> 共同输入区间内的文字对应和相邻区间合并
  -> 每个工作单元提前生成实际 fallback_text
       -> 单来源：passthrough
       -> 多来源一致：agreement passthrough
       -> 多来源分歧：LLM 优选勘误，失败输出 fallback_text
  -> 汇总完整连续最终稿及来源映射
  -> MFA 长音频准备、批量对齐、全局词位置映射和校验
       -> 失败部分：保留文本与粗时间，标记待人工
  -> 原子发布 ASR 正式 JSON
  -> translate_srt:
       beautify -> glossary -> translate -> split -> proofread -> ASS
  -> 可选 burn -> 最终人工审阅和修订
```

| 阶段 | 负责 | 明确不负责 |
|---|---|---|
| Recognizer | 从音频生成候选文字，记录真实来源与原生输出 | 读取 glossary、决定最终文稿或字幕断句 |
| Reconciler | 比较听录差异，处理漏听、明显语素错误、重复和缺词，确保实际文本输出 | 领域术语查证、生成时间戳 |
| MFA | 为已确定的最终文字定位词时间 | 证明文稿真实、改写源文、决定字幕语义断句 |
| translate / proofreading | 翻译与译文校对，出现具体疑点时局部校对语境或术语类源文 | 重新识别、重新 MFA、自动回卷上游 |
| split | 按现有 LLM 分割和源文词轴匹配规则生成字幕 event | 为无效词轴强切时间或自行制造词轴 |
| 最终人工 | 处理剩余听录疑点、译文疑点、定时与断句 | 作为自动阶段可以省略实际文本的理由 |

原始 ASR segment、比较工作单元、MFA utterance、翻译句段和字幕 event 是不同对象。一个模型的分段不能成为其他阶段不可跨越的硬边界。

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
  "reconcile": {
    "provider": "",
    "model": "",
    "timeout_seconds": 120,
    "max_retries": 1
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
- reconcile 的 provider/model 都留空时，复用既有翻译 Provider 和模型解析结果；显式指定 Provider 但 model 留空时，使用该 Provider 的 default_model。
- 只有实际要调用多源勘误 LLM 时才解析密钥和初始化客户端。单来源或一致文本不要求这组配置；缺少配置的分歧单元使用文本 fallback 并记录原因。
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
  reconcile.py    # 多源对应、fallback 和 LLM
  mfa.py          # MFA 工作目录、调用和结果转换
  normalize.py    # 正式 JSON、词映射和状态校验
  backends/
    whisperx.py
    parakeet.py
    qwen_asr.py
```

现有 stages.py 与 batch_runtime.py 复用同一 ASR stage。whisper_worker.py / controller 可以作为迁移 facade，调度器只处理统一命令和结构化结果，不直接解析厂商对象。

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

语义词序相同且只有比较允许的格式差异时，可直接采用确定性候选文字，记录 agreement。存在词差异、增删或顺序变化才进入 LLM。重复词仍是有效 token，不能以去重方式消除真实口头重复。

同一模型/权重的重复运行只算一个识别来源。Source Result 的数量不等于独立证据数量。

## 6. 文本 fallback 与 LLM 勘误

### 6.1 首版确定性 fallback

本节将此前悬空的选择规则具体化为工程默认 medoid-v1：

1. 对当前单元的有效、非空候选按不同识别来源去重。
2. 一个来源时，实际文本 passthrough。
3. 多来源时，计算候选两两的 token 编辑距离，除以两者较长 token 数，得到规范化距离。
4. 选择到其他候选平均距离最小的现有候选，原样作为 fallback_text。
5. 同分时按原始文字 SHA-256 排序；文字相同时才按稳定 source_id 排序以记录引用。

该算法只选择已有实际文字，不拼接出新词，不使用原生 score、不依赖注册顺序或固定 WhisperX 优先级。它是失败兜底，不宣称多数结果一定正确。选择结果、参与来源及 policy_version 随单元保存并纳入缓存。

准备 fallback 在请求 LLM 之前完成。共识文本、单源透传和 LLM 失败均能直接使用实际文字，下一阶段不需要等待人工选文。

### 6.2 调用条件与边界

- 显式单来源：passthrough_single，无 ASR 勘误 LLM 初始化和调用。
- 多来源但只有一个有效结果：passthrough_degraded，记录缺失来源。
- 多份有效文字一致：agreement，无 LLM 调用。
- 多份有效文字有分歧：调用 LLM；有效响应为 reconciled，否则为 fallback。

ASR 勘误 LLM 只比较候选、相邻听录语境和位置对应，处理漏听、明显语素错误及重复/缺词。不读 glossary、标题术语表或网页证据，不执行领域专名查证，不为了通顺删填充词或重写句式。

所有来源平等呈现。请求使用候选 ID，实际 Provider 信息留在来源表供追溯，不在 prompt 中指定某模型为正确答案。新词可以是多源勘误的派生结果，不要求等于某个完整候选，但须说明所参考的实际候选和修改依据。

### 6.3 请求与响应

请求是顶层 items 数组，每项包含 id、language、candidates、neighbor_context 和 fallback_text。候选引用绑定原始 source_id 与文字范围。响应形状示例：

```json
{
  "items": [
    {
      "id": "u0001",
      "en": "Hello, I have a nice day!",
      "decision": "correct",
      "evidence_ids": ["A:u0001", "B:u0001"],
      "review_required": false
    }
  ]
}
```

en 是 source_language 的实例，不是通用硬编码字段。decision 允许 select、correct、fallback；疑点信息可以另存，但不能代替实际源文。

本地检查 JSON、ID 唯一性、引用、非空源文、数据类型及输出范围。不得仅因 source_ids 有效就宣称文字已经声学证实。不能把 LLM 自报置信度当作发布保证。

缺项、重复/非法 ID、空文字、超时、服务失败、无法确定或非法响应，只使受影响单元使用 fallback。已通过校验的其他单元保留结果。整批响应不可解析时，整批各自使用预先准备的 fallback。

### 6.4 初始调用预算

| 参数 | 首版工程默认 |
|---|---|
| 请求超时 | 每次 120 秒 |
| 重试 | 最多 1 次，即最多 2 次尝试；同一层管理，不叠加 SDK 无限重试 |
| 批次 | 最多 8 个工作单元，超过服务上下文预算时减少批次 |
| 相邻上下文 | 左右各一个相邻单元，明确只作参考 |
| 并行请求 | 调度器统一限制，最多 4 路 |
| 超限处理 | 拆小批次或单元，不能截掉真实听录；无法处理则实际文本 fallback |

这些值作为版本化默认，可在受控配置中调整；不形成用户必须填写的一组 .env 参数。单元处理预算内仍无有效响应就结束重试，不能反复询问 LLM 直到满意。

阶段输入前提是已有实际听录文字。静音窗口不伪造非空句子；全部来源都无有效文本且音频存在语音时属于上游识别失败，不通过生成占位文字满足接口。只要有效单元进入本阶段，就必定有非空文字输出。

## 7. MFA 全文对齐

### 7.1 完整稿与实际执行粒度

把所有 passthrough、agreement、reconciled、fallback 单元按音频和文字顺序汇总为完整稿。记录工作单元和候选字符位置到完整稿的映射，来源未匹配任何完整候选不影响对齐资格。

逻辑输入为完整音频和完整稿，实际执行不把任意长度视频当成一个超长 utterance。首版采用 MFA 的有文本长音频准备流程 mfa segment 形成 TextGrid/utterance 输入，再批量运行 mfa align；具体命令签名由已锁定的 MFA profile 提供。

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

完成后合并全局词轴。正式翻译句段从完整稿的语句和段落重建，不复刻 Provider segments 或 MFA blocks。首版可用保留字符位置的现成英文句界库 pySBD，配合已有明确段落边界；这只是翻译输入组织，不是最终字幕 split。

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

### 8.1 ASR Transcript v2

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
      "text_origin": "reconciled",
      "source_refs": ["A:u0001", "B:u0001"],
      "word_timing": {
        "status": "available",
        "aligner": "mfa",
        "text_sha256": "<asr-text-hash>",
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
      "text_origin": "fallback",
      "source_refs": ["C:u0002"],
      "word_timing": {
        "status": "unavailable",
        "aligner": "mfa",
        "text_sha256": "<asr-text-hash>",
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
    "reconciliation_manifest": "<reconciliation-ref>",
    "aligner": "mfa",
    "alignment_manifest": "<alignment-ref>",
    "final_text_sha256": "<complete-text-hash>"
  }
}
```

- text_origin 允许 passthrough_single、passthrough_degraded、agreement、reconciled、fallback。
- word_timing.status 只有 available / unavailable；只在全部必要检查通过时 available。
- reason 使用枚举常量，例如 preparation_failed、oov_unresolved、mfa_alignment_failed、mfa_timeout、word_mapping_failed、invalid_timestamps。
- manual_review 是人工任务状态，不得嵌入字幕正文；不能以 words=[] 唯一推断所有失败原因。
- text、来源和状态必须覆盖全部有效输入单元。没有语音的结果可以 segments=[]，不生成“静音”占位字幕。

### 8.2 translate_srt 的工作副本

ASR JSON 和原始候选不可反向修改。工作缓存继续保留 text 作为 ASR 输入，复用 proofread_text 保存后置有据的源文校对；effective_source_text = proofread_text or text。translation 始终对应实际使用的 effective_source_text。

新增 source_edits 记录修改阶段、前后文字 hash、修改前版本的字符范围、依据和可选 event ID。源文修订与对应译文作为同一次本地更新提交，避免只改一边；校对失败则保留修改前的实际文字和译文。

MFA words 仍绑定 ASR text 的 hash，不将修改后的词写回 words。以版本化 token 策略检查当前工作源文与词轴是否仍可对应。

### 8.3 Split 状态

保留既有 ok、fallback、unsplit 语义，增加显式 skipped 状态表示本方案不应自动 split 的句段。reason 区分 word_timing_unavailable 和 source_changed_before_split，不能伪装成普通对轴失败而反复重试。

- MFA 词轴不可用：不加入 split 的 LLM 请求，不本地切词分配时间；保留整段源文、译文和粗时间。
- 翻译阶段已修订语词，使工作源文与 MFA 词轴不再对应：同样 skipped，转人工断句。
- 仅格式变化且 token 对应仍有效：可以正常 split，须记录所用 token 策略。
- split 后 proofreading 局部校对源文：保留已有 event 时间和边界，保存展示修订；缓存恢复不能再用旧词轴对修订文本自动重切。
- 正常 split 仍按源片段首尾词匹配 words，不新增本地强切算法。

每个 split 结果保存其输入源文、译文和词轴身份。载入缓存时区分“已完成且后置修订的 event”与“尚未完成、需要重新 split 的句段”，不能仅比较一个文本字段就触发重试。

## 9. translate_srt 的后置职责

顺序保持 beautify -> glossary -> translate -> split -> proofread -> ASS。

- beautify 只处理可用词轴；无词轴段不执行逐词场景吸附，不从空数组重写边界。
- glossary 从这一阶段开始生成/复用，规则沿用现有实现。ASR 阶段没有术语库依赖或反馈循环。
- 翻译/校对对人名、读音相近的名词、词不合语境等实际疑点，结合 glossary 和现有语境作局部源文校对。不得借此重新通篇润色、删口头词或执行一次新的 ASR。
- 有据的源文修订可进入源文字幕，并同步对应译文；ASR 原始产物、MFA 词轴和既有 event 时间保持不变。
- 未确认或服务失败时保留当前实际源文/译文，不自动回调 Recognizer、Reconciler 或 MFA。

校对疑点只写非展示元数据或人工报告，不在 ASS、SRT 或目标语言描述正文追加“（存疑）”“[待确认]”或无内容依据的问号。正常疑问句和原内容括号可以保留，不能全局删除问号括号。

后置 glossary 和源文修订只影响 translate_srt 工作缓存，不反向使 ASR/MFA 重跑。对原有 prompt 的调整在既定 prompt 仓库完成，本项目只同步经维护的版本；不得直接改写 *_prompt.example.md 文案。

## 10. Batch、资源和进程协议

保留既有资源规则：
- CPU/IO 容量 max(1, (os.cpu_count() or 1) // 4)。
- prepare/burn 共用最多 4 路 NVENC；ASR 模型波次与 NVENC 互斥。
- GPU ASR worker 串行，一个模型服务同一波次的多个任务，完成后卸载，再运行下一模型。
- 任务 ID 和用户输入顺序不变，不靠解析终端 marker 传递结果。
- 底层 worker 意外退出沿用 fail-fast；它与有结构化错误返回的 Provider 业务失败分开。

Provider 业务失败只减少有效候选集合。剩一份实际文字时 degraded passthrough；仍有多份时按有效来源比较。WhisperX 不拥有特别的失败规则。

MFA 默认使用 CPU，按项目 corpus 批量处理。调度器原子申请 r 个 CPU/IO 资源份额，MFA num_jobs=r，r 不超过 4 和当前预算。不能每个外层任务各自再起一个完整进程池，也不能持有一个份额后无限等待额外份额造成死锁。

LLM 请求占独立的有界网络容量，不保持已完成的 GPU 模型。MFA 可在资源允许时与其他 CPU/IO 工作并行，首次实现不要求流式抢占或极细粒度增量。

第一次 Ctrl+C 停止新 stage admission，允许活动阶段结束；第二次终止活动 Provider/MFA 进程树并返回 130。取消优先于 fallback，不在取消后继续发起请求、启动 MFA 或发布新产物。用户主动取消不伪装成 LLM 失败继续运行。

已有错误铃、聚合报告和失败退出码规则保留。正常单来源及有标记的人工处理不是任务失败；如果实际字幕成功导出，可返回 0 并在报告中明确 degraded/manual 数量，不能把它们算为自动处理成功。

## 11. 缓存、文件与原子发布

建议新增生成物目录，全部属于本地输出，不提交：

```text
<project>/.asr_work/<audio_id>/<run_id>/
  input_manifest.json
  sources/<source_key>.json
  reconciliation.json
  final_source.txt
  mfa_input/
  mfa_output/
  alignment.json
  review_tasks.json
  final.candidate.json
```

既有 <base>.asr.json、.asr.lock、media/alignment generation 等继续由原 batch 协调层拥有，不把它们重新解释成某一 Provider 的裸返回。

source_id 是不透明标识，不直接作为文件名；source_key 使用 source_id 的稳定 hash，manifest 保存映射，避免冒号、斜杠或模型名称造成 Windows 路径非法。所有本地配置、runtime manifest 和生成物路径均属于忽略清单。

| 缓存层 | 关键身份 | 可复用条件 |
|---|---|---|
| 原始识别 | 音频、输入区间、Provider/backend、模型 revision、依赖/profile、语言、解码参数 | 上述内容相同 |
| 文本优选 | 全部有效候选及失败集合、单元划分、fallback policy、LLM 模型/prompt、语言策略 | 相同来源状态和相同规则 |
| MFA | 最终全文、corpus 清单、分块、词典、声学模型、工具版本、说话人分组和自适应配置 | 整个项目输入身份相同 |
| 正式 ASR JSON | 文本优选与 MFA 身份、规范化 schema | 当前 generation 完整一致 |
| 下游工作缓存 | ASR 正式身份、glossary、源文修订、翻译/split/proofreading 参数与 prompt | 按各下游阶段检查 |

首版 MFA 使用项目级缓存：完全相同输入直接复用，相关文字或 corpus 改变后重跑当前项目；不承诺单段增量。说话人自适应的数据依赖已计入身份，避免局部重跑与完整运行混算。

LLM 模型、prompt 或预算变化要允许重新尝试先前 fallback；不能永久把一次服务失败当作最终“已勘误”。复用原始识别即可，不必重新下载模型或转录。

正式发布沿用锁、快照核对、唯一 candidate 和同目录原子替换：发布前确认媒体 generation、候选清单、最终源文和 MFA 身份没有变化；先使不匹配的 beautified 缓存失效，再在提交锁内原子替换正式 JSON。进程失败不得留下半份正式 JSON，也不能沿用不同词轴的旧 split_events。

Manifest 中记录请求、来源、工具版本和失败原因；不写 API key、cookies 或完整认证头。只有当前项目及 generation 的资料可进入相应上下文。

## 12. 失败处理表

| 情况 | 结果 |
|---|---|
| 未注册 Provider、未安装依赖、模型/语言不支持 | preflight 明确配置错误；不静默改用 WhisperX |
| 单一有效来源 | 正常 passthrough，继续 MFA |
| 某可选来源返回业务错误，其他仍有文字 | 按实际来源继续，记录 degraded |
| 有语音但全部来源无实际文字 | 上游识别失败；不凭空制造 fallback |
| LLM 缺配置、超时、坏 JSON、缺项或无法确定 | 对受影响单元输出实际 fallback，继续 MFA |
| MFA 的分块、词典、运行或校验失败 | 保留实际文本和粗时间，词轴 unavailable，最终人工 |
| split 前后置改字不再匹配词轴 | skipped，实际译文正常输出，最终人工断句 |
| proofreading 失败 | 保留处理前源文及译文，不返工上游 |
| worker 意外退出 | 统一 fail-fast、资源回收和恢复记录 |
| 用户取消 | 按两级中断规则结束，不再启动新阶段 |

MFA 工具及基础模型缺失应在 preflight 报告；已经进入执行并产生实际源文后，MFA 处理失败按人工路径保留文稿，不退回原候选或隐藏导出内容。

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

whisper 兼容命令映射到显式 WhisperX 单来源的新 ASR 流程。旧 --skip-whisper / PIPELINE_SKIP_WHISPER 继续表示跳过整段 ASR 处理并复用指定输入，而不是按模型名字只跳过一个来源；URL batch 的 skip 输入限制继续遵循既有规则。

新主路径通过 SDK adapter 和受管理 worker 识别，不执行自定义 WHISPERX_PATH_* 的旧完整识别/对齐命令。旧 WhisperX alignment 参数不能暗中控制 MFA；升级时给出迁移说明，MFA 改用独立配置。

旧 JSON 仍可直接 translate。无新 _asr 元数据的词轴按既有读入逻辑检查，可标记 legacy；缺少新 fingerprint 的恢复缓存视为 miss。新增 asr_providers.json、asr_runtime.json、.asr_work 和审阅记录加入生成物忽略清单，example 仍只有包内一份权威。

回滚时停止活动进程，恢复原代码和对应环境；原始媒体和听录候选保留。新 schema 的工作恢复缓存不能被旧版本强制复用；旧工具对无 words 的新句段不具备状态语义保证，应使用原始兼容输入或重新生成。

## 14. 验证与交付门槛

### 14.1 必须通过的确定性检查

- 单一 WhisperX、Parakeet、Qwen 等走相同单源分支，不初始化 ASR 勘误 LLM。
- 多来源缺失时不因 WhisperX 身份单独失败；fallback 结果在相同输入下完全确定。
- 有效文本工作单元有 100% 实际文字输出和来源记录，不丢项、不以疑点标签代替文本。
- 前置 ASR 不读 glossary；glossary 变更不重跑 ASR/MFA。
- 不匹配任何完整候选的勘误稿同样进入 MFA。
- 每个自动 split 的词轴均通过文字、时间、版本校验；无词轴段绝不自动强切。
- MFA 失败不改源文；后置源文校对不覆盖原始词轴，不返工上游。
- 字幕正文没有程序生成的存疑标签；正常问号和括号保留。
- 缓存、并发、取消、candidate 发布及恢复测试保持通过。

### 14.2 模型和平台实测

首批考据只有官方源码和示例，Parakeet/NeMo、Qwen 官方 wrapper 尚未在本项目运行。接入时分别锁版本，采集真实输出 fixtures，验证输入对应、时间单位、空结果、长音频和取消，不把人工构造 JSON 标成真实推理。

Windows/PowerShell 7 与 Linux/bash 分别验证默认 setup、单引擎替换、可选依赖不被导入、工具路径含空格、配置升级幂等性和模型缓存保留。未通过的平台/profile 不标为已支持。

单元测试 mock 网络、ASR、LLM、MFA、ffmpeg 等外部依赖；修改核心翻译、setup、入口、缓存或并发时运行全量 unittest。真实模型和人工基准单独执行。

### 14.3 英文质量基准

建议初始集不少于 30 段、总计不少于 60 分钟，覆盖专名、数字、口音、重复/填充词、长停顿、背景声、窗口边界和真实 ASR 分歧；人工标注听录及抽样词边界。调参集和验收集分开。

分别报告每个单引擎、多源勘误、MFA、后置源文校对和译文质量。指标包括 WER/CER、漏听/语素错误、实体错误、修正与新增错误、fallback/degraded 比例、可用词轴覆盖、边界误差、split 自动完成率、最终人工数量/时长、耗时和峰值内存。

首版质量门槛建议：多源模式的整体 WER 不劣于同集最佳单引擎，新增错误数低于修正错误数；常规英文验证集可用词轴覆盖目标至少 90%，抽样词边界绝对误差中位数目标不超过 100 ms、P95 不超过 300 ms。数值属于待实测校准的候选发布标准，不是能力承诺；未达标只能记录实验状态或继续修正，不能填为已通过。

不得用排除人工处理句段后的 split 成功率掩盖总自动完成率，也不能用后置术语修订掩盖前置 ASR 评估错误。

## 15. 实施顺序与设计对齐

1. 固定 SourceResult/ASR Transcript schema、配置、runtime manifest 和真实输出 profile。
2. 跑通默认 WhisperX 识别、单源 passthrough、MFA 及下游无词轴分支。
3. setup 支持可选引擎替换，逐个验证单一 Parakeet/Qwen，不要求同时安装所有引擎。
4. 实现公共窗口、候选对应、medoid fallback 和按需 LLM 多源勘误。
5. 接入最终全文、MFA 准备、全局词映射、工作源文及人工报告。
6. 完成 batch 调度、缓存、两级取消和原子发布回归，运行真实基准后声明支持状态。

这是正常开发迭代的主方案。与 D1/D2/D3/D4/D6/D7/D10 相关的数据源、阶段、缓存、LLM、split、职责和安装边界在实现 PR 中说明，同步 AGENTS、README、MIGRATION、examples 及两套 setup；不直接修改受维护的 prompt 文案。

已确定的原则不再列为“是否采用”的问题。仍需实验的内容是依赖/模型版本组合、MFA 分段可靠性、运行预算和质量数值；这些是进入稳定支持前的验证门槛，不允许静默替换算法、输出格式或对齐器。

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
