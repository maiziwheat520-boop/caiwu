# Independent review: Phase 0 scaffold

- Reviewer: Claude (independent, read-only)
- Date: 2026-08-21
- Branch: `ai/chatgpt/phase-0-scaffold`
- Reviewed commit: `e504a42` ("chore: finalize Hermes deployment scaffold")
- Base commit: `7ca224c`
- Working tree at review time: clean (`git status --porcelain` empty)
- Hermes 实机核查: 已完成（补充轮，SSH 只读，见第 1 节）
- Verdict: **NOT APPROVED FOR PHASE 1**

本报告按 `docs/reviews/README.md` 要求给出：severity / 文件与行号 / 违反的规则或不变量 /
可复现失败场景 / 验收条件。所有结论基于本次实际执行的命令，命令清单见文末附录 A。

---

## 0. 审查范围与访问方式

已审查：`CLAUDE.md`、`AGENTS.md`、`PROJECT_STATUS.md`、`docs/architecture/*`、
`docs/governance/*`、`docs/tasks/2026-08-21-phase-0-scaffold.md`、`README.md`、
`pyproject.toml`、`.gitignore`、`.gitattributes`、`.dockerignore`、`.env.example`、
`docker-compose.yml`、`docker/app.Dockerfile`、`.github/workflows/ci.yml`、
`alembic.ini`、`alembic/env.py`、`alembic/script.py.mako`、`src/ledgerbridge/*`、
`tests/test_health.py`，以及本地 Git 仓与父目录 `G:\我的云端硬盘\AI` 仓的边界。

**Hermes 实机已核查**（补充轮，2026-08-21）：通过 `ha_auto_ed25519` 密钥以 `aiadmin`
身份 SSH 登录 `ai-hub`（192.168.1.39），执行了一组只读命令，全部结果见第 1 节。
过程中未读取 `.env` 内容、未打印任何数据库口令（`psql` 调用一律在容器内用
`$POSTGRES_USER`/`$POSTGRES_DB` 展开）。

附带发现的本机问题（与本仓库无关但会影响后续运维）：
Windows 自带的 `C:\Windows\System32\OpenSSH\ssh.exe`（OpenSSH_9.5p2）在这台机器上
**已损坏**——连 `ssh -V` 都返回 exit 255 且 stdout/stderr 全空。
本轮改用 Git 附带的 `C:\Program Files\Git\usr\bin\ssh.exe`（OpenSSH_10.3p1）才连通。

**范围豁免**：Phase 1 业务 schema / migration 未实现属于有意保留，本报告不将其记为缺失。
`alembic/versions/` 仅含 `.gitkeep`，符合任务卡"Out of scope"。

---

## 1. Hermes 实机核查结果（2026-08-21，只读）

主机 `ai-hub` / `192.168.1.39`，部署目录 `/srv/ai-center/ledgerbridge`，
Docker 29.7.2 + Compose v5.4.0，镜像存储为 containerd snapshotter（overlayfs）。

**与文档一致、核查通过的项：**

| 声明 | 出处 | 实测 |
|---|---|---|
| API 仅绑定 `127.0.0.1:8650` | `DEPLOYMENT_HERMES.md:5` | ✅ `ss -lnt` 全表中 LedgerBridge 相关监听只有 `127.0.0.1:8650`；主机上无 5432 监听 |
| 不开放公网/LAN 端口 | `DEPLOYMENT_HERMES.md:13` | ✅ 从 192.168.1.19 探测 8650/5432/8000/8080 全部 closed/filtered |
| API 与 PostgreSQL healthy，worker 运行 | 任务卡 `:44` | ✅ `ledgerbridge-api-1` Up 50m (healthy)、`ledgerbridge-postgres-1` Up 48m (healthy)、`ledgerbridge-worker-1` Up 50m |
| `/health/live`、`/health/ready` 返回 200 | 任务卡 `:45` | ✅ 均 `HTTP/1.1 200 OK`，ready body 为 `{"status":"ready"}` |
| 专用命名卷已创建 | 任务卡 `:46` | ✅ `ledgerbridge_artifacts`、`ledgerbridge_postgres-data`；网络 `ledgerbridge_default` |
| 生产 `.env` 为 mode 600 且不入 Git | `DEPLOYMENT_HERMES.md:12` | ✅ `-rw------- 1 aiadmin aiadmin 370 .env`（未读取内容） |
| 部署内容等于评审提交 | `DEPLOYED_REVISION` = `e504a42` | ✅ 逐文件 sha256 比对：31 个受控文件与本地 `e504a42` **全部一致**，无一处漂移 |
| Compose 渲染可用 | 任务卡 `:37` | ✅ `docker compose config --quiet` 通过 |

**实机确认为缺陷的项（原报告的预测被证实）：**

| 编号 | 实测证据 |
|---|---|
| **H-3** | `docker compose exec api stat -c "%u %g %a %n" /var/lib/ledgerbridge/artifacts` → `0 0 755`；`os.getuid()` = **10001**，`os.access(path, os.W_OK)` = **False**。应用**现在就无法向证据卷写入**。api 与 worker 均以 `rw=true` 挂载该卷（无 `:ro`） |
| **H-5** | `pg_controldata` → `Data page checksum version: 0`（**未启用**）。当前库内只有 `alembic_version` 一张表，账本数据为空——这是启用 checksums 代价最低的时刻 |
| **M-4** | api healthcheck 无 `StartPeriod` 字段；`ledgerbridge-worker-1` 的 `.Config.Healthcheck` 为 **null** |
| **M-5** | api 容器：`ReadonlyRootfs=false`、`CapDrop=[]`、`SecurityOpt=[]`、`Memory=0`、`PidsLimit=<nil>`。且该主机同时对 LAN 暴露 8642/5244/5245/2283/11435 等其他服务，数据库网段不隔离的代价更高 |
| **H-4** | `alembic_version` 表存在但为空——`alembic upgrade head` 确实跑过，但因为零条 revision 是 **no-op**。迁移路径**从未真正施加过一次 revision**，CI 与 Hermes 都没有验证过它 |

**本轮新增的两条发现**（详见 M-12、M-13）：部署目录不是 Git 检出；镜像只有 `:latest` 标签。

**一处需要澄清的观察**：`docker compose ps` 把 api/worker 的 IMAGE 显示为裸 `sha256:…`，
且 `docker image inspect <该digest>` 报 "No such image"。这**不是镜像丢失**——
该主机启用了 containerd 镜像存储，`docker inspect <容器>.Image` 返回的是 OCI **config** 摘要，
而 `docker images` 显示的是 **manifest** 摘要，两者本就不同。
经 `docker image inspect ledgerbridge-api:latest` 交叉验证，镜像在库中完好。此项**不构成缺陷**。

---

## BLOCKER

### B-1 迁移目标数据库由静默默认值决定，README 记录的本地流程不可用且不安全

- 文件与行号：`src/ledgerbridge/config.py:23-25`、`alembic.ini:5`、`alembic/env.py:10`、
  `README.md:29-35`、`docker-compose.yml:37-50`（postgres 未发布任何主机端口）
