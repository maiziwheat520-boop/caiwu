# Independent security audit: Phase 2 evidence preservation and import framework

| 项 | 值 |
|---|---|
| Reviewer | Claude（独立只读复审克隆） |
| Date | 2026-08-22 |
| Repository | `maiziwheat520-boop/caiwu`（private） |
| PR | #10 — "Phase 2: evidence preservation and import framework"（已于 2026-08-22T04:41:16Z 合并） |
| Audit base | `232378ef70f3cfa24324dc6add61ce6089d107b4` |
| Merged SHA | `23bfbd3bcc79068c3744dab05a961d497590ec8e`（= `origin/main`） |
| Final executable SHA | `b092eb88772d30964524c7475ee96b0ccc86c395` |
| Review clone | `G:\我的云端硬盘\AI\LedgerBridge-Claude` |
| Review branch | `ai/claude/phase-2-evidence-import-review`（`--no-track`，无 upstream） |
| Verdict | **CHANGES REQUIRED** — 1 BLOCKER / 1 HIGH / 8 MEDIUM / 8 LOW |

本轮为 fixed-SHA 只读安全审计，范围是 `git diff 232378ef..23bfbd3b`（21 文件 +3884/−38）。
未推送、未合并、未创建 PR、未写入 Codex 克隆或已退役克隆、未触碰 Hermes 与生产数据。
唯一新增文件是本报告。全部结论均在 Claude 侧一次性沙箱 PostgreSQL 上独立复现，
**不采信 Codex 自审报告的任何"已修复"结论**；该报告仅用于交叉核对措辞。

---

## 0. 审计对象与 CI 证据核验

| 核验项 | 结果 |
|---|---|
| `origin/main` | `23bfbd3bcc79068c3744dab05a961d497590ec8e` ✓ 与指定 merged SHA 一致 |
| merged SHA 父提交 | `232378ef…`（base，第一父） + `87e48d7c…`（PR head，第二父）✓ |
| `git diff 87e48d7c 23bfbd3b` | **空** —— 合并提交未引入任何 PR 之外的内容 ✓ |
| `git diff b092eb88..23bfbd3b` | 仅 `PROJECT_STATUS.md`、Phase 2 任务卡、Codex 自审报告三个文档 → **b092eb88 确为最终可执行 SHA** ✓ |
| PR #10 元数据 | `state=closed merged=true commits=5 changed_files=21 merged_by=maiziwheat520-boop` ✓ |
| CI（`87e48d7c`） | 6 个 check run 全 `success`（`secrets`/`quality`/`compose` × push + pull_request）✓ |
| CI（`23bfbd3b`，main） | 3 个 check run 全 `success` ✓ |
| CI（`b092eb88`） | **0 个 check run**。其可执行内容与 `87e48d7c` 完全一致（差异仅三个文档），因此仍被上面的 6/6 覆盖；但"最终可执行 SHA"本身没有独立 CI 记录，这一点应在流程上写清 |
| 本地工作树 | 建分支前 `git diff --numstat HEAD` 为空；本轮以 `git show <SHA>` 与只读 packfile 拷贝审计，未 checkout 被审代码 |
| 提交作者 | `1044d66` / `7ab9e52` / `b092eb8` 均为 `Codex <codex@ledgerbridge.local>`；两条文档提交同上 |
| 范围越界 | `grep -rnIE "RawArtifact\|SourceRecord\|ImportJob\|settlement_status\|openai\|anthropic\|llm\|connector\|parser" src alembic tests scripts` 无越界实现（仅 `argparse` 的 `parser` 变量名）✓ |

**复现环境**：把复审克隆 `.git/objects/pack` 的两个 packfile 只读拷入 Claude 沙箱重建裸库，
`git archive 23bfbd3b` 展开，并逐文件用 git blob hash 校验字节一致（全部 OK）。
PostgreSQL 16.13 一次性实例，按 CI 拓扑建 `ledgerbridge_owner`（属主）与
`ledgerbridge_app`（LOGIN / NOSUPERUSER / 非属主 / 无 `CREATE`）。

**独立复跑**：`110 passed`，coverage **96.22%**（与任务卡与 Codex 报告声称一致）。
`ruff` / `ruff format --check` / `mypy --strict` / `pip-audit --strict` 均通过。

> 环境差异说明：复现实例为 PostgreSQL 16.13，CI 与生产为 `postgres:15-alpine`。
> 本报告依赖的行为（`pg_temp` 名称解析顺序、触发器时序、`xmin` 与 `pg_current_xact_id()`、
> 列级 GRANT、`TEMPORARY` 默认授予 PUBLIC）在 15 与 16 之间一致，未依赖 16 独有语义。

---

## 1. Findings

