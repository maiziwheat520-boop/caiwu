# R1 database Core read adapter and S1 read boundaries

日期：2026-08-25
结论：数据库 reader 适配器与 S1 应用边界已实现；生产运行 gate 仍关闭

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
- S1 evidence decryptor 现在只接受 `internal_read.resolve_active_evidence_blob`
  的单行结果，严格校验 scope/ref-to-UUID 绑定、canonical ciphertext storage key、
  envelope 元数据、大小/摘要和 allowlisted media type/display name，再通过注入的
  `EncryptedArtifactStore` 做 descriptor-backed ciphertext verification、secretstream
  解密和 plaintext 摘要复核。没有注入解密器时仍固定 503，不降级到明文文件读取。
- `internal_read.get_ledger_summary_as_of(...)` 现在由 0015 安装并只授予
  `ledgerbridge_reader`。它固定校验精确 audit horizon、entity/business-unit 归属、
  月份边界，先执行 owner-only `r1_assert_posted_total_integrity()`，再按
  `POSTED` primary posting 的 immutable category snapshot 聚合。应用层再次校验
  scope、月份、`POSTED/CNY` 和金额类型；异常不会静默变成空汇总。
- 0015 的 evidence descriptor 同时返回 entity/business-unit、媒体类型和安全显示名，
  便于应用层在解密前完成授权和元数据绑定。数据库 receipt/audit 函数仍保留为后续
  durable read-receipt 接入点；当前 HTTP 路由没有启用数据库 decryptor 或 reader
  bootstrap。

## 验证

- 新增数据库 reader 单元测试覆盖 horizon 固定、函数调用、无 `public.*` SQL、UUID
  绑定和未完成边界的 fail-closed 行为。
- Windows 定向 cursor/reader 回归：`39 passed, 1 warning`；上一轮 Windows 全量：
  `478 passed, 189 skipped, 1 warning`。
- Ruff check、strict mypy、compileall 通过；未连接生产数据库、未创建 reader 凭据、
  未启用生产路由或真实数据读取。

## 2026-08-25 S1 implementation slice

- 新增 `DatabaseInternalReadService` 的可注入 `EncryptedArtifactStore` 边界和严格
  metadata parser；单元测试覆盖成功解密、scope 绑定、非 canonical storage key、
  LedgerSummary category projection，以及默认无 decryptor 的 fail-closed 行为。
- 0015 新增 `get_ledger_summary_as_of(uuid, uuid, date, date, bigint, bytea)` 和精确
  `ledgerbridge_reader` EXECUTE grant，并扩展 `resolve_active_evidence_blob` 的返回
  元数据。迁移 downgrade 同步删除新函数。
- 解密 descriptor 现在保留并逐项绑定实际 secretstream chunk/header/wrapped-key
  元数据；未知 evidence 在 resolver 中返回零行，和越权资源保持同形 404。Sol
  复核最终结论为 **BLOCKER 0 / HIGH 0**，此前发现的 horizon TOCTOU 与两项
  MEDIUM 已修复；全局完整性 guard 的跨范围可用性耦合仍是明确的 fail-closed LOW。
- 当前 Windows 全量为 **510 passed / 190 skipped / 1 warning**；S1/database
  reader 定向为 **23 passed**，routes/contract/migration source 回归为
  **42 passed / 40 skipped / 1 warning**。Hermes/PostgreSQL 真实 replay、
  KeyProvider 生产实现、durable receipt wiring、reader bootstrap 和启用仍未执行。

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
- PG18.6 is not PG15. Hosted run `32751756532` verified pushed SHA `d0f0cf2`
  on PostgreSQL 15; `secrets`, `quality`, and `compose` all completed
  successfully. Quality also completed the unchanged 90% coverage gate,
  Alembic upgrade→downgrade→upgrade, Bandit, and pip-audit. No threshold or
  coverage bypass was added. Production mTLS, reader bootstrap, S1 decryptor,
  scoped ledger aggregate, Hermes/real-data replay, and enablement remain
  explicitly closed.
- The run covers the clean pushed SHA only. After it completed, parallel agents
  left uncommitted edits in `0015`, `internal_read_service`, and the matching
  test; those edits were not staged or pushed and need a separate review and
  Hosted run before they can be treated as verified.

## 未闭环 gate

生产 mTLS verifier、签名 cursor key、reader bootstrap、生产 KeyProvider、durable
read receipt wiring、Hermes/真实数据回放及 production enablement 仍需独立实现和审计。
