# Independent preflight review: Phase 1 governance and task card

- Reviewer: Claude (independent, review-only clone)
- Date: 2026-08-21
- Repository: `maiziwheat520-boop/caiwu` (private)
- PR: #4 — "Phase 1 preflight: isolate agent workspaces and freeze task"
- Base: `main` / `61ad9103d68d10a07191c7ad00a4fbb8953deddd`
- Head: `ai/chatgpt/phase-1-prep` / `360ab03e5816b511ac55c0a19c466f747e941d27`
- Review clone: `G:\我的云端硬盘\AI\LedgerBridge-Claude`
- Review branch: `ai/claude/phase-1-preflight-review` (created from `origin/ai/chatgpt/phase-1-prep`)
- Verdict: **APPROVED FOR PHASE 1 IMPLEMENTATION**

审查范围限定为 PR #4 的**治理规则与 Phase 1 任务卡**。本轮未实现任何 schema、
未连接 Hermes、未写入 Codex 克隆或已退役的共享克隆。唯一新增文件是本报告。

---

## 0. 复审对象与身份核验

| 项 | 核验结果 |
|---|---|
| `origin/main` | `61ad9103d68d10a07191c7ad00a4fbb8953deddd` ✓ 与指定 base 一致 |
| `origin/ai/chatgpt/phase-1-prep` | `360ab03e5816b511ac55c0a19c466f747e941d27` ✓ 与指定 head 一致 |
| `git merge-base` | `61ad9103`——分支是 `main` 的干净后继，无回滚、无重写 |
| `refs/pull/4/head` | `360ab03e5816b511ac55c0a19c466f747e941d27` ✓ 与被审提交一一对应 |
| `refs/pull/4/merge` | `4ccd77a9e9e385756ff2e506936899ccc5855832`（存在 → PR 开放且无冲突） |
| PR #4 状态 | `state=open` `draft=false` `base=main` `mergeable=true` `mergeable_state=clean` `commits=1` `changed_files=6` `reviews=0` |
| CI（`360ab03`） | **6 个 check run 全部 `success`**：`secrets` / `quality` / `compose` × 2（push + pull_request） |
| 提交 | `360ab03 | Codex <codex@ledgerbridge.local> | sig=N | docs: prepare isolated Phase 1 workflow` |
| 本地克隆身份 | `user.name=Claude` / `user.email=claude@ledgerbridge.local` ✓ |
| 工作树 | 建立复审分支前 `git status --porcelain` 为空 |

**范围越界检查（关键）**：
`git diff --name-only origin/main..HEAD -- src/ alembic/ tests/ docker/ docker-compose.yml pyproject.toml .github/`
输出**为空**。PR #4 只改动 6 个文档/治理文件（`AGENTS.md`、`CLAUDE.md`、
`PROJECT_STATUS.md`、`docs/governance/WORKFLOW.md`、Phase 0 任务卡、新增 Phase 1 任务卡），
**没有任何 Ledger Core schema、模型、迁移或测试被提前写入**。这与任务卡
`docs/tasks/2026-08-21-phase-1-core-schema.md:3`（`Status: planned (preflight)`）自洽。

**Phase 0 收尾核验**：`git ls-tree -r origin/main -- docs/reviews/` 显示
`2026-08-21-phase-0-scaffold-claude.md` 与 `2026-08-21-phase-0-review-fixes-claude.md`
**均已进入 `main`**（`11a2aee docs: add Claude Phase 0 re-review` → 经
`61ad910 Phase 0 review remediation and Hermes hardening (#1)` 合并）。
`PROJECT_STATUS.md:14-15` 关于"PR #1 merged with full review history"的声明属实。

---

## 1. F-1 是否真正关闭

上一轮 `docs/reviews/2026-08-21-phase-0-review-fixes-claude.md` 的 **F-1** 要求：
消除共用工作树、移交写权限时切换 Git 身份、在 `PROJECT_STATUS.md` 记录移交时刻 HEAD。

实测：