严重度按 `CLAUDE.md:30-33`。每条含 severity、精确 file:line、被违反的不变量、
可复现攻击路径、影响、最小修复与验收条件。

### BLOCKER

#### P2-B1 — 最小权限运行时角色仍可用 `pg_temp` 影子表关停 Phase 1 的全部账本不变量

- **Severity**：BLOCKER（`CLAUDE.md:30` —— 可损坏金额与可审计性）
- **文件与行号**：`alembic/versions/20260821_0002_ledger_core.py`
  - `:286` `account_block_protected_dimension_change()`，非限定引用在 `:299-300`
  - `:421` `journal_entry_validate_relationships()`，非限定引用在 `:439`
  - `:467` `journal_entry_block_posted_mutation()`
  - `:496` `posting_enforce_entity()`，非限定引用在 `:504-505`
  - `:526` `posting_block_posted_mutation()`，非限定引用在 `:536`、`:545`
  - `:564` `posting_assert_balanced()`，非限定引用在 `:584`
  - `:616` `journal_entry_assert_posted_complete()`，非限定引用在 `:626`、`:634-635`

  以上函数**无一个带 `SET search_path`**。对照本 PR 已加固的两个函数：
  `alembic/versions/20260821_0003_evidence_import.py:306-309`（`raw_artifact_validate_audit`）
  与 `:413-416`（`journal_entry_validate_post_audit`），二者都设了
  `SET search_path = pg_catalog` 并把关系名限定为 `public.`。
- **被违反的不变量**：`AGENTS.md:27-33`（整数 minor units、符号约定、每币种配平、
  POSTED 不可变、"application convention is insufficient"）、
  `IMPLEMENTATION_BASELINE.md:26/33/74/77`、Phase 1 任务卡 `:59-62`。
- **为什么属于本次审计范围**：`0002` 本身不在 `232378ef..23bfbd3b` 的改动清单内，
  漏洞在时间上先于本 PR；但
  (a) 它在被审 SHA 与 `main` 上现在就是 live 的；
  (b) 本 PR 的 `CDX-H1` 明确声称已修复该攻击类别，实际只覆盖 10 个触发器函数中的 2 个；
  (c) `docs/architecture/EVIDENCE_IMPORT.md:96-99` 把 Phase 2 POSTED 绑定的安全性
  **显式建立在**这些延迟余额 / 完整性 / 实体约束之上。
- **可复现攻击路径**（全部以 `ledgerbridge_app` 身份实测；PostgreSQL 对 `pg_temp`
  在**关系与类型**解析上隐式优先于 `pg_catalog`，函数与操作符不受影响）：

  ```sql
  -- 前提：TEMPORARY 权限默认授予 PUBLIC，仓库中从未 REVOKE
  CREATE TEMP TABLE probe(x int);                          -- 实测成功

  -- ① 提交失衡分录
  BEGIN;
    …建 entity / account / DRAFT journal_entry（均写 public.）…
    CREATE TEMP TABLE posting (id uuid, entry_id uuid, account_id uuid,
                               amount_minor bigint, currency text);
    INSERT INTO public.posting … (-100), (+1);              -- 真实表，合计 -99
  COMMIT;                                                   -- 实测：提交成功
  ```

  | # | 攻击 | 无影子表（对照） | 有影子表 | 实测结果 |
  |---|---|---|---|---|
  | ① | 提交失衡分录 | 拒绝 | **接受** | `entry_sum=-99, n=2` |
  | ② | 删除 POSTED 分录的 posting | 拒绝 | **接受** | 剩 1 条 posting，POSTED 历史失衡 |
  | ③ | 修改 POSTED 分录的 posting 金额 | 拒绝 | **接受** | `500 → 4242`，status 仍 POSTED |
  | ④ | POST 一条只有 1 条真实 posting、账户属于**另一实体**、合计 +777 的分录 | 拒绝（`posting entry and account must belong to the same entity`） | **接受** | `POSTED / real_postings=1 / sum=777 / cross_entity=t`，并携带**合法的 `journal.post` 审计事件** |
  | ⑤ | 已有 POSTED posting 的账户改 `account_class` | 拒绝（`account_class is immutable after POSTED use`） | **接受** | `EXPENSE → INCOME` |

  攻击者所需条件仅为"能以运行时角色执行任意 SQL"：持有 `.env` 中的
  `LEDGERBRIDGE_APP_DB_PASSWORD`，或在应用进程内执行任意 Python
  （Phase 3 的第三方 connector 正处于这个位置）。
  运行时角色**无法** `ALTER TABLE … DISABLE TRIGGER`、`TRUNCATE audit_event`、
  `SET session_replication_role`（三项均实测被拒），`pg_temp` 是它唯一可用的通道。
