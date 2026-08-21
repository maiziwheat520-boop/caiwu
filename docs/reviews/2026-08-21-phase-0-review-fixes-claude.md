# Independent re-review: Phase 0 review fixes

- Reviewer: Claude (independent, read-only)
- Date: 2026-08-21
- Repository: `maiziwheat520-boop/caiwu` (private)
- PR: #1 — `ai/chatgpt/phase-0-review-fixes` → `main`
- Base commit (`main`): `e504a428cab9d88c39beeec601e7b15a74ac5700`
- Head commit (PR head): `d6b89640e73efca1ca468c2546833af6b983def7`
- Prior report under re-review: `docs/reviews/2026-08-21-phase-0-scaffold-claude.md`
- Remediation task card: `docs/tasks/2026-08-21-phase-0-review-fixes.md`
- Hermes deployed revision: image tag `ledgerbridge-app:207c9f8`
- Verdict: **APPROVED FOR PHASE 1 WITH FOLLOW-UPS**

本报告只判定"原报告的问题是否真正关闭"，不重新做一次全量审查。所有结论基于本轮
实际执行的命令，命令与原始输出摘要见附录。本轮未修改任何代码、配置、既有报告或
Hermes 服务；唯一写入的文件是本报告。

---

## 0. 复审范围与独立验证方式

| 维度 | 方式 |
|---|---|
| 代码与 diff | 本地 `git` 只读；`main..HEAD` 共 5 个提交、22 个文件 |
| 质量门禁 | 在本地 `.venv` 中**实际重跑** ruff / ruff format / mypy / pytest+coverage / bandit / check_sensitive_paths |
| GitHub PR 与 CI | GitHub REST API（凭据取自 Windows 凭据管理器，**未打印**）；逐 job、逐 step 读取结论，并抽取 `secrets` job 原始日志 |
| Hermes | `ha_auto_ed25519` 密钥 SSH 只读登录 `aiadmin@ai-hub`；未重启、未重建、未删除、未写入 |
| 部署一致性 | 对 Hermes 部署树逐文件 sha256，与本地 `207c9f8` / `d6b8964` 比对 |

未读取 Hermes 生产 `.env` 内容；`psql` 调用一律在容器内用 `$POSTGRES_USER` / `$POSTGRES_DB`
展开，数据库口令未离开容器。

**分支/PR 独立核对（不依赖任务卡自述）**：`git ls-remote origin` 显示
`refs/pull/1/head = d6b8964`、`refs/pull/1/merge = 57950fc`、`refs/heads/main = e504a42`，
本地 HEAD 与 `origin/ai/chatgpt/phase-0-review-fixes` **IN SYNC**。
GitHub API：`state=open`、`draft=false`、`base=main`、`mergeable=true`、
`mergeable_state=clean`、`merged=false`、`commits=5`、`changed_files=22`、`reviews=0`。

---

## 1. 原 BLOCKER 逐条结论

### B-1 迁移目标数据库由静默默认值决定 — **CLOSED**

证据：

- `src/ledgerbridge/config.py:22`：`database_url: str = Field(min_length=1)`——**默认值已删除**，
  缺失环境变量时 pydantic 直接 `ValidationError`，不再静默指向任意库。
- `alembic.ini:5`：`sqlalchemy.url =` 置空；`alembic.ini:2` 改为
  `script_location = %(here)s/alembic`，不再依赖 CWD。
- `alembic/env.py:11`：`config.set_main_option("sqlalchemy.url", escape_alembic_ini_value(get_settings().database_url))`
  ——迁移目标库只能来自环境变量。
- `README.md` 的本地流程已改为 `cp .env.example .env` +
  `docker compose run --rm api alembic upgrade head`，不再教用户在宿主机上对着
  默认值执行迁移。
- 本地复跑 `tests/test_config.py::test_database_url_is_required` 通过（`pytest` 7 passed）。
- CI `quality` job 在真实 PostgreSQL 15 服务上跑通
  `alembic upgrade head` → `downgrade base` → `upgrade head`（三步 step 结论均 `success`）。
- Hermes 实测 `alembic current` = **`20260821_0001 (head)`**，
  `select extname from pg_extension` 含 `pgcrypto`——迁移**真的施加过**，
  不再是原报告里"零 revision 的 no-op"。

判定：关闭。原报告的"静默默认值"和"README 流程不可用且不安全"两个失败场景均已不可复现。

### B-2 唯一代码副本在 Drive 内、无 Git 远端、父目录未忽略 — **CLOSED（有残余风险）**

