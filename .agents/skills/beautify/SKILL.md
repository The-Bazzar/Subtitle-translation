---
name: beautify
description: 视频场景检测，只生成 sidecar，不修改字幕时间
platform: Win + Linux
---

# 视频场景检测

技能名保留兼容，不再执行时间轴美化或吸附。

```bash
subtitle-translation translate video.json --video video.mkv --only-scenes
```

SCENE_THRESHOLD / SCENE_MIN_INTERVAL_FRAMES 在 .env 调优，PIPELINE_SKIP_SCENE_DETECTION 控制是否跳过检测；命令行仅保留项目参数和 only 模式。subtitle-translation help 查看所有命令帮助。

输出 .scenes.json 和 .scenechange.txt。内部 .beautified.json 名称保留，但不代表修改时间。检测不调用 ASR、翻译、MFA 或校对，不修改任何字幕词/事件时间。
