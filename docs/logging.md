# 运行日志

## 位置与生命周期

- 单项目：视频文件夹的 subtitle-translation.log；显式 --video 优先决定目录，否则取输入 JSON 所在目录。不是 .code 或 --project-dir 配置根。
- batch：命令执行目录的 batch-result.log，所有视频、阶段和 worker 故障诊断集中在此；不会额外创建各视频的应用日志。--report 只改变结果报告位置，不改变日志位置。
- 每次外部运行以 w 模式覆盖旧文件。内部 glossary/translate/MFA/proofread 等阶段共享本轮 sink，不再次清空。无轮转、追加或时间戳备份。
- help、only-output-path、batch dry-run 不创建日志。无法定位有效项目的早期参数错误只打印控制台。
- 同进程内禁止两轮运行同时覆盖同一项目文件；不同项目并发通过任务上下文隔离。不要启动多个独立 CLI 同时写同一日志文件。

## 格式与级别

Loguru 格式为：时间及 UTC 偏移 | LEVEL | 项目 | 阶段 | 消息。
控制台始终输出 ANSI 配色：时间为绿色、项目为青色、阶段为洋红色，级别和消息按 Loguru 的级别色彩区分。
不检测 TTY、不受 NO_COLOR 关闭，不提供颜色开关或配置项；直接重定向控制台也会包含 ANSI。
两个固定日志文件使用独立纯文本 formatter，永远不包含 ANSI 控制码。
阶段包含 source_prepare、glossary、source_review、translate、mfa、scene_detection、embedding、proofread、export、metadata 和媒体处理。
阶段开始/结束记录耗时，错误/取消保留类型与原有传播语义，不把日志异常当成模型回退。

LOG_LEVEL 仅在 .env 设置，默认 INFO，接受 TRACE、DEBUG、INFO、WARNING、ERROR、CRITICAL。
控制台和 UTF-8 文件统一按此阈值过滤；文件实时刷新并在结束时关闭。
TRACE 用于缓存诊断，DEBUG 用于工具调用/响应体积。正常 split/unsplit 句段和每条 event 均为 INFO；whole_fallback、retry、failed 和未知缓存状态等异常记录为 WARNING。任务级异常仍为 ERROR。
每条采用的 event 都完整记录原文、译文、稳定 ID、status 和当前时间，不截断正文。MFA 前的 time=pending 是正常状态，不因此升级警告。
缓存命中也逐条打印并标注 cache=hit，不伪造 LLM 轮次。每轮中已通过的句段不因其他句段重试而重复打印。
QUIET 配置及内部参数/输出分支已删除，LOG_LEVEL 是控制台和文件唯一的级别入口；查看全部 event 应设为 INFO 或更低，WARNING 及以上会过滤正常事件。不修改路径 marker 或 help 等命令结果输出。

## 数据边界

路径 marker 和命令结果仍写 stdout；应用诊断写 stderr 和日志文件。
日志不参与模型 prompt、缓存身份、generation、admission 或字幕内容，不修改 ASR/MFA 时间。
不记录完整 prompt、glossary、检索上下文、未采用的 LLM 原始回复或密钥；关闭 Loguru 的异常局部变量诊断。
外部媒体工具原生输出及其工作目录内部诊断不改为应用数据协议，不接管其 stdout/stderr。
原始 worker 失败文件仍可兼容程序化独立调度器；正式 batch 的故障详情只汇入 batch-result.log。
日志可能包含项目路径及采用的字幕内容，是本地生成物，禁止提交或随安装包发布。

## 验证

mock 测试覆盖时间/级别/阶段、终端与文件一致过滤、覆盖/嵌套复用、异常回收、
异步/线程项目隔离、batch 单文件、worker 故障汇总和显式视频目录优先。
本变更涉及 D2/D6/D7 的入口、配置与诊断边界，需 PR 评审；batch 资源、锁和发布协议不变。
