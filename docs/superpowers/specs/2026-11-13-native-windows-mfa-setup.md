# Windows 原生 MFA 安装

状态：2.1 开发实现；涉及 D6/D10 工具安装和平台边界，需 PR 评审。

Windows x86_64 的 setup-mfa 默认原生；setup.ps1 -InstallMfa 只转发该 Python 入口。
显式 --distribution NAME 保留 WSL，Linux 不变。不自动安装或静默回退到 WSL。
安装与 MFA_ENABLED 分离，不改已有 .env、WhisperX 或项目 CLI 环境。

资源根为 LOCALAPPDATA/subtitle-translation。固定 micromamba 2.10.0 win-64，
仅提取 Library/bin/micromamba.exe 普通文件，限制大小并原子发布。
conda-forge 安装 Python 3.11、MFA 3.4.3、CPU Kaldi 和 Hub<1；
官方 CPU wheels 安装 torch/torchaudio 2.8.0，SpeechBrain 固定 1.0.3。
VAD 使用 COPY，不要求符号链接权限。模型版本、指纹与离线运行规则不变。
固定 VAD 模型没有 custom.py，使用公开 YAML/Pretrainer/VAD 接口加载权重，
不走 from_hparams 的可选自定义模块抓取，不写占位文件；完整本地快照优先复用。
所有工具子进程禁用用户 site-packages，并清除继承的 PYTHONPATH/PYTHONHOME，
避免用户全局依赖覆盖工具环境。原生与 WSL 的隔离规则一致，含空格路径仍按 argv/Path 处理。

通过 micromamba run 和绝对路径调用，不初始化 shell、不要求手工 PATH。
安装环境变量按子进程传递，不经 shell 拼接。runtime 使用既有 native transport，
沿用 corpus、praatio、CPU/超时/取消与缓存校验。
原生与 WSL 资源隔离，切换只更新项目 manifest，不删除旧环境或缓存。
失败不得替换已就绪 manifest；环境内部安装不具有事务回滚保证。

测试 mock 下载、模型和进程，覆盖 Windows archive/路径、CPU wheels、无 WSL 依赖、
VAD COPY 与注册。依赖解算不等于模型加载或声学验收，完整原生安装与真实对齐需独立验收。

参考：[MFA 官方安装说明](https://montreal-forced-aligner.readthedocs.io/en/stable/installation.html)。