逐条对照原报告的四条验收条件：

| # | 原验收条件 | 状态 | 证据 |
|---|---|---|---|
| 1 | 建立专用 GitHub 私有仓，`git remote -v` 非空，`ls-remote` 可列分支 | ✅ | `origin https://github.com/maiziwheat520-boop/caiwu.git`；`ls-remote` 列出 `main`、`ai/chatgpt/phase-0-review-fixes`、`refs/pull/1/head` |
| 2 | 建立 `main` 主线并**保护分支** | ⚠️ 部分 | `main` 已存在且为 default branch；分支保护 **平台不支持**（见第 4 节） |
| 3 | 父仓 `.gitignore` 忽略 `/LedgerBridge/` | ✅ | `git -C "G:\我的云端硬盘\AI" check-ignore -v LedgerBridge` → `.gitignore:30:/LedgerBridge/`，退出码 0；`git status --porcelain -- LedgerBridge` 输出**为空**（原为 `?? LedgerBridge/`） |
| 4 | 把工作副本移出 Drive 同步范围 | ❌ 未做 | 仓库仍位于 `G:\我的云端硬盘\AI\LedgerBridge` |

判定：**关闭**。原报告认定 B-2 为 BLOCKER 的核心理由是"**唯一**副本 + 无远端 →
Drive 冲突或 `.git` 损坏将**不可逆地丢失可审计迁移的历史**"。现在 GitHub 私有仓
持有全部 5 个提交与两条分支，历史不再是单点；父仓误提交 gitlink 的路径也被
`.gitignore` 关死。条件 4 未做属于**残余风险而非阻断项**：Drive 并发同步仍可能
损坏本地 `.git`，但后果从"永久丢失"降级为"从 origin 重新克隆"。

---

## 2. 原 HIGH 逐条结论

### H-1 `.gitignore` 未覆盖凭据/证据类型，且无秘密扫描 — **CLOSED**

- `.gitignore:1-2` 改为 `.env*` + `!.env.example`；`:15-34` 新增
  `var/`、`data/`、`secrets/`、`*.pem`、`*.key`、`*.p12`、`id_rsa*`、`*token*.json`、
  `*credential*.json`、`*.eml`、`*.ofx`、`*.qif`、`*.pdf`、`*.sql`、`*.tar.gz`。
- 实测 `git check-ignore` 探针（10 条）：`.env.production`、`token.json`、
  `secrets/oauth_token.json`、`statement.pdf`、`dump.sql`、`id_rsa`、`var/x.eml`、
  `docs/x.pdf` 全部 **IGNORED**。
- `scripts/check_sensitive_paths.py:6-22` 把 15 条候选路径固化为 CI 断言，
  本地执行退出码 0；CI `secrets` 与 `quality` 两个 job 各跑一次。
- 秘密扫描**真实执行**：`secrets` job 原始日志显示
  `gitleaks version: 8.24.3`、`5 commits scanned.`、`scanned ~65851 bytes`、
  `no leaks found`；并显示 `[maiziwheat520-boop] is an individual user. No license key is required.`
  ——私有仓 + GitHub Free 下 gitleaks-action 未被跳过。
- `git ls-files` 中与 `.env` 相关的只有 `.env.example`；无任何
  `.csv/.xlsx/.pdf/.sql/.key/.pem/.eml/.ofx/.qif` 被跟踪。

判定：关闭。

### H-2 Alembic `target_metadata` 为空、无模型导入钩子 — **CLOSED**

- `src/ledgerbridge/db.py:19-20`：新增 `class Base(DeclarativeBase)`，
  并带 `NAMING_CONVENTION`（`db.py:10-16`）——这一点超出原验收条件，
  对 Phase 1 的约束命名可复现是加分项。
- `src/ledgerbridge/models/__init__.py` 建立模型注册表，文档明确要求
  "Phase modules must be imported here so Alembic autogenerate sees every table"。
- `alembic/env.py:5` `import ledgerbridge.models  # noqa: F401`；
  `:8` 导入 `Base`；`:16` `target_metadata = Base.metadata`；
  `:25` / `:41` 两处均加 `compare_type=True`。

判定：关闭。Phase 1 的 autogenerate 不再会输出"删除所有表"的错误结果。
注意：注册表当前为空是正确的（Phase 0 无模型），但**它的有效性要到 Phase 1 第一个
模型出现时才被真正验证**，见 F-3。

