# DISCIPLINE.md — Subtitle-translation 协作秩序规范与操作说明

> 适用范围：`The-Bazzar/Subtitle-translation` 仓库 `main` 分支，自 `v1.7.9` 起执行。
> 本文件是**协作过程规范**；技术事实仍以 `AGENTS.md` 和当前仓库代码为准。两者冲突时，代码行为优先，但必须在 Issue/PR 中说明并修复文档。

---

## 0. 与当前 GitHub Branch Ruleset 对齐

当前 `main` 已启用 ruleset：**`main branch lock`**（active，仅作用于 `refs/heads/main`）。本文的所有要求不得弱化该 ruleset，ruleset 未覆盖的部分由本文件补强。

| Ruleset 项 | 当前配置 | 协作要求 |
|---|---|---|
| deletion / creation / update / non_fast_forward | 全部拦截 | 禁止直接创建、删除、推送、强推 `main`；任何变更必须走 PR |
| pull_request | `require_code_owner_review=true` | 已通过 `.github/CODEOWNERS` 将全部代码分配给 `@oculr`；后续路径拆分时保持 owner 覆盖，不得出现无 owner 文件 |
| pull_request | `dismiss_stale_reviews_on_push=true` | 已获批准后不要继续补推；确需补推，必须重新请求评审 |
| pull_request | `required_review_thread_resolution=true` | 所有评审 thread 必须逐条 resolve，不得强行合并 |
| pull_request | `allowed_merge_methods=["merge"]` | 只允许 **merge commit** 进入 `main`，不得 squash/rebase 合并 |
| code_quality | `severity=warnings` | PR 不得引入 code quality warnings |
| code_scanning | CodeQL，security alerts ≥ high，errors 阻断 | 不得引入高危告警或 error 级 CodeQL 结果 |
| copilot_code_review | `review_on_push=true`，draft PR 不审 | 推送后保留 Copilot 意见，由人类逐条确认或说明不采纳原因 |
| merge_queue | `ALLGREEN`，5/1/5，min wait 5min | 普通合并必须通过 merge queue，不得绕过 |

**ruleset 的缺口需要人工纪律补上：**
- 当前 `required_approving_review_count=0`。本规范强制要求：**至少 1 名 maintainer 明确 approve**，且所有自动化检查通过后，才允许进入 merge queue。
- 仅仓库 owner 配置有 ruleset bypass。bypass 只用于紧急修复，使用后必须在 24 小时内补 PR/Issue 复盘（见 6.7）。

---

## 1. 协作总则

1. `main` 永远可发布。它只接受经过完整评审、测试和 merge queue 的合并提交。
2. 所有代码、配置、文档、脚本行为变更一律通过 PR；不得直接向 `main` push。
3. 一个 PR 只解决一个问题；禁止把“顺手重构”“顺手修 typo”“顺手改 prompt”混入同一 PR。
4. 每位贡献者在 push 前必须本地完成：格式化检查、测试、自审 diff。
5. 维护者有权对不符合本规范的 PR 直接 `CHANGES_REQUESTED` 或关闭，并在评论中引用本文件具体条目。
6. 重复违规将暂时取消协作者权限或改为 fork-based 流程。

---

## 2. 分支命名规范