- 违反的规则 / 不变量：
  - `CLAUDE.md`「BLOCKER: can corrupt … migration safety」
  - `AGENTS.md`「Use `.env` only for local non-committed configuration and an external
    secret store in production」——配置缺失必须失败，而不是回落到内置口令
  - `docs/architecture/DEPLOYMENT_HERMES.md:9`「本地仓库是代码与已评审迁移的来源」
- 事实：
  - `Settings.database_url` 有内置默认值
    `postgresql+psycopg://ledgerbridge:change-me@localhost:5432/ledgerbridge`；
    实测在清空所有 `LEDGERBRIDGE_*` 环境变量、且仓库无 `.env` 时，
    `Settings().database_url` 正是该默认值——**不报错、不告警**。
  - `alembic/env.py:10` 无条件把该值写入 `sqlalchemy.url`，没有任何"生产必须显式配置"的校验。
  - `alembic.ini:5` 又硬编码了同一条连接串，形成第二处静默兜底。
- 可复现的失败场景：
  1. 开发者按 `README.md:29-35` 执行。`docker compose up -d postgres` 后，
     compose **没有把 5432 发布到宿主**（`docker-compose.yml:44-45` 只有命名卷，
     无 `ports`），因此宿主侧 `alembic upgrade head` 根本连不到该库。
  2. 若开发者按 `.env.example` 生成 `.env`，`LEDGERBRIDGE_DATABASE_URL` 的主机名是
     compose 服务名 `postgres`，在宿主上无法解析——同一流程仍然失败。
  3. 若开发者机器上恰好有另一个监听 5432 的 PostgreSQL，且存在
     `ledgerbridge` / `change-me` 角色（这正是 `.env.example` 教出来的用户名口令组合），
     则 `alembic upgrade head` 会**静默地把 LedgerBridge 迁移应用到那个无关数据库**。
     Phase 1 的迁移包含 DDL 与触发器，这是不可逆的写入。
- 建议验收条件：
  1. `database_url` 去掉默认值（`Field(...)` 必填），或在 `env == "production"` 时
     强制要求显式配置；缺失时进程启动/迁移必须以非零码失败并给出明确信息。
  2. `alembic.ini:5` 改为 `sqlalchemy.url =`（空）或删除该键，唯一来源是 `env.py` 读取的配置。
  3. 新增测试：清空 `LEDGERBRIDGE_DATABASE_URL` 时 `get_settings()` / `alembic` 入口抛出可断言异常。
  4. 修正 `README.md` 本地流程：要么给 postgres 增加 `ports: "127.0.0.1:5432:5432"`
     的本地 override 文件，要么把 `alembic upgrade head` 改为
     `docker compose run --rm api alembic upgrade head`（与 `DEPLOYMENT_HERMES.md:18` 一致）。
     修正后需实际跑通一次并把命令写进任务卡证据。

### B-2 唯一代码副本在 Google Drive 同步目录内、无 Git 远端，且父目录本身是未忽略它的 Git 仓

- 文件与行号：`docs/architecture/STORAGE.md:16-17`、`docs/architecture/DEPLOYMENT_HERMES.md:9-10`；
  仓库位置 `G:\我的云端硬盘\AI\LedgerBridge`
- 违反的规则 / 不变量：
  - `STORAGE.md:16-17`「This directory is an independent Git repository and **should map to
    one dedicated GitHub repository**. It is not committed into the parent `AI` document repository.」
    —— 后半句目前**没有任何机制保证**。
  - `CLAUDE.md`「BLOCKER: can corrupt evidence, **auditability** …」
  - `AGENTS.md`「Source records are permanent」/ 已评审迁移是审计链的一部分
- 事实（实测）：
  - `git remote -v` 输出为空——**没有任何远端**。仓库只有 2 个提交，且只有一条分支
    `ai/chatgpt/phase-0-scaffold`，不存在 `main`/基线分支。
  - `Test-Path "G:\我的云端硬盘\AI\.git"` → `True`；
    `git -C "G:\我的云端硬盘\AI" status --porcelain -- LedgerBridge` → `?? LedgerBridge/`；
    `git -C "G:\我的云端硬盘\AI" check-ignore -v LedgerBridge` → 退出码 1（**未被忽略**）。
    父仓 `.gitignore` 中没有任何 LedgerBridge 相关规则。
  - 仓库整体（含 `.git/`、`.venv/`、缓存）位于 Google Drive 同步盘，用户在多台机器上工作。
- 可复现的失败场景：
  1. 在父目录执行一次 `git add -A`（父仓也没有远端，日常极易发生），
     Git 会把 `LedgerBridge` 记为 gitlink（mode 160000）指向一个**没有远端可解析**的提交；
     若此后 `LedgerBridge/.git` 因同步异常缺失，父仓会转而吞入全部工作树文件——
     而父仓 `.gitignore` 不含 `.env`、`var/`、`*.csv`、`*.xlsx` 等规则，
     Phase 2 之后即等于把财务证据提交进文档仓。
  2. 两台机器先后修改同一仓库时，Drive 以文件粒度、非原子地同步 `.git/index`、
     `.git/objects`、`refs`，产生冲突副本或半写对象；由于无远端，
     损坏即等于**永久丢失已评审迁移的历史**——这正是 `DEPLOYMENT_HERMES.md:9` 声明的事实源。
- 建议验收条件：
  1. 建立专用 GitHub 私有仓并 `git push -u`，`git remote -v` 非空；`git ls-remote` 能列出
     `ai/chatgpt/phase-0-scaffold`。
  2. 建立 `main` 基线分支（Phase 0 合并目标），并开启分支保护（禁止直推、要求评审）。
  3. 在 `G:\我的云端硬盘\AI\.gitignore` 增加 `/LedgerBridge/`，验收命令
     `git -C "G:\我的云端硬盘\AI" check-ignore -v LedgerBridge` 退出码为 0。
  4. 或（更彻底）把工作副本移出 Drive 同步范围，Drive 只留文档。
     与用户既有工作区约定「文档走 Google Drive，代码走 GitHub」保持一致。

---

## HIGH

### H-1 `.gitignore` 未覆盖治理文件点名的凭据与证据类型，且无秘密扫描

- 文件与行号：`.gitignore:1-28`；被违反的规则见 `AGENTS.md`「Security and data handling」
  与 `docs/architecture/STORAGE.md:33-36`
- 实测 `git check-ignore` 结果（NOT-IGNORED = 会被 `git add .` 吞入）：
  - NOT-IGNORED：`.env.local`、`.env.production`、`.env.hermes`、`token.json`、
    `credentials.json`、`oauth_token.json`、`id_rsa`、`server.key`、`cert.pem`、
    `private.pem`、`report.ofx`、`report.qif`、`statement.pdf`、`dump.sql`、`backup.tar.gz`
  - IGNORED：`.env`、`secrets/`、`var/`、`data/`、`*.eml`、`*.xlsx`、`*.csv`、`.venv/`
