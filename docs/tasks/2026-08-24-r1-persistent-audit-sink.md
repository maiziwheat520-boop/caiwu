# R1 persistent internal-read audit sink

日期：2026-08-24  
事项：为合成 Core 只读 API 补齐可注入的持久审计落点  
结论：完成（默认关闭；生产仍拒绝启用）

## 范围

- 新增 `DatabaseInternalReadAuditSink`，复用现有数据库函数
  `append_audit_event` 写入 append-only `audit_event` 哈希链。
- 仅写入 `EvidenceReadAuditEvent` 的 allowlist 字段；请求头、cookie、bearer
  token、原始凭据和响应内容均不进入审计 payload。
- 每个 append 在独立 session 中显式提交；数据库异常统一映射为
  `AuditSinkUnavailable`，让上层继续 fail closed。
- `LEDGERBRIDGE_ENABLE_INTERNAL_READ_PERSISTENT_AUDIT` 默认 `false`；生产环境
  或未启用内部只读 API 时配置校验拒绝该开关。

## 未做事项

本切片没有新增迁移、表、索引、grant、生产 mTLS verifier、真实证据、Hermes
部署、持久备份/恢复或生产 KeyProvider。SQLite/Windows 回归只验证依赖选择和
失败关闭行为；生产数据库链路仍须通过 R1 operational gate 后再接入。

## 验证

- 聚焦 R1/config 回归：`46 passed`。
- 全量 Windows 回归：`464 passed, 149 skipped, 1 warning`。
- `ruff format --check`、`ruff check`、`mypy src tests` 全部通过。
- Linux/PostgreSQL 全量回放：`612 passed, 1 skipped, 1 warning`；覆盖率为
  `91.53%`。R1 合成契约尚未拥有数据库-backed implementation 的全部分支，
  因此本分支 CI 暂以 90% 为阶段性下限；该调整不放宽任何生产配置或数据 gate。