- **影响**：每币种配平、POSTED 不可变、实体边界、POSTED ≥2 posting、账户维度不可变
  五条数据库级金额保证全部被击穿。第 ④ 行最严重：哈希链会为被篡改的账本状态
  出具有效的 `journal.post` 证明——防篡改链认证了被篡改的数据。
- **最小修复**（两条都要）：
  1. 在 `docker/postgres-init-runtime-role.sh` 增加
     `REVOKE TEMPORARY ON DATABASE <db> FROM PUBLIC;`。
     实测：执行后同一条失衡分录立刻被拒绝
     （`journal entry … is unbalanced for currency CNY: -99 minor units`）。
  2. 给 `0002` 中全部触发器函数加 `SET search_path = pg_catalog`，
     并把 `posting` / `journal_entry` / `account` 一律改为 `public.` 限定，
     与 `0003` 的写法保持一致。
- **验收条件**：为上表五条攻击各补一条影子表回归测试；
  把任一函数的 `public.` 限定或 `SET search_path` 去掉后，对应测试**必须失败**。
  当前全套 110 条测试对这五条攻击的敏感度为 **0**。

---

### HIGH

#### P2-H1 — `open_verified()` 的摘要校验对连接器真正读到的字节不成立（TOCTOU）

- **Severity**：HIGH（`CLAUDE.md:31` —— 违反冻结不变量并允许实质错误结果）
- **文件与行号**：`src/ledgerbridge/artifacts.py:133-143`
  —— `:136` 打开①校验摘要并关闭，`:138` **按路径二次打开**：

  ```python
  destination = self._destination(artifact.sha256)
  self._verify_path(destination, artifact.sha256, artifact.byte_size)   # open ①：读、算摘要、关闭
  flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
  descriptor = os.open(destination, flags)                              # open ②：中间窗口可替换
  ```
- **被违反的不变量**：`IMPLEMENTATION_BASELINE.md:41`
  「Raw artifacts are immutable and keyed by SHA-256」；
  `docs/architecture/EVIDENCE_IMPORT.md:16-17`
  「An existing blob is accepted only after its size and digest are recomputed」。
- **可复现攻击路径（实测）**：在 `_verify_path` 返回后、`os.open` 之前
  `os.replace()` 一个同长度不同内容的文件 —— 连接器拿到 `b'BBBBBBBBBB'`，
  而存储层认为交付的是 `b'AAAAAAAAAA'`。
- **前提**：以应用 UID 写入 shard 目录。`docker-compose.yml:53` 中 worker 的 artifact
  卷为读写；连接器又在同进程内运行；`ReadOnlyArtifactStream` 还会泄露宿主路径与 fd
  （见 P2-M6），使定位目标文件变得容易。
- **影响**：`raw_artifact.sha256` 不再描述被解析的字节，SourceRecord 溯源失真；
  证据链的核心承诺（摘要即身份）在读取路径上不成立。
- **最小修复**：只 open 一次（`O_RDONLY | O_NOFOLLOW`），用 `os.fstat(fd)` 校验类型与大小、
  经该 fd 读完计算摘要、`os.lseek(fd, 0, 0)` 后把**同一个 fd** 交出去。
- **验收条件**：新增"校验通过后替换文件"的回归测试；恢复二次 `os.open` 后该测试必须失败。

---

### MEDIUM

#### P2-M1 — 证据篡改被记录为连接器合约失败

- **文件与行号**：`src/ledgerbridge/imports.py:327-339`
  （`except (ConnectorContractError, ArtifactIntegrityError, OSError):` →
  `error_code="CONNECTOR_CONTRACT"`, `summary="connector parse failed validation"`）
- **失败场景（实测）**：在 ingest 之后、parse 之前篡改磁盘上的证据 →
  job 与 `import.complete` 审计事件都记为 `CONNECTOR_CONTRACT`。
  运维按"证据完整性事件"检索时，系统里最强的篡改信号是不可见的。
- **最小修复**：为 `ArtifactIntegrityError` 单列错误码（如 `EVIDENCE_INTEGRITY`）。
- **验收条件**：篡改场景下 `import_job.error_code` 与审计载荷必须与连接器 bug 可区分。

#### P2-M2 — 关键异常逃出 `ingest_and_import`，失败导入不留任何审计痕迹

- **文件与行号**：`src/ledgerbridge/imports.py:107`（`self._store.publish(stream)`）、
  `:289` 与 `:311`（`self._find_or_create_job(...)`）—— 三处均在任何 `try` 之外。
- **失败场景（实测）**：
  1. 磁盘上的证据已被篡改时再次 ingest → `ArtifactIntegrityError` 逃出公共 API；
  2. 连接器身份漂移（见 P2-L1）→ `DataError (StringDataRightTruncation)` 逃出。

  两种情况都**没有 ImportJob、没有 `import.complete` 审计事件**，导入彻底无痕消失。