| F-1 子项 | 状态 | 证据 |
|---|---|---|
| 分离工作树/克隆 | ✅ | `LedgerBridge-Codex`（实施）与 `LedgerBridge-Claude`（复审）为两个独立克隆，各有独立 `.git`、分支、index；旧 `LedgerBridge` 标记为 retired |
| 独立 Git 身份 | ✅ | 本克隆 `user.email=claude@ledgerbridge.local`，与 Codex 的 `codex@ledgerbridge.local` 分离；本报告提交即为第一条 Claude 身份提交 |
| 记录移交 HEAD | ✅ | `PROJECT_STATUS.md:25-34` 新增 "Ownership checkpoint"，含 `Common base HEAD: 61ad9103…`、两个克隆路径、两个身份、retired 克隆 |
| 规则落文档 | ✅ | `AGENTS.md:18-22`、`CLAUDE.md:16-25`、`docs/governance/WORKFLOW.md:17-25` 三处一致地写明隔离与交接边界 |

**判定：F-1 关闭。** 这是本 PR 最实质的贡献——上一轮被判为"部分关闭"的 H-6 残余风险
（两个模型共用一个 Drive 工作树）在机制上已被消除。

需要说明的是，隔离仍是**约定层面**的：没有任何技术手段阻止在错误的克隆里写入。
但相比上一轮，风险性质已经变了——上一轮是"同一个 index 被并发写"，现在是
"人为走错目录"，后者可被 `AGENTS.md:21-22` 要求的写前四项自检（仓库根、分支、身份、
干净/暂存状态）覆盖。这是可接受的残余风险。

---

## 2. 任务卡与冻结基线的一致性

把 `docs/tasks/2026-08-21-phase-1-core-schema.md` 逐条对照
`docs/architecture/IMPLEMENTATION_BASELINE.md:73-83`（Database requirements for Phase 1，共 8 条）：

| 基线条目 | 任务卡覆盖 |
|---|---|
| 1. 延迟余额触发器同时检查 OLD 与 NEW entry ID | ✅ In scope `:30-31`、Frozen invariants `:43`、Acceptance `:60-61` 三处一致 |
| 2. 审计创建避免 journal-entry/audit-event 循环依赖 | ✅ Frozen invariants `:47-48` |
| 3. raw-artifact 删除不得级联删除 source record | ⚠️ 随 RawArtifact/SourceRecord 一并移出范围（`:36`），**未注明去向** |
| 4. 实际余额只含 POSTED | ✅ In scope `:33`、Frozen invariants `:50`、Acceptance `:67` |
| 5. 账户标识唯一性显式且 entity-safe | ✅ In scope `:23` |
| 6. 对账保留结构化证据 JSON | ⚠️ 随 reconciliation groups 移出范围（`:37`），**未注明去向** |
| 7. 规则动作携带 schema 版本 | ⚠️ 随 classification rules 移出范围（`:37`），**未注明去向** |
| 8. 不引入含义模糊的 `settlement_status` | ✅ Frozen invariants `:51` |

覆盖进 Phase 1 的 5 条表述精确，尤其第 1 条把"OLD 和 NEW 都要查"这个最容易漏的点
写进了不变量**和**验收测试两处——这正是原基线特意点名的陷阱。

另外，任务卡 `:72-80` 的 F-1~F-7 表格与我上一轮报告的七条后续条件**逐条对应且未被稀释**：
F-5 的五个子项（Hermes 措辞+manifest、`/openapi.json`、`pip-audit --strict`、
worker 心跳探针、全历史 gitleaks）全部保留。`:82-88` 的 Review gate 明确写了
"Phase 1 cannot merge on test output alone"，并点名要审触发器 SQL、审计函数权限与哈希
序列化、POSTED 不可变性、downgrade 行为、以及**测试是否真能因目标缺陷而失败**——
这与 `CLAUDE.md:13-14` 一致，是正确的审查标准。

---

## 3. Findings

### MEDIUM — MP-1 验收测试遗漏了基线要求的、Phase 1 就能验证的业务场景