### H-3 artifacts 卷 root 属主、应用无写权限、三服务均读写挂载 — **CLOSED（Hermes 实测）**

| 检查 | 上一轮实测 | 本轮实测 |
|---|---|---|
| 容器 UID | api uid=10001 | api **10001**、worker **10001** |
| artifacts 目录属主/权限 | `0 0 755`（root） | **`10001 10001 755`** |
| worker 可写 | — | 挂载 `rw=true`，`os.access(W_OK)` = **True** |
| api 可写 | `rw=true`，`W_OK False`（坏） | 挂载 **`rw=false`**，`W_OK` = **False**（按设计） |

- `docker/app.Dockerfile:9`：`RUN install -d -o 10001 -g 10001 /var/lib/ledgerbridge/artifacts`
  ——挂载点在镜像里预建并授权，新卷继承正确属主。
- `docker-compose.yml:36`：api `artifacts:...:ro`；`:48` worker 读写；`:62` mail-collector 读写。
- 关键差别：上一轮**应用根本写不进证据卷**（`W_OK False` 且无人可写）；
  本轮 worker 可写、api 只读，正是原报告要求的"写入者与读取者分离"。
- 卷是**重建**的，且重建前留有备份：`/srv/ai-center/backups/ledgerbridge/` 下有
  `20260821-phase0-review-43a7fbb`、`-r1`、`-r2`、`20260821-port-hotfix-207c9f8` 四份。

判定：关闭。

### H-4 CI 从不接触数据库/不执行迁移/覆盖率无阈值/mypy 与 Bandit 只覆盖 `src` — **CLOSED**

CI `quality` job 在 PR head `d6b8964` 上的**逐 step 结论**（全部 `success`）：

```
 2. Initialize containers                                  [success]  <- postgres:15-alpine 服务
 7. python scripts/check_sensitive_paths.py                [success]
 8. ruff check .                                           [success]
 9. ruff format --check .                                  [success]
10. mypy src alembic tests scripts                         [success]  <- 范围已扩到 alembic/tests/scripts
11. pytest --cov=ledgerbridge --cov-fail-under=80          [success]  <- 阈值已设
12. alembic upgrade head                                   [success]
13. alembic downgrade base                                 [success]
14. alembic upgrade head                                   [success]
15. bandit -c pyproject.toml -r src alembic scripts        [success]  <- 范围已含 alembic
16. pip-audit                                              [success]
```

- `.github/workflows/ci.yml:32-46` 起了真实 PostgreSQL 15 服务（且带
  `POSTGRES_INITDB_ARGS: --data-checksums`，与生产一致）；`:47-50` 注入
  `LEDGERBRIDGE_DATABASE_URL`。
- 迁移**往返**（upgrade→downgrade→upgrade）在 CI 中真实执行并通过——这条独立于
  Hermes，是本次最有价值的证据之一。
- 本地复跑同一组门禁：ruff 0 问题；`ruff format --check` 26 files already formatted；
  `mypy` Success: no issues found in 12 source files；`pytest` **7 passed, 95% coverage**
  （与任务卡自述一致）；`bandit` 0 issues。

判定：关闭。**但覆盖率门禁的分母偏小**，见 M-open 与 F-3。

### H-5 PostgreSQL 未启用 data checksums — **CLOSED（Hermes 实测）**

- `docker-compose.yml:73`：`POSTGRES_INITDB_ARGS: --data-checksums`。
- Hermes 双重实测：`pg_controldata` → `Data page checksum version: 1`；
  `SHOW data_checksums` → **`on`**（上一轮为 `0` / `off`）。
- 卷重建前有备份（见 H-3），符合原报告"趁库为空、代价为零时处理"的建议。
- CI 的 postgres 服务也带同一参数，本地与生产口径一致。

判定：关闭。

### H-6 写权限移交与"一次只有一个模型写"没有技术强制 — **PARTIALLY CLOSED（残余风险，不阻断）**

逐条对照原报告的三条验收条件：

| # | 原验收条件 | 状态 | 证据 |
|---|---|---|---|
| 1 | 远端 + `main` + 分支保护，所有实施走 PR、评审在 PR 上 | ⚠️ 部分 | 远端/`main`/PR #1 均已落地；**分支保护平台不支持**（第 4 节） |
| 2 | 每个模型独立 `git worktree` 或独立克隆，禁止共用工作树 | ❌ 未做 | `git worktree list` 只有一个工作树：`G:/我的云端硬盘/AI/LedgerBridge d6b8964` |
| 3 | 移交写权限时切换 `git config user.name/email`，并在 `PROJECT_STATUS.md` 记录移交时刻 HEAD | ❌ 未做 | `git config user.name` = `Codex`；5 个提交作者全部为 `Codex <codex@ledgerbridge.local>`，且 `%G?` = `N`（**未签名**）；`PROJECT_STATUS.md:33-39` 记录了 owner 但未记录移交 HEAD；无 `.github/CODEOWNERS` |

