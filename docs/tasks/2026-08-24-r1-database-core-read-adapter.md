# R1 database Core read adapter

日期：2026-08-24
结论：数据库 reader 适配器已实现；真实运行 gate 仍关闭

## 本轮实现

- 新增 `LEDGERBRIDGE_READER_DATABASE_URL` 与显式 `internal_read_backend=database`。
  数据库后端只有在内部 read API 已开启、策略代次存在且 reader URL 明确提供时才会
  构造；默认仍为合成 fixture，生产配置继续被拒绝。
- `DatabaseInternalReadService` 使用独立 reader session，只调用 Migration C 的
  `internal_read.current_audit_horizon()`、`list_candidates_as_of(...)` 与
  `get_reconciliation_as_of(...)`。它不发出 `public.*` 查询，并在投影结构、审计
  horizon、策略 UUID 绑定异常时 fail closed。
- `EntityGrant` 保留 HTTP 使用的营业单元 stable ref，同时可携带不可变 UUID；数据库
  reader 拒绝仅有字符串 ref 的 grant，避免应用层通过宽表解析权限。
- 新增 HMAC + 压缩 canonical JSON cursor。cursor 绑定 contract、principal 摘要、策略
  代次、grant 摘要、过滤条件、审计 horizon 和 `(created_at, candidate_ref)` 边界；
  验签失败为 400，过期/权限变更不会静默刷新 horizon。合成 backend 继续拒绝非空
  cursor。
- Evidence content 仍等待已审核的 S1 decryptor/ArtifactStore 边界；LedgerSummary
  仍等待带 scope/horizon 的专用聚合函数。两者在数据库 backend 下返回固定 503，
  不降级到宽表或文件系统读取。

## 验证

- 新增数据库 reader 单元测试覆盖 horizon 固定、函数调用、无 `public.*` SQL、UUID
  绑定和未完成边界的 fail-closed 行为。
- Windows 定向 cursor/reader 回归：`39 passed, 1 warning`；上一轮 Windows 全量：
  `478 passed, 189 skipped, 1 warning`。
- Ruff check、strict mypy、compileall 通过；未连接生产数据库、未创建 reader 凭据、
  未启用生产路由或真实数据读取。

## 未闭环 gate

生产 mTLS verifier、签名 cursor key、reader bootstrap、S1 解密器、ledger scoped
aggregate、Hermes/真实数据回放及 production enablement 仍需独立实现和审计。
