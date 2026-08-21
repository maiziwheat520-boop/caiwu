# Independent implementation review: Phase 1 Ledger Core

| 项 | 值 |
|---|---|
| Reviewer | Claude（独立只读复审克隆） |
| Date | 2026-08-21 |
| Repository | `maiziwheat520-boop/caiwu`（private） |
| PR | #5 — "Phase 1: implement database-enforced Ledger Core" |
| Base | `main` / `55f88dd9f8125d34a8952e5af56844c0033d7b27` |
| Head | `ai/chatgpt/phase-1-core-schema` / `80d01ee3afe9f6b9954e8a93ec206f174d73880f` |
| Review clone | `G:\我的云端硬盘\AI\LedgerBridge-Claude` |
| Review branch | `ai/claude/phase-1-core-schema-review`（`--no-track`，无 upstream） |
| Verdict | **CHANGES REQUIRED** |

本轮审查了 `origin/main..HEAD` 的完整 diff（26 个文件，+3787/-72），并在一台
**隔离沙箱 PostgreSQL** 上独立复跑了全部测试、独立复现了每一条 BLOCKER/HIGH。
未推送、未合并、未写入 Codex 克隆或已退役克隆、未触碰 Hermes 正式服务。
唯一新增文件是本报告。

---

## 0. 审查对象与 CI 证据核验

| 核验项 | 结果 |
|---|---|
| `origin/main` | `55f88dd9f8125d34a8952e5af56844c0033d7b27` ✓ 与指定 base 一致 |
| `origin/ai/chatgpt/phase-1-core-schema` | `80d01ee3afe9f6b9954e8a93ec206f174d73880f` ✓ 与指定 head 一致 |
| 复审分支 HEAD | `80d01ee3afe9f6b9954e8a93ec206f174d73880f` ✓ |
| 复审分支 upstream | 无（`branch.*.remote` / `.merge` 均为空）✓ LP-2 已按上轮建议闭环 |
| `git merge-base origin/main HEAD` | `55f88dd9…` — 分支是 `main` 的干净后继，无回滚、无重写 |
| 建分支前工作树 | `git status --porcelain` 为空 ✓ |
| PR #5 元数据 | `state=open` `draft=false` `merged=false` `base=main` `head_sha=80d01ee3…` `mergeable=true` `mergeable_state=clean` `commits=3` `changed_files=26` |
| CI（`80d01ee3`） | **6 个 check run 全部 `success`** — `secrets` / `quality` / `compose` 各两轮 |
| CI 事件覆盖 | 两个 workflow run：`32445315895`（`event=push`）与 `32445339925`（`event=pull_request`），两个 check suite 均 `conclusion=success` ✓ |
| 提交与作者 | `3fe2a77 feat: implement Phase 1 ledger core` / `b3ca312 build: lock dependencies and harden runtime gates` / `80d01ee docs: record Phase 1 implementation evidence`，三条均为 `Codex <codex@ledgerbridge.local>`，`sig=N`（未签名） |
| 范围越界 | `grep -rnIE "RawArtifact\|SourceRecord\|ImportJob\|settlement_status\|openai\|anthropic\|llm\|connector\|parser" src alembic tests scripts` 只命中 `argparse` 的 `parser` 变量名，**无越界实现** ✓ |
| 敏感数据 | `.gitignore:1-2` 为 `.env*` + `!.env.example`；`.env.example` 只有 `change-me` 占位；`secrets` job 以 `fetch-depth: 0` 跑全历史 gitleaks 并通过 ✓ |

**独立复跑结果（沙箱 PostgreSQL 16.13，隔离实例，`uv sync --frozen`）**：

- `pytest --cov=ledgerbridge --cov-fail-under=95` → **31 passed，coverage 97.53%**
- `ruff check .` → All checks passed；`ruff format --check .` → 22 files already formatted
- `mypy src alembic tests scripts`（`strict = true`）→ Success: no issues found in 20 source files
- `pip-audit --strict`（对 `uv export --frozen --no-emit-project` 导出的 1088 条 hash 锁定依赖）→ **No known vulnerabilities found**
- `alembic upgrade head` → `downgrade 20260821_0001` → `upgrade head` 全部成功

**上述数字与任务卡 `:104-107` 声称的证据逐位吻合。** 但 §1 说明：这些绿灯**不足以**证明
`CLAUDE.md:13-14` 要求的"测试能否因目标缺陷而失败"，本轮正是在这一点上发现了主要问题。

> 环境差异说明：复现实例为 PostgreSQL 16.13（Ubuntu 包），CI 与生产为 `postgres:15-alpine`。
> 本报告用到的所有行为（`SET` 的事务语义、行触发器/约束触发器时序、advisory lock、
> jsonb 规范化顺序、FK/ACL 语义）在 15 与 16 之间一致；未依赖任何 16 独有行为。

---

## 1. Findings

严重度按 `CLAUDE.md:30-33`。每条含文件行号、可复现失败场景、验收条件。

### BLOCKER

#### P1-B1 — 运行时降权 `SET ROLE` 会被连接池回滚，应用连接实际以数据库属主身份运行；审计表 ACL 与全部完整性触发器均可被应用自身绕过

- **文件与行号**：`src/ledgerbridge/db.py:29-36`（`@event.listens_for(engine, "connect")` 中
  `cursor.execute(f'SET ROLE "{database_role}"')`，第 33 行）；
  受影响的控制在 `alembic/versions/20260821_0002_ledger_core.py:556-568`（`REVOKE ALL ON TABLE audit_event FROM PUBLIC` /
  `GRANT SELECT ON TABLE audit_event TO ledgerbridge_app`）。
- **违反的不变量**：`IMPLEMENTATION_BASELINE.md:63-64`「Audit events are append-only …
  **Applications may not insert them directly**」；任务卡 `:25-26` 同款表述。
- **机制**：psycopg3 默认 `autocommit=False`，`connect` 事件里的 `cursor.execute("SET ROLE …")`
  会开启一个隐式事务。PostgreSQL 的 `SET` 是**事务性**的。SQLAlchemy 连接池默认
  `reset_on_return='rollback'`，连接归还时执行 `ROLLBACK` —— 如果该池化连接上的
  **第一个事务不是以 COMMIT 结束**，`SET ROLE` 随之被撤销，此后该连接在整个池化生命周期内
  都以**登录角色**运行。登录角色即 `POSTGRES_USER`（`.env.example:3`、`docker-compose.yml:70-74`），
  也就是 Phase 1 全部表与 `append_audit_event` 的**属主**，在官方 postgres 镜像中还是集群
  **bootstrap superuser**。
- **可复现的失败场景（已在沙箱实测，非推演）**：
  1. `build_engine(url, "ledgerbridge_app")`，连续 4 次 `with Session(engine) as s: s.execute(select current_user)`：
     `cycle 0 → ledgerbridge_app`，`cycle 1/2/3 → ledgerbridge_test`（同一 `pg_backend_pid`）。
     对照组：若每个 cycle 显式 `s.commit()`，则 4 次全部保持 `ledgerbridge_app`。
     结论：**角色是否生效，取决于该连接上第一个事务碰巧提交还是回滚。**
  2. 走真实生产路径：`TestClient(app).get("/health/ready")` → 200。
     `readiness()`（`src/ledgerbridge/main.py:28-37`）只读一次 `SELECT 1`，
     `get_session()`（`db.py:50-54`）退出 `with` 时 `close()` → `ROLLBACK`。
     紧接着在**同一池化连接**上：`current_user = ledgerbridge_test`；
     `INSERT INTO audit_event (…) VALUES (…, 'forged', …)` → **成功**；
     `ALTER TABLE posting DISABLE TRIGGER posting_posted_immutable` → **成功**。
  3. `docker-compose.yml:39-44` 的 API healthcheck 每 30s 打一次 `/health/ready`，
     `start_period: 20s`。也就是说在部署形态下，**新建池化连接上的第一个事务几乎必然是只读回滚**，
     该连接此后一直是属主/超级用户。
