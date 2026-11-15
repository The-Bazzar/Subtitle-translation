---
name: translate
description: 联合双语语义翻译、可选 MFA 对轴与 ASS 输出
platform: Win + Linux
---

# 翻译与对轴

使用原始 WhisperX JSON 为主输入，原片/编辑视频通过 --video 指定，源/目标语言通过 --lang / --target-lang 指定。subtitle-translation help 查看所有命令帮助。

```bash
subtitle-translation translate video.json --video video.mkv --lang en --target-lang zh
subtitle-translation translate video.json --only-glossary
subtitle-translation proofread video.beautified.json
```

功能开关和调优仅在 .env 设置：MFA_ENABLED、PROOFREAD、SPLIT_ENABLED、TRANSLATE_BATCH_SIZE、SCENE_THRESHOLD、SCENE_MIN_INTERVAL_FRAMES、PIPELINE_SKIP_*、LOG_LEVEL、FORCE_REBUILD。旧开关 argv 不再接受。

## 流程

- glossary/source_review 是唯一自动源文修订阶段，完整术语常驻上下文。
- translate 同一响应生成整句译文与成对源/目标字幕片段；以语义为主，不要求边界 ID、字符数、时间或严格目标拼接等式。不再读取 split_prompt 或单独请求 split。
- 模板默认 MFA_ENABLED=1，在翻译后按来源粗时间窗口批量对齐；0 才使用兼容 ASR 词轴。场景检测只输出 sidecar，不改变时间。
- 纯译文校对由 PROOFREAD 控制，不改源文、ID、数量或时间。默认模板关闭。
- 源文无法定位或对轴失败时保留整句双语内容，不强切或伪造词时间；失败粗时间重叠输出协调合并。

## 产物与恢复

.beautified.json 是兼容命名的工作 JSON，不表示改动了时间；原始 ASR JSON 不覆盖。另输出 .split.<lang>.srt、源/目标/双语 ASS 与目标 description，按当前项目 generation 复用缓存。MFA 文本检查点独立于定时缓存。
源文 ASS 为 .<source>.ass（如 .en.ass），不再生成 .<source>.proofread.ass。独立 proofread 接受已翻译且已定时的工作 JSON，默认原位更新并重导字幕；--output-json 另存 JSON，--output 控制双语 ASS。显式命令不受 PROOFREAD 开关控制，不调用上游或元数据翻译，优先读取工作 JSON 保存的 subtitle_languages。
显式 translate 每次重新翻译和联合断句，启用时重新校对，不从上述工作 JSON/文本或定时检查点恢复译文；--only-* 仍独立执行。glossary/采用稿、ASR、检索及原生 MFA 声学缓存保留，pipeline/batch 内部续跑缓存不变。

translate_prompt / proofread_prompt 风格文件仍按既有配置加载，阶段协议由代码追加；外部维护的 prompt example 不修改。检索证据不替代完整 glossary。
