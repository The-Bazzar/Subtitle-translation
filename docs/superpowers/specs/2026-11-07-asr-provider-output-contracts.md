# ASR Provider 原生输出考据与适配契约

状态：已完成官方文档及固定源码快照核对，尚未完成真实模型运行。核对日期：2026-11-07。

本文件是 [ASR 完整设计](2026-11-07-en-zh-multi-asr-design.md)的接口依据，说明候选 Source Result 如何生成；正式配置、阶段和状态以完整设计为准，本文只维护原生格式考据。正式下游词轴仍只由 MFA 生成。以下示例都是说明字段的人工构造数据，不是真实推理采样。

## 1. 考据范围与版本

| Provider | 本次核对的接口 | 固定源码快照 |
|---|---|---|
| Parakeet TDT v3 | NVIDIA NeMo 的 `ASRModel.transcribe`、`Hypothesis` 和时间戳转换 | `NVIDIA-NeMo/Speech`：`cd61689d41b1962d3e1957ae8faf28bf8bee0e3b` |
| Qwen3-ASR | 官方 `qwen_asr` 包的 `Qwen3ASRModel.transcribe` 和 `ASRTranscription` | `QwenLM/Qwen3-ASR`：`7c6daf77a2421100f5fb066495372c00129d39ff` |

这些提交是考据快照，不是已验证的安装版本，也不等同于模型 checkpoint revision。实现时要分别锁定依赖、推理后端、模型 revision 和 adapter schema，并在目标环境取得真实样本。

同一模型使用不同后端会有不同返回结构。NeMo、原生 Transformers pipeline、`qwen_asr` wrapper、vLLM 服务和云 API 不能只按模型名称共用一个结果解析器。本次拟以 Parakeet/NeMo 和 Qwen3-ASR/官方 wrapper 的离线接口作为首批适配参考；其他路径需独立声明并验证。

## 2. Parakeet / NeMo

### 已确认的结构

NeMo 的返回形式受调用模式影响，可以是文本或 Hypothesis；需要结构化结果时显式请求 `return_hypotheses=True`。官方模型示例的时间戳模式使用 `timestamps=True`，从结果的 `timestamp` 读取 `word`、`segment` 和 `char`。

已核对的 `Hypothesis` 字段包括 `text`、`score`、`timestamp` 及可选的 `word_confidence`。`timestamp` 不保证总是词轴字典，未启用相应解码能力时也可能是列表或张量。Adapter 需依据调用模式检查结构，不能无条件下标取 `timestamp['word']`。

下面是包含时间信息的结果投影示意，忽略了解码状态和张量：

```json
{
  "text": "Hello, I have a nice day!",
  "score": -12.3,
  "timestamp": {
    "word": [
      {"word": "Hello,", "start_offset": 3, "end_offset": 8, "start": 0.24, "end": 0.64}
    ],
    "segment": [
      {"segment": "Hello, I have a nice day!", "start_offset": 3, "end_offset": 37, "start": 0.24, "end": 2.96}
    ]
  }
}
```

示意仅展示一个词，不能作为完整词覆盖测试 fixture。输入秒时间和 frame offset 的单位不能混用。已核对的转换逻辑为：

```text
seconds = offset * window_stride * subsampling_factor
```

`start/end` 已转换为秒时不可再乘 stride。只有 frame offset 时，转换参数必须取自实际模型和所锁版本，不能硬编码某个模型的帧长；再根据调用方裁剪信息恢复绝对时间。

### 适配规则

- 顶层文本读取 `Hypothesis.text`；时间戳中的拼接文字只作关联检查，不覆盖带标点的顶层原文。
- 候选句段文字为 `segment`，候选词文字为 `word`，时间从已确认的秒字段或显式帧转换取得。
- `score` 保留为该后端的原生分数，不映射成跨 Provider 可比较的 0 到 1 正确率；缺少 word confidence 时不生成虚假值。
- 参考 profile 使用显式 Hypothesis 模式。官方示例以列表取样，但不同模型路径和注释可能涉及 tuple / N-best 容器；所支持版本必须通过真实 fixture 锁定外层结构和每个输入对应的 best hypothesis。未知结构报适配错误，不能通过 `str(result)` 伪装成听录文本。
- 不将 WhisperX 的 `language` 参数直接套给 RNNT `transcribe`；语言能力由模型声明及统一请求记录管理。
- 原生词轴和分段仅供候选比较，不用于正式源文事件规划。正式源文事件在 MFA/beautify 后由独立 LLM 语义断句选择边界，本地映射 MFA 词轴；不额外加载另一个模型给 Parakeet 候选补词轴，也不把原生分段当作字幕边界。

