# LedgerBridge 0006 角色成员关系 HIGH 修复

日期：2026-08-24  
修复提交：`c05e9ab` + `54b0f2e`  
分支：`ai/chatgpt/phase-3-connector-runner`

## 修复

`alembic/versions/20260823_0006_runtime_role_split.py` 现在枚举
`pg_auth_members` 中 `ledgerbridge_api` / `ledgerbridge_worker` 的全部直接成员关系，使用 `%I` 标识符安全撤销，再重新声明 `NOINHERIT`、非特权属性和兼容角色撤销。运行时角色的成员 allowlist 为空，历史 owner/高权限成员关系不会遗留到迁移后。

新增 `tests/test_ledger_core.py::test_runtime_role_split_removes_preexisting_owner_membership`：在 0005 后授予 owner 成员关系，升级 0006，检查成员关系为空，并以 `SET SESSION AUTHORIZATION ledgerbridge_api` 模拟 API 登录后验证 `SET ROLE ledgerbridge_owner` 被拒绝。`tests/test_phase2_runtime_boundary.py` 增加迁移结构断言。

## Hermes 隔离证据

使用唯一临时 Compose 项目和全新 PostgreSQL 15 卷，生产模式 `LEDGERBRIDGE_ENV=production`：

1. 回放迁移至 `20260823_0005`。
2. 预先执行 `GRANT ledgerbridge_owner TO ledgerbridge_api, ledgerbridge_worker`，观测 owner 成员关系 **2**。
3. 升级至 `20260823_0006`，观测 API/worker 全部成员关系 **0**。
4. 在 owner 维护会话中切换 `SET SESSION AUTHORIZATION ledgerbridge_api`，随后 `SET ROLE ledgerbridge_owner` 返回 `permission denied`。

结果：`role_drift_replay=PASS`。临时容器、卷、网络、工作目录和测试密码均在 trap 中清理；生产 Hermes 仍为 `e426b488b2abb02f10ef02a61aae7ebe24c3283f / 20260822_0004`，未迁移、未重启、未写入。

## 本地验证

- 全量 pytest：`241 passed, 140 skipped, 1 warning`。
- Ruff format/check、严格 mypy、Bandit、`git diff --check`：通过。
- PostgreSQL 集成测试在本 Windows 工作站因未配置 `LEDGERBRIDGE_MIGRATION_DATABASE_URL` 跳过；Hermes 回放覆盖了同一迁移和 `SET ROLE` 行为。

## 剩余发布闸门

原发布前审计中的 HIGH 已关闭。MEDIUM runner 接收阶段全局连接/aggregate spool admission、LOW heartbeat symlink hardening、未变更 0002 的 P2-B1 `search_path` 遗留、trusted auth、signed manifest/key custody、真实 Connector、生产密码 rollout、merge/deploy 仍未授权或未闭环。