判定：**部分关闭**。原报告的失败场景（两个模型并发写同一 Drive 工作树 → 冲突副本/
后写覆盖/`.git/index` 损坏）**在机制上仍然成立**，但后果已被 B-2 的远端大幅削弱：
损坏可从 origin 恢复，且分支+PR 使并发改动以"分叉"形式可见。

**为什么不判为阻断 Phase 1**：当前只有一位人类操作者按次序显式指派写权限；
不可逆的那一半风险（历史永久丢失）已经消除；剩下的"共用工作树"是一条两行命令就能
消除的操作风险，适合作为 Phase 1 启动条件而不是合并前置条件。见 **F-1**。

---

## 3. Medium / Low 逐条状态

### 已关闭

| 编号 | 结论与证据 |
|---|---|
| **M-1**（api/worker 分属两个镜像） | **部分关闭**：`docker-compose.yml:4` 改为共享 `image: ledgerbridge-app:${LEDGERBRIDGE_REVISION:-dev}`，Hermes 实测 api 与 worker 均为 `ledgerbridge-app:207c9f8`（同一镜像 id `a1c0f7f6d8a6`）。**锁文件部分仍未做**，见下方"仍开放" |
| **M-2**（`alembic.ini` 硬编码连接串、`%` 口令崩溃） | 关闭：`alembic.ini:5` 置空；`config.py:33-34` `escape_alembic_ini_value`；`tests/test_config.py:22-25` 覆盖 `p%ss` 场景并在 CI 中执行 |
| **M-3**（mail-collector 无限重启） | 关闭：`docker-compose.yml:60` `restart: "no"`（覆盖 `x-app` 的 `unless-stopped`）；`:58` 仍在 `phase3` profile 内未启用 |
| **M-4**（健康语义不完整） | 关闭：`:38` api healthcheck 改探 `/health/ready`，`:42` `start_period: 20s`；`:49-54` worker 新增 healthcheck（Hermes 实测 worker 状态为 **healthy**）；`tests/test_health.py:39-58` 新增 ready 成功与 503 两条测试。**worker 探针偏弱**，见 F-5 |
| **M-5**（容器与网络硬化缺失） | 关闭：Hermes 实测 api 与 worker 均为 `ReadonlyRootfs=true`、`CapDrop=[ALL]`、`SecurityOpt=[no-new-privileges:true]`、`Mem=268435456`、`Pids=128`；`docker network inspect ledgerbridge_backend` → `internal=true`，`ledgerbridge_ingress` 单独承载发布端口 |
| **M-6**（无 `py.typed`） | 关闭：新增 `src/ledgerbridge/py.typed`，并在 Hermes 容器内实测**已安装进包目录** `/usr/local/lib/python3.12/site-packages/ledgerbridge/py.typed`（非仅存在于源码树） |
| **M-8**（`*.csv`/`*.xlsx` 吞掉合成夹具） | 关闭：`.gitignore:36-38` 反向包含 `tests/fixtures/`；实测 `tests/fixtures/sample.csv`、`tests/fixtures/sub/deep.xlsx` 均 **NOT IGNORED**，而同名扩展在其他路径仍被忽略 |
| **M-9**（`artifact_root` 默认值与部署不一致、`.env` 依赖 CWD） | 关闭：`config.py:23` 默认值改为 `/var/lib/ledgerbridge/artifacts`（与部署一致）；`config.py:15-18` 移除了 `env_file`，配置只来自环境变量，CWD 依赖消失；`:25-30` 新增绝对路径校验并有测试 |
| **M-10**（导入即创建全局 engine） | 关闭：`db.py:23-29` engine 改为 `get_session_factory()` 内惰性创建，模块导入不再冻结配置 |
| **M-13**（镜像只有 `:latest`，无法回滚） | 关闭：Hermes 镜像库中同时保留 `ledgerbridge-app:207c9f8` 与 `ledgerbridge-app:43a7fbb` 两个按 revision 命名的标签，`DEPLOYMENT_HERMES.md:36` 的"保留上一版镜像"现在可执行 |
| **L-1**（Actions 未按 SHA 固定） | 关闭：`ci.yml:19,22,26,52,53,73` 全部按 commit SHA 固定 |
| **L-5**（缺 concurrency 与依赖机器人） | 关闭：`ci.yml:7-9` concurrency 组；新增 `.github/dependabot.yml`（pip + github-actions，weekly） |