- **文件与行号**：`docs/tasks/2026-08-21-phase-1-core-schema.md:57-70`（Acceptance tests）；
  对照 `docs/architecture/IMPLEMENTATION_BASELINE.md:85-90`（Required acceptance scenarios）
- **违反的规则/不变量**：基线 `:85-90` 列出 5 条必需验收场景。其中 4 条
  （等额内部转账不产生收入或支出；带 0.10 手续费的转账恰好记 0.10 支出；
  信用卡消费与还款只把消费计入支出；部分退款减少支出而不产生收入）
  **不依赖任何采集层，纯粹是 Ledger Core 的记账与符号语义**，本可在 Phase 1 验证。
  任务卡自己的 Frozen invariants `:44` 也已写明符号约定
  （资产/支出正、负债/收入/权益负），但**没有任何一条验收测试去检验它**。
- **可复现的失败场景**：Phase 1 交付时余额触发器、POSTED 不可变性、审计哈希链全部正确，
  但 LIABILITY/INCOME 的符号约定接反——例如信用卡还款被记成一笔支出。
  此时任务卡 `:58-70` 的全部验收测试**依然通过**：它们检验的是"每笔分录按币种配平"、
  "POSTED 不可改"、"审计链有效"、"实际余额只含 POSTED"，
  这四类断言对符号方向完全不敏感（一笔方向错误的分录仍然配平）。
  缺陷要到 Phase 2/3 导入真实账单、有人核对"这个月支出为什么翻倍"时才暴露，
  而那时相关分录已是 POSTED 且不可变，更正必须走冲销链——修复成本从
  "改一行常量 + 重跑测试"变成"为历史数据生成成批冲销分录并解释差异"。
- **建议验收条件**：在 `:57-70` 的验收清单中补入至少四条，且**断言必须打在实际余额查询上，
  不是打在原始 posting 行上**：
  1. 两个 ASSET 账户之间的等额转账 → 收入合计与支出合计**均不变**；
  2. 带手续费的转账（费用 0.10 CNY = 10 minor units）→ 支出增量**恰好等于 10**；
  3. 信用卡消费后还款 → 支出合计只计消费一次，还款计 0；
  4. 部分退款 → 支出减少，收入合计**不变**。
  验收：这四条测试在把符号约定人为接反后**必须失败**（即它们对该缺陷是敏感的）。

### MEDIUM — MP-2 基线中三条 Phase 1 数据库要求被延期，但没有指定承接位置

- **文件与行号**：`docs/tasks/2026-08-21-phase-1-core-schema.md:36-37`（Out of scope）；
  对照 `docs/architecture/IMPLEMENTATION_BASELINE.md:75,80,81`（第 3、6、7 条）
- **违反的规则/不变量**：基线该节标题是 "Database requirements **for Phase 1**"。
  任务卡把 RawArtifact/SourceRecord、reconciliation、classification rules 移出范围，
  于是第 3 条（raw-artifact 删除不得级联删除 source record）、第 6 条（对账保留结构化
  证据 JSON）、第 7 条（规则动作带 schema 版本）**随对象一起消失，卡上没有任何一行提到它们**。
  `AGENTS.md:17` 要求：不得静默改动冻结架构，需把提议的变更记为任务文档中的 open issue
  并等待用户批准——当前是静默省略，不是记录在案的延期。
- **可复现的失败场景**：Phase 2 任务卡按惯例（如本卡按我的复审报告撰写那样）从 Phase 1 卡
  派生。第 3 条从未被复述，于是 RawArtifact 建表时对 SourceRecord 用了
  `ON DELETE CASCADE`；某次清理一条重复的原始邮件附件，静默删掉了由它派生的 source record。
  这在一个以"Source records are permanent"为前提的系统里属于证据完整性缺陷，
  按 `CLAUDE.md:29` 的定义是 BLOCKER 级——而它的根因只是一条要求在阶段交接时掉了。
