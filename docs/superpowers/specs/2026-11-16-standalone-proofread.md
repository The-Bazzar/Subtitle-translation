# 独立校对与字幕输出契约

状态：v2.1.1；涉及 D2/D3/D6/D7 的 CLI、工作数据、缓存及输出契约，需要实现 PR 评审。

## 命令与输入

```text
subtitle-translation proofread video.beautified.json
subtitle-translation proofread video.beautified.json --output-json reviewed.beautified.json
subtitle-translation proofread video.beautified.json --output custom.en-zh.ass
```

输入是明确的已翻译、已定时工作 JSON，不从相邻原始 ASR 文件推断，也不解析 ASS/SRT 恢复双语关联。要求 json-first-transcript 标记、唯一整数句段 ID、整句译文、有效规划/翻译身份，以及包含稳定 ID/源文范围的非空双语事件。源片段必须与冻结原文匹配；时间必须有限、有序且非 pending。缺失字段或旧扁平结构需先运行 translate，不能由校对补造时间、ID 或译文。

新翻译工作结果保存 subtitle_languages.source/target。独立校对以其为默认，显式语言参数必须匹配；不因当前环境已切换目标语言而误改保存的译文。旧工作文件缺少语言对时按 CLI > .env > 原始 language/默认 zh 解析并警告，无法自动证明旧译文的实际语言，应显式指定。旧文件只有完整新事件结构才能进入。

独立命令每次清空工作副本的校对命中条件后重新请求，不受 PROOFREAD、MFA_ENABLED、FORCE_REBUILD 或 pipeline skip 开关控制；无须媒体、ASR 模型或 MFA runtime。模型按原有 PROOFREAD_PROVIDER/MODEL 回退规则解析，专用 provider 可独立于 TRANSLATE_PROVIDER 使用。批量、RAG、共享会话、重试/超限和失败保留原译文策略复用现有纯译文校对，不增加翻译或隐式 split 请求。

## 输出与命名

- 默认原位更新输入工作 JSON；需要保留上次结果时使用 --output-json。该参数只接收 JSON；--output 只指定双语 ASS。
- 默认字幕目录/基名跟随输出 JSON，.beautified 只作为工作文件后缀剥离。glossary、简介语境、RAG 和日志仍属于输入项目；不在另存目录搜另一个项目的术语。
- 输出源/目标/双语 ASS 和检查 SRT。源文 ASS 恒为 .<source>.ass，例如 .en.ass；目标为 .zh.ass，双语为 .en-zh.ass。是否执行可选校对不改变这些名称。
- 不再生成 .en.proofread.ass，也不自动删除、改名或同步旧路径。播放器或外部脚本需使用新规范路径。
- 不重新生成 glossary/采用稿、ASR、语义边界、MFA/场景或目标 description/章节；不导入另一个阶段的文本检查点。

## 数据与发布

只更新整句 translation、事件目标文本和成功项 proofread_key。直接在原始 JSON payload 上修补这些字段，保留其他根/句段/事件字段及原有时间精度，不能通过反序列化/全量重建丢失扩展字段。新增语言对；沿用派生文件去除旧根 word_segments 的约定，正式 segments[].words[] 不改。模型未知/非目标字段仍无效，冻结状态在发布前复核。

模板、输入结构、输出扩展名/路径碰撞和目标目录在请求前验证；不允许 --output-json 覆盖一个已有原始 ASR 或非工作 JSON。输入及另存目标记录初始 hash，发布前必须仍匹配。所有字幕先在各自目标文件系统的临时目录渲染，再逐文件原子替换，JSON 最后发布；运行期异常/取消尽力回滚已替换项并清理暂存文件。进程被强制终止或机器断电仍不具有跨多个文件的全局事务保证；回滚 I/O 失败必须记录错误，不声称成功。

原位改写时，只有原有定时投影 manifest 的 digest 确认匹配输入，才保留其 key 并刷新结果 digest，使后续内部续跑能读取更新后的校对结果。无效/缺失 manifest 不创建或冒充命中；另存不改原项目 manifest。文本检查点和原生 MFA corpus 不改，batch generation/锁/发布协议不变。manifest 刷新失败则警告，下次自然视作 miss。

日志沿用输入 JSON 目录的 subtitle-translation.log，每轮覆盖，ANSI 仅控制台；发布后逐事件输出采用文字/ID/状态/原时间，正常为 INFO，保留原译文为 WARNING。不输出 prompt、glossary、检索证据或密钥。

## 验证

mock LLM/工具，验证开关关闭仍执行、两次显式重校、专用校对入口与上游隔离、原位/另存和双语 ASS 路径、精确字段保留、旧文件语言回退/显式冲突、无效/pending 输入、模板/路径错误、取消、输入/目标竞争修改、渲染/发布回滚和定时投影 digest。CLI help 包含真实 proofread 帮助；普通 translate 在关闭校对时也只生成规范源文 ASS。
