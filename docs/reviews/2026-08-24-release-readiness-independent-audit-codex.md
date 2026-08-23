# LedgerBridge 发布前独立安全审计

日期：2026-08-24  
分支：`ai/chatgpt/phase-3-connector-runner`  
审计范围：`801a2f2bd0f76ca6a91c42047e1c2943f23fe373..350e415d00949435ced81b21bc08684a56c2d840`  
机器合同：`C:\Users\87614\AppData\Local\Temp\ledgerbridge-release-audit-350e415-v2`

## 结论

本轮审计结论为 **CHANGES REQUIRED**，不是发布批准：发现 1 个 HIGH、1 个 MEDIUM、1 个 LOW。生产 Hermes 当前仍未触碰，async 路由、真实 Connector 和 manifest 仍关闭，因此这些问题不会改变当前已部署 Slice A 的运行状态；但 HIGH 必须在任何现有数据库升级前关闭，MEDIUM 必须在 hostile/真实 Connector 启用前关闭。

## Findings

### HIGH：0006 未清除或拒绝任意 API/worker 角色成员关系

位置：`alembic/versions/20260823_0006_runtime_role_split.py:29-35`

迁移重新声明 `NOSUPERUSER`、`NOCREATEDB`、`NOCREATEROLE`、`NOINHERIT` 等属性，只撤销了 `ledgerbridge_app` 对 API/worker 的成员关系，没有枚举 `pg_auth_members` 中其它历史成员关系。若旧数据库曾执行 `GRANT ledgerbridge_owner TO ledgerbridge_api`（或授予其它高权限角色），`NOINHERIT` 不会阻止该成员关系被显式 `SET ROLE` 激活；运行时凭据可能因此获得 owner/高权限能力。

验证：静态源代码与 PostgreSQL 角色语义确认；当前 Hermes 一次性回放显示新库没有额外成员关系，这是反证默认 bootstrap 状态，不是对历史漂移数据库的安全证明。

最小修复：迁移时发现 API/worker 任意非允许成员关系就 fail closed，或先撤销全部成员关系再授予精确 allowlist；增加 disposable PostgreSQL 回归，预先授予 owner 角色并验证迁移后 `SET ROLE` 被拒绝。

### MEDIUM：runner 接收阶段缺少全局连接/临时 spool admission

位置：`src/ledgerbridge/connector_runner.py:173-195, 437-440`

每个 Unix socket 连接在执行槽限制生效前创建独立的 `SpooledTemporaryFile`，单个 artifact 可接近 50 MiB；server 启动没有连接数或 aggregate spool 预算。能够访问 socket 的同 UID/恶意 Connector 可保持大量半包连接，耗尽 runner 内存或 32 MiB `/tmp`，造成重启或不可用。

当前 manifest 为空且生产路径关闭，所以这是启用时 hostile-IPC gate，不是当前生产阻断。最小修复是连接 semaphore + aggregate 内存/磁盘 spool 预算，并在 Hermes Linux 做并发半包/近上限 artifact 压测。

### LOW：worker heartbeat 使用可预测 `/tmp` 临时路径

位置：`src/ledgerbridge/worker.py:50-56`

`write_heartbeat()` 对可预测的 `/tmp/ledgerbridge-worker-heartbeat.tmp` 直接 `write_text()`，没有私有运行目录、`O_NOFOLLOW` 或 exclusive-create。共享主机本地用户可预置 symlink，使写入失败并造成 worker heartbeat 失效；Compose 的私有 `/tmp` 降低了当前部署可达性。

最小修复是使用 worker 私有运行目录，或采用 `O_NOFOLLOW|O_EXCL` 创建并做 descriptor identity 检查，同时补 Linux symlink 回归。

## 覆盖与排除

审计覆盖 22 个本分支新增/修改的安全敏感源、迁移和运行时文件；并行完成数据库/配置审计与 artifact/runner/dispatch 审计。`tests/`、文档、未变更的旧迁移和生产外部基础设施不计入源清单。

明确未闭环项：

- 未变更的 `20260821_0002_ledger_core.py` 仍有 P2-B1 的 `search_path`/非限定引用遗留风险；当前依赖撤销 `TEMPORARY`，不应当作为永久函数级保证。
- trusted internal auth、signed manifest/key custody、真实 Connector、生产密码 rollout、merge/deploy 均未执行。
- 直接绕过 Compose 的生产 Alembic CLI 仍需单独确认环境变量 fail-closed 约束。

机器可验证合同：`scan-manifest.json`、`findings.json`、`coverage.json`、`report.md` 已在上述临时目录通过 `finalize_scan_contract.py` 和 `validate_scan_contract.py`；规范化候选记录为 3 条。

## 验证证据

- Windows 全量：`241 passed, 139 skipped, 1 warning`。
- `ruff format --check`、`ruff check`、严格 `mypy src alembic tests scripts`、Bandit：通过。
- `uv lock --offline`、敏感路径检查、`git diff --check`：通过；本机无 Docker CLI，Compose 由 Hosted CI 与先前 Hermes 隔离回放覆盖。
- Hermes disposable PostgreSQL 15：此前已完成 `0001→0008` 生产模式回放，API/worker 为非特权 `NOINHERIT`，无额外成员关系；该证据不覆盖历史角色漂移场景。生产 Hermes 仍为 `e426b488b2abb02f10ef02a61aae7ebe24c3283f / 20260822_0004`。
- Hosted CI push `32656184274` 与 PR `32656181743` 的 `secrets`、`quality`、`compose` 均成功。

## 发布闸门

在 HIGH 修复并完成带历史角色漂移的 PostgreSQL 回归前，不应把本分支迁移到任何现有数据库。MEDIUM/LOW 与 P2-B1、trusted auth、signed manifest、真实 Connector、密码 rollout、merge/deploy 仍保持独立闸门；本报告不授权生产变更。