- **影响**：`append_audit_event` 不再是唯一写入口；哈希链可被任意伪造行延长或分叉；
  POSTED 不可变性、余额触发器、实体边界触发器全部可被应用连接 `DISABLE TRIGGER` 关闭；
  `TRUNCATE audit_event` 亦可用（TRUNCATE 不触发行触发器）。这同时命中
  `CLAUDE.md:30` 的"可损坏证据、金额、可审计性"三项。
- **为什么 CI 没抓到**：见 P1-M2。唯一的守卫 `tests/test_ledger_core.py:457-531`
  之所以通过，是因为在 pytest 顺序下 `runtime_engine` 的**第一个**事务
  （`test_entity_safe_account_identifier_uniqueness:170-204`）恰好 `commit()` 了。
  这是排序巧合，不是设计保证。
- **建议验收条件**（三条都要）：
  1. 按 SQLAlchemy 文档的既定写法在 `connect` 事件里临时切 autocommit 再执行 `SET ROLE`
     （或改用 `SET SESSION AUTHORIZATION` + 专用登录角色），使其不受事务回滚影响；
  2. **应用登录角色不得是属主/超级用户**（见 P1-H1）；
  3. 新增回归测试：在同一 `Engine` 上先做一次**只读且不提交**的 Session，
     再用**复用的池化连接**断言 `current_user = 'ledgerbridge_app'` 且直接
     `INSERT INTO audit_event` 被拒。该测试在当前 `db.py` 上**必须失败**。

---

### HIGH

#### P1-H1 — 应用登录角色即数据库属主（Compose 形态下是集群 superuser），降权只靠会话级 `SET ROLE`，`RESET ROLE` 一句即可恢复全部权限

- **文件与行号**：`.env.example:3`（`LEDGERBRIDGE_DATABASE_URL=…ledgerbridge:change-me@postgres…`）
  与 `docker-compose.yml:70-74`（`POSTGRES_USER: ${POSTGRES_USER}`，同一个 `ledgerbridge`）；
  `alembic/versions/20260821_0002_ledger_core.py:44-55`（迁移里 `CREATE ROLE ledgerbridge_app NOLOGIN`
  并 `GRANT ledgerbridge_app TO current_user`）。
- **违反的不变量**：同 P1-B1（审计写入口唯一性），以及 `AGENTS.md:33`
  「Database migrations and tests must prove invariants; application convention is insufficient」。
- **可复现的失败场景（已实测）**：即使 P1-B1 被修好，在应用连接上执行
  `RESET ROLE`（或 `SET ROLE NONE`）后 `current_user` 立即变回 `ledgerbridge_test`，
  随后 `INSERT INTO audit_event` **成功**。任何一处 SQL 注入、一个疏忽的
  `session.execute(text(...))`、或一次运维直连，都能取回属主权限。
  迁移需要 `CREATE ROLE`，因此该登录角色还必须具备 `CREATEROLE`/superuser，
  也就是说 **`append_audit_event` 这个 `SECURITY DEFINER` 函数的属主就是超级用户**
  （`:263-264`），其权限面被放大到最大。
- **建议验收条件**：拆成两个角色——迁移/属主角色（仅在一次性 `alembic upgrade` 容器中使用，
  凭据不进 API/worker 容器）与运行时登录角色 `ledgerbridge_app`（`LOGIN`，非属主，
  不是任何属主角色的成员）。验收：以运行时凭据连接后，
  `RESET ROLE` 之后 `INSERT INTO audit_event`、`ALTER TABLE … DISABLE TRIGGER`、
  `TRUNCATE audit_event` **三者全部被拒**。

#### P1-H2 — 同一 POSTED 分录可被重复冲销，实际余额被重复计算；数据库层与应用层都没有任何防护

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:146`（`reverses_entry_id` 列）、
  `:190-194`（其 FK）、`:152-164`（三条 CHECK：非自指、adjusts/reverses 互斥）、
  `:196`（唯一约束**只**加在 `audit_event_id` 上）；
  `:335-378`（`journal_entry_validate_relationships` 只校验目标存在/同实体/为 POSTED）；
  测试侧 `tests/test_ledger_core.py:348-374` 只验证了**一次**冲销。
- **违反的不变量**：任务卡 `:62`「POSTED entries are immutable; corrections never rewrite posted history」
  与 `IMPLEMENTATION_BASELINE.md:33`；`AGENTS.md:27-28`（符号约定）。
- **可复现的失败场景（已实测）**：
  ```
  原始分录  POSTED [bank -500, expense +500]        → EXPENSE 合计 = 500
  冲销分录1 POSTED [bank +500, expense -500] reverses=原始 → EXPENSE 合计 = 0     （正确）
  冲销分录2 POSTED [bank +500, expense -500] reverses=原始 → 被接受
                                                     → EXPENSE 合计 = -500，BANK = +500
  ```
  第二条冲销**没有触发任何约束**：`reverses_entry_id` 无唯一索引，
  `journal_entry_validate_relationships` 只要求目标是 POSTED（原始分录永远是 POSTED，见 P1-L1），
  余额触发器只看单条分录内配平。结果不仅金额错，`EXPENSE = -500` 还直接违反
  `AGENTS.md:28`「Asset/expense normal balances are positive」。
  同样的路径也允许"冲销一条冲销分录"，可无限叠加。
- **建议验收条件**：对 `journal_entry.reverses_entry_id` 加**部分唯一索引**
  （`CREATE UNIQUE INDEX … ON journal_entry (reverses_entry_id) WHERE reverses_entry_id IS NOT NULL`），
  或在 `journal_entry_validate_relationships` 中拒绝已被冲销的目标。
  验收：新增测试"对同一 POSTED 分录发起第二次冲销必须被数据库拒绝"，
  且断言打在 `actual_totals_by_class` 上——去掉该约束后测试**必须失败**。

#### P1-H3 — `account` 行可被自由 UPDATE：POSTED 历史被静默改写、实体边界被静默突破，且无任何审计事件

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:75-103`（`account` 表，
  除 `btrim` 与两条唯一约束外**没有任何不可变性触发器**）、
  `:561-563`（`GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE entity, account, journal_entry, posting TO ledgerbridge_app`）；
  `:407-431`（`posting_enforce_entity` 只在 `posting` 的 INSERT/UPDATE 时校验，
  **不监听 `account` 侧的变更**）；
  `src/ledgerbridge/ledger.py:14-24, 31-42`（余额与分类合计都实时读 `Account.account_class` / `Account.entity_id`）。
- **违反的不变量**：任务卡 `:62`（POSTED 不可变）、`:61`（符号约定）、
  `IMPLEMENTATION_BASELINE.md:26`（`Entity -> Account <- Posting -> JournalEntry` 链）与实体边界要求。
- **可复现的失败场景（已实测，且以 `ledgerbridge_app` 身份亦可执行）**：
  1. 记一笔 POSTED `[bank -500, expense +500]`，`actual_totals_by_class` = `{ASSET:-500, EXPENSE:500}`。
     执行 `UPDATE account SET account_class='INCOME' WHERE id=<expense 账户>` →
     合计变成 `{ASSET:-500, INCOME:500}`。**500 minor units 从支出变成收入，
     没有任何分录变动，没有任何 audit event，POSTED 行一个字节都没动。**
  2. 执行 `UPDATE account SET entity_id=<另一实体>` →
     被接受；随后 `SELECT count(*) FROM posting p JOIN journal_entry je … JOIN account a …
     WHERE a.entity_id <> je.entity_id` = **1**，即出现了 `posting_enforce_entity`
     专门要防的跨实体分录；实体 1 的合计静默变成 `{ASSET:-500}`，支出凭空消失。
     `fk_journal_entry_primary_account_entity`（`:184-189`）只在该账户恰好是某分录的
     `primary_account_id` 时才拦得住，而 `primary_account_id` 可空且通常只指向源账户。
  3. 权限确认：`has_table_privilege('ledgerbridge_app','account','UPDATE')` = `t`。
     即使 P1-B1 修好，这条路径仍对运行时角色开放。