- 违反的不变量：`AGENTS.md`「Never commit … passwords, tokens, private keys, or
  **OAuth refresh tokens**」；`STORAGE.md:35-36`「Microsoft OAuth tokens and mailbox
  credentials are never database payloads, logs, docs, or Git」
- 可复现的失败场景：Phase 3 邮件采集器按 MSAL/Graph 惯例把刷新令牌缓存写为
  `token.json` / `*.cache.json`；实施者执行 `git add -A && git commit` 时该文件被正常收录，
  推送后令牌进入 Git 历史。当前仓库没有任何 pre-commit 钩子
  （`.git/hooks` 下只有 `.sample`），CI 也没有 gitleaks/detect-secrets 之类扫描步骤。
- 建议验收条件：
  1. `.gitignore` 增加 `.env*`（配合 `!.env.example`）、`*.pem`、`*.key`、`*.p12`、
     `id_rsa*`、`*token*.json`、`*credential*.json`、`*.ofx`、`*.qif`、`*.pdf`、`*.sql`、`*.tar.gz`。
  2. 上述 15 个候选文件名逐一 `git check-ignore -v` 退出码为 0（可作为一条 CI 检查脚本）。
  3. CI 增加秘密扫描步骤，并对全历史扫描一次。

### H-2 Alembic `target_metadata` 为空且 `env.py` 无模型导入钩子——Phase 1 autogenerate 会给出错误结果

- 文件与行号：`alembic/env.py:7`、`alembic/env.py:15`、`src/ledgerbridge/db.py:18-19`
- 实测：`python -c "from ledgerbridge.db import Base; print(sorted(Base.metadata.tables))"` → `[]`
- 违反的规则：`AGENTS.md`「Database migrations and tests must prove invariants;
  application convention is insufficient」；任务卡 In scope「Alembic infrastructure」
  应当足以承接 Phase 1
- 可复现的失败场景：Phase 1 在 `src/ledgerbridge/models/*.py` 定义 Entity/Account/
  JournalEntry/Posting 后执行 `alembic revision --autogenerate -m "core schema"`。
  由于 `env.py` 只 `import Base`、从不导入模型模块，`Base.metadata` 仍为空：
  - 情形 A（空库）：生成一个 `pass` 的空迁移，实施者可能误以为"没有变化"；
  - 情形 B（库中已有表，例如重跑或对着别的库）：autogenerate 会把库中所有表
    判定为"metadata 中不存在"，生成 `op.drop_table(...)` —— 直接指向财务数据删除。
- 建议验收条件：
  1. `alembic/env.py` 显式导入模型聚合模块（例如 `import ledgerbridge.models  # noqa: F401`），
     或在 `ledgerbridge/db.py` 中集中 re-export。
  2. 增加一条测试：`assert Base.metadata.tables`，且断言核心表名齐全，
     使"模型未被导入"能让 CI 失败。
  3. Phase 1 的第一条迁移必须人工审阅，不得直接采信 autogenerate 输出。

### H-3 【实机确认】artifacts 卷 root 属主，应用进程无写权限；且三个服务都以读写方式挂载"不可变证据卷"

- 文件与行号：`docker/app.Dockerfile:8,17`（创建 uid 10001 并切换用户，但从未创建
  `/var/lib/ledgerbridge/artifacts`）、`docker-compose.yml:13-14`（`x-app` 把 artifacts
  卷同时挂给 api / worker / mail-collector，无 `:ro`）、
  `docs/architecture/STORAGE.md:23`「immutable raw evidence」
- 违反的不变量：`AGENTS.md`「Source records are permanent」；
  `IMPLEMENTATION_BASELINE.md:41`「Raw artifacts are immutable and keyed by SHA-256」
- **实机实测（不再是预测）**：
  - `docker compose exec api stat -c "%u %g %a %n" /var/lib/ledgerbridge/artifacts` → `0 0 755`
  - `docker compose exec api python -c "...os.getuid(), os.access(p, os.W_OK)"` → `uid 10001 W_OK False`
  - `docker inspect ledgerbridge-api-1` / `-worker-1` → 均 `volume ledgerbridge_artifacts -> /var/lib/ledgerbridge/artifacts rw=true`
- 可复现的失败场景（两条，独立）：
  1. Docker 在镜像中不存在的挂载点上创建命名卷时，容器内该路径属主为 `root:root` 0755；
     应用以 uid 10001 运行，因此 Phase 2 第一次落盘原始证据时会
     `PermissionError: [Errno 13]`。此缺陷在 Phase 0 不可见，因为当前没有任何写入路径，
     但**现在已经可以复现**（上面的 `W_OK False`）。
  2. API 进程对证据卷有写权限。任何 API 侧缺陷（路径穿越、误删）都能改写"不可变"证据，
     与 `STORAGE.md:23` 的声明不符。
- 建议验收条件：
  1. Dockerfile 在 `USER` 之前加
     `RUN install -d -o 10001 -g 10001 /var/lib/ledgerbridge/artifacts`。
  2. 验收命令（在 Hermes 上执行）：上面那条 `os.access` 探针必须返回 `W_OK True`。
     **注意：既有卷已由 root 创建（实测 `0 0 755`），只改 Dockerfile 不会修正已存在卷的属主；
     由于卷当前为空，现在删除并重建代价为零，晚做则需要在有证据的卷上做 `chown`。**
  3. `api` 服务把 artifacts 改为 `:ro` 挂载，只有负责导入的进程读写。

### H-4 CI 从不接触数据库、从不执行迁移，覆盖率无阈值，mypy/Bandit 只覆盖 `src`

- 文件与行号：`.github/workflows/ci.yml:22`（`mypy src`）、`:23`（`pytest --cov` 无
  `--cov-fail-under`）、`:24`（`bandit -r src`）、`:27-33`（compose job 只 `config` + `build`）
- 违反的规则：`AGENTS.md`「Definition of done: … Migration upgrade and downgrade behavior
  is reviewed」「Database migrations and tests must prove invariants」
- 事实（实测本地复现 CI 步骤）：
  - `pytest --cov` 结果：1 passed，TOTAL 覆盖率 **56%**；
    `main.py` 29-36（即整个 `/health/ready` 分支）、`db.py` 31-32、`worker.py` 1-26、
    `mail_collector.py` 3-15 全部未覆盖。
  - CI 无 `services: postgres`，`alembic upgrade head` / `downgrade` 在 CI 中**从未被执行过**。
  - `mypy alembic tests` 实测 3 errors（见 M-6），但 CI 只跑 `mypy src`，永远不会暴露。
  - `bandit -r src` 不扫描 `alembic/`——而 Phase 1 的原始 SQL、触发器、`op.execute()`
    恰好全部写在 `alembic/versions/` 里。
  - **实机佐证**：Hermes 上 `alembic_version` 表存在但为空，`alembic current` 无输出——
    部署时的 `alembic upgrade head` 因为零条 revision 是一次 **no-op**。
    也就是说，从本地到 CI 到生产，**迁移路径从未真正施加过任何一条 revision**。