- **建议验收条件**：在任务卡中新增 "Deferred baseline requirements" 小节，
  逐条列出基线第 3、6、7 项、承接它们的阶段/任务卡名称，并按 `AGENTS.md:17`
  标注为待用户确认的范围变更。
  验收：`IMPLEMENTATION_BASELINE.md:73-83` 的每一个编号条目，都能在任务卡的
  In scope 列表或该延期表中被找到，无一遗漏。

### LOW — LP-1 `AGENTS.md` 的 Definition of done 要求了 CI 尚未执行的严格依赖审计

- **文件与行号**：`AGENTS.md:45-46`（新增 "strict dependency audit"）；
  对照 `.github/workflows/ci.yml:68`（`- run: pip-audit`，无 `--strict`）
- **违反的规则/不变量**：Definition of done 是合并门槛的定义。当前它声明的标准
  严于 CI 实际执行的检查，而 F-5（`:78`）把 `pip-audit --strict` 排在 "During Phase 1"。
- **失败场景**：某依赖缺少可审计元数据被 `pip-audit` 静默跳过，`quality` job 仍为绿；
  作者按 Definition of done 的字面意思认为"严格依赖审计已通过"而合并。
  绿色对勾与文档承诺不是同一件事，这种偏差正是审计场景下最难事后解释的一类。
- **建议验收条件**：二选一——(a) 把 F-5 的 `pip-audit --strict` 与本措辞放在同一个 PR 落地；
  或 (b) 把 `AGENTS.md:45-46` 改为 "dependency audit（F-5 起转为 strict）"。
  验收：`ci.yml` 中出现 `pip-audit --strict`，或 `AGENTS.md` 不再出现 "strict"。

### LOW — LP-2 Claude 复审分支的 upstream 指向实施者分支

- **文件与行号**：本克隆分支配置（非仓内文件）——
  `ai/claude/phase-1-preflight-review` 的 `@{u}` = `origin/ai/chatgpt/phase-1-prep`，
  由 `git checkout -b <name> origin/<codex-branch>` 自动建立；
  对照 `docs/governance/WORKFLOW.md:20-21`（"Each clone has its own .git, branch, index,
  and local Git identity" / 复审产出经 `ai/claude/*-review` 提交交接）
- **失败场景**：本机 `push.default` 未设置（即 `simple`），该模式下上游分支名与本地不同时
  会拒绝推送，因此**今天一次裸 `git push` 会报错而非误推**。但配置本身把复审克隆的
  默认写入目标指向了实施者分支：任何人把 `push.default` 设为 `upstream`，
  或执行 `git push origin HEAD:@{u}`，复审提交就会落到 `ai/chatgpt/phase-1-prep` 上，
  从复审克隆改写实施分支——正是这套隔离要防的那类并发写。
- **建议验收条件**：在 `WORKFLOW.md` 的 Workspace isolation 小节写明复审分支创建方式为
  `git switch -c ai/claude/<task>-review --no-track origin/<codex-branch>`。
  验收：任一 `ai/claude/*-review` 分支上执行
  `git rev-parse --abbrev-ref --symbolic-full-name '@{u}'` **应报错**（无 upstream）。

### LOW — LP-3 两个 dependabot PR 会在 Phase 1 期间移动工具链

- **文件与行号**：仓库开放 PR 列表——
  #2 `pytest-cov >=6,<7 → >=6,<8`、#3 `mypy >=1.14,<2 → >=1.14,<3`，均以 `main` 为目标；
  对照 `pyproject.toml:25,29` 与任务卡 F-2（`:76`，锁文件尚未落地）
- **失败场景**：#3 在 Phase 1 实施期间合并，mypy 2.x/3.x 对新增的
  SQLAlchemy `Mapped[...]` 注解在 `strict` 下的推断收紧，Phase 1 的 PR 因与账本正确性
  无关的类型报错转红。此时的现实压力是放宽 `[tool.mypy] strict`，
  而 `strict` 恰恰是 Ledger Core 金额与符号类型安全的一道防线。