- **最小修复**：把这两处纳入 `_route_terminal` 的终态化路径。
- **验收条件**：任何 `ingest_and_import` 调用都必须落一条终态 ImportJob 与一条审计事件，
  或以受控异常类型返回；新增两条对应回归测试。

#### P2-M3 — RawArtifact 审计载荷与元数据一致性检查没有行为敏感测试

- **文件与行号**：`alembic/versions/20260821_0003_evidence_import.py:331-337`（被测分支）；
  `tests/test_evidence_import.py` 中 `test_raw_artifact_requires_fresh_semantic_audit_and_digest_key_binding`
  的第三段断言为 `match="storage_key_matches_sha256"`，命中的是**表 CHECK**（`0003:78-82`），
  不是触发器的载荷比对分支。
- **失败场景（缺陷注入实测）**：删掉该分支后全套 **110 passed**。
  去掉该检查后，持有运行时凭据者可在同一事务里铸造一条声称
  `source="bank-of-china"` 的 `artifact.ingest` 事件，却插入 `source="attacker"` 的
  artifact 行，审计链将为错误的溯源背书。
- **附带缺陷**：`original_filename` 根本不在审计载荷内
  （`imports.py:177-183` 构造载荷 vs `models/evidence.py:65` 的列），
  该字段不受哈希链保护。
- **最小修复**：补一条"`source`/`media_type`/`byte_size` 与载荷不一致必须被拒"的测试；
  把 `original_filename` 纳入载荷与比对。
- **验收条件**：删掉 `0003:331-337` 后测试必须变红。

#### P2-M4 — downgrade 静默销毁全部证据行，且破坏性说明缺失

- **文件与行号**：`alembic/versions/20260821_0003_evidence_import.py:556-559`
  （`drop_index` + `drop_table source_record / import_job / raw_artifact`，无任何守卫）。
  对照：upgrade 侧对存量 POSTED 分录**有**拒绝守卫（`0003:275-287`）。
- **被违反的规则**：`docs/architecture/EVIDENCE_IMPORT.md:31-33`
  「`RawArtifact` metadata and `SourceRecord` rows are permanent and immutable …
  must not delete either database row」；`AGENTS.md:47`
  「destructive downgrade limitations are documented」。
  Phase 2 任务卡 `:235-236` 只写了"downgrade removes only Phase 2 objects/bindings"，
  未说明它会删除已保全的证据行；`EVIDENCE_IMPORT.md` 完全没有提 downgrade。
- **失败场景（实测）**：库中已有 1 条 `raw_artifact` 时执行
  `alembic downgrade 20260821_0002` → **直接成功**，三张证据表全部消失。
  CI 例行跑 `alembic downgrade base`，该操作在肌肉记忆中属于"安全动作"。
- **最小修复**：downgrade 首步加与 upgrade 对称的守卫——存在任一
  `raw_artifact`/`source_record` 行即 `RAISE EXCEPTION`，需显式 override 才继续；
  并在任务卡与 `EVIDENCE_IMPORT.md` 写明该限制。
- **验收条件**：有证据数据时 downgrade 必须失败；无数据时仍可 round-trip。

#### P2-M5 — 分录**创建**审计绑定仍无 action / 新鲜度 / 载荷校验

- **文件与行号**：`alembic/versions/20260821_0003_evidence_import.py:413-424`
  —— `journal_entry_validate_post_audit` 的 INSERT 分支只检查
  "不得直接 POSTED"，对 `NEW.audit_event_id` 不做任何语义校验。
- **失败场景（实测 Z1）**：用一条 `artifact.ingest` 事件作为 journal entry 的创建证据
  → **接受**。事件可以来自任意历史事务、任意 action，且可与某条 `raw_artifact`
  共用（`raw_artifact.audit_event_id` 与 `journal_entry.audit_event_id`
  是两个独立唯一约束，彼此不排斥）。
- **被违反的不变量**：`IMPLEMENTATION_BASELINE.md:65`
  「Every journal entry references the audit event that authorized its creation」。
- **最小修复**：对创建绑定加 `action = 'journal.create'` 与同事务 `xmin` 校验；
  并禁止与证据类事件交叉复用。
- **验收条件**：以 `artifact.ingest` 事件或跨事务陈旧事件创建分录必须被拒。

#### P2-M6 — 文档声明的连接器隔离边界不成立

- **文件与行号**：`src/ledgerbridge/artifacts.py:44-53`（`ReadOnlyArtifactStream`）；
  `docs/architecture/EVIDENCE_IMPORT.md:26-27`
  「a read-only stream with no host path, file descriptor, or write method」。
