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
| proofread | 已完成的翻译工作 JSON、--output-json、--output、--template、--glossary、--source-lang/--lang、--target-lang |
| download | URL、--project-dir |
| prepare-video | 原片路径、--project-dir |
| whisper | 视频路径、--lang/--source-lang、--project-dir |
| burn | 视频/字幕路径、--sub-file、--output、--project-dir |
| merge-ass | 两份 ASS 路径、--output |
| init | --directory |
| setup-mfa | Windows 默认原生；--distribution 显式选择已有 WSL 发行版 |

全局 --project-dir 仍用于选择配置根，不改变输出根。语言等项目参数可覆盖配置对应默认值。only 模式保留：显式选择 only-scenes/only-glossary 时执行所选阶段，不因 pipeline 对应 skip 配置而变成空操作。内部候选工作 JSON 路径仍供 batch 协调器使用。

普通 subtitle-translation translate 每次重新翻译及联合断句，PROOFREAD=1 时重新校对，覆盖旧输出；不以 .beautified.json 或文本/定时检查点命中跳过这些请求，无需设置 FORCE_REBUILD。
原始 ASR 不改写，glossary/采用稿、场景、检索库和原生 MFA 声学缓存可复用；--only-* 仍只执行所选操作。pipeline/batch 的内部续跑策略不变。

proofread <base>.beautified.json 明确校对已有目标文本，每次重跑校对，不受 PROOFREAD=0 影响，也不启动翻译/对轴。默认更新输入工作 JSON；--output-json 另存 JSON，--output 单独控制双语 ASS。规范源文 ASS 为 .<source>.ass，不再带 proofread 后缀。
新工作 JSON 的 subtitle_languages 优先于环境默认语言；与已保存语言冲突的显式参数报错。旧文件无该字段时使用 CLI/.env/ASR 默认值并警告。只接受完整已定时的新结构，不从原始 ASR、ASS/SRT 或文本检查点恢复缺失字段。详见 [输入输出契约](superpowers/specs/2026-11-16-standalone-proofread.md)。

## 开关和调优全部从环境配置读取

以下参数不再解析：--mfa/--no-mfa、--proofread/--no-proofread、--no-split、所有 --skip-*、--quiet、--force、--dry-run、--batch-size、分段/场景调优参数、provider/model/device/编码器调优和任意额外 burn argv。旧参数直接报错，不提供静默兼容。

| 配置 | 默认值 | 作用 |
|---|---|---|
| MFA_ENABLED | 模板 1 | 默认常开翻译后窗口化 MFA，需要已准备的工具环境；0 为 ASR 词轴兼容模式 |
| PROOFREAD | 模板 0 | 翻译流程中的纯译文校对；不控制显式 proofread 命令；既有配置不覆盖 |
| SPLIT_ENABLED | 1 | 翻译同回合语义分段，0 保留整句 |
| TRANSLATE_BATCH_SIZE | 20 | 每次请求完整句段数；已有显式配置不覆盖 |
| PROOFREAD_BATCH_SIZE | TRANSLATE_BATCH_SIZE 的一半，至少 1 | 按 event 数量预算组织完整句；单句不拆开，允许超预算 |
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

TRANSLATE_BATCH_SIZE 必须大于 0；SCENE_MIN_INTERVAL_FRAMES 不得为负数；SCENE_THRESHOLD 必须是 0 到 1 之间的有限数。无效数值在执行媒体或模型阶段前报错，不静默截断或接受 NaN。
TRANSLATE_CONTEXT_WINDOW 已删除，翻译按项目共享会话历史，RAG 使用 EMBEDDING_TOP_K，不再为每项添加邻句字段。

`.env` 是常规配置入口，进程环境仍按原有优先级覆盖。已有本地值不被 setup/init 覆盖，缺失键由 setup 补齐；包内 examples/.env.example 是唯一示例源。修改配置不要删除字幕或 ASR 缓存；模型/prompt 或输入身份变化才失效相应产物，日志设置不参与语义身份。

## 内部阶段调用

模型元数据不是 .env 配置。在 providers.json 的对应 provider 下可选填写：

```json
{
  "model_info": [
    {"id": "my-deployed-model", "context_length": 65536},
    {"id": "another-model", "context_length": null}
  ]
}
```

列表缺省/为空或 context_length 缺省/null 时自动查询；正整数用于覆盖当前部署窗口，按实际请求模型 ID 精确匹配，重复 ID/非法窗口报错。各 provider 相互独立，model_info 不是模型白名单，也不改变 default_model 或模型选择优先级。
正常使用无需填写列表。程序内部查询部署元数据，缺失时匿名查询 [OpenRouter 公开目录](https://openrouter.ai/api/v1/models)；可直接在该目录查看 id/context_length。仅公开目录查窗口时忽略 vendor 前缀；版本和变体仍精确匹配，候选窗口不一致则回退。实际发送的模型 ID 不改写，不会自动开通模型或提供 API key。用户仍需 BYOK，不新增 models 命令。未知模型首次成功查询后，仅将窗口和来源缓存到配置根 providers.json 的对应 provider/model_info；已有有效项不被覆盖。自动条目换端点失效，可删除该项或其 context_length 后重启刷新。完整格式见包内 providers.example.json。

pipeline/batch 不再拼接已删除的开关 argv，而将任务配置快照通过私有 stage_overrides 映射传给 translate。batch 仍按原 admission/取消/原子 publish 规则调度；阶段内 skip/reuse 只是协调器决定，不对用户暴露另一个开关通道，不通过全局临时改环境反复切换任务状态。
显式 translate 的重新翻译策略由 CLI 分派内部传递，不使用 FORCE_REBUILD 代替，因此不会附带强制重建 glossary/采用稿或清除声学缓存。

本变更涉及 D2/D3/D6/D7 的入口、阶段协议、配置和缓存边界，需实现 PR 评审。only 模式、配置根、输出根、generation 与模型安装策略不变；不修改外部维护的 prompt example 文案。