### 仍开放（不阻断 Phase 1，但需登记）

| 编号 | 现状 | 影响 |
|---|---|---|
| **M-1（锁文件部分）** | `git ls-files` 无 `uv.lock`/`requirements.lock`；`pyproject.toml:11-31` 仍为版本区间，Dockerfile `pip install .`、CI `pip install -e ".[dev]"` 均无 `--require-hashes` | 镜像与 CI 不可字节复现。Phase 0 可接受；Phase 1 账本代码进入审计范围后需要 → **F-2** |
| **M-11**（备份只有路径没有实现） | `git ls-files scripts/` 只有 `check_sensitive_paths.py`；`docs/architecture/STORAGE.md` 本 PR **未改动**；Hermes 上的 4 份备份是人工目录 | Phase 2 落证据前必须有可验证的备份+恢复演练 → **F-4** |
| **M-12**（Hermes 目录不是 Git 检出） | Hermes 上 `git rev-parse HEAD` 仍报 `fatal: not a git repository`，且本轮 `DEPLOYED_REVISION` 文件也已消失；`DEPLOYMENT_HERMES.md:10` 仍称其为 "deployment checkout" | **已被镜像标签部分替代**：`ledgerbridge-app:207c9f8` 现在为运行中的代码提供不可变标识。但磁盘上的 compose/`.env` 配置树仍无出处校验 → **F-5** |
| **M-7**（文档声明与事实一致性） | 本轮**反向核对通过**：任务卡与 `PROJECT_STATUS.md` 的每一条 Hermes/CI 声明都被独立复现（详见第 4、5 节）。仅一处可更精确：CI 实际在 **`d6b8964`** 上也全绿（6/6 success），比自述的 "`3ff2055`" 更强 | 无 |
| **L-2**（`pip-audit` 未加 `--strict`） | `ci.yml:68` 仍为裸 `pip-audit` | 无法审计的包被静默跳过 → **F-5** |
| **L-3**（`/openapi.json` 仍对外提供） | `main.py:11-16` 只关了 `docs_url`/`redoc_url`，未关 `openapi_url`；**Hermes 实测 `GET /openapi.json` → `HTTP/1.1 200 OK`** | 仅回环可达，风险低；Phase 1 出现业务端点后应关闭 → **F-5** |
| **L-4**（提交身份与签名） | 5 个提交作者均为 `Codex <codex@ledgerbridge.local>`，`%G?` = `N`（无签名） | 提交作者可被任意伪造，审计链靠"约定"而非密码学 → 与 **F-1** 合并处理 |

---

## 4. GitHub / CI / 治理验证结果

**PR #1 状态（GitHub API，独立于任务卡）**

```
state=open  draft=false  base=main  head=ai/chatgpt/phase-0-review-fixes
head_sha=d6b89640e73efca1ca468c2546833af6b983def7
mergeable=true  mergeable_state=clean  merged=false  commits=5  changed_files=22
reviews=0   created=2026-08-21T01:54:05Z
```

**CI 结论（check-runs）**

| 提交 | 结果 |
|---|---|
| `d6b8964`（PR head） | **6 个 check run 全部 `success`**：`secrets`/`quality`/`compose` × 2（push 事件 + pull_request 事件） |
| `3ff2055` | 6 个 check run 全部 `success` |
| `43a7fbb` | 0 个 check run（未单独推送，属正常） |

任务卡只声称"CI passed on `3ff2055`"，实测 **PR head `d6b8964` 本身也是全绿的**，
证据强度高于自述。`3ff2055..d6b8964` 仅改动 `PROJECT_STATUS.md` 与任务卡两个文档文件。

**三个 job 是否"真实有效"**

- `secrets`：`check_sensitive_paths.py` + gitleaks 8.24.3 均实际执行；日志确认
  `5 commits scanned` / `no leaks found`，未因私有仓授权问题被跳过。
  **注意**：扫描范围是 PR 的 5 个提交，**不是全历史** → F-5。