- **失败场景（实测）**：`stream._ReadOnlyArtifactStream__stream` 可直接取到底层文件对象，
  `.name` 返回宿主路径、`.fileno()` 返回文件描述符。Python 名字改写不是封装。
- **影响**：这是 P2-H1 的助攻条件（连接器可精确定位要替换的文件），
  也是 Phase 3 引入真实连接器时唯一的书面安全边界。
- **最小修复**：改为只持有裸 fd 并用 `os.read` 提供 `read()`，
  或修正文档措辞使其与实现一致。
- **验收条件**：新增测试断言连接器可见对象上不存在任何可到达的路径 / fd 属性。

#### P2-M7 — 同字节、不同申报元数据的二次 ingest 静默沿用首次元数据

- **文件与行号**：`src/ledgerbridge/imports.py:223-241`（`_stored_artifact` 只比较
  `sha256` / `byte_size` / `storage_key`，不比较 `source` / `media_type` / `original_filename`）。
- **失败场景（实测 E6）**：以 `source='OTHER-SOURCE'`、
  `media_type='application/x-msdownload'`、`original_filename='evil.exe'`
  重新 ingest 相同字节 → **SUCCEEDED**，库中仍是首次的
  `('src','d.txt','text/plain')`，调用方得不到任何提示；
  检测阶段随后用旧的 `source` / `media_type` 做连接器路由。
- **影响**：溯源声明被静默丢弃。对"这份文件来自哪家银行/哪个 App"敏感的证据链，
  这是应当进入人工复核的冲突，而不是静默成功。
- **最小修复**：申报元数据与既有 artifact 不一致时走
  `NEEDS_REVIEW` + `PROVENANCE_CONFLICT`。
- **验收条件**：上述场景必须返回 NEEDS_REVIEW，且既有 artifact 行保持不变。

#### P2-M8 — `b092eb88` 的全部修复内容零测试覆盖

- **文件与行号**：`src/ledgerbridge/artifacts.py:199-208`（`_fsync_directory`）、
  `:115`（发布后 fsync 目标目录）、`:172-173`（新建目录后 fsync 父目录）；
  以及 `:137` / `:185` 的 `O_NOFOLLOW`。
- **失败场景（缺陷注入实测）**：
  - 整体禁用 `_fsync_directory` → **110 passed**；
  - 去掉两处 `O_NOFOLLOW` → **110 passed**。

  即被指定为"最终可执行 SHA"的 `b092eb88`（`fix: persist artifact directory entries`）
  所做的唯一改动，删掉之后测试全绿。
- **最小修复**：为目录 fsync 增加可观测断言（例如对目录 fd 的 `os.fsync` 计数），
  为 `O_NOFOLLOW` 增加 lstat→open 之间替换为符号链接的竞争测试。
- **验收条件**：删除 `_fsync_directory` 的实体或任一 `O_NOFOLLOW` 后必须变红。

---

### LOW

| ID | 文件与行号 | 内容与实测 | 最小修复 / 验收 |
|---|---|---|---|
| P2-L1 | `imports.py:265` / `:266` / `:273-274` | 连接器 `.name` 被读三次：`validate_connector` 校验的值与写进 `_ConnectorBinding` 的值可以不同。实测：属性首读返回合法名，第三读返回 150 字符名，DB 列 `String(100)` 抛 `DataError`（并入 P2-M2 的逃逸证据）。Codex `CDX-M2` 称已"snapshot once"——**绑定**快照了，**校验**读的是另一次 | 先赋值到局部变量再校验与绑定；验收：漂移型连接器不得产生未处理异常 |
| P2-L2 | `artifacts.py:186-195` | `with os.fdopen(...)` 内抛异常时 fd 已由上下文管理器关闭，`except BaseException` 分支再 `os.close(descriptor)`，多线程下可能误关被复用的 fd | 去掉重复 close，或改用 `try/finally` 单点释放 |
| P2-L3 | `connectors.py:113-125` | `amount_minor` 只校验类型不校验量级；实测 `10**40` 通过。Phase 3 转 posting（`BIGINT`）时溢出 | 加 `int64` 范围校验；验收：超范围值必须被拒 |
| P2-L4 | `connectors.py:93-110`、`imports.py:625-641` | JSON 无深度/大小上限；实测 2000 层嵌套触发 `RecursionError`（不是 `ConnectorContractError`），被 `except Exception` 兜成 `PARSE_ERROR`。已收敛，但错误分类不准且依赖解释器递归上限 | 显式深度与序列化字节上限，抛 `ConnectorContractError` |
| P2-L5 | `artifacts.py:108` | `os.link(temporary, destination, follow_symlinks=False)` 只影响**源**路径，对目的地符号链接没有防护作用；真正的防护是 `EEXIST` 加 `_verify_path` 的 `lstat`。缺陷注入去掉该参数后 110 passed，可证其无安全效果。文档与自审把它计入 symlink 防护属名不副实 | 修正注释与文档，明确防护来源 |
| P2-L6 | `artifacts.py:151-174` | `_ensure_private_directory` 只在自己创建目录时设 `0700`，不修复既有目录权限；容器内 artifact root 由 `docker/app.Dockerfile` 的 `install -d` 以默认 `0755` 建立 | 对既有目录校验并收紧模式，或在镜像里显式 `0700` |
| P2-L7 | `imports.py:575-589`；实测 E5 | 连接器版本升级后对同一 artifact 必然 `NEEDS_REVIEW / IDENTITY_CONFLICT`（fail-closed，设计合理），但"重解析需人工介入"的运维含义未写进任务卡或 `EVIDENCE_IMPORT.md` | 写入文档并给出重解析的批准流程 |
| P2-L8 | `tests/test_phase2_runtime_boundary.py:1-18` | 用 `split("  worker:\n")` 切分 YAML 后做字符串断言；改缩进或键序即失效或假通过 | 改为解析 YAML 后断言结构 |

