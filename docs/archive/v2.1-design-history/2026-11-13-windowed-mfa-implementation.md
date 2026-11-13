# 后置窗口化 MFA 首版实施边界

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

当前不再执行时间轴美化、显示补时长/间隔或场景吸附；仅保留只读场景检测，以下早期显示处理记录由[最新设计](../../superpowers/specs/2026-11-13-scene-detection-only.md)取代。MFA 原时间、粗时间重叠协调及旧词轴清理不变。

状态：codex/v2.1.0 开发实现，不是默认启用或发布质量声明。对应[目标设计](2026-11-13-post-translation-windowed-mfa-design.md)，涉及 D1/D2/D3/D6/D7 的数据、顺序、缓存、协议和职责变化，需 PR 评审。

当前语义阶段已更新为[同回合双语翻译/断句](../../superpowers/specs/2026-11-13-semantic-bilingual-translation.md)，不再单独调用 split 或预填英文事件；以下文字保留窗口化首版实施记录，窗口声学保护与批量 corpus 规则仍适用。

## 当前接通的路径

显式 translate/pipeline/batch --mfa：glossary/source_review -> 无词轴 LLM 文本断句 -> 单回合整句翻译/事件分配 -> 窗口化 MFA -> 时间微调 -> 可选纯译文校对 -> ASS。默认无 --mfa 路径不变。only-glossary/only-beautify 仍保留专用入口语义，不触发整条新路径。

`windowed_mfa.py` 用完整采用稿及 pySBD 字符位置组织翻译上下文。事件先只保存 ID、segment-local source_range 和源文/译文，timing_status=pending，不写 start/end，也不能导出。原 ASR words 不参与规划。文本检查点独立存于 `.asr_work/text_events/<identity>/translated.json`，与最终 .beautified.json 分开。

按句段来源映射合并共享或跨越的连续 ASR 单元，携带全部对应定稿。首版最大 60 秒/12 源单元，零额外 padding，无自动循环扩窗。超限保留原句双语内容作来源粗时间投影；不级联合并为全片。规范 WAV 按 sample 裁剪，窗口局部时间只加回一次 sample 偏移。

MFA 桥接当前使用 direct-window-corpus-align-v1：全部有效窗口以独立 WAV/.lab 加入同一项目 corpus，一次 align、一次批量导出，不执行全文 segment。共享一次模型初始化和说话人自适应，缓存绑定整个有序 corpus、模型及环境，不复用旧单窗口声学结果。固定 num_jobs=1、CPU/offline 环境和项目总预算不变；不吞取消。见[批量执行设计](../../superpowers/specs/2026-11-13-windowed-mfa-corpus-batching.md)。

词序/必要词/局部范围通过后，单词时长大于 2 秒或相邻词空洞大于 3 秒保守降级。这些是版本化的初始工程阈值，不是置信度或声学正确性证明。预选边界切入不可分离的词映射也不插值，直接投影为完整双语粗时间事件。失败只影响对应语句，不移动后续窗口的锚点。

后置显示修复见[输出与显示时间设计](2026-11-13-mfa-output-postprocess-fixes.md)：重叠粗时间语句协调为完整双语输出，派生 JSON 移除旧根级词轴。beautify 应用最小时长/间隔和短间隙延展，只改显示留白，不移动词时间或截断语音；显示可超出粗搜索范围但受邻接字幕、场景与媒体边界限制。coarse 不移动，条件不足仅日志提示。定时缓存绑定后处理版本及文件 SHA，纯译文校对仍只读源文/事件。

## 验证与限制

自动测试 mock 外部 LLM/ASR/MFA/ffmpeg，覆盖无 ASR words 规划、pending 序列化/禁止导出、跨段合并、窗口上限、单次 sample 偏移、局部失败隔离、异常大空洞、不可分离映射、翻译整句回退、文本/定时缓存、beautify 保留事件、取消和项目预算。

真实 Windows/WSL MFA 短样本已走通：11.765 秒、27 个词、2 条双语 ASS 事件；第二次只做一次环境探测，没有重复对齐或语义请求。语义响应使用 schema fixtures，不是自然翻译质量验收。额外的局部粗时间锚点回放不能替代全片人工边界基准。

逐窗口重复启动已替换为项目 corpus 批量 align。真实 4 窗口结构验证使用同一公开短样本的四份独立输入：总计 108 个词，一次 native align，首次全流程约 35.7 秒，缓存约 1.7 秒；这不是四种音频的质量集，也不外推为固定倍数加速。完整长片性能/质量仍需验收。上下文 padding 尚未实现，当前仅使用完整来源单元作为块内上下文。未验证其他语言/平台 profile，不宣称消除所有局部错轴。

多引擎接入、默认切换和是否删除纯译文校对仍独立推进。未覆盖的用户媒体/字幕不自动重写，不删除旧缓存；迁移首次运行可能因新文本事件身份重新请求语义模型。