- `quality`：见 H-4 的逐 step 清单，含真实 PostgreSQL 与迁移往返。
- `compose`：`cp .env.example .env` → `docker compose config --quiet` → `docker compose build api`
  均 success。**它只做渲染与构建，不启动栈**，因此 `read_only`/`cap_drop` 等运行时属性
  由 Hermes 实测背书，而非由 CI 背书。

**分支保护 —— 独立核实的平台限制，不是被跳过的步骤**

```
GET /repos/maiziwheat520-boop/caiwu/branches/main/protection
  -> HTTP 403 {"message":"Upgrade to GitHub Pro or make this repository public to enable this feature."}
GET /repos/maiziwheat520-boop/caiwu/rulesets
  -> HTTP 403 {"message":"Upgrade to GitHub Pro or make this repository public to enable this feature."}
GET /repos/.../branches/main  -> protected=false
GET /repos/.../             -> private=true, visibility=private, owner type=User
```

**本报告明确不把它写成"已启用强制保护"。** 当前实际形态是：

- 技术强制：**无**。任何持有写权限的人可以直推 `main`，CI 会跑但**不会阻止**推送。
- 已有的实质控制：
  1. `on: push` 无分支过滤 → 直推 `main` **也会触发 CI**，坏改动会被**事后检测**（而非事前阻止）。
  2. 单人操作 + `PROJECT_STATUS.md:33-39` 的显式 ownership 约定。
  3. PR #1 提供了可审计的评审载体，`refs/pull/1/head` 与被审提交一一对应。
  4. 本报告本身构成合并前的书面 gate。

**剩余风险评估**：在"单人操作 + 尚无真实财务数据 + 历史已在远端"的当前条件下，
这是**可接受的残余风险，不阻断 Phase 1**。它会在以下任一条件出现时变为阻断项：
(a) 第二位人类获得写权限；(b) 真实财务凭证或 OAuth token 进入系统（Phase 2）；
(c) 出现一次绕过 PR 的直推。届时应升级 GitHub Pro 启用分支保护，或改用其他
支持私有仓保护的托管。见 **F-6**。

---

## 5. Hermes 只读验证结果

主机 `ai-hub` / `192.168.1.39`，目录 `/srv/ai-center/ledgerbridge`。

| 项目 | 实测 |
|---|---|
| 运行镜像 | api 与 worker 均为 **`ledgerbridge-app:207c9f8`**（共享镜像 + 不可变 revision 标签） |
| 容器状态 | api `Up 28 minutes (healthy)`、worker `Up 28 minutes (healthy)`、postgres `Up 33 minutes (healthy)` |
| 端口 | `ss -lnt` 中 LedgerBridge 只有 **`127.0.0.1:8650`**；postgres 未向宿主发布 |
| 健康接口 | `/health/live` 200；`/health/ready` 200 `{"status":"ready"}` |
| 运行身份 | api、worker 均 `uid=10001(ledgerbridge) gid=10001` |
| artifacts 属主 | **`10001 10001 755`**（上一轮为 `0 0 755`） |
| artifacts 挂载 | api `rw=false` → `W_OK False`；worker `rw=true` → **`W_OK True`** |
| 容器硬化 | 两者均 `ReadonlyRootfs=true` `CapDrop=[ALL]` `no-new-privileges` `Mem=256m` `Pids=128` |
| 网络 | `ledgerbridge_backend` **`internal=true`**；`ledgerbridge_ingress` 单独承载 8650 |
| data checksums | `pg_controldata` → `Data page checksum version: 1`；`SHOW data_checksums` → **`on`** |
| 迁移水位 | `alembic current` → **`20260821_0001 (head)`**；`pg_extension` 含 `pgcrypto` |
| 数据 | 仍只有 `alembic_version` 一张表（Phase 1 未开始，符合预期） |
| 备份 | `/srv/ai-center/backups/ledgerbridge/` 下 4 份，含 `20260821-phase0-review-43a7fbb-r2` 与 `20260821-port-hotfix-207c9f8` |
| 部署一致性 | 部署树 39 个文件逐文件 sha256 **与本地 `207c9f8` 完全相同（零漂移）** |
| 与 PR head 的差距 | 与 `d6b8964` 仅差 `.github/workflows/ci.yml`、`PROJECT_STATUS.md`、任务卡三个文件；`src/`、`docker/`、`alembic/`、`docker-compose.yml`、`pyproject.toml`、`.env.example` **完全一致** |