- **建议验收条件**：为 `account` 增加不可变性触发器——当该账户已存在归属于 POSTED 分录的
  posting 时，禁止 `UPDATE account_class` 与 `UPDATE entity_id`（`identifier`/`name` 可放行或
  另行留痕）；或把 `posting` 的 `account_id` 改为对 `(id, entity_id)` 的复合 FK，
  使 `entity_id` 变更被 FK 直接拒绝。验收：新增两条测试，
  "改 POSTED 账户的 account_class"与"改 POSTED 账户的 entity_id"必须被数据库拒绝，
  且 `actual_totals_by_class` 在尝试前后完全不变。

#### P1-H4 — 冻结基线里最被点名的那条不变量（触发器同时校验 OLD 与 NEW），其唯一验收测试对"漏掉 OLD"完全不敏感

- **文件与行号**：`tests/test_ledger_core.py:256-280`（`test_posting_move_checks_old_and_new_entries`）；
  被测实现在 `alembic/versions/20260821_0002_ledger_core.py:485-490`
  （`IF TG_OP IN ('UPDATE','DELETE') THEN v_entry_ids := array_append(v_entry_ids, OLD.entry_id)`）。
- **违反的规则**：`CLAUDE.md:13-14`「Do not approve based only on tests written by the implementer;
  inspect whether the tests can fail for the intended defect」；
  `IMPLEMENTATION_BASELINE.md:74`（Phase 1 数据库要求第 1 条）；任务卡 `:60, :72-73`。
- **实测证据**：把 `posting_assert_balanced()` 替换为**只 append `NEW.entry_id`**的缺陷版本后——
  - `pytest tests/test_ledger_core.py::test_posting_move_checks_old_and_new_entries` → **passed**
  - 整套用例（除需要新建库的迁移往返用例外）→ **30 passed，0 failed**

  原因：该测试把 `+100` 的 posting 从 entry1 移到 entry2 后，
  entry1 变成 `-100`（只有 OLD 检查抓得到）而 entry2 变成 `+100`（NEW 检查也抓得到）。
  两侧同时失衡，因此**无法区分实现是否检查了 OLD**。
- **缺陷确实会造成实际损坏（同一缺陷版本下实测）**：
  事务 A 提交两条各自配平的 DRAFT 分录；事务 B 中把 entry2 的抵销 posting 由 `-300` 改成 `-400`，
  同时把 entry1 的 `+100` posting 移入 entry2 → entry2 收支为 0（NEW 通过），
  entry1 只剩 `-100`（OLD 未检查）→ **事务提交成功，账本永久留下一条失衡分录**。
  同一场景在**当前 head 的实现**上会在 COMMIT 抛
  `journal entry … is unbalanced for currency CNY: -100 minor units` ——
  **实现是正确的，不合格的是测试。**
- **建议验收条件**：把 `:256-280` 改成上面这种**只让 OLD 侧失衡**的场景
  （源分录与目标分录先在独立事务中提交，移动后目标分录仍然配平）。
  验收：删掉 `:486` 那一行 OLD append 之后，该测试**必须失败**。

---

### MEDIUM

#### P1-M1 — 审计哈希链在 REPEATABLE READ / SERIALIZABLE 下会分叉，且没有任何约束能阻止

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:279-284`
  （`PERFORM pg_advisory_xact_lock(...)` 后 `SELECT hash INTO v_prev_hash … ORDER BY sequence DESC LIMIT 1`）；
  `:105-124`（`audit_event` 表只有 `sequence` 唯一约束，`prev_hash` 无唯一约束）。
- **实测**：两个并发事务各调用一次 `append_audit_event`——
  - `READ COMMITTED`：`rows=3, chain_linked=True, duplicate_prev_hash=False` ✓ 串行化正确
  - `REPEATABLE READ`：两个事务**均成功提交**，`rows=3, chain_linked=False, duplicate_prev_hash=True`
    —— 两条记录带着**相同的 `prev_hash`**，链断成 Y 形分叉
- **原因**：advisory lock 只保证互斥执行顺序，快照隔离级别下后到者读到的是**取快照时**的链尾。
  当前应用未设置 `isolation_level`（`db.py:26` 只传 `pool_pre_ping=True`），默认 READ COMMITTED，
  因此**今天不可触发**；但这是一条完全没有数据库兜底的隐含前提。
- **建议验收条件**：给 `audit_event.prev_hash` 加唯一约束（`NULL` 天然不冲突，恰好允许唯一的创世行），
  或把链尾读取改为 `SELECT … ORDER BY sequence DESC LIMIT 1 FOR UPDATE`。
  验收：在 `REPEATABLE READ` 下并发调用两次 `append_audit_event`，**必须有一个事务失败**。

#### P1-M2 — 运行时角色控制只有一条断言，且该断言依赖测试执行顺序，检测不到 P1-B1

- **文件与行号**：`tests/test_ledger_core.py:457-531`（唯一断言点在 `:459-476`）；
  `tests/test_health.py:70-77` 用 `sqlite+pysqlite` 建 engine，**完全绕开** `db.py:29-36` 的 PostgreSQL 分支；
  `tests/test_config.py:29-31` 只校验角色名合法性，不校验角色是否生效。
- **实测**：把 `db.py:29` 的分支整体关闭（模拟角色约束失效）后重跑全套 →
  **1 failed, 30 passed**，唯一失败的就是 `test_audit_function_acl_append_only_and_hash_chain`。
  换言之：整个 31 条用例里，运行时降权只有 **1/31** 的覆盖；而这一条只能发现"角色从未设置"，
  发现不了"角色被池回滚撤销"这一真实故障模式（因为它跑在一个第一个事务恰好提交过的连接上）。
- **建议验收条件**：见 P1-B1 第 3 条。另建议把该断言拆成独立用例并显式构造
  "先只读回滚、再复用同一池化连接"的前置条件。

#### P1-M3 — "币种桶独立校验"这条验收测试是对函数源码做字符串匹配，没有真正验证行为

- **文件与行号**：`tests/test_ledger_core.py:282-315`，关键断言在 `:290`
  `assert "GROUP BY p.currency" in definition`；被测实现 `alembic/…:494-500`。
- **问题**：`ck_posting_posting_currency_cny_v01`（`:216`）把币种硬限制为 `CNY`，
  因此在 v0.1 里**不可能**构造真正的多币种分录，测试只好退化为
  "函数定义里出现了这个字符串"。这条断言对任何保留该字面量但把分组写错的实现都会通过
  （例如 `GROUP BY p.currency` 后接 `HAVING SUM(...) <> 0` 被误改为 `HAVING SUM(ABS(...)) <> 0`）。
  任务卡 `:74`「Currency buckets are checked independently」目前是**声明而非验证**。
- **建议验收条件**：在测试中以属主身份临时 `ALTER TABLE posting DROP CONSTRAINT ck_posting_posting_currency_cny_v01`
  （测试库内，事务结束回滚），构造 `CNY` 桶配平而 `USD` 桶失衡的分录，断言 COMMIT 被拒；
  或明确把这条验收标记为"v0.2 多币种时兑现"，并从 Phase 1 验收清单移出，避免误认已验证。

#### P1-M4 — 迁移创建的是**集群级**角色，downgrade 不删除、缺角色时硬失败，且 `pg_dump` 还原到新集群会静默丢掉全部授权

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:44-55`（`CREATE ROLE … IF NOT EXISTS` 守卫 + `GRANT … TO current_user`）
  与 `:572-583`（downgrade 只 `REVOKE`，**无对应的角色/USAGE 清理**）。