### 2.1 分支前缀

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat/` | 新功能 | `feat/translation-context-window` |
| `fix/` | 缺陷修复 | `fix/proofread-zero-budget` |
| `refactor/` | 不改变外部行为的重构 | `refactor/safety-gate-layout` |
| `docs/` | 仅文档 | `docs/migration-guide` |
| `test/` | 仅测试与测试设施 | `test/chat-session-recovery` |
| `chore/` | 构建、依赖、琐碎维护 | `chore/exa-py-floor` |
| `release/` | 发布准备 | `release/v1.7.10` |
| `agent/` | 自动化代理产出的实验分支 | `agent/proofread-concurrency-transactional` |

### 2.2 命名规则

- 一律小写 ASCII，使用连字符 `-` 分隔；除 `/` 作为前缀分隔符外，不得使用空格、下划线、中文或其他特殊字符。
- 名称 ≤ 48 字符，必须能看出“类型 + 对象”。
- 有 Issue 时追加编号：`fix/123-proofread-zero-budget`。
- 禁止使用 `main`、`master`、`dev`、`test` 等通用名；个人临时分支不得包含用户名，例如 `zhangsan-test` 不允许。
- 同一主题的 stacked PR 必须显式标记依赖顺序：`feat/stack-01-safety`、`feat/stack-02-retry`，并在 PR 描述中写“基于 #xx”。

### 2.3 分支生命周期

- 合并后 48 小时内删除源分支。
- 长期不活跃（14 天无更新且无 `do-not-delete` 说明）的实验分支由维护者清理。
- `agent/` 分支同样遵守本规范；不得直接合入 `main`。

### 2.4 版本标签

- 只允许在 `main` 上打 `vMAJOR.MINOR.PATCH` 标签。
- 打 tag 前必须更新版本说明；tag 由 maintainer 操作，不得强推或移动。

---

## 3. PR Principle（PR 原则）

### 3.1 单个 PR 的变更粒度

一个 PR 必须是**一个可独立解释、可独立回滚的逻辑变更**。

| 规模 | 标准 | 处理方式 |
|---|---|---|
| 常规 | ≤ 8 个文件，且净变更 ≤ 400 行 | 正常 PR |
| 较大 | 9–15 个文件，或净变更 400–1200 行 | 必须先建 Issue/设计说明，PR 描述写清架构理由 |
| 超大规模 | > 15 个文件，或净变更 > 1200 行 | **默认要求拆分**；只有 maintainer 书面批准后才可例外，并必须提供拆分不了的证据 |

判定净变更时，自动生成的测试数据、lockfile、纯删除文件按实际 diff 计算，不搞“刷行数”。**禁止**以“代码本来就是大文件”为由绕过粒度控制。

提交粒度要求：
- 一个 commit 只做一步逻辑变更，commit message 能独立解释“为什么”。
- 禁止提交 `WIP`、`fixup`、`tmp`、`.bak`、合并冲突残留。
- 每个 commit 都应保证测试可运行；做不到时在 PR 描述中说明该 commit 的临时性并尽快 squash 整理。
- 不要在同一个 PR 中混入：功能 + 格式化、功能 + prompt 文案、重构 + 新功能、文档改动 + 无关代码改动。

### 3.2 测试要求

任何 PR 都必须包含与变更对应的测试，并满足以下门槛：

**必跑命令：**

```bash
# 项目 venv（推荐 setup 后使用）
.venv/bin/python -m unittest discover -s tests

# 或使用 uv
uv run python -m unittest discover -s tests
```

**测试基线：**
历史 `v1.7.9` 基线存在 3 个测试问题（2 个 `-crf 19` 断言、1 个 `template.ass` 路径错误），已由 `test/align-baseline-tests`（PR #8）修复。该 PR 合入后，`main` 全量测试必须为 `OK`，**不再保留任何基线豁免**；任何 PR 出现失败/错误即为阻断项。

**其他强制要求：**
- 修改 `translate_srt.py`：必须跑全量 unittest，且至少有一个新增/调整的测试直接覆盖改动行为。
- 修改 `*.ps1` / `*.sh` 或 setup/env 链路：必须同时检查 Windows PowerShell 与 Linux/WSL 行为对齐，并更新 `tests/test_setup_scripts.py` 或对应脚本测试。
- 新增环境变量：必须同步 `.env.example`、`AGENTS.md`、`README.md` 和两个 setup 的升级流程，并补测试证明旧 `.env` 能被正确升级。
- 修改缓存/JSON 格式：必须包含旧格式读取兼容或显式迁移路径，并测试 round-trip。
- 网络、LLM、ffmpeg、yt-dlp 调用必须 mock；测试不得依赖真实 API key、真实视频或真实联网。
- 并发相关改动必须有并发/死锁/重复提交类回归测试。
- 不写只为了“覆盖行数”而不断言行为的测试。

### 3.3 变更总结报告

每个 PR 描述必须使用以下模板，缺项视为不合格：

```markdown
## Summary
一句话说明这个 PR 做什么。

## Motivation / Problem
解决什么问题？没有它会怎样？关联 Issue 编号。

## Design Alignment
与 AGENTS.md / DISCIPLINE.md 的设计原则是否一致？
- [ ] 未触碰任何 design-invariant（见第 4 章）
- [ ] 触碰了 design-invariant，已按 4.3 提交设计偏离报告

## Changes
- 变更文件清单
- 关键行为变化
- 配置/CLI/缓存格式是否变化

## Validation
- 命令：`.venv/bin/python -m unittest discover -s tests`
- 结果：Ran N tests；新增失败/错误 0
- 新增测试名称及覆盖点
- 人工验证步骤（涉及脚本/CLI 时）

## Risk & Rollback
- 最坏情况与影响范围
- 回滚方式：revert 单个 merge commit 是否足够？
- 是否需要数据迁移