- 可复现的失败场景：Phase 1 提交一条 upgrade 正确、downgrade 会丢数据（或干脆报错）的迁移，
  CI 全绿；`/health/ready` 被改坏（例如吞掉异常改返回 200）也不会有任何测试失败。
- 建议验收条件：
  1. `quality` job 增加 `services: postgres:15`，并新增步骤
     `alembic upgrade head` → `alembic downgrade base` → `alembic upgrade head`，任一失败即 CI 失败。
  2. `pytest` 增加 `--cov-fail-under`（Phase 0 可先设 80，明确排除 worker/mail_collector）。
  3. `mypy` 与 `bandit` 的扫描范围扩到 `src alembic tests`（或 `mypy .`）。
  4. 增加一条能因 H-4 场景失败的负向测试：DB 不可用时 `/health/ready` 必须返回 503。

### H-5 【实机确认】PostgreSQL 未启用 data checksums，且该选项只能在 initdb 时设置

- 文件与行号：`docker-compose.yml:37-42`（postgres 服务无 `POSTGRES_INITDB_ARGS`）
- 违反的不变量：`README.md:8-12` 的优先级「1. ledger correctness；2. evidence preservation」；
  `STORAGE.md:38-43` 的永久保存承诺
- **实机实测**：`docker compose exec postgres pg_controldata /var/lib/postgresql/data | grep -i checksum`
  → `Data page checksum version:           0`（未启用）。同时 `\dt` 显示库内**只有 `alembic_version` 一张表**，
  账本数据为空。
- 可复现的失败场景：存储层发生静默位翻转时，PostgreSQL 在未启用 checksum 的情况下
  会照常返回损坏的数据页，账本余额与凭证内容被无声污染，且 `pg_dump` 会把损坏一并带走。
  由于 Hermes 上的数据卷 `ledgerbridge_postgres-data` **已经初始化完成**，
  之后再启用需要 dump/restore 或离线 `pg_checksums -e`——等有真实财务数据之后成本会高得多。
- 建议验收条件：
  1. `docker-compose.yml` 的 postgres 服务增加
     `POSTGRES_INITDB_ARGS: "--data-checksums"`。
  2. 在 Hermes 上（**实测当前库为空，代价为零**）重建数据卷后验收：
     `docker compose exec postgres pg_controldata /var/lib/postgresql/data | grep -i checksum`
     显示 `Data page checksum version: 1`。
  3. 同时把该项写入 `DEPLOYMENT_HERMES.md` 的初始部署清单，避免将来重建时丢失。

### H-6 写权限移交与"一次只有一个模型写"只有文档约定，没有任何技术强制

- 文件与行号：`docs/governance/WORKFLOW.md:5-16`、`AGENTS.md:15-18`、`CLAUDE.md:31-39`
- 事实（实测）：
  - 只有一条分支 `ai/chatgpt/phase-0-scaffold`，无 `main`，无远端（见 B-2），
    因此 `WORKFLOW.md` 表格里的「Merge readiness」没有合并目标，
    "Claude 用 `ai/claude/<task>`" 也无法通过 PR 隔离。
  - Codex 与 Claude 操作的是**同一个 Google Drive 工作树**，并跨机器同步；
    没有 worktree 隔离、没有 lock 文件、没有分支保护、没有 CODEOWNERS。
  - 仓库 Git 身份被固定为 `user.name=Codex` / `user.email=codex@ledgerbridge.local`，
    Claude 若被移交写权限，提交作者仍会显示为 Codex——审计上无法区分实施者。
- 可复现的失败场景：用户在 A 机让 Codex 改 `src/`，同时在 B 机让 Claude 复核并被临时授权
  修一行；两侧各自写盘 → Drive 生成冲突副本或后写覆盖先写，`.git/index` 亦可能被并发写入。
  `PROJECT_STATUS.md` 中的"ownership"字段不会阻止任何一次写入。
- 建议验收条件：
  1. 落地 B-2 的远端 + `main` + 分支保护后，规定所有实施走 PR，评审在 PR 上进行。
  2. 每个模型使用独立 `git worktree`（或独立本地克隆），禁止两个模型共用一个工作树。
  3. 移交写权限时同时切换 `git config user.name/user.email`，
     并在 `PROJECT_STATUS.md` 记录移交时刻的 HEAD，使提交作者与 ownership 记录可对账。

---

## MEDIUM

### M-1 依赖没有锁文件，镜像与 CI 不可复现（与"按 digest 固定基础镜像"的意图矛盾）
- 文件与行号：`pyproject.toml:11-31`（全部为范围约束）、`docker/app.Dockerfile:15`
  （`pip install --no-cache-dir .`）、`.github/workflows/ci.yml:19`
- 失败场景：今天构建通过的镜像，明天因某个传递依赖发布新版本而行为不同；
  Hermes 上的 `docker compose build` 与本地/CI 得到不同的依赖集，
  出现"本地绿、生产坏"且无法回溯是哪一版依赖导致。基础镜像已按 sha256 固定
  （`app.Dockerfile:1`、`docker-compose.yml:38`），说明团队意图是可复现构建，Python 层却缺失。
- **实机佐证**：api 与 worker 是**两个独立构建的镜像**
  （`ledgerbridge-api:latest` = `sha256:17a3d3e3…`，`ledgerbridge-worker:latest` = `sha256:97746ac4…`，
  同一次构建、不同 manifest）。`docker-compose.yml:3-14` 的 `x-app` 锚点让人以为它们同源，
  但 Compose 是按服务分别 build 的：没有共享 `image:` 键，也没有锁文件，
  因此**没有任何机制保证 api 和 worker 跑在同一套依赖上**。
- 验收条件：引入 `uv.lock` / `requirements.lock`（`pip-compile --generate-hashes`），
  Dockerfile 改为 `pip install --require-hashes -r requirements.lock`，CI 使用同一锁文件；
  锁文件变更必须走评审。另外给 `x-app` 加上共享的 `image: ledgerbridge-app:<revision>`，
  让 api/worker/mail-collector 复用同一个构建产物。

### M-2 `alembic.ini` 硬编码连接串；`set_main_option` 遇到含 `%` 的口令直接抛错
- 文件与行号：`alembic.ini:5`、`alembic/env.py:10`
- 实测复现：
  `python -c "from alembic.config import Config; Config().set_main_option('sqlalchemy.url','postgresql+psycopg://lb:p%ssw@db:5432/lb')"`
  → `ValueError: invalid interpolation syntax in '...' at position 25`
- 失败场景：Hermes 生产 `.env` 使用随机生成口令（mode 600），只要口令含 `%`，
  `docker compose run --rm api alembic upgrade head` 在 `env.py:10` 直接崩溃，
  升级序列（`DEPLOYMENT_HERMES.md:29-35` 第 4 步）中断。此故障是"响亮失败"，不会损坏数据，
  但会卡住发布，且错误信息与口令无关联，排查成本高。
- 验收条件：改为 `config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))`，
  或跳过 ini 直接把 URL 传给 `create_engine`/`engine_from_config`；
  增加一条使用含 `%` 口令的单元测试。同时清空 `alembic.ini:5`（与 B-1 一并处理）。

