# R1 database schema/grants design task

日期：2026-08-24
结论：设计中/待 Sol 批准；只产出设计与验收计划，不创建迁移或启用数据库读取

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
- 生产分页使用绑定 principal/grants/filters/policy 与全局
  `audit_event.sequence`/hash horizon 的签名 keyset cursor；首屏先调用 reader-only
  `internal_read.current_audit_horizon()` 固定同一 `(sequence bigint, hash bytea)`，
  后续每页把两值传给 as-of 函数并精确复验 audit 行；每页按该 horizon 选择最新可见
  revision，并重新计算授权摘要。
- 三段迁移依次为 Candidate/evidence、ledger/reconciliation、reader/views/audit/grants。
- Core-only typed retrieval descriptor 不进入 HTTP wire；它携带完整、可验证的
  S1 encrypted blob metadata；`internal_read.resolve_active_evidence_blob(evidence_ref)`
  是 `SECURITY DEFINER`，owner 才是函数 owner，`ledgerbridge_reader` 仅有
  `EXECUTE`，不能选择旧 blob，reader/API/worker 不获得宽表或密钥材料权限。
- S1 metadata 固定为：`object_ref varchar(64)` 小写 hex、generation
  `varchar(128)` canonical regex、artifact purpose/AAD literal、schema/algorithm
  literal、chunk `1..1048576`、stream header/nonce 24 bytes、wrapped ciphertext
  48 bytes、ciphertext `<=268435456`，storage key 必须由 ciphertext digest 精确派生。
- Candidate 创建、revision、CREATE event、transition、typed event、audit、blob
  version 和 snapshot 使用 deferred composite FK/UNIQUE 与 deferred constraint
  trigger；audit writer 复用既有 `audit_event.xmin/action/payload`，在同事务
  以 `pg_current_xact_id()` 做短暂 liveness 检查并要求 exact canonical
  payload，不新增或持久化 XID；CREATE event 不计入 wire review count。
- Blob 只有一个 genesis 和一个 active tip；REWRAP 沿同一 evidence predecessor
  链保持 object_ref、schema/algorithm/chunk/stream header 与 secretstream
  payload frames，但 wrapped-key header 改变，因此完整 envelope 的
  ciphertext digest/size/storage_key 重新计算且通常变化（各自仍唯一）；
  REENCRYPT 使用此前未出现的新 object_ref；两者都必须在同一事务写 audit，
  不允许无关链复用 object_ref。新增 append-only
  `encrypted_object_identity(object_ref PK, evidence_ref, created_at)`，GENESIS/
  REENCRYPT 同事务插入 identity，REWRAP 只复用 predecessor identity，PK 负责并发
  全局唯一而非 trigger snapshot 查询。
- Snapshot builder 为 owner-only `SECURITY DEFINER`，在 `REPEATABLE READ` 下先获取
  与 `append_audit_event` 相同的全局 `hashtext('ledgerbridge.audit_event')`
  advisory lock，再按 scope 加锁并以真实 audit sequence/hash 构建 POSTED facts、
  children 和 audit；冲突回滚重试。`posting.entry_id`/`journal_entry.status` 的
  primary posting SQL 与 `reconciliation_leg.is_primary` proposal SQL 分离，无法
  可靠归属时拒绝。
- Source provenance 必须闭环：真实 SQL 列 `public.source_record.artifact_id`
  与 `evidence.raw_artifact_id` 两者非空时必须相等，
  source registry/channel 与 linked source record/raw artifact 不一致即 fail closed。
- Cursor horizon 必须实际参与 as-of revision 查询（不得当作 revision 编号），每页
  重新计算 principal/grants/filter/policy digest；reader bootstrap 必须清理双向
  membership、ownership、旧 ACL、`PUBLIC` 与 default ACL；只有专用 Core internal-read
  进程接收独立 `LEDGERBRIDGE_READER_DATABASE_URL`，API/worker/migrate 不接收 reader URL 且无 fallback。
- reader 没有 `public` 基表 `SELECT`：生产 cursor/as-of 只能调用固定 owner、固定
  `search_path` 的 `SECURITY DEFINER` `internal_read.current_audit_horizon()`、
  `internal_read.list_candidates_as_of(...)` 与 `internal_read.get_reconciliation_as_of(...)`，
  reader 仅 `EXECUTE`；两个 as-of 函数同时接收 sequence+hash 并在开头精确验证
  `public.audit_event` 同一行。unassigned 谓词只返回 `r.business_unit_id IS NULL`，
  不作 NULL 通配；函数严格校验 scope/horizon/keyset 与 `limit<=100`，返回最多
  `limit+1`（最多101）closed rows，Core 截断并据 sentinel 生成 has_more/cursor。数据库 ACL
  先撤 `PUBLIC CONNECT/TEMPORARY/CREATE`，再显式给 `ledgerbridge_api`、worker、reader
  `CONNECT`（固定非 runtime owner/migration/backup 角色仅按 allowlist），reader 不得
  `TEMPORARY/CREATE`，生产 `ledgerbridge_app` 无 `CONNECT`。
- as-of `superseded_by_candidate_ref` 只有所选 revision 为 `SUPERSEDED` 且 successor
  revision-1 的 CREATE audit sequence 不晚于同一 horizon 时才返回，否则必须为 NULL，
  禁止未来 successor 泄露；accounting month wire 输出固定为
  `to_char(..., 'YYYY-MM')::varchar(7)`。
- reconciliation partial unique primary index 只保证 at-most-one；另设
  `DEFERRABLE` 末端 constraint trigger 对 group/scope 保证恰好一个 primary，并用
  scope advisory/row lock 串行并发。完整 R0 字符串 CHECK 覆盖 source/display label、
  summary、blocker message、evidence media_type、event actor/reason、BU/category
  ref-label 非空长度，以及 nullable/可空字符串的 display_name 禁止 `/`、反斜线、
  CR/LF/NUL。
- Migration B 先盘点既有 POSTED/相关事实；任何不完整归属都 fail closed，不通过 inner
  join 漏掉事实。代码 `0011` 与 Hermes 当前 `0004` 是不同事实，生产不得跳版本。
- fresh-host restore 必须用外部 KeyProvider 验证 FINAL、plaintext digest/size、active
  blob、object-identity registry、views/functions/triggers/constraints/default ACL 和角色隔离。

## 完成条件

- 设计覆盖表、约束、索引、视图、安全函数、精确 grant matrix、迁移/恢复验收和明确 gate。
- Luna 完成现状/字段/决定并行盘点；Sol 完成关键架构和权限复核。
- 任何审计发现先修订设计，不把“文档完成”表述为“数据库 R1 已实现”。

## 并发提交记录与当前边界

初稿曾被并发提交 `1620007` 带入；本次仅通过后续 docs-only 修订纠正设计规范，
不把并发带入的 persistent-audit 代码纳入本任务，也不声称任何数据库迁移、reader
bootstrap、KeyProvider 适配、snapshot builder 或审计实现已经完成。最终设计仍需
Sol 批准，并在批准前保持 `NOT APPROVED`、真实数据关闭和生产读取关闭。