- **实测**：
  1. upgrade 后 `pg_roles` 出现 `ledgerbridge_app`；`downgrade 20260821_0001` 后
     `role_still_present=1`、`membership_still_present=1`、`pgcrypto_still_installed=1`。
     downgrade 还**没有**撤销 `:559` 的 `GRANT USAGE ON SCHEMA public TO ledgerbridge_app`。
     即 upgrade 有 `IF NOT EXISTS` 守卫，downgrade 却是非幂等的单向操作。
  2. 先手工 `DROP ROLE ledgerbridge_app` 再跑 downgrade → 迁移**直接报错中止**
     （`REVOKE … FROM ledgerbridge_app` 无守卫）。
  3. 灾难恢复模拟：`pg_dump` 输出含 10 处 `ledgerbridge_app` 引用但 **0 处 `CREATE ROLE`**
     （`pg_dump` 从不导出角色）。把该 dump 还原进一个没有该角色的库 →
     8 条 `ERROR: role "ledgerbridge_app" does not exist`，但**还原照常完成**：
     6 张表齐全、`append_audit_event` 存在、
     `information_schema.role_table_grants WHERE grantee='ledgerbridge_app'` = **0**。
     一个"看起来健康"的库，审计表 ACL 已经整个消失。
- **关联**：F-4（备份/恢复演练）目前排期在 Phase 2，因此这条不是被忽略而是被延期；
  但它把"schema 依赖一个迁移之外的集群对象"这一事实固化了下来。
- **建议验收条件**：(a) downgrade 的 `REVOKE` 全部加 `IF EXISTS` 等价守卫（`DO $$ … IF EXISTS (SELECT 1 FROM pg_roles …)`），
  并补上 `REVOKE USAGE ON SCHEMA public`；(b) 在 `DEPLOYMENT_HERMES.md` 的"Upgrade sequence"里
  写明角色属于集群级对象、`pg_dump` 不含它、恢复流程必须先 `pg_dumpall --roles-only` 或运行引导脚本；
  (c) F-4 的恢复演练必须包含"还原后 `role_table_grants` 非空"的断言。

#### P1-M5 — 无法在单个事务里原子地创建一条 POSTED 分录；唯一可行的 DRAFT→POSTED 两步流程没有写进任何文档

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:437-469`
  （`posting_block_posted_mutation` 的 `INSERT` 分支，INSERT 分支：只要 `NEW.entry_id` 指向的分录是 POSTED 就拒绝插入 posting）；
  与 `:527-550`（`journal_entry_posted_complete` 要求 POSTED 分录至少 2 条 posting）。
- **实测**：`INSERT journal_entry(status='POSTED')` 后再插 posting →
  `postings on POSTED entries are immutable`；若不插 posting，则 COMMIT 时
  `POSTED journal entries require at least two postings`。两条路都堵死，
  **直接写入一条 POSTED 分录在结构上不可能**。
  唯一可行路径是 `tests/test_ledger_core.py:131-168` 里的
  DRAFT → 插 posting → `UPDATE status='POSTED'`。
- **判断**：这属于"至少两个 posting 的约束"与不可变性触发器叠加造成的**合法流程被阻断**，
  但存在等价可行路径，因此不是 HIGH。风险在于：Phase 2 的导入器若按直觉写原子 upsert，
  会在集成阶段才撞上，而那时改写路径的成本比现在高。
- **建议验收条件**：把 DRAFT→postings→POSTED 写成 `README.md` 或
  `docs/architecture/IMPLEMENTATION_BASELINE.md` 中的显式写入协议，
  并补一条测试断言"直接以 POSTED 插入必然失败"，把这个行为**固化为契约而不是副作用**。

#### P1-M6 — 95% 覆盖率门槛的分母不含迁移、脚本与 worker，最安全攸关的 611 行 SQL 完全在门槛之外

- **文件与行号**：`.github/workflows/ci.yml:64`（`pytest --cov=ledgerbridge --cov-fail-under=95`）；
  `pyproject.toml:57-61`（`[tool.coverage.run] omit` 掉 `worker.py` 与 `mail_collector.py`）。
- **实测覆盖率明细**：`config 100% / db 86% / ledger 100% / main 100% / models.ledger 100%`，
  TOTAL 162 条语句。`alembic/versions/20260821_0002_ledger_core.py`（611 行）、
  `scripts/deployment_manifest.py`（139 行）、`worker.py` 一行都不在分母里。
- **判断**：任务卡 F-3 的原话是"core ledger modules remain in the denominator"，这一点**属实**；
  但 `PROJECT_STATUS.md:25-28` 与任务卡 `:106` 把"coverage 97.53%"与迁移/触发器证据并列陈述，
  容易被读成"迁移也被覆盖到 97.53%"。F-5 的 worker 心跳虽有
  `tests/test_worker.py:1-22` 覆盖，却因 `omit` 而不计入门槛。
- **建议验收条件**：在 `PROJECT_STATUS.md` 与任务卡里注明覆盖率分母范围；
  把 `scripts` 纳入 `--cov`（`--cov=ledgerbridge --cov=scripts`），
  并把 `worker.py` 从 `omit` 移除（它已有测试，不会拉低门槛）。

#### P1-M7 — 部署清单的目录排除按名字在任意深度生效，会造成静默的漂移盲区；清单本身未签名

- **文件与行号**：`scripts/deployment_manifest.py:14-24`（`EXCLUDED_DIRECTORIES` 含 `data`、`var`、`secrets`）
  与 `:42`（`if any(part in EXCLUDED_DIRECTORIES for part in relative.parts)`）。
- **失败场景**：Phase 5 的规则引擎若按惯例放在 `src/ledgerbridge/data/rules/`，
  整棵子树会**静默**退出漂移检测——`verify` 依然打印"verified deployment manifest for N files"，
  但那些文件被改了不会被发现。分类规则是版本化数据
  （`IMPLEMENTATION_BASELINE.md:66`），恰恰是最需要绑定 revision 的一类。
  另外 `MANIFEST.sha256` 只是一份并置的哈希列表，没有签名：能改文件的人也能重算清单，
  它是**漂移检测器**而非防篡改证据。
- **建议验收条件**：把排除规则改为**锚定在根目录**的相对路径前缀
  （`var/`、`data/`、`secrets/` 只在顶层生效），或改用显式 allowlist；
  在 `DEPLOYMENT_HERMES.md` 里写明清单未签名及其威胁模型边界。
  验收：新增测试——`root/src/data/x.py` 必须出现在清单中，`root/data/x` 不出现。

#### P1-M8 — 授权审计事件与分录的绑定很弱：一次性、创建前生成、无内容关联，且 DRAFT→POSTED 这一真正动钱的状态迁移不产生任何审计事件

- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py:148`（`audit_event_id` NOT NULL）、
  `:196`（`uq_journal_entry_audit_event_id`）、`:252-332`（`append_audit_event` 在分录存在**之前**返回 uuid，
  因此 payload 里不可能含 entry id）；`:383-401`（POSTED 迁移只被不可变性触发器拦截，不产生审计事件）。
- **判断**：`IMPLEMENTATION_BASELINE.md:65` 只要求"Every journal entry references the audit event
  that authorized its **creation**"，因此**没有违反冻结基线**。但实际效果是：
  数据库只强制"存在一条被引用的 audit_event"，不强制它的 `action`/`payload` 与该分录有关——
  一条 `action='login'` 的事件同样能通过；而"谁在什么时候把这条分录 POST 了"在链上无迹可寻。
