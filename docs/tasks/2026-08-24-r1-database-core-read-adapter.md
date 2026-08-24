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

## 2026-08-24 security follow-up

- Commit `f3c2a73` adds explicit immutable `(business_unit_ref, UUID)` bindings
  to database grants and revalidates every returned candidate against that pair.
  Grants containing independent ref/UUID sets, IDs without refs, or multiple
  candidate scopes fail closed.
- Cursor decoding now rejects non-canonical Base64URL aliases and malformed
  compressed bodies; month-filtered reads continue bounded keyset pages before
  issuing a cursor, and candidate detail lookup follows issued cursors.
- Windows full suite is **494 passed / 190 skipped / 1 warning**. Safe branch
  `ai/chatgpt/r1-core-reader-cursor` was pushed; hosted CI is the remaining gate.

## 2026-08-25 final local verification

- Current reviewed head is `b9e3446` (implementation/test base `c61825e`).
  Sol's independent short recheck of
  `0014`, `0015`, the migration chain, and the CI/bootstrap delta found no
  validated BLOCKER/HIGH/MEDIUM. The recheck does not authorize merge,
  production role migration, reader bootstrap, or real-data access.
- The database reader now has immutable entity/business-unit bindings,
  canonical signed cursor and audit-horizon binding, candidate entity/scope
  revalidation, and reconciliation entity/business-unit/month revalidation;
  multiple-scope union remains fail closed. The backup verifier covers the
  optional backup role, memberships, owners, object/default ACLs, internal-read
  privileges, and the narrowly allowed database-owner schema-creation case.
- Windows R1 migration regression: **49 passed** against disposable WSL
  PostgreSQL **18.6**. A separate CI-like Linux/WSL run against PostgreSQL
  18.6 produced **696 passed / 1 warning** and **91.36%** coverage
  (`6860` statements, `593` missed), passing the unchanged
  `--cov-fail-under=90` gate. Full Alembic head→base→head and the focused
  backup/reader/R1 run (**59 passed / 40 skipped**) also passed.
- PG18.6 is not PG15; no latest-HEAD Hosted PG15 result is claimed. Earlier
  hosted runs failed only at the unchanged coverage step, and no threshold or
  coverage bypass was added. Production mTLS, reader bootstrap, S1 decryptor,
  scoped ledger aggregate, Hermes/real-data replay, and enablement remain
  explicitly closed.
- Hosted validation preparation confirms the pinned PostgreSQL 15 service,
  `secrets`/`quality`/`compose` triggers, and the unchanged 90% floor. The
  local tree is clean at `b9e3446`, but GitHub `:443` was unreachable for both
  read-only remote inspection and push dry-run; no Hosted run exists for this
  head yet.

## 未闭环 gate

生产 mTLS verifier、签名 cursor key、reader bootstrap、S1 解密器、ledger scoped
aggregate、Hermes/真实数据回放及 production enablement 仍需独立实现和审计。
