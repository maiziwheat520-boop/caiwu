# R1 Migration B: ledger attribution and reconciliation snapshots

日期：2026-08-24  
事项：把 R1 设计中的账本归属、主腿语义和不可变快照基础落成第二段 migration。  
结论：完成（schema foundation；未部署）

## 实现

- Alembic `20260824_0013` 以 `20260824_0012` 为父迁移。
- 新增 `journal_entry_attribution` 与 `posting_attribution`，保存不可变营业单元、
  业务月和 entity-scoped reporting category snapshot。
- 为既有 Phase 5 `reconciliation_leg` 增加兼容的 `posting_id`、`is_primary`、
  entity/business-unit/month 归属字段与单组主腿唯一索引；旧数据不被猜测填充。
- 新增 append-only `reconciliation_snapshot` 及 proposal/suspense child facts，
  固定 audit watermark、`PRIMARY_LEG` 金额基准和本地 scope/month revision。
- 所有新表只 revoke 权限，不授予 API/worker/app/reader；尚未创建 snapshot builder、
  writer command 或 reader view。

## 验证

- WSL disposable PostgreSQL：完整 `0011→0013`、降级回 `0011`、再升级到 head 通过。
- 新增 migration contract tests、Ruff 和 Python compile 通过；Windows 全量在
  Migration A 后为 `466 passed / 149 skipped / 1 warning`。

## 未做事项

Migration C（reader role/views/evidence audit wrapper）、ORM/read service 接线、
主腿/快照 builder command、生产 mTLS、Hermes 加密运维、真实数据、merge/deploy
仍保持 gate。