## Documentation
- [ ] README.md
- [ ] AGENTS.md
- [ ] .env.example / setup 链路
- [ ] MIGRATION.md / CHANGELOG（破坏性变更时）
- [ ] `.agents/skills/*`

## Checklist
- [ ] 分支命名符合 DISCIPLINE.md
- [ ] 单个 PR 仅一个逻辑变更，粒度符合 3.1
- [ ] 全量测试通过且不新增失败
- [ ] 未提交本地配置/产物/密钥
- [ ] 未修改 `*_prompt.example.md`
- [ ] 评审 thread 已全部回复/解决
```

### 3.4 评审与合并流程

1. 推送功能分支 → 打开 PR（复杂变更先开 Draft）。
2. 自评：按 3.3 checklist 逐项确认。
3. 等待自动化：code quality、CodeQL、Copilot code review、merge queue checks。
4. 至少 1 名 maintainer approve；相关 CODEOWNERS 必须覆盖。
5. Approve 后不要继续 push；ruleset 会 dismiss stale reviews。确需修改，先转回 Draft 或重新请求 review。
6. 所有 conversation threads resolve 后，进入 merge queue（`ALLGREEN`）。
7. 合并后删除分支，按需更新版本标签。

---

## 4. 初始设计理念保护（Design Invariants）

以下内容是本项目“初始设计理念”的硬边界。普通 PR 不得更改；任何触碰都属于 **design change**，必须走 4.3 流程。

### 4.1 受保护的初始设计理念

| 编号 | 设计理念 | 当前代码/文档依据 |
|---|---|---|
| D1 | WhisperX `.json` 是主字幕唯一源；SRT 不再作为输入缓存 | `AGENTS.md` Overview |
| D2 | 流水线固定：download → whisper → beautify → glossary → translate → split → proofread → burn | `AGENTS.md` Pipeline Flow |
| D3 | `.beautified.json` 是主缓存，保存 `translation` / `proofread_text` / `split_events`；`split_status` / `split_reason` 语义不得漂移 | `AGENTS.md` Pipeline Flow / Step Behavior |
| D4 | 所有 LLM 阶段 user prompt 为 JSON object，顶层 `items` array；返回严格 JSON，item 只使用 `id` 与 ISO 639 语言代码 key | `AGENTS.md` translate/split/proofread |
| D5 | `glossary.md` 是完整常驻的全局硬规则；`retrieved_context` 只能补充，不能截断或替代 | `AGENTS.md` Config |
| D6 | split 只用源语言首尾 token 匹配 `words[]`；匹配失败整句回退，禁止本地强切 | `AGENTS.md` translate/split/proofread |
| D7 | 本地工具处理下载/识别/时间轴/硬压，远端 LLM 只处理 glossary/翻译/分割/校对；本地与远端边界不得模糊 | `AGENTS.md` Overview |
| D8 | PowerShell 与 bash 入口行为对齐；Windows 与 WSL 路径/编码必须兼容 | `AGENTS.md` Working Notes |
| D9 | 不得提交 `.env`、`providers.json`、`tavily_domains.json`、`cookies.txt`、本地 prompt、`glossary.md`、生成产物 | `AGENTS.md` Working Notes |
| D10 | setup 必须从 example 创建缺失配置，并只追加新增变量、不覆盖用户已有配置 | `AGENTS.md` Config |
| D11 | `*_prompt.example.md` 不在本仓库修改；prompt 文案归口 `The-Bazzar/prompt` | 本文件第 5 章 |
| D12 | 任意源/目标语言组合应保持可用；不支持的特定能力必须显式 capability-gate 并文档化，不得假装已保护 | `AGENTS.md` / 现有语义门实现 |

### 4.2 什么算“严重违背”

以下变更默认视为严重违背初始设计理念，**未获批准不得开始实现**：

- 删除、重排、合并流水线阶段，或改变输入/输出主数据源；
- 改变 `.beautified.json` 字段语义、`split_status` 枚举含义、缓存 fingerprint 规则；
- 放宽或绕过 JSON 协议（例如允许散文、markdown、非 `items` 顶层、非 ISO code key）；
- 让 `retrieved_context` 或 embedding 结果覆盖、截断、替代 `glossary.md`；
- 改变“整句翻译 → 未校对源文分割 → split event 校对”的顺序或数据来源；
- 引入本地强切、按目标语言切分后再对源文轴、或跨 event 自由合并/拆分；
- 破坏 PowerShell/bash 任一入口，或只在单一平台实现核心功能；
- 提交用户本地文件、密钥、生成产物；
- 在未定义迁移/兼容策略的情况下改变 sidecar、缓存或输出命名约定；
- 把 prompt 质量策略同时散落到 `.example`、built-in fallback 和多处常量，造成多权威。

### 4.3 Design Change 必须提交的报告

触碰 D1–D12 的 PR 必须先建 Issue（标题带 `[design-change]`），并经 maintainer 在 Issue 中同意后，PR 描述附上：

```markdown
## Design Change Report
- 受影响的 invariant：D1–D12 中哪几条
- 变更范围：哪个阶段、哪些文件、哪些配置/CLI/缓存格式
- 为什么必须改：当前设计在什么真实场景下失效
- 已考虑的替代方案：不改设计、外部配置、单独工具、兼容层等
- 对旧数据/旧配置的影响：是否需要迁移、能否回退读取
- 跨平台影响：Windows/PowerShell 与 Linux/bash 分别如何验证
- 回滚路径：revert 后数据是否可继续使用
- 新增测试：如何证明新设计成立且旧约束未被静默破坏
- 相关文档：AGENTS/README/MIGRATION/CHANGELOG 更新计划
```

### 4.4 不提交该报告会引入的问题与后果

| 严重违背类型 | 典型问题 | 可能后果 |
|---|---|---|
| 改变数据源/主缓存 | 新老 `.json` 混用，状态丢失 | 字幕重跑结果不一致，缓存无法复现 |
| 放宽 JSON 协议 | 模型返回散文/伪 JSON 被静默接受 | 下游解析失败、ID 错位、语言 key 错配 |
| 降级或截断 `glossary.md` | 术语权威从全局硬规则变为检索摘要 | 术语漂移、翻译前后不一致、人工无法审计 |
| 改变 split 数据/对齐逻辑 | 本地强切或跨 event 合并 | 时间轴与 `words[]` 脱节，字幕错位 |
| 删除既有 CLI/request contract 且无迁移 | 旧脚本、旧 prompt、旧调用立即失败 | 用户升级中断，无回滚说明 |
| 单平台实现 | 行为只在 Windows 或 WSL 可用 | 另一端 pipeline 静默产出错误结果 |
| 提交本地配置/密钥 | 泄漏 API key、cookie | 安全事件、仓库回滚 |
| prompt 多权威并存 | 同一策略散落多处 | 新装用户、旧用户、fallback 路径行为分叉 |
| 语言能力未 gate | 对非 en→zh 假装已做硬约束 | 其他语言方向误报安全/术语保护 |

---

## 5. 提示词 `.example` 文件修改禁令

### 5.1 保护范围

以下文件**默认只读，禁止在 `Subtitle-translation` 仓库修改**：

- `glossary_prompt.example.md`
- `translate_prompt.example.md`
- `proofread_prompt.example.md`
- `split_prompt.example.md`

### 5.2 正确归口：`The-Bazzar/prompt`

所有 prompt 文案、策略、语气、示例、质量规则变更，必须提交到 **`The-Bazzar/prompt`** 仓库：

1. 在 `The-Bazzar/prompt` 新建分支并按该仓库规范提交 PR；
2. 在该 PR 中说明影响范围：哪个阶段、哪条 pipeline 行为、是否改变模型输出协议；
3. 该仓库评审通过并合并后，如需同步到本项目，由 maintainer 在 `Subtitle-translation` 开 `docs/sync-prompt-*` 或 `chore/sync-prompt-*` PR；
4. 同步 PR 必须引用 `The-Bazzar/prompt` 的 commit/PR 编号，且 diff 只允许是“逐字同步”，不得夹带本仓库代码改动；
5. 普通协作者提交到本仓库的 prompt 修改 PR 将被直接拒绝。

### 5.3 边界说明

- `translate_srt.py` 内的 `_TRANSLATE_PROMPT_FALLBACK`、`_PROOFREAD_PROMPT_FALLBACK`、`_SPLIT_PROMPT_FALLBACK`、`_GLOSSARY_PROMPT_FALLBACK` 是内置兜底文案，同样属于 prompt 策略。修改这些常量视同修改 prompt 文案，必须先在 `The-Bazzar/prompt` 完成评审。
- `_TRANSLATE_FORMAT`、`_SPLIT_FORMAT`、`_JSON_FORMAT` 等**输出格式/JSON 协议常量**属于代码契约，不属于 prompt 文案，可以在本项目通过 PR 修改，但必须附 JSON 协议回归测试。
- `providers.example.json`、`tavily_domains.example.json`、`template.ass.example` 是配置/模板示例，不是 prompt 示例；其修改仍需遵守普通 PR 原则和 setup 兼容测试。

---

## 6. 其他补充规范

### 6.1 提交信息

采用 Conventional Commits：

```
<type>(<scope>): <subject>
```

- `type`: `feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `build`
- `subject` ≤ 72 字符，小写开头，不以句号结尾
- 需要解释“为什么”时补 body；破坏性变更补 `BREAKING CHANGE:` 并同步 `MIGRATION.md`
- 示例：`fix(proofread): honor zero web search budgets`