### M-3 `mail-collector` 一旦启用即无限重启循环
- 文件与行号：`docker-compose.yml:12`（`restart: unless-stopped` 被 `<<: *app` 继承）、
  `docker-compose.yml:32-35`、`src/ledgerbridge/mail_collector.py:11`（`raise SystemExit(2)`）
- 失败场景：`docker compose --profile phase3 up -d` 后，容器以退出码 2 结束，
  `unless-stopped` 与退出码无关地重启它，形成秒级崩溃循环，持续刷日志并占用 Hermes 资源。
- 验收条件：`mail-collector` 显式设置 `restart: "no"`（覆盖锚点），
  或改为长驻但不做任何事并记录一次"Phase 3 未启用"。
  验收：`docker compose --profile phase3 up -d && sleep 60 && docker compose ps`
  的 RESTARTS 计数不增长。

### M-4 【实机确认】健康语义不完整：只探 live 不探 ready，worker 无健康检查，`/health/ready` 零测试
- 文件与行号：`docker-compose.yml:22-26`（healthcheck 仅打 `/health/live`，无 `start_period`）、
  `docker-compose.yml:28-30`（worker 无 healthcheck）、
  `src/ledgerbridge/main.py:27-36`（实测覆盖率 0，缺失行 29-36）
- **实机实测**：`docker inspect ledgerbridge-api-1` 的 `.Config.Healthcheck` 无 `StartPeriod` 字段；
  `ledgerbridge-worker-1` 的 `.Config.Healthcheck` 为 **null**。
- 失败场景：PostgreSQL 挂掉或凭据变更后，API 的 `/health/live` 仍返回 200，
  `docker compose ps` 显示 healthy，运维与 Hermes 侧都看不出账本已不可用；
  同时 worker 只是 `time.sleep(1)` 空转（`worker.py:21`），"worker running"不代表任何业务能力。
- 验收条件：
  1. 新增一条 compose healthcheck 或外部探针覆盖 `/health/ready`（可用较长 interval）；
  2. 增加 `start_period`（建议 20s）避免启动期误判；
  3. 新增测试：用依赖覆盖注入一个会抛 `SQLAlchemyError` 的 session，断言 `/health/ready` 返回 503；
     该测试必须能因"把 except 改成返回 200"而失败。

### M-5 【实机确认】容器与网络硬化缺失
- 文件与行号：`docker-compose.yml:1-54`（无 `networks:` 声明、无 `security_opt`、
  无 `cap_drop`、无 `read_only`、无 `mem_limit`/`pids_limit`）
- 现状评价：卷与网络的**项目级隔离本身是成立的**——顶层 `name: ledgerbridge` 固定了项目名，
  卷为 `ledgerbridge_artifacts` / `ledgerbridge_postgres-data`，默认网络为 `ledgerbridge_default`，
  postgres 未发布任何主机端口。这一条不是隔离失败，而是硬化不足。
- **实机实测**：`ledgerbridge-api-1` → `ReadonlyRootfs=false CapDrop=[] SecurityOpt=[] Memory=0 PidsLimit=<nil>`；
  网络只有一个 `ledgerbridge_default`（bridge，非 internal）。
  另外该主机**同时对 LAN 暴露 8642 / 5244 / 5245 / 2283 / 11435 等其他服务**，
  LedgerBridge 与它们共享同一台宿主，数据库网段不隔离的代价比单用途主机更高。
- 失败场景：应用侧一旦出现 RCE，容器具备全部默认 capability 与可写根文件系统，
  且数据库网段可出公网（默认 bridge 非 `internal`），便于外带财务数据。
- 验收条件：显式声明两个网络（`edge` 给 api、`data` 且 `internal: true` 给 postgres+api+worker）；
  三个应用服务加 `security_opt: ["no-new-privileges:true"]`、`cap_drop: [ALL]`、
  `read_only: true` + 必要 tmpfs；`docker inspect` 可验证。

### M-6 包未提供 `py.typed`，跨模块导入被判为 untyped
- 文件与行号：`pyproject.toml:33-34`（`packages.find` 无 `package-data`）；缺少 `src/ledgerbridge/py.typed`
- 实测：`mypy alembic tests` → 3 errors
  （`tests/test_health.py:3`、`alembic/env.py:6`、`alembic/env.py:7`：
  `module is installed, but missing library stubs or py.typed marker [import-untyped]`）
- 失败场景：任何把 mypy 范围扩到 `tests` 或 `alembic` 的尝试立刻失败，
  实施者最省事的修法是加 `ignore_missing_imports = true`，从而把 `strict = true` 实质架空。
- 验收条件：新增空文件 `src/ledgerbridge/py.typed`，
  `[tool.setuptools.package-data] ledgerbridge = ["py.typed"]`；`mypy src alembic tests` 通过。

### M-7 文档中的完成声明与可核实事实不一致
- 文件与行号：`docs/tasks/2026-08-21-phase-0-scaffold.md:36,43,44-47`、`PROJECT_STATUS.md:7,16`
- 逐条比对：
  1. `:36`「pass in a clean **Python 3.12** environment」——实测仓库内 `.venv` 为
     **Python 3.13.14**（`.venv\Scripts\python.exe --version`），CI 用的才是 3.12。
     即"本地证据"与验收条件描述的环境不是同一个，`:43` 的本地结论并未在 3.12 上复现过。
  2. `:43`「pip-audit passed」——在本机默认控制台（中文 GBK 代码页）实测
     `pip-audit` 以 `UnicodeDecodeError` 崩溃、退出码 1；强制 UTF-8
     （`PYTHONUTF8=1` + `chcp 65001`）后才输出 "No known vulnerabilities found"（退出码 0）。
     结论本身成立，但"通过"的可复现性依赖未记录的环境前提。
  3. `:44-47` 与 `PROJECT_STATUS.md:16` 的 Hermes 断言——**补充轮已独立验证为真**
     （容器 healthy、worker 运行、live/ready 200、仅绑 127.0.0.1:8650、专用卷已建，
     且部署内容逐文件哈希等于 `e504a42`）。这几条从"自述"升级为"已核实"，见第 1 节。
  4. `PROJECT_STATUS.md:7`「awaiting independent Claude review **before merge**」——
     仓库不存在合并目标分支，也无远端（见 B-2/H-6），这条声明目前没有对应的动作可执行。
  5. `:47`「Docker was unavailable locally」属实（`Get-Command docker` 未找到）。
- 验收条件：任务卡「Implementation evidence」改为记录**可复现命令 + 关键输出摘要 + 环境**
  （Python 版本、代码页/locale、执行主机），使第三方能在不登录 Hermes 的情况下判断证据强度。

### M-8 `*.csv` / `*.xlsx` 全局忽略会静默吞掉 Phase 1 的合成夹具
- 文件与行号：`.gitignore:19-21`；`STORAGE.md:29-30`要求 tests 使用合成夹具
- 实测：`git check-ignore -v tests/fixtures/real.csv` → 命中 `.gitignore:21:*.csv`
- 失败场景：Phase 1 生成合成对账 CSV 夹具并 `git add`，文件被静默忽略；
  CI 检出后测试因缺文件而失败，或更糟——测试改为运行时生成，从而失去可复现的黄金样本。