- **建议验收条件**（Phase 2 起）：让 `append_audit_event` 接受可选的目标分录 id 并写入 payload，
  或改为分录写入后再补一条 `journal.post` 事件并要求状态迁移必须携带它。
  验收：能从审计链单独重建"某分录何时由谁进入 POSTED"。

---

### LOW

| ID | 文件与行号 | 内容 | 验收条件 |
|---|---|---|---|
| P1-L1 | `alembic/…:383-390` | POSTED 分录**永远**无法变为 REVERSED（任何 `UPDATE` 在 `OLD.status='POSTED'` 时即被拒，已实测）。因此 `journal_status.REVERSED`（`:32`）只能用于从未 POSTED 的分录，系统里**没有任何字段记录"这条分录已被冲销"**——这正是 P1-H2 无法在数据层自查的根因 | 要么删除 REVERSED 枚举值并在文档中说明冲销只用关系表达，要么引入不可变的"已冲销"标记（如 P1-H2 的部分唯一索引即可充当） |
| P1-L2 | `alembic/…:527-550` | DRAFT 分录可以零 posting 长期存在（已实测提交成功），却已消耗一条 audit_event，且 `posting_assert_balanced` 对"零 posting"返回通过 | 明确这是允许状态并写入文档，或对 DRAFT 也设最小 posting 数/清理策略 |
| P1-L3 | `docs/architecture/DEPLOYMENT_HERMES.md:19-63` | 部署 runbook 的 shell 片段没有 `set -euo pipefail`；`verify` 失败、`test "$image_revision" = "$revision"` 失败都不会中止后续 `docker compose build` / `alembic upgrade` | 在每个代码块首行加 `set -euo pipefail`；验收：故意破坏一个文件后整段流程必须在 verify 处停下 |
| P1-L4 | `scripts/deployment_manifest.py:46`（symlink 拒绝）、`:90-96`（绝对路径/`..` 拒绝）对照 `tests/test_deployment_manifest.py:20-49` | 这两条硬化分支**没有任何回归测试**；且 `--cov=ledgerbridge` 不含 `scripts/`，覆盖率完全不体现 | 补两条测试：树中含 symlink 必须 `raise`；清单条目为 `../x` 或 `/x` 必须 `raise` |
| P1-L5 | `.github/workflows/ci.yml:57`、`docker/app.Dockerfile:17` | `pip install "uv==0.12.5"` 版本固定但未 hash 固定，是整条构建链上唯一不受 `uv.lock` 约束的网络依赖 | 改用 `--require-hashes` 或固定 uv 的安装器摘要 |
| P1-L6 | `tests/test_health.py:71-72`、`tests/test_config.py:11` | 多处调用 `get_settings.cache_clear()` / `get_session_factory.cache_clear()` 后不恢复，形成跨文件的顺序耦合（当前按字母序恰好无害） | 改为 fixture 内 `try/finally` 复位，或用 `monkeypatch` 隔离 |
| P1-L7 | `src/ledgerbridge/worker.py:12,22-24` + `docker-compose.yml:49-56` | 心跳文件 `.worker-heartbeat` 直接写在 `artifact_root` 下，把运行态文件混进了本应只放不可变证据的卷 | 心跳改写到 `/tmp`（worker 已挂 tmpfs），或放到 artifact_root 下的专用 `.runtime/` 子目录并在未来的证据枚举中排除 |
| P1-L8 | `alembic/…:285` | `nextval` 在 advisory lock 之后取得，事务回滚不会归还，`audit_event.sequence` 会留下永久空洞。哈希链本身不受影响（靠 `prev_hash` 串联），但按 sequence 连续性做完整性核对的人会得到假阳性 | 在 `docs/` 中写明 sequence 允许空洞、链的唯一权威是 `prev_hash`；或核对脚本按 `prev_hash` 遍历而非按 sequence |

---

## 2. Frozen invariants checklist

| # | 冻结不变量（来源） | 数据库层是否强制 | 结论 |
|---|---|---|---|
| 1 | 每笔分录按币种在事务提交时配平（任务卡 `:59`） | `posting_balanced_per_currency`，CONSTRAINT TRIGGER，DEFERRABLE INITIALLY DEFERRED（`:518-521`） | ✅ 实现正确（实测在 COMMIT 抛错，不是语句级） |
| 2 | 延迟触发器同时校验 OLD 与 NEW（基线要求 1；任务卡 `:60`） | `:485-490` 两侧都 append | ✅ 实现正确 / ❌ **验收测试不敏感 → P1-H4** |
| 3 | 币种桶独立校验（任务卡 `:74`） | `GROUP BY p.currency … HAVING SUM<>0`（`:494-500`） | ⚠️ 代码正确，但 v0.1 的 CNY CHECK 使其无法被真正验证 → P1-M3 |
| 4 | 资产/支出正、负债/收入/权益负（`AGENTS.md:28`） | **无数据库强制**（有意——冲销分录必须能反向） | ⚠️ 仅由 4 条业务场景测试间接守卫；P1-H2 可产生 `EXPENSE=-500` |
| 5 | POSTED 分录及其 posting 不可更新/删除/追加/移走（任务卡 `:62`、`:75`） | `journal_entry_posted_immutable`（`:399-401`）+ `posting_posted_immutable`（`:467-469`） | ✅ 实测六路全封：改分录/删分录/改 posting/删 posting/移出 POSTED/移入 POSTED/POSTED→DRAFT 全部被拒 |
| 6 | 更正不改写历史，用显式 REVERSAL/ADJUSTMENT（任务卡 `:62`、`:76`） | 关系列 + 三条 CHECK + `journal_entry_validate_correction`（`:375-377`） | ❌ **可重复冲销、可重复计算 → P1-H2**；原始行确实保持 POSTED ✅ |
| 7 | 分录引用授权审计事件且无循环依赖（任务卡 `:63-64`） | 先 `append_audit_event()` 返回 uuid，再插分录；`audit_event_id` NOT NULL + UNIQUE | ✅ 无循环依赖 / ⚠️ 绑定很弱 → P1-M8 |
| 8 | 审计事件 append-only、经单一数据库函数哈希串联（基线 `:63-64`；任务卡 `:65`） | `audit_event_no_update_delete`（`:246-248`）+ ACL（`:556-568`） | ❌ **ACL 在运行时失效 → P1-B1 / P1-H1**；UPDATE/DELETE 触发器本身有效 ✅；哈希序列化确定且可复算 ✅；并发在 RC 下正确、在 RR/SER 下分叉 → P1-M1 |
| 9 | 实际余额只含 POSTED，排除 DRAFT/REVERSED（任务卡 `:66`） | `ledger.py:14-24, 31-42` 的 `CASE WHEN status=POSTED` | ✅ 逻辑正确（LEFT JOIN 保留零余额账户，posting→entry 多对一不会重复计数）；⚠️ 结果可被 P1-H3 静默改写 |
| 10 | 账户标识唯一且 entity-safe（基线要求 5） | `uq_account_entity_identifier(entity_id, identifier)`（`:101`）+ `uq_account_id_entity`（`:102`）支撑复合 FK | ✅ 实测两个实体可共用同一 identifier，同实体内重复被拒 |
| 11 | 金额只用有符号整数 minor units（`AGENTS.md:27`） | `amount_minor BIGINT`；全仓 `grep -E "Float\|Numeric\|DECIMAL\|REAL\|double precision" src alembic` 无命中 | ✅ |
| 12 | 不引入含义模糊的 `settlement_status`（基线要求 8） | 全仓无该标识符 | ✅ |
| 13 | 实体边界（`posting.entry` 与 `posting.account` 同实体） | `posting_entity_boundary`（`:429-431`）+ `fk_journal_entry_primary_account_entity`（`:184-189`） | ⚠️ posting 侧有效，**account 侧可被 UPDATE 绕过 → P1-H3** |
| 14 | 迁移真实可逆，CI 断言对象消失与重建（任务卡 `:86-88`、F-7） | `tests/…:601-677` 在**独立临时库**建库→upgrade→断言表/触发器/函数存在→downgrade→断言表消失且 `to_regprocedure(...) IS NULL`→再 upgrade→再断言 | ✅ 断言的是对象而非版本号；⚠️ 集群级角色不在往返范围内 → P1-M4 |
| 15 | 基线要求 3/6/7 的延期去向被保留（上轮 MP-2） | 任务卡 `:42-56` 的 "Deferred baseline requirements" 小节逐条指定了 Phase 2 / Phase 5 承接 | ✅ 上轮 MP-2 已闭环 |
| 16 | 四条纯记账语义验收场景（上轮 MP-1） | `tests/…:533-599` 四条，且断言全部打在 `actual_totals_by_class` 聚合上而非原始 posting 行 | ✅ 上轮 MP-1 已闭环（见 §3 的敏感度实测） |