---

## 2. 已实测且未发现问题的攻击路径

1. **Phase 2 的 POSTED 审计绑定抗 `pg_temp` 影子表**：`0003:413-416` 的
   `SET search_path = pg_catalog` 加 `public.` 限定确实生效；伪造
   `pg_temp.audit_event` + `pg_temp.journal_entry` 后仍被拒
   （`POSTED audit evidence does not exist`）。缺陷注入其未加固版本 →
   `test_post_audit_trigger_cannot_be_shadowed_by_temporary_tables` **确实变红** ✓
2. **RawArtifact 审计绑定**：影子表伪造→拒绝；载荷不匹配→拒绝；
   跨事务陈旧事件→拒绝（`artifact audit evidence must be appended in this transaction`）✓
3. **`storage_key` 不可由调用方影响**：`0003:74-82` 同时有格式正则与
   "必须等于摘要派生值"的等式 CHECK，路径穿越与自选路径在数据库层不可能 ✓
4. **证据不可变**：`raw_artifact` / `source_record` 对运行时角色只有 SELECT+INSERT，
   UPDATE/DELETE 直接 `permission denied`；对**属主**也被触发器拒绝 ✓
   （属主 `DISABLE TRIGGER` 后可绕过，属 PostgreSQL 固有，见残余风险）
5. **基线要求 3（RawArtifact 删除不得级联删除 SourceRecord）**：
   `ON DELETE RESTRICT` 加 `raw_artifact` DELETE 整体禁止 ✓
6. **ImportJob 状态机**：`INSERT` 非 PENDING、PENDING→SUCCEEDED、RUNNING→PENDING、
   终态改计数、改身份、DELETE —— 全部拒绝；PENDING→RUNNING→SUCCEEDED 正常 ✓；
   列级 GRANT（`0003:493-506`）与触发器双重设防 ✓
7. **运行时角色的能力边界**：不能 `ALTER TABLE … DISABLE TRIGGER`、
   不能 `TRUNCATE audit_event`、不能 `SET session_replication_role`；
   `has_database_privilege(CREATE)` 与 `has_schema_privilege('public','CREATE')` 均为 false ✓
8. **并发**：12 线程并发 ingest+import 同一字节 → 1 个 artifact、1 个 job、
   5 条 source_record、恰好 1 个 `artifact_created=True`、
   **0 条孤儿 `artifact.ingest` 审计事件**、哈希链完整且 `prev_hash` 链接连续 ✓
9. **失败回滚**：连接器解析中途抛异常 → `FAILED/PARSE_ERROR` 且 **0 条部分写入**；
   批内重复 `record_locator` → `CONNECTOR_CONTRACT` 且 0 条写入 ✓
10. **连接器嵌套 JSON 可变性**：`imports.py:625-641` 的 JSON 往返深拷贝加重新校验有效；
    实测"yield 之后再改同一个 dict"落库的是改前值。缺陷注入去掉深拷贝 → 测试变红 ✓
11. **金额守卫**：float、NaN、bool、非 CNY、嵌套层缺 currency 全部拒绝 ✓
12. **artifact 落盘**：中间目录符号链接拒绝、目的地符号链接拒绝（未跟随、
    未污染攻击者文件）、同 key 不同内容不覆盖、超限拒绝且 staging 无残留、
    文件 `0440` / shard 目录 `0700`；目录 fsync 链
    （root → sha256 → xx → yy → 发布后再 fsync）完整 ✓