**关于 `207c9f8 fix: restore loopback API publishing` 的澄清**：查 `git show 207c9f8`，
`43a7fbb` **并没有**把端口改成 `0.0.0.0`——`ports: "127.0.0.1:8650:8000"` 一直保留。
真实问题是 api 从 `x-app` 继承了 `networks: [backend]` 而 `backend` 是 `internal: true`，
导致端口发布不可用；`207c9f8` 补上 `ingress` 网络修复。
**因此整改过程中不存在"API 曾被暴露到回环之外"的窗口。**

---

## 6. 残余风险与后续条件

以下条目均**不阻断 Phase 1 开工**，但按标注时点必须落实。建议在 Phase 1 任务卡中逐条登记。

| ID | 条件 | 触发时点 | 验收条件 |
|---|---|---|---|
| **F-1** | 消除共用工作树（H-6 残余） | **Phase 1 实施开工前** | `git worktree list` 输出 ≥2 个工作树，或两个模型使用不同的本地克隆；移交写权限时切换 `git config user.name/user.email`，并在 `PROJECT_STATUS.md` 记录移交时刻的 HEAD。验收：任取一个 Claude 提交，`git log --format='%an %G?'` 能与 ownership 记录对账 |
| **F-2** | 依赖锁文件（M-1 剩余） | **Phase 1 合并前** | 仓库存在 `uv.lock` 或 `requirements.lock`（`--generate-hashes`）；Dockerfile 用 `pip install --require-hashes -r`；CI 用同一锁文件；锁文件变更走 PR |
| **F-3** | 覆盖率分母不得再缩小 | **随 Phase 1 schema 一起** | `pyproject.toml:56-60` 的 `[tool.coverage.run] omit` 不得新增条目，且**账本核心模块不得进入 omit**；`--cov-fail-under` 随之上调。当前 95% 是在 60 个语句、5 个文件上测得的，`worker.py`/`mail_collector.py` 被排除，数字不能直接外推 |
| **F-4** | 备份与恢复演练（M-11） | **Phase 2 落原始证据前** | 仓库内有可执行的备份脚本；至少完成一次"从备份恢复到空实例并通过 `alembic current` + 校验和"的演练记录 |
| **F-5** | 小项清理 | Phase 1 期间 | ① `DEPLOYMENT_HERMES.md:10` 措辞改为与实际部署机制一致，并为 Hermes 树增加 `MANIFEST.sha256` 校验步骤；② `main.py` 关闭 `openapi_url`（Hermes 实测现为 200）；③ `pip-audit --strict`；④ worker healthcheck 从 `os.kill(1,0)`（该探针在容器存活时**恒为真**，无信号量）改为探测真实工作循环心跳；⑤ 对全历史跑一次 gitleaks（当前只扫 PR 的 5 个提交） |
| **F-6** | 分支保护 | 满足第 4 节 (a)/(b)/(c) 任一条件时 | `GET /branches/main/protection` 返回 200 且 `required_status_checks` 含 `secrets`/`quality`/`compose`；或给出等效的替代托管方案 |
| **F-7** | 迁移 downgrade 必须真实可逆 | **Phase 1 每一条 migration** | `alembic/versions/20260821_0001_platform_baseline.py:23-25` 的 `downgrade()` 是**空实现**（有注释说明是为保留共享扩展，可接受）。但 Phase 1 起，凡创建表/约束/触发器的 revision，其 `downgrade()` 必须真实回滚，否则 CI 的 upgrade→downgrade→upgrade 往返会退化成只测版本号记账。验收：CI 在往返后断言目标表确实被删除又重建 |

---

## 7. 最终结论

**APPROVED FOR PHASE 1 WITH FOLLOW-UPS**

依据：

- 原报告的 **2 条 BLOCKER 全部关闭**，且关闭方式经独立复现，不是文档层面的声明。
- 原报告的 **6 条 HIGH 中 5 条完全关闭**（H-1、H-2、H-3、H-4、H-5），其中 H-3 与 H-5
  由 Hermes 实机反证——上一轮实测为"证据卷写不进去 / checksums 关闭"，本轮实测为
  "worker 可写、api 只读 / `data_checksums=on`"。
- 剩余的 **H-6 为部分关闭**：PR 工作流已建立，但分支保护受 GitHub Free 私有仓限制
  （API 403 已独立核实，非实施者遗漏），且共用工作树与提交身份两项未做。
  该项被判为**残余风险**而非阻断项，理由是不可逆后果已被远端消除，且剩余部分
  可由 F-1 在 Phase 1 开工前以极低成本消除。
