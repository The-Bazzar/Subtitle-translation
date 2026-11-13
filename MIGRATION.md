# Migration

## 2.1.0

多 ASR/多语言 MFA profile 不在本版发布范围，
2.1 重点是窗口 corpus MFA、联合双语翻译/断句、只读场景检测及配置入口收敛。

### CLI 与配置

subtitle-translation help 打印全部真实子命令帮助。
CLI 只保留输入/输出路径、URL、语言、配置根和 --only-*；--help/--version 元信息保留。
功能开关与调优参数全部从 .env 读取，旧参数会直接报错。
--print-output-path 改为 --only-output-path。
完整字段见 [配置对照表](docs/cli-and-env.md)。

| 旧参数 | .env 配置 |
|---|---|
| --mfa / --no-mfa | MFA_ENABLED=1 / 0 |
| --proofread / --no-proofread | PROOFREAD=1 / 0 |
| --no-split | SPLIT_ENABLED=0 |
| --skip-* | PIPELINE_SKIP_* |
| --batch-size | TRANSLATE_BATCH_SIZE |
| --split-context-window | TRANSLATE_CONTEXT_WINDOW |
| --quiet | 已删除；通过 LOG_LEVEL 控制日志 |
| --dry-run / --force | DRY_RUN / FORCE_REBUILD |
| --model / --align-model / --device | WHISPER_MODEL / WHISPER_ALIGN_MODEL / WHISPER_DEVICE |
| --translate-provider / --translate-model | TRANSLATE_PROVIDER / TRANSLATE_MODEL |
| 场景检测调优 | SCENE_THRESHOLD / SCENE_MIN_INTERVAL_FRAMES |
| 编码设置 / 额外 argv | BURN_* / BURN_EXTRA_ARGS_JSON |

旧 --split-max-chars/duration 及吸附、帧偏移、补时长/间隔等参数已删除，不提供新等价约束。
--only-scenes / --only-glossary 保留，显式选择阶段时不受对应 pipeline skip 设置屏蔽。
GLOSSARY_REUSE=1 可让 only-glossary 复用已有术语；默认 0 强制重建。
内部 batch/pipeline 传递配置快照，不再拼接旧开关 argv。
translate 日志新增逐句段/event 明细，包含轮次、编号、稳定 ID、状态和当前时间；MFA 前显示 pending。
每条采用的 event（含缓存）完整记录原文/译文。QUIET 及其内部参数/输出分支已删除；正常 split/unsplit 为 INFO，异常状态为 WARNING，任务级错误为 ERROR。
缓存注明 cache=hit，不伪造 LLM 轮次；不记录未采用回复或提示词，不改变缓存身份。
应用日志改用 Loguru，带时间/时区、级别、项目和阶段；LOG_LEVEL 默认 INFO，统一控制终端及文件过滤。
控制台固定开启 ANSI 配色（包括重定向输出），日志文件不含 ANSI，不提供颜色开关或环境变量。
单项目的 subtitle-translation.log 位于视频目录（无 --video 时为 JSON 目录）；batch 全部集中在执行目录 batch-result.log。
每次外部 run 覆盖，不在内部阶段重复清空，不轮转；batch worker 故障诊断也汇入中央文件，报告及 generation 协议不变。
旧 ffmpeg-burn/mpv-burn wrapper 和 launcher 别名均转发到 burn，不再注入 --backend；
后端统一由 BURN_BACKEND 选择，脚本名称不覆盖配置。

setup/init 不覆盖已有配置值，setup 只追加缺失键。
模板 PROOFREAD=0、MFA_ENABLED=1、PIPELINE_SKIP_BURN=1，默认常开 MFA。
setup/init 不覆盖其他用户已有的 MFA_ENABLED；彻底缺失该键时仍沿用兼容关闭回退，setup 会补齐缺失键。
旧 .env 中已有的 PROOFREAD=1 不会自动变成 0；需要关闭时自行修改。
变量缺失时的既有兼容回退不变。
PIPELINE_SKIP_BEAUTIFY 改为 PIPELINE_SKIP_SCENE_DETECTION，其他新增键从模板补齐。

### 翻译与对轴