13. **幂等**：同字节同连接器二次导入返回既有终态，source_record 不增长 ✓
14. **权限与凭据分离**：`docker-compose.yml:63-73` 的 `migrate` 服务在 `tools` profile，
    且只有它拿到 `LEDGERBRIDGE_MIGRATION_DATABASE_URL`；api/worker 只有运行时凭据 ✓
15. **Phase 1 复审的四条 BLOCKER/HIGH 已在本基线中真实修复**：
    应用改为直接以 `ledgerbridge_app` 登录（`db.py:23-25`，`SET ROLE` 已删除）、
    `0002` 增加运行时角色"非属主 / 无特权 / 非任何角色成员"断言、
    `uq_journal_entry_reverses_entry_once`、`uq_audit_event_prev_hash_once`
    与单创世索引、`account_protected_dimensions_immutable`、downgrade 角色守卫 ✓
    （其中账户维度与余额相关的部分仍可被 P2-B1 绕过）

---

## 3. 测试可信度：缺陷注入矩阵

| 注入的缺陷 | 是否被测试发现 |
|---|---|
| POSTED 审计触发器去掉 `search_path` 与 `public.` 限定 | ✅ 1 failed |
| RawArtifact 去掉 `xmin` 新鲜度检查 | ✅ 1 failed |
| **RawArtifact 去掉载荷与元数据一致性检查** | ❌ **110 passed** |
| ImportJob 去掉终态不可变分支 | ✅ 1 failed |
| 去掉连接器 JSON 深拷贝 | ✅ 1 failed |
| **`os.link` 去掉 `follow_symlinks=False`** | ❌ 110 passed（该参数本就无目的地防护作用） |
| **去掉目录 fsync（`b092eb88` 的全部内容）** | ❌ **110 passed** |
| **去掉 `O_NOFOLLOW`** | ❌ **110 passed** |
| `open_verified` 不再校验摘要 | ✅ 1 failed |
| 去掉中间目录符号链接拒绝 | ✅ 1 failed |

另：全仓库只有 **1 条**影子表测试（`tests/test_evidence_import.py:923`），
且只覆盖 Phase 2 的 POSTED 绑定；`0002` 的八个触发器函数**零影子表覆盖**——
这正是 P2-B1 能在 110/110 全绿下存活的原因，属典型的
「测试通过但攻击仍成立」（`CLAUDE.md:13-14` 点名要查的那一类）。

---

## 4. 残余风险与 Phase 3 阻断项

| 风险 | 性质 | 结论 |
|---|---|---|
| 属主可 `DISABLE TRIGGER` 后篡改证据 | PostgreSQL 固有 | 实测确认。`EVIDENCE_IMPORT.md:82-84` 的"even from the migration owner"仅在触发器启用时成立；建议在恢复校验清单加入"全部触发器 `tgenabled='O'`"断言 |
| 50 MiB 为单件上限，无聚合配额 | Codex 已列，同意 | `.staging` 可被反复写满磁盘；无人值守生产摄取前必须补配额 |
| `source_record.source` 为自由文本且参与外部身份唯一索引 | 应用层约定 | 连接器命名不一致会静默破坏去重，**Phase 3 阻断项** |
| `normalized_fields` 金额无量级上限（P2-L3） | 数据库层缺失 | Phase 3 转 posting 时 `BIGINT` 溢出 |
| 升级路径对存量 POSTED 数据不可运行 | `0003:275-287` 主动拒绝 | 部署前必须先确认 Hermes 生产库的 POSTED 计数并设计回填方案 |
| `b092eb88` 无独立 CI 记录 | 流程 | 其可执行内容等同 `87e48d7c`，但"最终可执行 SHA"应有可直接引用的 CI 证据 |
| Hermes 部署声明与生产健康状态 | 本轮范围外 | 未做任何 SSH 或容器操作；Codex 报告中的镜像/容器烟测**未由本报告核实**，不应视为已确认 |
| F-6 分支保护缺失 | 平台限制 | 维持既有判断；本 PR 已在无分支保护下合并进 `main`，因此 P2-B1 目前是 `main` 上的 live 缺陷 |

---

## 5. 最终结论

**CHANGES REQUIRED**

依据 `AGENTS.md:49`「Claude review has no unresolved BLOCKER or HIGH findings before merge」：
本轮存在 **1 条 BLOCKER 与 1 条 HIGH**。PR #10 已经合并，因此结论落在
"`main` 当前带伤、部署前必须先合入一个加固 PR"，而不是阻止合并本身。

需要说明的是，Phase 2 **新增**的这一层质量确实很高：证据表、审计绑定、状态机、
并发收敛、失败回滚、内容寻址落盘共 15 类攻击路径实测全部挡住，
其中并发（12 线程零孤儿事件、链完整）与失败原子性（0 条部分写入）尤其扎实。
问题集中在两处接缝：