### 6.2 文档同步

- 行为或配置变更必须同步 `AGENTS.md`、`README.md`、`.env.example` 和两个 setup 升级流程。
- 用户可见破坏性变更必须新增/更新 `MIGRATION.md`。
- `AGENTS.md` 是技术权威文档；PR 不得只改代码不更新文档。
- 本 `DISCIPLINE.md` 修改也必须走 PR，且不得弱化任何既有规则。

### 6.3 密钥、配置与产物

- 禁止提交任何真实 key、token、cookie、本地 `.env`、`providers.json`、`tavily_domains.json`、`glossary.md`、本地 `*_prompt.md`、`template.ass`、视频、字幕产物、`chroma_db`。
- 示例文件中的 key 只能使用占位符（如 `tvly-xxx`、`xxx`）。
- 提交前执行：

```bash
git status --short
git diff --cached --check
git diff --cached --name-only | grep -E '(\.env$|providers\.json$|cookies\.txt$|glossary\.md$|template\.ass$)' && echo "STOP: local files detected"
```

### 6.4 跨平台对齐检查

任何修改 `*.ps1` / `*.sh` / `setup.*` / `pipeline.*` 的 PR，必须逐项确认：

- 环境变量名称、默认值、语义一致；
- 路径引用使用仓库根相对路径；
- 输出 `OUTPUT_*` 变量两边一致；
- 失败退出码与非零判断一致；
- 重编码、burn、目录清理等外部命令参数一致；
- 测试同时覆盖 PowerShell 与 bash 文本契约。

