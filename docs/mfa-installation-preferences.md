# MFA 安装偏好与运行配置

本文说明当前项目的 MFA 安装策略（preferences）、版本约束、目录和运行默认值。以 `core/subtitle_translation/mfa_setup.py`、`mfa.py` 及本地 `asr_runtime.json` 为准，不是 MFA 全局配置文件的副本。

当前属于 2.1.0 开发功能：模板 MFA_ENABLED=1 默认常开，0 关闭，translate/pipeline/batch 一致，不再提供 --mfa/--no-mfa。已有配置值不被安装器覆盖，完全缺失键时保留兼容关闭回退。安装与启用分离；支持 Windows x86_64 原生、Linux x86_64 或显式 Windows/WSL。

## 1. 安装方式

| 项目 | 当前偏好 |
|---|---|
| Windows 后端 | 默认原生 x86_64 独立环境，不依赖 WSL |
| 发行版选择 | 显式 --distribution NAME 才使用已有 WSL，不静默回退 |
| Linux 后端 | Linux x86_64 本机独立工具环境；安装器已有实现，尚需独立平台实测 |
| 环境管理器 | micromamba，不使用全局 conda 环境 |
| conda channel | 仅 conda-forge，使用 `--override-channels` |
| 安装隔离 | MFA 拥有自己的 Python、torch 和依赖，不装入项目 `.venv`，不复制第二套 CLI |
| 计算设备 | CPU-only；不需要 CUDA，不替换现有 WhisperX/PyTorch 后端 |
| shell / PATH | 不初始化 shell，不要求手工激活环境或修改 PATH；由绝对路径与 `micromamba run --prefix` 调用 |
| WSL 安装 | 不自动安装 WSL；没有可用发行版时明确报错 |

Windows 原生资源位于 `%LOCALAPPDATA%/subtitle-translation/`：tools/bin/micromamba.exe、mfa-3.4 环境，模型/huggingface/vad/work 子目录结构与下方 Linux 布局相同。以绝对路径及 micromamba run 准备 DLL/工具搜索环境，不修改用户 PATH。显式 WSL 模式才使用 Linux 用户目录和 UNC 路径转换。

## 2. 版本约束

实装快照核对日期：2026-11-11。固定版本指安装器明确要求的版本；范围约束或间接依赖不等同于全依赖锁定。

| 组件 | 安装约束 | 当前实装快照 |
|---|---|---|
| micromamba | 固定 `2.10.0` | `2.10.0` |
| MFA | 固定 `3.4.3` | `3.4.3` |
| 工具 Python | `python=3.11`，未固定补丁版 | `3.11.18` |
| Kaldi | `kaldi=*=cpu*`，未固定完整版本/build | 按 conda 解算结果安装 CPU build |
| PyTorch | `pytorch=2.8.0=cpu*` | `2.8.0`，`torch.version.cuda` 为 `None` |
| torchaudio | `torchaudio=2.8.0=cpu*` | `2.8.0` |
| SpeechBrain | 固定 `1.0.3`，通过工具环境内 pip 安装 | `1.0.3` |
| Hugging Face Hub | `huggingface_hub<1`，要求兼容 0.x | `0.36.0` |
| praatio | MFA 环境依赖，未在安装器单独固定 | `6.2.2` |

实际 conda 规格：

```text
python=3.11
montreal-forced-aligner=3.4.3
kaldi=*=cpu*
pytorch=2.8.0=cpu*
torchaudio=2.8.0=cpu*
huggingface_hub<1
```

随后在该工具环境内执行 `python -m pip install speechbrain==1.0.3`（仅缺失或版本不符时）。pip 使用其当前配置的索引；代码没有另外指定 PyPI 镜像。

上方 conda 规格用于 Linux/WSL。Windows 原生使用 Python 3.11、MFA 3.4.3、CPU Kaldi、Hub<1 和 pip；torch==2.8.0、torchaudio==2.8.0 从 https://download.pytorch.org/whl/cpu 安装，不依赖缺失的 conda-forge Windows torchaudio 2.8.0 build。安装后重验依赖版本；VAD 使用 LocalStrategy.COPY，不要求符号链接权限。