---

## 3. 实际执行 / 复跑的测试与证据

全部在 **Claude 沙箱内的一次性 PostgreSQL 实例**（`initdb` 到 `/tmp`，端口 55432，用完即弃）
上执行。**未连接 Hermes、未使用任何生产凭据、未改动被审仓库的任何文件。**

### 3.1 原样复跑（确认 Codex 声称的证据属实）

```
uv sync --frozen --extra dev            → 73 个包按 uv.lock 安装，无重解析
pytest --cov=ledgerbridge --cov-fail-under=95
                                        → 31 passed, coverage 97.53%
ruff check . / ruff format --check .    → All checks passed / 22 files formatted
mypy src alembic tests scripts (strict) → no issues in 20 source files
uv export --frozen --no-emit-project    → 1287 行，1088 条 sha256
pip-audit --strict                      → No known vulnerabilities found
alembic upgrade head → downgrade 20260821_0001 → upgrade head  → 全部成功
```

### 3.2 缺陷注入（检验测试能否因目标缺陷而失败）

| 注入的缺陷 | 测试套件反应 | 结论 |
|---|---|---|
| `posting_assert_balanced()` 去掉 `OLD.entry_id` 分支 | `test_posting_move_checks_old_and_new_entries` **passed**；全套 **30 passed / 0 failed** | ❌ 不敏感 → P1-H4 |
| `db.py` 的 `SET ROLE` 分支整体关闭 | **1 failed / 30 passed**（仅审计 ACL 用例） | ⚠️ 1/31 覆盖，且只能发现静态缺失 → P1-M2 |
| 把符号约定接反（对照上轮 MP-1） | 四条业务场景测试断言在 `actual_totals_by_class` 上，方向错误会直接体现在 EXPENSE/INCOME 合计 | ✅ 敏感 |

### 3.3 行为复现（每条 BLOCKER/HIGH 都有实测输出）

```
# P1-B1  连接池回滚撤销 SET ROLE
cycle 0: backend_pid=6279 current_user=ledgerbridge_app
cycle 1: backend_pid=6279 current_user=ledgerbridge_test     ← 同一后端连接，角色已丢失
cycle 2/3: 同上
（对照：每个 cycle 显式 commit() → 4 次全部 ledgerbridge_app）

# P1-B1  真实生产路径
/health/ready -> 200 {'status': 'ready'}
ledger session after readiness probe: current_user=ledgerbridge_test
>>> FORGED audit_event INSERT SUCCEEDED from the application connection
>>> ALTER TABLE ... DISABLE TRIGGER SUCCEEDED from the application connection

# P1-H1  显式逃逸
after RESET ROLE: ledgerbridge_test ledgerbridge_test
>>> RESET ROLE + direct INSERT SUCCEEDED -> audit chain forgeable from app connection

# P1-H2  重复冲销
after original      : EXPENSE = 500
after 1st reversal  : EXPENSE = 0
2nd reversal of the SAME entry ACCEPTED
after 2nd reversal  : EXPENSE = -500  BANK = 500

# P1-H3  账户可变性
before: {'ASSET': -500, 'EXPENSE': 500}
after UPDATE account SET account_class='INCOME': {'ASSET': -500, 'INCOME': 500}
UPDATE account SET entity_id ACCEPTED; postings now crossing entity boundary: 1
entity1 totals now: {'ASSET': -500}
（以 SET ROLE ledgerbridge_app 身份复测：NOTICE: ledgerbridge_app CAN update account_class）

# P1-H4  OLD 缺失的真实后果（缺陷版触发器，跨事务移动）
 entry_sum | postings
      -100 |        1     ← 永久失衡的分录成功提交
         0 |        3
（同一场景在当前 head 上：ERROR: journal entry … is unbalanced for currency CNY: -100 minor units）

# P1-M1  隔离级别
isolation=READ COMMITTED : rows=3 chain_linked=True  duplicate_prev_hash=False
isolation=REPEATABLE READ: rows=3 chain_linked=False duplicate_prev_hash=True

# P1-M4  角色残留与恢复
after downgrade: role_still_present=1  membership_still_present=1  pgcrypto_still_installed=1
downgrade with role dropped → 迁移报错中止
pg_dump 含 10 处 ledgerbridge_app 引用 / 0 处 CREATE ROLE
还原到无该角色的库 → 8 条 role does not exist，但 tables=6, fn_present=t, grants_to_app=0
```

### 3.4 阅读的仓内文件

`AGENTS.md`、`CLAUDE.md`、`PROJECT_STATUS.md`、
`docs/architecture/IMPLEMENTATION_BASELINE.md`、`docs/architecture/DEPLOYMENT_HERMES.md`、
`docs/tasks/2026-08-21-phase-1-core-schema.md`、
`docs/reviews/2026-08-21-phase-1-preflight-claude.md`（以上均为全文）；
以及 head 上的 `alembic/versions/20260821_0001_platform_baseline.py`、
`alembic/versions/20260821_0002_ledger_core.py`（611 行全文）、`alembic/env.py`、
`src/ledgerbridge/{config,db,ledger,main,worker,models/ledger,models/__init__}.py`、
`scripts/{deployment_manifest,check_sensitive_paths}.py`、
`tests/{test_ledger_core,test_config,test_health,test_worker,test_deployment_manifest}.py`、
`.github/workflows/ci.yml`、`docker-compose.yml`、`docker/app.Dockerfile`、
`.env.example`、`.gitignore`、`pyproject.toml`、`uv.lock`（结构与导出验证）、`README.md`。

---

## 4. 已检查且未发现问题的项

1. **POSTED 不可变性是完整的**：改分录、删分录、改 posting、删 posting、把 posting 移出 POSTED 分录、
   把 posting 移入 POSTED 分录、POSTED→DRAFT，七条路径实测**全部被数据库拒绝**；
   删除 entity/account 被 `RESTRICT` 链拦住。
2. **余额触发器确实是延迟的**：失衡在 `COMMIT` 抛出而非语句结束时，
   允许"先建分录再逐条插 posting"的正常写入序列。
3. **哈希序列化确定、可复算、无时区与 JSON 顺序歧义**：
   `jsonb_build_object` 的键序由 jsonb 规范化决定；payload 传入 `{"z":1,"a":2,"a":3}`
   落库为 `{"a": 3, "z": 1}` 且哈希复算通过；
   在 `TimeZone` 为 `UTC` / `Asia/Shanghai` / `America/New_York` 三种会话设置下复算**均为 true**
   （`to_char(... AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')` 与本地时区无关）。