- **建议验收条件**：在切出 `ai/chatgpt/phase-1-core-schema` **之前**处置 #2/#3——
  要么现在合并并让 `main` 的 CI 重新基线为绿，要么显式标注为 "blocked until F-2"。
  验收：Phase 1 实施开工时，针对 `main` 的 dependabot PR 要么为 0，
  要么每个都带有明确的延期标注。

---

## 4. 已检查且未发现问题的项

1. **范围未越界**：PR #4 未触碰 `src/`、`alembic/`、`tests/`、`docker/`、
   `docker-compose.yml`、`pyproject.toml`、`.github/`（命令与输出见第 0 节）。
2. **单一提交、作者一致**：`360ab03` 为唯一提交，作者 `Codex <codex@ledgerbridge.local>`，
   与 `PROJECT_STATUS.md:37-38` 记录的实施 owner 一致。
3. **CI 在被审提交上真实全绿**：`360ab03` 的 6 个 check run 全 `success`，
   覆盖 `secrets` / `quality` / `compose`，push 与 pull_request 两个事件各一轮。
4. **PR 元数据自洽**：`refs/pull/4/head` 与被审 SHA 完全一致，
   `mergeable_state=clean`，无冲突、无 draft 状态。
5. **不变量表述精确**：任务卡 `:41-51` 的 8 条 Frozen invariants 与
   `IMPLEMENTATION_BASELINE.md` 无矛盾；"延迟触发器需同时校验 OLD 与 NEW"
   这一最易遗漏项在不变量与验收测试中各出现一次。
6. **迁移可逆性已被前置**：`:68-70` 明确要求 CI 断言"对象消失与重建"，
   而非只看 Alembic 版本号——这正是 F-7 的要点，未被稀释为版本号记账。
7. **审查标准未被降低**：`:84-88` 明确 Phase 1 不得仅凭测试输出合并，
   并点名需人工检视触发器 SQL、审计函数权限、哈希序列化、POSTED 不可变性、downgrade。
8. **F-1~F-7 转录忠实**：七条后续条件的触发时点与要求与原报告一致，无一被弱化或删除。
9. **Phase 0 闭环**：Phase 0 任务卡 `:1-6` 状态由 `review` 改为 `complete`，
   Remaining work 改为 "None. PR #1 merged at `61ad9103`"，与 `main` 的实际历史相符。
10. **本轮未触碰 Hermes**：按指令未做任何 SSH 或容器操作。因此
    `PROJECT_STATUS.md:18-19`（Hermes 运行 `ledgerbridge-app:61ad910`、
    `DEPLOYED_REVISION=61ad9103…`）与 `:20-22` 的部署类声明
    **本轮属于范围外未验证项**，不构成 finding，但也不应被本报告视为已核实。
11. 一处纯排版观察（不计为 finding）：`CLAUDE.md` 新增的 Workspace isolation
    小节与其后的 `## Review output` 之间缺一个空行。CommonMark 下 ATX 标题可中断段落，
    渲染不受影响，故不作要求。

---

## 5. 残余风险

| 风险 | 性质 | 结论 |
|---|---|---|
| 工作区隔离仅靠约定 | 无技术强制，依赖写前自检 | 可接受；风险性质已从"并发写同一 index"降为"走错目录" |
| 分支保护缺失（F-6） | GitHub Free 私有仓平台限制，上一轮已 API 核实 403 | 维持上一轮判断：当前不阻断；触发条件见 `PROJECT_STATUS.md:52-55` |
| 锁文件未落地（F-2） | 计划在 Phase 1 合并前 | 与 LP-3 相关联，建议一并处置 |
| Hermes 部署声明 | 本轮范围外，未验证 | 建议在 Phase 1 实施评审时一并复核 |

---

## 6. 最终结论

**APPROVED FOR PHASE 1 IMPLEMENTATION**

依据：

- PR #4 **无 BLOCKER、无 HIGH finding**。按 `CLAUDE.md:28-31` 的严重度定义，
  本轮四条 MEDIUM/LOW 均不属于"可损坏证据、金额、可审计性或迁移安全"，
  也不属于"违反冻结不变量或允许实质错误结果"。