这些约束不是完整的 conda-lock 文件：传递依赖和部分 build 仍由解算器选择。运行探测会记录 Python、已安装 distributions 及 conda 包版本/build/校验信息的环境指纹，依赖变化后不会盲目复用旧对齐缓存。

## 3. 模型与 VAD

| 资源 | 当前选择 | 准备策略 |
|---|---|---|
| 英文声学模型 | `english_mfa`，发布版本 `v3.0.0` | 官方固定 release 中的 `english_mfa.zip` |
| 美式英语词典 | `english_us_mfa`，发布版本 `v3.0.0` | 官方固定 release 中的 `english_us_mfa.dict` |
| VAD 模型 | `speechbrain/vad-crdnn-libriparty` | 安装时下载并加载，记录实际 HF revision 和文件集合指纹 |
| 对齐语言 | 当前 profile 为 `en` | 尚不代表其他源语言已经支持或验收 |

声学模型与词典不使用自动追踪 latest 的下载方式，也不通过模型管理器枚举 GitHub release 列表。安装器检查固定资源大小、ZIP 完整性及本地模型加载情况；实际内容 SHA-256 用于运行身份和缓存判断。这不是发布者签名或完整供应链锁定机制。

VAD 首次准备使用 HF `main` 解析得到一个具体 revision，写入 `vad_manifest.json` 和 runtime。后续有效缓存直接复用，不在每个任务中跟随远端 main 更新。它用于音频分块，不是第二份 ASR 听录，也不判断原文是否真实正确。

安装阶段允许下载依赖和模型；正式对齐调用设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，使用已经准备好的资源，不在“对齐中”临时补下载。
VAD 先检查本地快照是否包含非空 hyperparams.yaml、model.ckpt、mean_var_norm.ckpt，完整则不访问 HF 网络，缺项才下载。
固定模型没有 custom.py；预加载通过 HyperPyYAML、Pretrainer.collect_files/load_collected 和 VAD 构造器完成，不依赖 from_hparams 的可选 Python 文件抓取，也不写占位文件。缓存与输出路径通过 argv/Path 传递，支持空格。
setup 及正式对齐均设置 PYTHONNOUSERSITE=1，清除继承的 PYTHONPATH/PYTHONHOME；WSL 子进程也显式清除，不污染父进程或全局配置。

## 4. 文件位置

下列 `$HOME` 指所选 Linux/WSL 用户的 home，不是 Windows 用户目录。目录由安装器生成，不需要用户手工填写个人绝对路径。

```text
$HOME/.local/share/subtitle-translation/
  tools/bin/micromamba
  mamba/                         # conda 包缓存
  mfa-3.4/                       # 独立 MFA Python 环境
  models/english-3.0.0/
    pretrained_models/
      acoustic/english_mfa.zip
      dictionary/english_us_mfa.dict
  huggingface/                   # 私有 HF 缓存
  vad/                           # VAD 预加载目录
  vad_manifest.json              # VAD revision / 文件指纹
  work/                          # 按项目及输入身份隔离的 MFA 工作目录
```

项目配置根（由 `--project-dir` 决定）保存 `asr_runtime.json`，其中包含 `mfa-3.4-textgrid-v1` profile、transport、发行版、工具命令、模型路径、VAD 身份及工作目录。它是本地生成物，不能提交或复制成其他机器可直接使用的公共配置。

项目输出目录的 `.asr_work/` 保存规范音频、采用稿/术语快照及派生 JSON；Linux 工具目录的 `work/` 保存 MFA 数据库、临时文件和对齐缓存。模型、音频、runtime 和上述工作目录都不进入代码提交或安装包。

## 5. 安装与使用入口

只准备 MFA，不重建现有项目 `.venv`：

```text
subtitle-translation setup-mfa
```

Windows 默认原生；需要保留 WSL 后端时才显式选用已有发行版：

```text
subtitle-translation setup-mfa --distribution Debian
```

显式选择配置根时，全局参数放在子命令之前：

```text
subtitle-translation --project-dir "<repo>" setup-mfa --distribution Debian
```

也可在完整 setup 中选择安装：

```powershell
.\scripts\setup.ps1 -InstallMfa -MfaDistribution Debian
```

```bash
./scripts/setup.sh --mfa
```