4. **审计链在默认隔离级别下的并发是正确的**：`pg_advisory_xact_lock` + READ COMMITTED
   下两个并发 writer 产生正确串联的链，无重复 `prev_hash`。
5. **`ledgerbridge_app` 的 grant 设计本身是对的**（前提是角色真的生效）：
   `has_table_privilege` 实测 `audit_event`: SELECT=t / INSERT=f / TRUNCATE=f；
   `journal_entry`/`account` TRUNCATE=f；`SET session_replication_role='replica'` 被拒
   （`permission denied to set parameter`）。缺陷在角色的**送达**，不在 ACL 的**设计**。
6. **四条财务验收场景的断言打在聚合上而非原始行上**：等额内部转账 `INCOME=0, EXPENSE=0`；
   0.10 手续费 `EXPENSE=10`（恰好 10 minor units）；信用卡消费+还款 `EXPENSE=1000`（还款计 0）；
   部分退款 `EXPENSE=200, INCOME=0`。这四条对"把还款当消费""漏掉状态过滤""符号接反"都敏感。
7. **`actual_account_balances` / `actual_totals_by_class` 的连接与分组正确**：
   `outerjoin` 保留零余额账户；`CASE WHEN status=POSTED` 把 DRAFT/REVERSED 归零而不是过滤掉行，
   因此不会漏算账户；posting→entry 是多对一，不产生行放大；
   `actual_totals_by_class` 预置全部 `AccountClass` 为 0，无缺键。
   `test_balanced_entry_commits_and_actual_balance_is_posted_only:206-231`
   同时放入 POSTED/DRAFT/REVERSED 三条分录，断言只有 POSTED 被计入 ✅。
8. **迁移往返是真实的**：在**另建的临时数据库**里 upgrade→断言表/触发器存在→
   downgrade→断言表消失且 `to_regprocedure('append_audit_event(...)') IS NULL`→再 upgrade→再断言，
   并在 `finally` 里 `pg_terminate_backend` + `DROP DATABASE`。这是 F-7 的正确形态，不是版本号记账。
9. **F-2 锁文件被三处一致消费**：本地 `uv sync --frozen`（实测成功）、
   CI `:58` `uv sync --frozen` 与 `:59-70` 全部 `uv run --frozen`、
   Docker `app.Dockerfile:20,23` 两段 `uv sync --frozen`。
   `--frozen` 语义上不重解析；Docker 分两层同步（先依赖后项目）也不会引入二次解析。
10. **F-5 严格依赖审计的构造是对的**：`uv export --no-emit-project` 把本地包排除，
    避免 `--strict` 因不可审计的本地项目假失败；导出文件带 1088 条 sha256。
    独立复跑 `pip-audit --strict` → No known vulnerabilities found。
11. **gitleaks 覆盖全历史**：`ci.yml:19-22` 的 checkout 带 `fetch-depth: 0` 且有注释说明；
    action 与 checkout/setup-python 均按 SHA 固定。
12. **部署清单的四项硬化都成立**：文件集合与逐文件 SHA-256 双向比对（漂移）；
    `is_symlink()` 直接 `raise`（symlink）；条目拒绝绝对路径与 `..`，且实际读取用的是
    `rglob` 得到的 Path 而非清单字符串（路径穿越，双重防护）；
    `.env` 在 `EXCLUDED_FILENAMES`（secret 纳入），测试 `:20-25` 显式断言 `.env` 不出现在清单里。
13. **revision 绑定是自洽的**：`docker-compose.yml:4` 用 `LEDGERBRIDGE_REVISION` 作**镜像 tag**、
    `:7-9` 用 `DEPLOYED_REVISION` 作 **label 值**，看似两个变量控制身份的两半，
    但 `DEPLOYMENT_HERMES.md:12, :39-41, :51-53` 明确规定前者为 short SHA、后者为 full SHA，
    并在部署序列中显式比对 `image_revision = $(cat DEPLOYED_REVISION)`。
    `create_manifest` 强制 revision 为 40 位小写十六进制，`verify` 用 `hmac.compare_digest` 比对。
14. **`openapi_url` 确实关闭**：`main.py:11-17` 三个都是 `None`；
    `tests/test_health.py:39-40` 断言 `/openapi.json` 为 404；部署 runbook `:57-59` 再验一次。
15. **worker 心跳反映工作循环而非进程存在**：`worker.py:56-59` 的循环体每 5s 原子重写心跳文件
    （先写 `.tmp` 再 `replace`），healthcheck 读 30s 新鲜度窗口；
    循环若卡死则文件转陈旧 → 容器不健康。`heartbeat_is_fresh` 还拒绝未来时间戳与畸形内容。
16. **范围未越界**：无 RawArtifact / SourceRecord / ImportJob / connector / parser / UI / LLM 实现；
    `journal_entry.source_record_id`（`:144`）是无 FK 的可空 UUID 占位列，符合 SourceRecord 尚未存在的现实
    （Phase 2 必须为其补上 FK，见 §5）。
17. **无凭据或财务数据入库**：`.gitignore` 覆盖 `.env*`、密钥、`*.eml/xlsx/csv/ofx/qif/pdf/sql`；
    `check_sensitive_paths.py` 逐条 `git check-ignore` 15 个高危文件名；`secrets` job 全历史扫描通过。
18. **三条提交作者一致**：全部 `Codex <codex@ledgerbridge.local>`，与 `PROJECT_STATUS.md:37-39` 记录的
    实施 owner 一致（均未 GPG 签名，与既有约定一致）。
19. **上轮 LP-2 已闭环**：本次复审分支用 `--no-track` 创建，`branch.*.remote` / `.merge` 均为空，
    不存在把复审提交推到实施分支的默认路径。
20. **上轮 LP-1 已闭环**：`ci.yml:70` 现在确实是 `pip-audit --strict`，与 `AGENTS.md:45-46` 的措辞一致。

---

## 5. 残余风险与 Phase 2 阻断项

| 风险 | 性质 | 结论 |
|---|---|---|
| 应用容器持有集群 superuser 凭据 | Phase 0 遗留的部署形态，Phase 1 因引入审计链而首次变得关键 | **必须在 Phase 1 合并前处置**（P1-H1）；Phase 2 引入真实财务证据后不可接受 |
| 账户维度可变 → 已 POSTED 的报表口径可被静默改写 | 数据库层缺失 | **必须在 Phase 1 合并前处置**（P1-H3）；Phase 5 的分类规则引擎会大量改 account 属性，届时成本更高 |
| 冲销关系无唯一性 | 数据库层缺失 | **必须在 Phase 1 合并前处置**（P1-H2） |
| 审计链的隔离级别前提 | 未文档化、无数据库兜底 | 建议同批处置（P1-M1）；一旦有人配置连接池或 pgbouncer 改变默认隔离级别即升级为 HIGH |
| 集群级角色不随迁移/备份走 | F-4 已排期 Phase 2 | Phase 2 的 F-4 恢复演练**必须**包含"还原后 `role_table_grants` 非空"断言（P1-M4） |
| `source_record_id` 目前无 FK | 有意为之，SourceRecord 尚不存在 | **Phase 2 阻断项**：建 SourceRecord 时必须补 FK，且按基线要求 3，RawArtifact 删除不得级联删除 SourceRecord（任务卡 `:47-49` 已登记） |
| 分支保护缺失（F-6） | GitHub Free 私有仓平台限制 | 维持上轮判断：当前不阻断；触发条件见 `PROJECT_STATUS.md:63-66` |
| 备份/恢复自动化（F-4） | 已排期 Phase 2 | 与 P1-M4 合并处理 |
| 多币种校验无法在 v0.1 验证 | v0.1 CNY 约束的必然结果 | v0.2 引入第二币种时，`GROUP BY p.currency` 必须补真实行为测试（P1-M3） |
| 隔离仅靠约定（工作区/写权限） | 上轮已判定为可接受残余风险 | 本轮复核：三条提交作者一致、复审分支无 upstream，约定被实际遵守 ✅ |
| 本轮未验证 Hermes 部署声明 | 按指令未做任何 SSH/容器操作 | `PROJECT_STATUS.md:25-30` 中关于 Hermes 隔离运行与生产未变更的陈述**属本轮范围外未验证项**，不构成 finding，但也不应被本报告视为已核实 |