依据：[NVIDIA 模型示例](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)、[Hypothesis 定义](https://github.com/NVIDIA-NeMo/Speech/blob/cd61689d41b1962d3e1957ae8faf28bf8bee0e3b/nemo/collections/asr/parts/utils/rnnt_utils.py)、[时间戳转换](https://github.com/NVIDIA-NeMo/Speech/blob/cd61689d41b1962d3e1957ae8faf28bf8bee0e3b/nemo/collections/asr/parts/utils/timestamp_utils.py)、[RNNT transcribe](https://github.com/NVIDIA-NeMo/Speech/blob/cd61689d41b1962d3e1957ae8faf28bf8bee0e3b/nemo/collections/asr/models/rnnt_models.py)。

## 3. Qwen3-ASR / 官方 qwen_asr wrapper

### 已确认的结构

离线 `transcribe` 返回 `list[ASRTranscription]`，每个对象对应一个输入音频。对象有 `language`、`text`、`time_stamps` 三个字段。默认 `return_time_stamps=False`，时间信息为空，不包含 WhisperX 式的 `segments` 或 `words`。

默认结果的 JSON 投影示意：

```json
{
  "language": "English",
  "text": "Hello, I have a nice day!",
  "time_stamps": null
}
```

本项目使用该文字识别路径：外部请求的 `en` 在 adapter 中转换为 SDK 接受的语言名 `English`，`context` 不注入 glossary，关闭 `return_time_stamps`，不配置或加载 Qwen ForcedAligner 模型权重。

该返回对象不提供原生句段边界。源码会合并内部识别块的文字及语言，所以不能根据返回文字中的标点推导精确时间，也不能假定内部块仍可从公开结果中恢复。

### 可选原生时间戳的格式核对

官方启用时间戳需配置其 ForcedAligner。此时 `time_stamps` 是带 `items` 的结构化对象，item 通过属性访问 `text`、`start_time`、`end_time`；时间单位为秒，SDK 可迭代访问。它不是默认的字典数组，不能写成通用 `result['segments']` 或 `stamp['word']` 解析。

该结构只用于考据差异，不作为本项目的另一条正式对齐路径，不为取得此结构安装模型权重或运行该对齐器。MFA 仍是唯一正式对齐器。

### 适配规则

- 读取 `.text`，将 `.language` 作为原生语言元数据保留；统一请求语言和实际原生语言分别记录。
- SDK 的语言值可能是语言名称、混合名称或空字符串。不能直接放进要求 ISO 语言代码的正式字段；英语验证路径需显式转换或记录异常，不能把混说结果静默归为 `en`。
- 没有原生时间时，用调用方已知音频范围标记覆盖区间，并注明时间依据是 `input_window`，不伪造“模型识别出的语音起止时间”。
- 空文本或空语言不能单独证明是静音。保留原始结果，结合音频窗口、VAD 和其他候选判断空结果状态；不能补写占位文字充当 fallback。
- 一次调用多份音频时按所锁 SDK 的输入 / 输出顺序绑定输入 ID，并校验数量；不能把返回的多个对象当作同一音频的多个语义段落。
- 官方 wrapper 与新式原生 Transformers 模型接口不是同一结果协议，后者需要独立 backend profile，不能仅改变 model ID 就假定本适配器可用。

依据：[ASRTranscription 及离线 transcribe](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/inference/qwen3_asr.py)、[原生时间戳对象定义](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/inference/qwen3_forced_aligner.py)、[语言转换及输入工具](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/inference/utils.py)。

## 4. 统一 Source Result 契约

兼容层先统一候选文字与来源，MFA 后才生成正式词轴 JSON。不能要求每个 ASR 先伪装成带精确时间的 WhisperX 输出。

以下为 Qwen 单窗口候选的结构草案，不代表当前代码已实现该 schema：

```json
{
  "schema_version": 1,
  "source_id": "qwen_asr:clip_0001",
  "provider": "qwen_asr",
  "backend": "qwen_asr_transformers",
  "model": "Qwen/Qwen3-ASR-0.6B",
  "model_revision": "<resolved-model-revision>",
  "runtime_version": "<installed-package-version>",
  "audio_id": "<decoded-audio-fingerprint>",
  "input_id": "clip_0001",
  "input_range": {"start": 10.0, "end": 20.0},
  "requested_language": "en",
  "native_language": "English",
  "text": "Hello, I have a nice day!",
  "native_segments": [],
  "native_words": [],
  "timing_basis": "input_window",
  "raw_ref": "sources/clip_0001-qwen.native.json",
  "status": "ok"
}
```

`input_range` 是输入音频在同一解码音频中的覆盖范围，来自调用方。`native_segments/native_words` 是 Provider 确实返回且经过单位转换的可选信息，统一保存绝对秒时间，另留原始字段投影与转换参数供追溯。原生时间不覆盖输入范围，也不冒充 MFA 的正式词轴。

整片音频输入且无原生句段时间时，只能记录整片覆盖范围，不能承诺更细的定位。若比较需要局部锚点，应在调用侧记录可控的输入窗口，或使用明确的多源映射方案；窗口只是识别和比较的坐标，不是最终语义边界。跨窗口拼接必须保留来源，不能按字数平均分配时间。

结构及文本校验在 adapter 完成，候选层不纠错、不去口头词、不按字段猜测相似意思。对象中的 decoder state、GPU tensor 等不直接穿过 worker 协议；导出与听录有关的 JSON-safe 原生投影，保留文字和时间原字段，明确未序列化字段。

## 5. 对现有详细设计的补充

- `Recognizer` 的必需输出为文字、来源与输入覆盖范围；原生语言、分段及词轴都不能被假定存在。
- 这些接口并非原生 WhisperX JSON。统一候选 `SourceResult v1` 与 MFA 后的正式 `ASR Transcript v2` 必须分开，具体字段以完整设计第 4、8 节为准。
- 默认单一 WhisperX、单一 Parakeet、单一 Qwen 都先产生同一候选契约，再进入 glossary 原文校对，之后才由 MFA 对修订稿生成词轴；不能因 Qwen 无原生词轴就判为不支持单来源。ASR 只收集候选，不调用语义 LLM。
- 所选 Provider 和 backend 共同决定 parser，schema/profile 版本纳入原始识别缓存；模型 revision 和依赖版本分别记录。
- 引擎依赖和接口结构未知时应在 setup / capability gate 报告具体问题，不能将未知对象或异常文本当成有效听录。

## 6. 验证状态及接入验收

本次已核对固定提交的源码及官方模型示例；当前项目环境未安装 `nemo_toolkit` 和 `qwen-asr`。本次未执行模型权重下载或真实转录，也未验证 Windows/CUDA 的版本组合。源码考据不等于运行兼容性验收。

实现接入前需完成：

1. 锁定 checkpoint、模型 revision、依赖版本和 backend profile。
2. 在各自所选环境中运行相同短英文音频，采集真实的原生输出及 JSON-safe 投影，明确容器、字段、时间单位和输入对应关系。
3. 采集纯文字、原生时间信息（仅作诊断）、空结果、多输入、异常响应和长音频合并等所支持路径，形成标为真实采样的 fixtures；人工构造 fixtures 单独标注。
4. 单元测试使用 fixtures / mock，断言准确取文字、时间单位转换、裁剪偏移、缺字段、数量不匹配、非有限时间和未知对象拒绝；不能依赖真实模型联网。
5. 验证单一 Parakeet / Qwen 且没有 WhisperX 或 ASR 专用 LLM 时，能透传实际文字进入 glossary，再将实际修订稿交给 MFA；多来源对比不假定字段结构相同。
6. 通过目标平台真实基准及 setup 安装验证后，才能标记该 backend 为已支持。