- 验收条件：保留全局忽略的同时增加例外 `!tests/fixtures/**`，
  并在 `STORAGE.md` 写明"仅 `tests/fixtures/` 下允许合成表格文件"；
  `git check-ignore tests/fixtures/x.csv` 退出码为 1。

### M-9 `artifact_root` 默认值与部署路径不一致；`.env` 查找依赖 CWD
- 文件与行号：`src/ledgerbridge/config.py:16`（`env_file=".env"` 为相对路径）、
  `src/ledgerbridge/config.py:26`（默认 `var/artifacts`）、`.env.example:4`
  （`/var/lib/ledgerbridge/artifacts`）、`docker-compose.yml:14`
- 失败场景：任何未设 `LEDGERBRIDGE_ARTIFACT_ROOT` 的进程会把证据写到相对于**当前工作目录**的
  `var/artifacts`，而不是命名卷；在容器里就是 `/app/var/artifacts`（镜像层，容器重建即丢失）。
  同理，从非仓库根目录运行 alembic/uvicorn 时 `.env` 不会被读取。
- 验收条件：`artifact_root` 去掉默认值或默认为绝对路径 `/var/lib/ledgerbridge/artifacts`；
  启动时校验该路径存在且可写（配合 H-3）；`env_file` 用相对包根解析或改为显式绝对路径。

### M-10 模块导入即创建全局 engine，配置在导入时被冻结
- 文件与行号：`src/ledgerbridge/db.py:26-27`、`src/ledgerbridge/config.py:29-31`（`@lru_cache`）
- 失败场景：`import ledgerbridge.db` 的瞬间就用当时的环境变量建好 Engine，
  Phase 1 的集成测试要指向临时数据库时无法通过 fixture 覆盖（`lru_cache` 也已缓存），
  实施者往往会改成 monkeypatch 全局变量，测试与生产路径就此分叉。
- 验收条件：改为 FastAPI lifespan 中初始化引擎（或 `Depends` 工厂），
  `get_session` 从 app.state 取；新增一条测试证明可以在不改环境变量的情况下注入替代 URL。

### M-11 `STORAGE.md` 列出了备份路径，但仓库与 Compose 中没有任何备份实现或验证
- 文件与行号：`docs/architecture/STORAGE.md:25,43`、`docs/architecture/DEPLOYMENT_HERMES.md:28`
- 失败场景：文档读起来像"备份已就绪"，实际 `/var/backups/ledgerbridge/` 无人写入、
  无加密、无恢复演练；Hermes 单机故障即丢失全部账本。
- 验收条件：要么把备份明确标注为 Phase N 未实现项并从"Runtime"清单降级为 TODO，
  要么落地一个定时 `pg_dump` + 加密 + 恢复演练脚本，并记录一次成功恢复的证据。

### M-12 【实机发现】Hermes 部署目录不是 Git 检出，文档却称其为 "deployment checkout"

- 文件与行号：`docs/architecture/DEPLOYMENT_HERMES.md:9-10`；Hermes 路径 `/srv/ai-center/ledgerbridge`
- 实机实测：
  - `git rev-parse HEAD` 在该目录下 → `fatal: not a git repository`
  - 目录内没有 `.git/`，取而代之的是一个手写文件 `DEPLOYED_REVISION`，内容为 `e504a42`
  - 目录共 33 个文件 = 31 个受控文件 + `.env` + `DEPLOYED_REVISION`
- 违反的规则：`DEPLOYMENT_HERMES.md:10`「The Hermes directory is a **deployment checkout**,
  not a second source of truth」——它实际是一次文件拷贝，没有任何自校验能力。
- 失败场景：有人在 Hermes 上直接改一行配置救急（这正是"部署目录"最常见的用法），
  `DEPLOYED_REVISION` 不会变，`docker compose ps` 也看不出来；
  下一次审计时无法回答"生产上跑的到底是哪次评审过的代码"。
  本轮我是靠**逐文件 sha256 比对**才证明当前没有漂移的——主机上没有任何常规手段能做到这一点。
- 建议验收条件：二选一——
  1. 改为真正的 `git clone` + `git checkout <revision>`，部署后
     `git rev-parse HEAD` 与 `git status --porcelain`（应为空）即为自校验；或
  2. 保留文件拷贝，但增加一个部署时生成的 `MANIFEST.sha256`，
     并在 `DEPLOYMENT_HERMES.md` 的升级序列里加一步 `sha256sum -c MANIFEST.sha256`。
  同时把 `DEPLOYMENT_HERMES.md:10` 的措辞改成与实际机制一致。

### M-13 【实机发现】镜像只有 `:latest` 标签，"保留上一版镜像以便回滚"无法满足

- 文件与行号：`docs/architecture/DEPLOYMENT_HERMES.md:36`
  「Keep the previous image/revision until post-deploy checks pass」；
  `docker-compose.yml:3-14`（`x-app` 无 `image:` 键，Compose 只能生成 `<project>-<service>:latest`）
- 实机实测：镜像库中只有 `ledgerbridge-api:latest`、`ledgerbridge-worker:latest`；
  另有一个 `<untagged>` 的 198MB 悬空镜像（上一次构建的残留）。
  没有任何按 revision 命名的标签。
- 失败场景：Phase 1 部署后发现迁移有问题需要回滚应用层，
  运维找不到"上一版"可用的镜像引用——只有一个无名悬空镜像，
  且它随时可能被 `docker image prune` 清掉。`DEPLOYMENT_HERMES.md:36` 的这一步无法执行。
- 建议验收条件：`x-app` 增加 `image: ledgerbridge-app:${LEDGERBRIDGE_REVISION}`，
  部署脚本用 `DEPLOYED_REVISION` 的值打标签；
  验收：`docker images | grep ledgerbridge` 至少能看到当前与上一个 revision 两个标签，
  且 `docker compose up -d` 可通过改 `LEDGERBRIDGE_REVISION` 完成回滚。

---

## LOW

### L-1 GitHub Actions 未按 commit SHA 固定
- `.github/workflows/ci.yml:14,15,30` 使用 `@v4` / `@v5` 可变标签，
  而基础镜像却按 sha256 固定，策略不一致。验收：改为 `@<40位SHA>` 并加注释标明版本。

### L-2 `pip-audit` 未加 `--strict`，无法审计的包被静默跳过
- `.github/workflows/ci.yml:25`。实测输出含
  `ledgerbridge  Dependency not found on PyPI and could not be audited`，
  退出码仍为 0。验收：加 `--strict` 并显式 `--ignore-vuln` 白名单（如需），
  或改用 `pip-audit -r requirements.lock`。

### L-3 `/openapi.json` 仍对外提供
- `src/ledgerbridge/main.py:11-16` 关闭了 `docs_url`/`redoc_url`，但未设 `openapi_url=None`。
  实测：`/docs` → 404、`/redoc` → 404、`/openapi.json` → **200**。
  当前仅回环可达、且 schema 为空壳，风险有限；Phase 1 之后会完整暴露账本 API 结构。
  验收：生产环境 `openapi_url=None`（或按 `settings.env` 条件开启）。