- PR #4 **严格限于治理与任务卡**，未提前写入任何 schema、模型、迁移或测试；
  这一点由 diff 命令实证，不依赖自述。
- 上一轮判为"部分关闭"的 **H-6 残余风险（F-1）已经关闭**：两个独立克隆、
  两个 Git 身份、`PROJECT_STATUS.md` 中的 ownership checkpoint 三者齐备。
- Phase 1 任务卡把基线中最容易出错的几处（延迟触发器的 OLD/NEW、审计循环依赖、
  实际余额只含 POSTED、禁止 `settlement_status`、迁移真实可逆）**写成了可测试的验收条件**，
  并明确禁止仅凭测试输出合并。这是一份可以据以开工的范围契约。

**开工前应完成的卡面修订**（属实施者/用户，不属本复审者；两条都是改任务卡，不是改代码）：

1. **MP-1** — 把基线 `:86-90` 中四条纯记账语义场景补进验收清单，并要求它们对
   符号接反这一缺陷敏感。**建议在 PR #4 合并前改**，因为任务卡一旦冻结，
   验收清单就是 Codex 的建造目标。
2. **MP-2** — 增加 "Deferred baseline requirements" 小节，为基线第 3、6、7 条指定承接阶段。

LP-1、LP-2、LP-3 可在 Phase 1 期间处理，其中 **LP-3 建议在切出
`ai/chatgpt/phase-1-core-schema` 之前决策**，以免工具链在实施中途变动。

---

## 附录：本轮实际执行的验证命令

均在 `G:\我的云端硬盘\AI\LedgerBridge-Claude` 内执行；未推送、未合并、未触碰 Hermes、
未写入其他克隆。

```powershell
git config user.name ; git config user.email          # Claude / claude@ledgerbridge.local
git remote -v ; git rev-parse --abbrev-ref HEAD ; git rev-parse HEAD ; git status --porcelain
git fetch origin --prune                               # 首次因瞬时 DNS 失败，重试成功
git rev-parse origin/main                              # 61ad9103…
git rev-parse origin/ai/chatgpt/phase-1-prep           # 360ab03…
git merge-base origin/main origin/ai/chatgpt/phase-1-prep
git ls-remote origin "refs/pull/4/*"                   # head=360ab03, merge=4ccd77a
git checkout -b ai/claude/phase-1-preflight-review origin/ai/chatgpt/phase-1-prep
git log --format="%h | %an <%ae> | sig=%G? | %s" origin/main..HEAD
git diff --stat origin/main..HEAD ; git diff --name-status origin/main..HEAD
git diff --name-only origin/main..HEAD -- src/ alembic/ tests/ docker/ docker-compose.yml pyproject.toml .github/
git diff origin/main..HEAD -- AGENTS.md CLAUDE.md docs/governance/WORKFLOW.md docs/tasks/*.md
git ls-tree -r --name-only origin/main -- docs/reviews/
git show HEAD:.github/workflows/ci.yml                 # 确认 pip-audit 仍无 --strict
git config push.default ; git rev-parse --abbrev-ref --symbolic-full-name '@{u}'
git log --oneline -3 origin/main
```

GitHub REST API（只读；凭据取自 Windows 凭据管理器，未打印）：

```
GET /repos/maiziwheat520-boop/caiwu/pulls/4
GET /repos/.../pulls/4/reviews
GET /repos/.../commits/360ab03e5816b511ac55c0a19c466f747e941d27/check-runs
GET /repos/.../pulls?state=open
```

阅读的仓内文件：`docs/tasks/2026-08-21-phase-1-core-schema.md`（全文）、
`PROJECT_STATUS.md`（全文）、`CLAUDE.md`（全文）、`docs/reviews/README.md`、
`docs/architecture/IMPLEMENTATION_BASELINE.md:55-90`、以及 PR #4 对
`AGENTS.md` / `WORKFLOW.md` / Phase 0 任务卡的全部改动。