独立 split 阶段移除，translate 一次返回整句译文及成对源/目标片段。
所有句段参与，不按 72 字符跳过，不让模型计算 ID/坐标/时间或严格拼接等式。
本地定位源文，源文不可定位或片段无效但整句译文有效时保留整句；完全无译文才重试/失败。
校对只改译文，不改源文、ID、数量或时间，也不要求目标字符串严格拼接相等。

MFA_ENABLED=1 使用 glossary/source_review -> 联合 translate -> 窗口 corpus MFA -> 场景检测 -> 可选校对。
显式 MFA_ENABLED=0 继续使用 ASR 词轴。当前 MFA 仅验证英文源文，启用前需准备 runtime 和媒体。
Windows x86_64 的 setup-mfa 默认原生安装；setup.ps1 -InstallMfa 使用同一入口。
显式 --distribution NAME 时才使用已有 WSL，Linux x86_64 本机；开关不自动安装环境。
原生资源位于 LOCALAPPDATA/subtitle-translation，不迁移或删除已有 WSL 环境。
Windows 原生的 corpus/SQLite/特征/TextGrid 改在独立短执行目录运行，避免深层缓存目录触发 SQLite CLI 路径限制；完整项目/窗口/corpus 哈希和缓存身份不截短、不改写，Linux/WSL 布局不变。
缓存命中仅探测环境，不创建新的执行目录；失败不发布成功缓存，旧缓存/诊断保留。本次修复不要求重译正文。
修复 VAD 本地快照加载时误取可选 custom.py 的失败；固定模型仅加载 YAML/权重，COPY 方式不要求符号链接权限。
完整本地快照优先复用，缺项才下载。setup 与对齐工具设置 PYTHONNOUSERSITE=1，清除继承的 PYTHONPATH/PYTHONHOME，不修改父进程或用户全局环境。
旧前置全文 segment、逐窗口独立 MFA 和 partial 重读入口已移除。

场景检测只生成 sidecar，不改变词/句段/事件时间；不再美化或缩放。
.beautified.json 仅保留命名兼容。派生输出只使用 segments[].words[]，
根级旧 word_segments 会清理；原始 JSON 不改。粗时间失败重叠输出协调成完整双语字幕。
ASS/SRT 时间序列化修复小数进位，避免 ASS 出现 .100，不改变内部对齐时间。

### 缓存和恢复

- 旧固定事件/独立断句译文不能冒充联合协议完成，首次切换可能重译。
- 实际文本、窗口和声学依赖不变时可复用原生 corpus，重新投影事件。
- 场景设置变化只刷新 sidecar，不失效有效译文或字幕时间。
- 旧被吸附/补显示留白的定时缓存使用新策略从原始 ASR 或 MFA 结果重建。
- 删除旧未使用的 Python helper 不保留其导入接口；正式 CLI 和原始数据格式继续支持。
- 安装资源/profile/解析器变化可能令旧声学缓存 miss，不删除旧文件，不伪造成功。
- 原 batch sidecar、锁、generation、candidate、取消与原子 publish 协议不变。

不要为迁移批量删除媒体、识别 JSON 或术语库。
旧 proofread_text 仅兼容读取，不能作为源文覆盖项。
本地配置、runtime、模型、真实媒体和项目产物禁止提交。

## 2.0.2 保留行为

同名 <base>.original.mkv 的章节通过 ffprobe JSON 提取，章节标题与元数据同回合翻译，
以原片时间写入目标 description。原标题逐项兜底，无章节或探测失败不阻断其他简介翻译，
不从编辑版或其他项目回退，不修改媒体/ASR/batch generation。

embedding 建索引/查询统一逐文本字符串输入，EMBEDDING_BATCH_SIZE 不再生效。
完整 glossary 不能由检索片段替代，证据及向量按项目/generation 隔离。

download/pipeline/batch 只从执行目录读取 cookies.txt；配置根不改变输出根。
每个 yt-dlp 业务操作前清理其缓存，失败则停止该下载阶段。
安装 CLI 使用项目 .venv 与转发 shim，不要求手工 PATH 或全局 Python；
Windows 需要 PowerShell 7。

## 文档入口

当前说明见 [README](README.md)、[架构与发布范围](docs/v2.1-architecture.md)、
[CLI/环境配置](docs/cli-and-env.md)、[MFA 安装](docs/mfa-installation-preferences.md)。
被替代的阶段方案见 [历史设计归档](docs/archive/v2.1-design-history/README.md)，不作为当前功能保证。