- 上一轮的 13 条 MEDIUM 中 **10 条关闭**；仍开放的 M-1（锁文件）、M-11（备份实现）、
  M-12（部署树无出处校验）均不触及 Phase 1 Ledger Core 的结构性前提。
- 未发现整改引入的新缺陷。特别核实：整改过程中 API **从未**被暴露到回环之外；
  Hermes 部署树与 `207c9f8` 零漂移；CI 三个 job 均为真实执行而非空跑。

**合并与开工的前置动作**（属于用户/实施者，不属于本复审者）：

1. 合并 PR #1 到 `main`。
2. 在 Phase 1 任务卡中登记 F-1 ~ F-7，并把 **F-1 标为开工前必须完成**。
3. 合并后把 Hermes 重新部署到合并后的 `main` 提交，使部署标签与主线一致
   （当前 Hermes 为 `207c9f8`，运行时代码与 PR head 等价，但标签不指向主线提交）。

---

## 附录：本轮实际执行的验证命令

**本地（只读）**

```powershell
git rev-parse HEAD / --abbrev-ref HEAD / status --porcelain / remote -v / branch -a
git log --oneline main..HEAD ; git merge-base main HEAD
git diff --stat main..HEAD ; git diff --name-status main..HEAD
git log --format="%h | %an <%ae> | sig=%G? | %s" main..HEAD
git ls-remote origin                       # 独立确认 refs/pull/1/head 与 main
git ls-files | Select-String "\.env|\.(csv|xlsx|pdf|sql|key|pem|eml|ofx|qif)$"
git check-ignore -q -- <10 条探针路径>      # H-1 / M-8
git worktree list                           # H-6
git -C "G:\我的云端硬盘\AI" check-ignore -v LedgerBridge   # B-2 条件 3
git -C "G:\我的云端硬盘\AI" status --porcelain -- LedgerBridge
python -m ruff check . ; python -m ruff format --check .
python scripts/check_sensitive_paths.py
python -m mypy src alembic tests scripts
python -m pytest --cov=ledgerbridge --cov-fail-under=80
python -m bandit -c pyproject.toml -r src alembic scripts
# 逐文件 sha256 比对 207c9f8 / d6b8964 / Hermes 部署树
```

**GitHub REST API（只读；凭据取自凭据管理器，未打印）**

```
GET /repos/maiziwheat520-boop/caiwu
GET /repos/.../pulls/1                    GET /repos/.../pulls/1/reviews
GET /repos/.../commits/{d6b8964|3ff2055|43a7fbb}/check-runs
GET /repos/.../actions/runs?head_sha=d6b8964
GET /repos/.../actions/runs/32438572029/jobs      # 逐 step 结论
GET /repos/.../actions/jobs/{secrets_job_id}/logs # gitleaks 原始输出
GET /repos/.../branches/main/protection           # -> 403 平台限制
GET /repos/.../rulesets                           # -> 403 平台限制
GET /repos/.../branches/main                      # -> protected=false
```

**Hermes（SSH 只读，未重启/未重建/未写入）**

```bash
cd /srv/ai-center/ledgerbridge
docker compose ps -a ; ss -lnt ; docker network ls ; docker images | grep ledgerbridge
docker network inspect ledgerbridge_backend -f '{{.Internal}}'
curl -sS -D - -o /dev/null http://127.0.0.1:8650/health/{live,ready}
curl -sS -D - -o /dev/null http://127.0.0.1:8650/openapi.json        # L-3 -> 200
docker compose exec -T {api,worker} id
docker inspect -f '{{range .Mounts}}...rw={{.RW}}{{end}}' ledgerbridge-{api,worker}-1
docker compose exec -T worker stat -c "%u %g %a %n" /var/lib/ledgerbridge/artifacts
docker compose exec -T {api,worker} python -c "...os.access(p, os.W_OK)"
docker inspect -f 'ReadonlyRootfs=... CapDrop=... SecOpt=... Mem=... Pids=...' <containers>
docker compose exec -T postgres pg_controldata /var/lib/postgresql/data | grep -i checksum
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW data_checksums"'
docker compose exec -T api alembic current
docker compose exec -T api python -c "...py.typed exists..."          # M-6
ls -la /srv/ai-center/backups/ledgerbridge/
find . -type f -not -name ".env" | sha256sum 逐文件                    # 部署漂移比对
```

全程未 `cat` 生产 `.env`，未打印任何数据库口令或 GitHub token。
