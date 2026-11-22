# 各文本 LLM 阶段的上下文预算

窗口按 provider 的 model_info 显式配置、当前部署元数据、OpenRouter 公开目录、内置精确端点与模型、32768 tokens 的保守规划值依次选取。model_info 为对象列表，每项 id 精确匹配实际请求模型，context_length 为正整数或 null；缺省/空列表或 null 保留自动查询，重复 ID 或非法值报错。正常使用无需填写。请求模型按 request_kwargs.model 覆盖后的实际模型匹配。未知窗口不是对服务实际容量的探测。内置回退参考来自 [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/)、[GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)、[Gemini 3.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash) 和 [OpenRouter Sonnet 4.6](https://openrouter.ai/anthropic/claude-sonnet-4.6)。不向自建端点套用同名模型的官方窗口。

通过当前 OpenAI 兼容端点 GET models 查询精确模型的 context_window/context_length/max_input_tokens，分别适配 [DeepSeek 模型元数据](https://api-docs.deepseek.com/api/list-models/)和 [OpenRouter](https://openrouter.ai/docs/api-reference/models/get-models)。Google 官方兼容端点使用同域原生 GET /v1beta/models/{model} 的 inputTokenLimit，参考 [Gemini models](https://ai.google.dev/api/models)。不保证所有供应商的 models 接口都包含窗口。未知格式、缺失模型、权限或超时失败会明确告警后回退，不使用其他模型的数据。元数据请求 5 秒超时、无 SDK 重试；按完整 provider 配置、实际模型及凭据哈希隔离缓存，成功 1 小时、失败/缺失 60 秒；最多 128 项；失败不落盘，不打印密钥或原始响应，不吞取消。

例如给 providers.json 中某个 provider 增加 `"model_info": [{"id": "my-deployed-model", "context_length": 65536}]`。包内 providers.example.json 为已核实的云端默认模型示范从公开目录填写正整数，并为本机默认模型示范省略 context_length。目录预填值是标称值；写入配置后即作为显式覆盖，删除该字段可恢复自动查询。元数据不放进 request_kwargs，不传给 SDK，不限制可调用模型列表。LLM_CONTEXT_TOKENS、TRANSLATE_CONTEXT_TOKENS 和旧映射不再读取。不提供 models 子命令；公开目录查询仅是内部辅助，用户需自备 provider 端点和密钥（BYOK），程序不注册模型。首次从部署或公开目录读到有效窗口时，按当前 provider 和完整请求模型 ID，将 context_length、context_length_source（provider/openrouter）和 context_length_endpoint（端点摘要）写入配置根 providers.json；已有效的配置优先。写入用独立文件锁，锁内重读并合并最新文件，原子替换；不可写、配置损坏或端点在查询间变更时不覆盖，保留本次查询结果。自动条目只在原端点有效，公开目录值跨运行沿用标称提示。删除条目/窗口并重启即可刷新。标称和部署查询结果都可持久化；静态/保守回退、失败、凭据及其他响应字段不可持久化。通用语义模型身份不包含 model_info，避免自动填入其他模型造成不相关的检查点失效；翻译仍按有效窗口确定计划身份。

## OpenRouter 公开目录补充

当前部署未返回窗口或查询失败后，再匿名 GET https://openrouter.ai/api/v1/models，不需要 OpenRouter key。使用独立 HTTP 客户端，不继承 provider 的认证和额外 headers，不发送项目内容或模型查询参数。目录读取设 5 秒超时和 16 MiB 上限，按进程共享成功 1 小时/失败 60 秒缓存，不为每个模型重复下载目录；取消继续传播。

公开目录查询忽略两侧 vendor 前缀，只精确比较最后一个斜杠后的 model_id。多个命中只在 context_length 全部有效且一致时采用；不模糊匹配版本、别名或 free 等变体。部署元数据和 model_info 仍按完整 ID 匹配，实际请求的模型 ID 不变。若最终仍未知，规划回退日志标明实际 provider 和模型。优先级是显式覆盖 > 部署元数据 > 公开目录标称值 > 既有静态/保守回退。公开目录标称窗口不能证明自建网关实际限制，采用时必须 WARNING 标记 deployment unverified，保留实际超限处理。前文“不跨端点套用”的约束指不能冒充部署实际值，不禁止这个明确标注的补充来源。

发送前估算下一轮输入：优先使用上一轮服务端 prompt_tokens，加上上一轮回复和当前实际 JSON 请求的估算；缺失 usage 时本地估算所有消息。估算考虑 ASCII/多字节文本，但不是各模型的精确分词器。门槛为窗口的 80%，显式 max_tokens/max_completion_tokens 要求更大输出空间时相应提前。

主翻译保留完整 system（含风格、简介与术语库）和原样当前请求；历史只使用实际校验通过的最近至多 128 对整句原译文，按预算从近到远挑选并恢复时间顺序，不保留旧 RAG 或 split_events。压缩后尽量回到窗口的 60% 以下；没有余量则不携带历史。不把摘要写入工作 JSON，不影响事件、时间或取消语义。窗口及策略纳入翻译规划身份。日志只给估算量、窗口及保留句数。

共享 SDK 入口同时覆盖 create 和 ask_validated，因此 glossary/Tavily、查询意图/查询翻译、glossary finalizer、source_review、翻译、校对（含独立命令）、简介/章节翻译均执行预算检查。非翻译阶段移除完整旧 user/assistant 轮次，不拆活动工具调用/结果组。格式纠错的原始请求和反馈一并保留，工具 schema/extra_body 参与本地估算。SDK usage 仅在历史前缀仍匹配时用于后续估算。自定义 request_kwargs.messages 不擅自修改，记录跳过压缩的警告。

服务端仍报告上下文超限时，共享入口可去除可移除的旧历史并额外重试一次；无历史不重复相同超限请求。原有阶段有界重试、校对缩批/去 RAG 和失败保留策略继续存在。若完整 system 和当前单次正文自身已经太大，日志告警，不静默截断任何正文/工具证据；这包括全文 glossary/元数据和只有一个 user 的工具循环，不能声称压缩能解决这类请求。该类需降低对应批量或使用更大窗口；自动分块/摘要另属阶段协议变更。本次不增加摘要 LLM、不改字幕输出，不改变 embedding 串行单文本协议。此变更涉及会话与缓存契约，需实现 PR 评审。