### 6.5 CODEOWNERS 与责任边界

- ruleset 已启用 code owner review，当前 `.github/CODEOWNERS` 使用 `* @oculr` 覆盖全部路径；后续细粒度拆分时，必须在新增 pattern 前先确认原 `*` 规则已被替代，不得出现无 owner 文件。
- 没有 owner 覆盖的路径不得由非 owner 直接合并。
- 本规范第 4 章的 design-invariant 文件（`AGENTS.md`、`translate_srt.py`、`*.example.md`、`setup.*`、`pipeline.*`）建议后续单独指定更细粒度 CODEOWNERS。

### 6.6 Merge Queue 操作

- 仅 `main` 受 merge queue 保护。
- PR 必须先通过全部 checks，再进入 merge queue；队列按 `ALLGREEN` 分组，最少等待 5 分钟。
- 队列中的 PR 不得再 force-push；如需更新，退出队列，修正后重新入队。
- 合并方式只允许 merge commit；不得在本地 rebase `main` 后强推。

### 6.7 紧急 bypass 规则

- 只有 maintainer 在真实线上故障（pipeline 完全不可用、密钥泄漏、阻断性错误）时，才允许使用 ruleset bypass 直接修复 `main`。
- 紧急修复后 24 小时内必须：
  1. 补一个 `fix/` PR 记录实际 diff；
  2. 在 PR 描述中写明 bypass 原因、时间、影响面；
  3. 补全测试与文档。
- 非紧急情况使用 bypass 视为违规。

### 6.8 违规处理

- 第一次：PR `CHANGES_REQUESTED` 并引用本文件条目。
- 第二次：PR 关闭，要求按规范重开。
- 第三次或恶意绕过：临时移除协作者权限，由 maintainer 决定后续协作方式。

---

## 7. Definition of Done（合并前最终清单）

- [ ] 分支命名符合第 2 章
- [ ] 单个 PR 一个逻辑变更，粒度符合 3.1
- [ ] 全量 unittest 通过，未新增失败（`main` 基线必须为 OK）
- [ ] 新增行为有测试，脚本/平台变更有 parity 测试
- [ ] PR 描述按 3.3 模板完整填写
- [ ] 未触碰 design-invariant；或已提交 Design Change Report 并获 maintainer 批准
- [ ] 未修改 `*_prompt.example.md`；或仅 maintainer 从 `The-Bazzar/prompt` 同步
- [ ] 文档同步：README / AGENTS / .env.example / setup / MIGRATION（按需）
- [ ] 无本地配置、产物、密钥进入 diff
- [ ] 评审 thread 全部解决
- [ ] 自动化与 merge queue 全绿
- [ ] 合并后源分支已删除
