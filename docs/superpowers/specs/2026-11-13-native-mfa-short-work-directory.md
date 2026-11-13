# Windows 原生 MFA 短执行目录

状态：2.1 开发实现；涉及 D3/D6/D7 的工具、诊断与缓存边界，需 PR 评审。

## 问题

完整项目 SHA-256、corpus SHA-256 和 generation UUID 叠加后，数据库路径可能超过
Windows 原生 sqlite3 CLI 可打开的长度；CPython 能创建数据库并不代表独立 SQLite CLI 能打开。
错误发生于 MFA 汇总词/音素时间结果，不应通过重新翻译、切换 ASR 或截短标识修复。

## 执行与缓存分离

- Windows 原生在 runtime.work_directory/r/<完整 UUID> 建独立执行目录；若过长，则使用系统临时目录下 subtitle-mfa/r/<完整 UUID>。
- 执行目录为后续派生路径预留 100 字符，总长度预算 240。候选位置均不满足时明确失败，不先启动昂贵对齐。
- corpus、MFA_ROOT_DIR、SQLite/特征临时目录及 TextGrid 全部使用短路径，仍一次 align、一次导出、num_jobs=1。
- 探测、输入/模型快照、导出 JSON、alignment.json、摘要和 current.json 保留原缓存位置与完整身份。窗口文件名保留完整 64 位十六进制 ID。
- generation/execution.json 记录执行目录供诊断，不参与声学/语义身份。临时物理路径不是新的模型参数。
- 缓存命中仅探测一次环境，不分配新执行目录。失败或取消不发布成功缓存，执行目录保留便于诊断；不递归删除已有数据。
- Linux/WSL 执行目录不变。不改 translation key、定稿、词映射、offset、batch generation、资源 admission 或原子发布规则。

## 验证

回归覆盖原生路径预算、完整窗口/缓存标识、同一 corpus 执行、命中不创建执行目录、过长配置回退及 Linux/WSL 不变。
真实 SQLite 验证需比较同一数据库的长路径与短路径只读打开；真实 corpus 还需确认导出和缓存复跑成功。
