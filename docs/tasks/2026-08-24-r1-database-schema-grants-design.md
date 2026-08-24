# R1 database schema/grants design task

日期：2026-08-24
结论：设计中；只产出设计与验收计划，不创建迁移或启用数据库读取

## 目标

为后续 PostgreSQL-backed R1 定义 Candidate、营业单元、证据/密文版本、账本归属、
对账快照、独立 reader 角色、只读视图与证据下载审计函数。正式设计见
`docs/architecture/R1_DATABASE_SCHEMA_GRANTS.md`。

## 范围边界

- 以当前 `20260824_0011` 为迁移基线，只设计后续三段 forward migrations。
- 本任务不编写 Alembic/ORM、不创建数据库角色、不改 Compose、不访问或迁移生产 Hermes。
- 不给 API/worker 任何新增 Candidate、证据、快照或账本归属写权限。
- R0 fixture 只作为 API projection golden；另行设计完整 database fact fixture。
- 生产 mTLS、KeyProvider、LUKS、持久审计 backend、cursor key、备份恢复适配、I1/D1、
  Web/Hermes/Outlook/OneDrive 和真实数据继续保持 gate。

## 已确认决策摘要

- 完整 Candidate revision + typed event，一对一绑定现有 append-only AuditEvent。
- 独立 `ledgerbridge_reader`；应用层 typed mTLS scope + 数据库白名单只读视图。
- LedgerSummary 实时聚合 POSTED facts；Reconciliation 使用 scope+month 局部 revision 的
  不可变快照并保存 ledger/audit watermark。
- 营业单元 UUID + entity-scoped stable ref；reporting category 按 entity 独立。
- 账本业务月与 reporting category 使用不可变一对一 attribution 表。
- Evidence 固定 entity/营业单元，密文版本 append-only；assigned Candidate 必须同 scope。
  unassigned Candidate 可引用同 entity evidence，但 evidence 下载仍按其自身营业单元授权。
- 生产分页使用绑定 principal/grants/filters/policy/horizon 的签名 keyset cursor。
- 三段迁移依次为 Candidate/evidence、ledger/reconciliation、reader/views/audit/grants。

## 完成条件

- 设计覆盖表、约束、索引、视图、安全函数、精确 grant matrix、迁移/恢复验收和明确 gate。
- Luna 完成现状/字段/决定并行盘点；Sol 完成关键架构和权限复核。
- 任何审计发现先修订设计，不把“文档完成”表述为“数据库 R1 已实现”。
