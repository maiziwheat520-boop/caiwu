# R1 Migration A: Candidate/evidence fact foundation

日期：2026-08-24  
事项：把 R1 设计中的 Candidate/evidence 第一段 forward migration 落成隔离代码。  
结论：完成（未部署；运行时角色仍无新增权限）

## 实现

- Alembic `20260824_0012` 以 `20260824_0011` 为唯一父迁移。
- 新增营业单元、实体独立 reporting category、evidence object、S1
  secretstream 加密 blob 版本、Candidate identity/source/revision、typed
  blocker/event/field-change/conflict-resolution/evidence link 表。
- 所有新事实表使用 immutable/append-only trigger；Candidate/evidence entity
  与 assigned business-unit scope 在 deferred constraint trigger 中双向复核。
- Secretstream envelope、purpose/AAD 相关元数据、digest-derived storage key、
  SHA-256/size/generation 长度和 Candidate 状态/版本约束在数据库层固定。
- 迁移只执行 `REVOKE`，不创建 reader role、不创建 view/function read surface，
  不给 API/worker/app 任何 Candidate/evidence 写或读权限。
- downgrade 仅允许空的隔离开发库；任一新表有数据即拒绝破坏性回滚。

## 验证

- WSL PostgreSQL 18 disposable database：完整 `0011 -> 0012`、空库
  `0012 -> 0011 -> 0012` 通过；catalog 核验新表、scope trigger 和 runtime
  无表权限通过。
- Windows 全量：`466 passed / 149 skipped / 1 warning`。
- `ruff format/check`、strict `mypy src alembic tests`、`git diff --check` 通过。

## 未做事项

Migration B（ledger/reconciliation attribution）、Migration C（reader role、
closed views、evidence audit wrapper）、ORM/read service 接线、生产 mTLS、
KeyProvider/Hermes 加密运维、真实数据、merge/deploy 仍保持 gate。
