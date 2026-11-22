# 各文本 LLM 阶段的上下文预算

上下文窗口优先读 providers.json 中当前 provider 的 model_info。按实际模型的完整 ID 精确匹配，每项 context_length 为正整数、null 或缺省；无有效条目才查 OpenRouter。重复 ID 或非法值报错。既有本地条目（包括旧版写入的 provider 来源）仍可读；不再查询实际 provider 的 /models 或 Gemini 原生元数据接口，不维护硬编码型号表。无匹配只使用 32768-token 规划预算，不代表部署容量。

## OpenRouter 公开目录

匿名 GET https://openrouter.ai/api/v1/models?output_modalities=all，不需要 OpenRouter key，也不使用 provider 认证/headers，不发送项目内容或模型查询参数。目录请求设 5 秒超时、16 MiB 上限；按进程只缓存精简的模型 ID 与窗口索引，成功 1 小时、失败 60 秒。比较最后一个斜杠后的 model_id，不改写实际请求 ID，不猜别名、版本或变体；所有同名条目的窗口必须有效且一致才使用。采用窗口时记录 OpenRouter 标称值及部署未验证提示。

## 配置写回

`"model_info": [{"id": "my-deployed-model", "context_length": 65536}]`。用户仍需自备模型端点和密钥；不提供 models 子命令。窗口探测仅供预算，不会写文件。只有实际 SDK 请求成功（向量化还要求返回一个向量），且该完整 ID 的窗口缺失并在目录里查到有效值，才通过同一函数按 provider 和真实请求 ID 写入配置根 providers.json。不扫描 .env 或 default_model 清单。锁内重读合并后原子替换，保留已有有效项；目录窗口标记来源和端点摘要，换端点后不复用自动值。失败、保守预算及凭据不写入。删除条目/窗口并重启即可刷新。语义通用模型身份不含 model_info，翻译计划仍绑定有效窗口。

发送前估算下一轮输入：优先使用上一轮服务端 prompt_tokens，加上上一轮回复和当前实际 JSON 请求的估算；缺失 usage 时本地估算所有消息。估算考虑 ASCII/多字节文本，但不是各模型的精确分词器。门槛为窗口的 80%，显式 max_tokens/max_completion_tokens 要求更大输出空间时相应提前。

主翻译保留完整 system（含风格、简介与术语库）和原样当前请求；历史只使用实际校验通过的最近至多 128 对整句原译文，按预算从近到远挑选并恢复时间顺序，不保留旧 RAG 或 split_events。压缩后尽量回到窗口的 60% 以下；没有余量则不携带历史。不把摘要写入工作 JSON，不影响事件、时间或取消语义。窗口及策略纳入翻译规划身份。日志只给估算量、窗口及保留句数。

共享 SDK 入口同时覆盖 create 和 ask_validated，因此 glossary/Tavily、查询意图/查询翻译、glossary finalizer、source_review、翻译、校对（含独立命令）、简介/章节翻译均执行预算检查。非翻译阶段移除完整旧 user/assistant 轮次，不拆活动工具调用/结果组。格式纠错的原始请求和反馈一并保留，工具 schema/extra_body 参与本地估算。SDK usage 仅在历史前缀仍匹配时用于后续估算。自定义 request_kwargs.messages 不擅自修改，记录跳过压缩的警告。

服务端仍报告上下文超限时，共享入口可去除可移除的旧历史并额外重试一次；无历史不重复相同超限请求。原有阶段有界重试、校对缩批/去 RAG 和失败保留策略继续存在。若完整 system 和当前单次正文自身已经太大，日志告警，不静默截断任何正文/工具证据；这包括全文 glossary/元数据和只有一个 user 的工具循环，不能声称压缩能解决这类请求。该类需降低对应批量或使用更大窗口；自动分块/摘要另属阶段协议变更。本次不增加摘要 LLM、不改字幕输出，不改变 embedding 串行单文本协议。此变更涉及会话与缓存契约，需实现 PR 评审。