完整 setup 仍保留其原有的重建项目 `.venv` 行为；只是准备/检查 MFA 时应使用独立 `setup-mfa`。Linux 本机不要传 `--distribution`。

安装成功后，确认 .env 的 MFA_ENABLED=1（新模板已启用），运行：

```text
subtitle-translation translate "input.json" --video "input.mkv"
subtitle-translation pipeline "URL"
subtitle-translation batch "URL1" "URL2"
```

## 6. 运行默认值

| 项目 | 当前设置 |
|---|---|
| 音频 | 16 kHz、单声道、16-bit PCM WAV；有效规范 WAV 可直接复用，否则由 ffmpeg 准备 |
| MFA 操作 | 翻译后对来源粗时间窗口直接 `align`，不调用全片 `segment`；工具环境 Python/praatio 读取 TextGrid word tiers |
| MFA jobs | 固定 `num_jobs=1`，不是用户可调的 batch jobs |
| CPU 库线程 | `OMP_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`、`MKL_NUM_THREADS=1` |
| GPU | `CUDA_VISIBLE_DEVICES` 置空 |
| 临时目录 | 每次调用独立 `MFA_ROOT_DIR`，WSL 模式放在 Linux 文件系统 |
| Windows 原生执行目录 | work/r/<完整 UUID>，过长时使用系统临时目录 subtitle-mfa/r/<完整 UUID>；SQLite/特征/TextGrid 不在深层缓存内执行 |
| 命令预算 | `max(300 秒, 4 × 音频秒数)`；环境探测最多 30 秒 |
| 并发所有权 | 外层 batch 使用既有 CPU/IO 预算；MFA 不新建外层并行池，不改变 NVENC/WhisperX 规则 |
| 缓存 | 绑定音频、采用稿、模型/VAD、解析器和环境；命中仍探测资源，不再重复 segment/align |
| 失败 | 保留采用稿，词轴不可用时标记粗时间/人工任务，不切换对齐器、不恢复旧听录文字 |

当前窗口化路径按定稿跨段关系合并来源范围，窗口上限 60 秒/12 源单元，零额外 padding，不改变安装版本及 CPU 配额。语义断句与翻译已在其前完成，MFA 失败保留已有整句双语内容及来源粗时间。所有有效窗口一次 corpus align/批量导出，旧全文、单窗口和 partial 解析入口已删除，旧缓存不能冒充新窗口结果；见[当前架构](v2.1-architecture.md)与[corpus 执行契约](superpowers/specs/2026-11-13-windowed-mfa-corpus-batching.md)。
Windows 原生执行目录为工具派生路径预留 100 字符，总预算 240；完整缓存 SHA-256 和窗口 ID 不缩短。缓存 generation 内 execution.json 指向原生执行目录；命中缓存只探测环境，不另建执行目录。旧成功缓存继续按原身份/摘要验证，不因路径修复强制重译。详见 [短执行目录设计](superpowers/specs/2026-11-13-native-mfa-short-work-directory.md)。

`glossary.md` 是语义术语库，不等于 MFA 发音词典。MFA 只负责对轴，实际字幕语义断句仍交给前置 LLM 阶段。

## 7. 重跑、更新与验证范围

重跑安装器会检查现有工具、补齐指定依赖，并复用有效模型/VAD 缓存；不是把所有组件自动升级到最新版本。micromamba/MFA 版本不匹配或准备失败时会报错，不覆盖已就绪的 runtime manifest。该 manifest 的原子发布不等于整个环境安装具有事务回滚能力。

已有声学实测覆盖 Windows CLI + WSL Debian 英文短样本与缓存复用。Windows 原生已验证完整 setup 的声学模型/词典检查、CPU VAD 实际加载及 runtime 注册，模型缓存与输出目录带空格的 VAD 加载也通过实测。这不等于原生 corpus 声学质量、长音频或完整平台矩阵已验收；OOV/口音及真实 LLM 质量仍需独立验证，不自动修改 MFA_ENABLED。

实现依据：[安装器](../core/subtitle_translation/mfa_setup.py)、[MFA 调用与资源约束](../core/subtitle_translation/mfa.py)、[环境指纹与 TextGrid 导出](../core/subtitle_translation/mfa_export.py)。数据流与验收边界见[当前架构](v2.1-architecture.md)。
