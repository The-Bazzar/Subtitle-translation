# 命令行与环境配置

## 查看帮助

```powershell
subtitle-translation help
subtitle-translation translate --help
```

help 打印全部子命令的真实参数帮助，不运行模型、下载、安装或初始化配置。--help / --version 属于元信息操作，仍保留。

## 命令行只描述本次项目

| 命令 | 保留的项目参数 |
|---|---|
| pipeline | URL、--video、--json、--existing-ass、--output、--source-lang/--lang、--target-lang、--project-dir |
| batch | URLs、--report、--source-lang/--lang、--target-lang |
| translate | JSON、--video、--template、--output、--glossary、--source-lang/--lang、--target-lang、--only-scenes、--only-glossary、--only-output-path |
| download | URL、--project-dir |
| prepare-video | 原片路径、--project-dir |
| whisper | 视频路径、--lang/--source-lang、--project-dir |
| burn | 视频/字幕路径、--sub-file、--output、--project-dir |
| merge-ass | 两份 ASS 路径、--output |
| init | --directory |
| setup-mfa | Windows 默认原生；--distribution 显式选择已有 WSL 发行版 |

全局 --project-dir 仍用于选择配置根，不改变输出根。语言等项目参数可覆盖配置对应默认值。only 模式保留：显式选择 only-scenes/only-glossary 时执行所选阶段，不因 pipeline 对应 skip 配置而变成空操作。内部候选工作 JSON 路径仍供 batch 协调器使用。

## 开关和调优全部从环境配置读取

以下参数不再解析：--mfa/--no-mfa、--proofread/--no-proofread、--no-split、所有 --skip-*、--quiet、--force、--dry-run、--batch-size、分段/场景调优参数、provider/model/device/编码器调优和任意额外 burn argv。旧参数直接报错，不提供静默兼容。

| 配置 | 默认值 | 作用 |
|---|---|---|
| MFA_ENABLED | 模板 1 | 默认常开翻译后窗口化 MFA，需要已准备的工具环境；0 为 ASR 词轴兼容模式 |
| PROOFREAD | 模板 0 | 纯译文校对；既有配置不覆盖 |
| SPLIT_ENABLED | 1 | 翻译同回合语义分段，0 保留整句 |
| TRANSLATE_BATCH_SIZE | 50 | 每次请求完整句段数 |
| TRANSLATE_CONTEXT_WINDOW | 1 | 邻接源文上下文范围 |
| SCENE_THRESHOLD | 0.15 | 场景检测阈值 |
| SCENE_MIN_INTERVAL_FRAMES | 2 | 最小场景变化间隔 |
| LOG_LEVEL | INFO | 控制台和文件统一按 TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL 阈值过滤 |
| DRY_RUN | 0 | pipeline/batch/burn 仅打印计划/命令 |
| FORCE_REBUILD | 0 | 重建工作缓存并重新规划/翻译；使用后应恢复 0 |
| GLOSSARY_REUSE | 0 | only-glossary 默认重建，设 1 复用；batch 内部按阶段使用 |
| PIPELINE_SKIP_* | 见模板 | DOWNLOAD/WHISPER/SCENE_DETECTION/KNOWLEDGE/TRANSLATE/BURN，原 batch skip 输入限制保留 |
| WHISPER_MODEL / WHISPER_ALIGN_MODEL / WHISPER_DEVICE | 见模板 | ASR 模型与设备 |
| TRANSLATE_PROVIDER / TRANSLATE_MODEL | 见模板 | 翻译模型 |
| BURN_OVC / BURN_OVCOPTS / BURN_OAC / BURN_RES | 见模板 | 编码设置 |
| BURN_BACKEND | ffmpeg | ffmpeg 或 mpv |
| BURN_EXTRA_ARGS_JSON | [] | 字符串 argv 数组，不经过 shell |

TRANSLATE_BATCH_SIZE 必须大于 0；TRANSLATE_CONTEXT_WINDOW 和 SCENE_MIN_INTERVAL_FRAMES 不得为负数；SCENE_THRESHOLD 必须是 0 到 1 之间的有限数。无效数值在执行媒体或模型阶段前报错，不静默截断或接受 NaN。

`.env` 是常规配置入口，进程环境仍按原有优先级覆盖。已有本地值不被 setup/init 覆盖，缺失键由 setup 补齐；包内 examples/.env.example 是唯一示例源。修改配置不要删除字幕或 ASR 缓存；模型/prompt 或输入身份变化才失效相应产物，日志设置不参与语义身份。

## 内部阶段调用

pipeline/batch 不再拼接已删除的开关 argv，而将任务配置快照通过私有 stage_overrides 映射传给 translate。batch 仍按原 admission/取消/原子 publish 规则调度；阶段内 skip/reuse 只是协调器决定，不对用户暴露另一个开关通道，不通过全局临时改环境反复切换任务状态。

本变更涉及 D2/D3/D6/D7 的入口、阶段协议、配置和缓存边界，需实现 PR 评审。only 模式、配置根、输出根、generation 与模型安装策略不变；不修改外部维护的 prompt example 文案。