### L-4 提交身份与文件模式设置
- `git config user.name=Codex` / `user.email=codex@ledgerbridge.local`：
  提交作者无法与真实责任人关联，与"审计事件可追溯"的整体取向不一致。
- `core.filemode=false`、`core.ignorecase=true`（Windows 默认）：
  将来加入 entrypoint/备份脚本时，可执行位不会被记录，Linux 侧需 `chmod +x` 才能运行。
  验收：在 Dockerfile/部署脚本中显式 `chmod`，或改用 `git update-index --chmod=+x`。

### L-5 CI 缺 concurrency 组与依赖更新机器人
- `.github/workflows/ci.yml:3-5` 对每次 push 都跑全量；无 `concurrency: cancel-in-progress`，
  也无 dependabot/renovate 配置。验收：加 concurrency 组；加依赖更新机器人（配合 M-1 的锁文件）。

---

## 已检查且未发现问题的项

以下项经实际验证成立，记录在此以便后续复核对账：

1. **分支与提交与任务卡一致**：`git rev-parse` 确认 HEAD = `e504a42`，
   分支 = `ai/chatgpt/phase-0-scaffold`；历史仅 `7ca224c` → `e504a42`；工作树干净。
2. **仓库确为独立 Git 仓**：`git rev-parse --show-toplevel` 指向 LedgerBridge 自身
   （边界问题见 B-2，是"未被父仓忽略"，不是"被父仓收录"——当前 `git ls-files` 在父仓为空）。
3. **已跟踪文件干净**：31 个跟踪文件，逐一核对无 `.env`、无凭据、无财务数据、
   无 `.venv/`、无缓存、无 `__pycache__`；`git status --porcelain -uall` 为空，
   说明本地 `.venv`、`.coverage`、`.mypy_cache` 等确实被 `.gitignore` 覆盖。
4. **API 端口绑定正确**：`docker-compose.yml:21` 为 `"127.0.0.1:8650:8000"`，
   worker / mail-collector 继承的锚点中不含 `ports`，不存在意外发布。
   LAN 侧实测 `192.168.1.39:8650`、`:5432`、`:8000`、`:8080` 均不可达。
5. **PostgreSQL 未发布主机端口**，数据仅落在命名卷 `postgres-data`（`docker-compose.yml:44-45`）。
6. **卷与项目隔离**：顶层 `name: ledgerbridge` 固定项目名，卷实际名为
   `ledgerbridge_artifacts` / `ledgerbridge_postgres-data`，不与其他 compose 项目共享；
   全文无绑定挂载到宿主敏感路径。
7. **镜像 digest 固定**：`app.Dockerfile:1`（python:3.12-slim@sha256:2c94…）与
   `docker-compose.yml:38`（postgres:15-alpine@sha256:fe07…）均已固定。
8. **非 root 运行**：`app.Dockerfile:8,17` 创建 uid 10001 并切换（权限缺陷见 H-3）。
9. **构建上下文收敛**：`.dockerignore` 排除 `.git`、`.env`、`.venv`、`docs`、`tests`、
   `var`、`data`、`secrets` 与各类缓存，无敏感内容进入 build context。
10. **本地质量门实测结果**：`ruff check .` = All checks passed；
    `ruff format --check .` = 20 files already formatted；`mypy src` = Success (6 files)；
    `pytest` = 1 passed；`bandit -c pyproject.toml -r src` = No issues identified；
    `pip-audit`（UTF-8 环境）= No known vulnerabilities found。
11. **worker 生命周期**：`worker.py:17-18` 正确注册 SIGTERM/SIGINT，
    以 `python -m` 作为 PID 1 时可优雅退出（业务空转问题见 M-4）。
12. **依赖服务顺序**：`docker-compose.yml:9-11` 使用
    `depends_on: postgres: condition: service_healthy`，postgres healthcheck 用
    `pg_isready` 且正确转义了 `$$`（`:47`）。
13. **Phase 1 范围未被提前写入**：`alembic/versions/` 仅 `.gitkeep`，
    `src/` 无任何账本模型，符合任务卡的 Out of scope，**不记为缺陷**。
14. **治理文档自洽性**：`CLAUDE.md` 的 severity 定义、`AGENTS.md` 的财务不变量、
    `IMPLEMENTATION_BASELINE.md` 的冻结语义三者无相互矛盾之处；
    `IMPLEMENTATION_BASELINE.md:70-79` 的 Phase 1 数据库要求条目清晰、可测试。
15. **部署内容无漂移**：Hermes 上 31 个受控文件的 sha256 与本地 `e504a42` 逐一相同
    （核查机制的问题见 M-12，但**当前内容本身是干净的**）。
16. **生产 `.env` 权限正确**：`-rw-------`（600）、属主 `aiadmin`，未被复制回仓库；
    本轮全程未读取其内容。
17. **端口面在实机上成立**：`ss -lnt` 全表中 LedgerBridge 只有 `127.0.0.1:8650` 一个监听，
    PostgreSQL 未向宿主发布端口，LAN 侧不可达。
18. **服务实际健康**：三个容器均 `Up`，api/postgres 为 `healthy`，`RestartCount=0`（无崩溃循环），
    `/health/live` 与 `/health/ready` 均返回 200。
19. **镜像未丢失**（澄清）：`docker compose ps` 显示裸 sha256 是 containerd 镜像存储下
    config 摘要与 manifest 摘要的差异所致，经交叉验证镜像在库中完好，**不构成缺陷**。

---

## 结论

**NOT APPROVED FOR PHASE 1**

补充轮的 Hermes 实机核查**改善了对实施质量的判断**：部署是真的、健康的、内容与 `e504a42`
逐字节一致，端口面与 `.env` 权限都符合文档声明。任务卡里关于 Hermes 的完成声明经核实为真。

但同一轮核查也**把两条 HIGH 从预测坐实为事实**：证据卷应用进程写不进去（`W_OK False`），
PostgreSQL 的 data checksums 是关的；并新增了两条部署机制上的 MEDIUM（M-12、M-13）。

Phase 0 的工程骨架在"能跑起来"这一层是成立的：目录结构、非 root 镜像、digest 固定、
回环端口、命名卷隔离、ruff/mypy/pytest/Bandit 接线、以及 Codex/Claude 角色文档都已到位，
并且没有把 Phase 1 的业务 schema 提前写进来。问题集中在**治理与安全的"声明 vs 强制"落差**：
多条被文档当作不变量的事情（不进父仓、不提交凭据、一次只有一个模型写、迁移可复现、
证据不可变、部署可回滚）目前完全依赖人的自觉，没有任何一条能在 CI、Git 或 Docker 层面失败。

进入 Phase 1 之前必须修复：

- **BLOCKER**：B-1（迁移目标库的静默默认值 + README 本地流程不可用）、
  B-2（无远端 + Drive 唯一副本 + 父仓未忽略）
