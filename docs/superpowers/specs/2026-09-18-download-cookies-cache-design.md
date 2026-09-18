# 下载 Cookies 与缓存清理

状态：开发分支实现，待评审。

## 目标与边界

统一 download、pipeline 和 batch 的下载行为：cookies 来自命令执行目录，每次 yt-dlp 业务调用前清理 yt-dlp 缓存。配置根仍用于 `.env`、provider 和 template，不再决定 cookies 位置。

## 实现

`ProjectConfig.load()` 在启动时记录 `invocation_dir`；batch 的 `create_platform_runners()` 在创建 runner 时记录当前目录。公共 `download_video()` 使用 `config.output_dir / "cookies.txt"`，不在任务运行时重新读取进程 cwd，也不使用新建的视频子目录。

存在 cookies 文件时，标题查询和下载/元数据刷新都传入该路径。不存在时省略 `--cookies`，不从配置根或安装目录寻找替代文件。本实现不改变 yt-dlp 自身配置文件的加载规则。

调用顺序：

```text
解析 yt-dlp executable 和执行目录 cookies
  -> yt-dlp --rm-cache-dir
  -> yt-dlp [cookies] --get-title URL
  -> 确定输出目录及是否复用原片
  -> yt-dlp --rm-cache-dir
  -> yt-dlp [cookies] 下载或 --skip-download 刷新元数据
```

清理与后续业务命令使用相同 executable 和 cwd。清理通过既有 `run_command()` 执行，继承控制台输出和活动进程管理，不引入 shell 命令拼接。清理操作本身不再触发额外清理。

## 失败与并发

- 找不到 executable 时沿用退出码 127，不执行清理。
- 任一清理失败时返回其退出码与命令诊断，不运行紧随其后的业务调用。
- 标题失败或为空时，不运行第二次清理和下载。
- 下载失败沿用现有阶段错误传播；不改动 batch 的聚合退出码和失败报告约定。
- batch 仍使用原有 CPU/IO 并发容量。清理与业务调用只保证单个任务内的先后关系，不新增跨任务或跨进程锁，也不保证其他任务不会同时写入 yt-dlp 缓存。

清理范围由 yt-dlp 决定，默认缓存可能被其他任务共享。这不是项目 ASR cache、generation sidecar 或翻译缓存的清理入口。

## 设计对齐与迁移

保持 D8 的公共 Python stage 与薄平台包装器边界。此次调整 cookies 配置来源，需同步 `AGENTS.md`、README 与 MIGRATION，并经 PR 评审。pipeline 顺序、输出根、GPU 资源规则和恢复协调协议不变。

升级时将所需 cookies 放在实际执行目录。回滚到旧版时，cookies 来源恢复为该版本的配置根；不需要迁移媒体或字幕，也不需要恢复已清空的 yt-dlp 缓存。

## 验证要求

mock 所有 yt-dlp 调用，覆盖执行目录与配置目录同时存在 cookies、仅一方存在、均不存在、路径含空格、新下载、原片存在时刷新元数据、两次清理的顺序与失败、标题和下载失败，以及真实 batch runner 到公共下载 stage 的配置传递。

运行完整 unittest 回归。测试不得访问真实 cookies、调用真实 yt-dlp 或清理主机缓存。
