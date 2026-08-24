# R1 Migration C：reader / internal_read 只读表面

日期：2026-08-24  
事项：在 Migration A/B 的事实模型之上落成独立 `ledgerbridge_reader` 只读边界。

## 交付

- `20260824_0014_r1_fact_hardening.py`：补齐密文 object identity、证据来源与
  Candidate scope/history 约束、POSTED attribution/completeness guards、snapshot
  blocker 事实和运行时/public ACL 收紧。
- `20260824_0015_r1_internal_read_surface.py`：创建独立 `internal_read` schema、8
  个 security-barrier views、5 个 `SECURITY DEFINER` as-of/read/audit wrapper
  functions，并只授予 `ledgerbridge_reader`。
- CI bootstrap 显式创建 test-only reader login；迁移本身不创建角色或凭据。

## 关键安全规则

- reader 不获得 `public` schema usage、基表 SELECT、sequence usage 或
  `append_audit_event` execute；API/worker/app 不获得 internal_read usage。
- 所有新函数固定 `SET search_path = pg_catalog`，所有事实 hardening trigger
  也固定 search path；视图固定 `security_barrier=true`、`security_invoker=false`。
- as-of 读取必须提供存在且 32-byte 的 AuditEvent sequence/hash horizon；显式
  unassigned scope 只匹配 `business_unit_id IS NULL`，不扩成 entity-wide 查询。
- downgrade 遇到 internal-read/fact 数据时 fail closed；空库可逆回滚。

## 验证

- WSL disposable PostgreSQL：R1 migration test 全部 **11 passed**，包含 role/ACL、
  view/function catalog、空库 downgrade、非空 downgrade 拒绝、horizon/as-of、
  active blob resolver、audit wrapper 和事实写入边界。
- Windows 静态门禁：`uv lock --offline`、Ruff check/format、strict mypy、Bandit、
  `git diff --check` 全部通过。
- 全仓 WSL 回归未作为本地通过证据：现有本机 PostgreSQL owner 密码与 CI disposable
  凭据不一致，dispatch 集成在连接初始化阶段失败；这不触碰生产数据库。CI bootstrap
  已补 reader role，待 Hosted CI 以新提交验证完整 job。

## 状态与边界

完成实现与隔离验证；未 merge、未部署、未创建生产 reader、未读取真实财务数据。
ORM/read service、mTLS workload 授权、恢复演练、独立安全复核和生产迁移仍是后续 gate。