- **HIGH**：H-1（凭据忽略规则 + 秘密扫描）、H-2（Alembic metadata 为空）、
  H-4（CI 从不执行迁移、无覆盖率阈值、门只覆盖 `src`）、H-5（data checksums 只能现在设）、
  H-6（写权限移交无技术强制）

H-3 已被实机证实（不再是"将来可能"），且卷现在还是空的——**现在重建代价为零，Phase 2 再修就要
在有证据的卷上动手**，因此建议与 H-5 一起，在 Phase 1 开工前的同一次维护窗口里处理掉。

M-12、M-13 属于部署机制问题，不阻塞 Phase 1 编码，但会阻塞 Phase 1 的**上线与回滚**，
建议排进 Phase 1 的部署准备。其余 MEDIUM 项建议在 Phase 1 任务卡中逐条登记为已知项并给出计划。

一条与本仓库无关但需要处理的运维问题：审查机（`onepiece`，Windows）自带的
`C:\Windows\System32\OpenSSH\ssh.exe` 已损坏（`ssh -V` 即 exit 255、无任何输出），
目前只能靠 Git 附带的 OpenSSH 10.3p1 连 Hermes。这会影响将来任何自动化部署脚本。

---

## 附录 A：本次实际执行的验证命令

Git 与仓库边界：

```
git -C <repo> rev-parse --show-toplevel --abbrev-ref HEAD HEAD
git -C <repo> remote -v                      # 输出为空
git -C <repo> branch -a -vv                  # 仅 ai/chatgpt/phase-0-scaffold
git -C <repo> log --oneline --decorate -n 20 # e504a42, 7ca224c
git -C <repo> status --porcelain=v1 -uall    # 空
git -C <repo> ls-files                       # 31 个文件
git -C <repo> config --local --list
git -C <repo> check-ignore -v -- <26 个候选敏感文件名>
Test-Path "G:\我的云端硬盘\AI\.git"                                   # True
git -C "G:\我的云端硬盘\AI" ls-files -- LedgerBridge                  # 空
git -C "G:\我的云端硬盘\AI" check-ignore -v LedgerBridge              # exit 1
git -C "G:\我的云端硬盘\AI" status --porcelain -uall -- LedgerBridge  # ?? LedgerBridge/
```

质量门（使用仓库内 `.venv`，Python 3.13.14）：

```
python -m ruff check .                       # All checks passed
python -m ruff format --check .              # 20 files already formatted
python -m mypy src                           # Success: no issues found in 6 source files
python -m pytest --cov=ledgerbridge --cov-report=term-missing   # 1 passed, TOTAL 56%
python -m bandit -c pyproject.toml -r src    # No issues identified
python -m pip_audit                          # 默认控制台崩溃(UnicodeDecodeError)
PYTHONUTF8=1 + chcp 65001; python -m pip_audit  # No known vulnerabilities found
python -m mypy alembic tests                 # 3 errors (import-untyped)
python -m bandit -c pyproject.toml -r alembic # No issues identified
```

行为探针（只读，不修改任何文件）：

```
python -c "清空 LEDGERBRIDGE_* 后 Settings().database_url"
   -> postgresql+psycopg://ledgerbridge:change-me@localhost:5432/ledgerbridge
python -c "from ledgerbridge.db import Base; print(sorted(Base.metadata.tables))"
   -> []
python -c "Config().set_main_option('sqlalchemy.url', '...p%ssw...')"
   -> ValueError: invalid interpolation syntax
TestClient(app).get('/openapi.json' | '/docs' | '/redoc' | '/health/live')
   -> 200 / 404 / 404 / 200
Test-Path src\ledgerbridge\py.typed          # False
```

网络（无凭据，外部视角）：

```
Test-Connection 192.168.1.39                 # 可达
TcpClient 192.168.1.39 : 22                  # OPEN
TcpClient 192.168.1.39 : 8650 / 5432 / 8000 / 8080   # closed/filtered
ssh -o BatchMode=yes aiadmin@192.168.1.39    # exit 255（本机无可用私钥）
```

本地无 Docker（`Get-Command docker` 未找到），因此 Compose 渲染、构建与运行时行为
**未在本机复现**，与任务卡 `:47` 的说明一致。

## 附录 B：Hermes 实机核查实际执行的只读命令

登录方式：`"C:\Program Files\Git\usr\bin\ssh.exe" -i ~/.ssh/ha_auto_ed25519
-o BatchMode=yes -o IdentitiesOnly=yes aiadmin@192.168.1.39`
（Windows 自带 ssh.exe 在审查机上已损坏，见结论）。

```bash
cd /srv/ai-center/ledgerbridge
ls -la                                  # .env 为 -rw------- aiadmin；无 .git；有 DEPLOYED_REVISION
cat DEPLOYED_REVISION                   # e504a42
git rev-parse HEAD                      # fatal: not a git repository   -> M-12
docker --version; docker compose version # 29.7.2 / v5.4.0
docker compose ps -a                    # api+postgres healthy, worker up, 127.0.0.1:8650->8000
docker compose config --quiet           # RENDER_OK
curl -sS -D - -o /dev/null http://127.0.0.1:8650/health/live    # HTTP/1.1 200 OK
curl -sS -D - -o /dev/null http://127.0.0.1:8650/health/ready   # HTTP/1.1 200 OK
curl -sS http://127.0.0.1:8650/health/ready                     # {"status":"ready"}
ss -lnt                                 # LedgerBridge 仅 127.0.0.1:8650；无 5432
docker volume ls | grep -i ledger       # ledgerbridge_artifacts / ledgerbridge_postgres-data
docker network ls | grep -i ledger      # ledgerbridge_default (bridge)
docker compose exec -T api id                                          # uid=10001(ledgerbridge)
docker compose exec -T api stat -c "%u %g %a %n" /var/lib/ledgerbridge/artifacts   # 0 0 755  -> H-3
docker compose exec -T api python -c "...os.access(p, os.W_OK)"        # W_OK False -> H-3
docker compose exec -T postgres pg_controldata /var/lib/postgresql/data | grep -i checksum
                                        # Data page checksum version: 0 -> H-5
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'
                                        # 仅 alembic_version 一张表（口令未离开容器）
docker compose exec -T api alembic current                             # 无 revision -> H-4
docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.RestartCount}}' <各容器>   # unless-stopped 0
docker inspect -f '{{range .Mounts}}...rw={{.RW}}{{end}}' ledgerbridge-api-1        # rw=true -> H-3
docker inspect -f '{{json .Config.Healthcheck}}' ledgerbridge-worker-1              # null -> M-4
docker inspect -f 'ReadonlyRootfs=... CapDrop=... SecurityOpt=...' ledgerbridge-api-1 # 全空 -> M-5
docker images -a | grep -i ledgerbridge # 仅 :latest 两个 + 一个 <untagged> -> M-13
find . -type f -exec sha256sum 逐文件比对 # 31 个受控文件与本地 e504a42 全部一致
```

全程未 `cat` 生产 `.env`，未打印任何数据库口令（`psql` 的用户名/库名在容器内展开）。