---

## 6. 最终结论

**CHANGES REQUIRED**

依据 `AGENTS.md:49`「Claude review has no unresolved BLOCKER or HIGH findings before merge」：
本轮存在 **1 条 BLOCKER 与 4 条 HIGH**，因此 PR #5 不能合并。

需要强调的是，这份判定**不是**因为实现质量差。恰恰相反：

- 触发器 SQL 的**语义是正确的**——OLD/NEW 双查、延迟到 COMMIT、按币种分桶、
  POSTED 七路封死、实体边界、更正目标校验，逐条实测通过；
- 审计函数的**哈希序列化是可信的**——确定、可复算、跨时区一致、jsonb 规范化自洽，
  在默认隔离级别下并发正确；
- ACL、迁移往返、锁文件、CI 门槛、部署清单的**设计意图全部到位**，
  上一轮的 MP-1 / MP-2 / LP-1 / LP-2 四条也都真实闭环。

问题集中在**三个"设计对了但没落地"的接缝**上：

1. **P1-B1**——降权动作被连接池悄悄撤销。这是整份 PR 里唯一一处"绿灯掩盖了失效控制"的地方：
   一个正确的 ACL 设计，因为一行客户端代码的事务语义，在部署形态下几乎必然失效，
   使 append-only 审计链、POSTED 不可变性、余额触发器**全部可被应用自身关闭**。
   它符合 `CLAUDE.md:29` 对 BLOCKER 的定义的全部三项。
2. **P1-H2 / P1-H3**——两处"数据库没有兜底、应用层也还不存在"的空档。
   Phase 1 的整个论点是"database-enforced"，这两条恰恰是数据库没有强制、
   而 Phase 2 的导入器会天天走到的路径。
3. **P1-H4**——冻结基线里被特意点名的那条不变量，其唯一守卫对目标缺陷不敏感。
   实现今天是对的，但没有任何东西能阻止它明天被重构掉。
   这正是 `CLAUDE.md:13-14` 和任务卡 `:118-124` 要求本轮必须查、且明确禁止仅凭测试输出批准的那一点。

**建议的处置顺序**（P1-B1 与 P1-H1 必须同批，单修其一无效）：

1. P1-B1 + P1-H1：`SET ROLE` 改为不受回滚影响，并把运行时登录角色降为非属主；
   补"复用池化连接"回归测试。
2. P1-H2：`reverses_entry_id` 部分唯一索引 + 聚合层断言测试。
3. P1-H3：`account` 不可变性触发器（或 posting 侧复合 FK）+ 两条拒绝测试。
4. P1-H4：把移动场景改成只让 OLD 侧失衡，并验证删掉 OLD 分支后测试确实变红。
5. MEDIUM 各条可在同一轮或紧随其后处置；其中 P1-M1、P1-M4 建议与上面同批，
   P1-M6、P1-M7 可随后。LOW 各条不阻断合并。

修复后建议重新提交一次同等力度的复审：以上五条都附了**可执行的验收条件**，
每一条都要求"移除修复后测试必须失败"，可以直接作为下一轮的核对清单。

---

## 附录 A：本轮实际执行的命令

### A.1 在复审克隆 `G:\我的云端硬盘\AI\LedgerBridge-Claude` 内（只读）

```powershell
git rev-parse --show-toplevel ; git status --porcelain=v1 ; git remote -v
git fetch origin --prune --tags
git rev-parse origin/main                                   # 55f88dd9…
git rev-parse origin/ai/chatgpt/phase-1-core-schema         # 80d01ee3…
git switch --no-track -c ai/claude/phase-1-core-schema-review origin/ai/chatgpt/phase-1-core-schema
git rev-parse HEAD ; git rev-parse --abbrev-ref HEAD
git config --get branch.ai/claude/phase-1-core-schema-review.remote   # 空 → 无 upstream
git merge-base origin/main HEAD                             # 55f88dd9…
git log --format="%h | %an <%ae> | sig=%G? | %s" origin/main..HEAD
git diff --stat origin/main..HEAD ; git ls-tree -r --long HEAD
git diff --numstat HEAD                                      # 空 → 工作树内容与 HEAD 完全一致
git hash-object <files> ; git rev-parse HEAD:<files>         # 逐一比对，确认复现用的文件即 head 内容
git show HEAD:.gitignore
```

GitHub REST API（只读 GET；凭据取自 Windows 凭据管理器，未打印）：

```
GET /repos/maiziwheat520-boop/caiwu/pulls/5
GET /repos/.../commits/80d01ee3afe9f6b9954e8a93ec206f174d73880f/check-runs
GET /repos/.../commits/80d01ee3afe9f6b9954e8a93ec206f174d73880f/check-suites
GET /repos/.../actions/runs?head_sha=80d01ee3afe9f6b9954e8a93ec206f174d73880f
```

### A.2 在 Claude 沙箱内的一次性 PostgreSQL 实例（不接触 Hermes / 不使用生产凭据）

```bash
initdb -D /tmp/pgdata -U lbowner --auth=trust        # 独立集群，端口 55432，用完即弃
createrole ledgerbridge_test / createdb ledgerbridge_test   # 复刻 CI 的单角色形态
uv sync --frozen --extra dev -p 3.12
pytest -q --cov=ledgerbridge --cov-report=term-missing --cov-fail-under=95
ruff check . ; ruff format --check . ; mypy src alembic tests scripts
uv export --quiet --frozen --extra dev --no-emit-project --format requirements.txt -o /tmp/req.txt
pip-audit --strict --no-deps --requirement /tmp/req.txt
alembic upgrade head ; alembic downgrade 20260821_0001 ; alembic upgrade head
pg_dump -d ledgerbridge_test > /tmp/lb.sql ; DROP ROLE ledgerbridge_app ; psql -d lb_restore -f /tmp/lb.sql
```

复现脚本（均为一次性沙箱脚本，未写入被审仓库）：

- `probe_role.py` / `probe2.py` —— 池化连接上的 `current_user` 演化、`RESET ROLE` 逃逸
- `probe_prod.py` —— `TestClient(app).get("/health/ready")` 之后在同一池化连接上伪造 audit_event、
  关闭 `posting_posted_immutable`
- `probe_ledger.py` —— 重复冲销、POSTED→REVERSED、`account` 可变性、直接插入 POSTED 分录
- `probe_chain.py` —— READ COMMITTED / REPEATABLE READ 下的并发哈希链、跨时区哈希复算
- 缺陷注入：`CREATE OR REPLACE FUNCTION posting_assert_balanced()`（去掉 OLD 分支）后重跑测试；
  临时关闭 `db.py` 的 `SET ROLE` 分支后重跑测试。两次注入结束后均已还原。

### A.3 本轮**没有**做的事

- 没有推送任何分支，没有合并，没有创建/修改 PR
- 没有写入 `LedgerBridge-Codex` 或已退役的 `LedgerBridge` 克隆
- 没有 SSH 到 Hermes、没有触碰生产 API/worker/PostgreSQL/卷/`.env`
- 没有修改被审仓库中除本报告以外的任何文件（`git diff --numstat HEAD` 为空可证）