1. **`CDX-H1` 的修复只做了 2/10。** 攻击类别识别得完全正确，
   但只加固了本 PR 新写的两个函数，Phase 1 的八个函数原样留在同一条攻击面上，
   而 Phase 2 的 POSTED 绑定文档又明确依赖它们。
2. **四个安全控制没有行为敏感测试**，其中一个就是被指定为
   "最终可执行 SHA"的那次提交的全部内容。

**建议处置顺序**：

1. **P2-B1**（`REVOKE TEMPORARY … FROM PUBLIC` 与全部触发器函数
   `SET search_path` + `public.` 限定，两件事一起做）——唯一的 BLOCKER。
2. **P2-H1**（单次 open + 经 fd 校验）。
3. **P2-M2 / M1 / M4**（异常逃逸、篡改错误码、破坏性 downgrade 守卫）——
   三条都直接关系到证据可审计性。
4. 其余 MEDIUM 与 LOW 可在同一轮补齐；每条均已给出
   "去掉修复后测试必须失败"的验收方式，可直接作为下一轮的核对清单。

在 P2-B1 与 P2-H1 关闭之前，不应向 Hermes 部署 Phase 2，也不应摄取任何真实财务证据。

---

## 附录 A：本轮实际执行的验证

### A.1 复审克隆内（只读）

```powershell
git status --porcelain=v1                       # 建分支前工作树内容与 HEAD 一致
git fetch origin --prune --tags                 # 首次瞬时连接重置，第二次成功
git rev-parse origin/main                       # 23bfbd3b…
git log -1 --format="%H|%P|%an <%ae>|%cI|%s" <三个 SHA>
git diff --stat 87e48d7c 23bfbd3b               # 空
git diff --stat 232378ef..23bfbd3b              # 21 files, +3884/-38
git diff --name-status b092eb88..23bfbd3b       # 仅三个文档
git show <SHA>:<path>                           # 逐文件阅读，未 checkout 被审代码
git switch --no-track -c ai/claude/phase-2-evidence-import-review 23bfbd3b…
```

GitHub REST API（只读 GET；凭据取自 Windows 凭据管理器，未打印）：

```
GET /repos/maiziwheat520-boop/caiwu/pulls/10
GET /repos/.../commits/{87e48d7c|23bfbd3b|b092eb88}/check-runs
```

### A.2 Claude 沙箱（一次性 PostgreSQL 16.13，未接触 Hermes 与生产凭据）

- 只读拷贝复审克隆的两个 packfile → 重建裸库 → `git archive 23bfbd3b` 展开 →
  逐文件 git blob hash 校验一致
- 按 CI 拓扑建 `ledgerbridge_owner`（属主）与
  `ledgerbridge_app`（LOGIN/NOSUPERUSER/非属主），`alembic upgrade head`
- `pytest --cov=ledgerbridge --cov=scripts.deployment_manifest --cov-fail-under=95`
  → **110 passed, 96.22%**
- `ruff check` / `ruff format --check` / `mypy --strict` / `pip-audit --strict` 全部通过
- 攻击脚本（均为一次性沙箱脚本，未写入被审仓库）：
  - `pg_temp` 影子表五连击（余额、POSTED 删除、POSTED 改值、跨实体+单 posting POST、account_class）
  - RawArtifact 伪造/陈旧/载荷不匹配审计绑定、属主级篡改、`DISABLE TRIGGER` 逃逸
  - ImportJob 状态机八种非法迁移
  - artifact store：符号链接分片、符号链接目的地、覆盖发布、超限、
    `open_verified` 校验后替换（TOCTOU）
  - 连接器：嵌套 JSON 事后可变、身份漂移、float/NaN/bool/非 CNY/超大整数、2000 层嵌套、
    `ReadOnlyArtifactStream` 逃逸
  - 12 线程并发 ingest+import、解析中途异常、批内重复 locator
  - 带数据的 `alembic downgrade`
- 缺陷注入十项（见 §3），每次注入后重建数据库并恢复源码，
  最终以 git blob hash 确认沙箱树仍与 `23bfbd3b` 逐字节一致
- 沙箱 PostgreSQL 已停止，全部临时目录已删除

### A.3 本轮**没有**做的事

- 没有推送任何分支，没有合并，没有创建或修改 PR
- 没有写入 `LedgerBridge-Codex` 或已退役的 `LedgerBridge` 克隆
- 没有 SSH 到 Hermes，没有触碰生产 API / worker / PostgreSQL / 卷 / `.env`
- 没有在用户机器上创建任何临时文件；跨机传输只用了 `.git` packfile 的只读拷贝
- 没有修改被审仓库中除本报告以外的任何文件
