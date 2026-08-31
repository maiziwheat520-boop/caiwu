# Task: Real-data web review cutover

- Status: implementation
- Implementation owner: Codex
- Branch: `ai/chatgpt/real-data-cutover`
- Web branch: `ai/chatgpt/web-real-data-cutover`

## Goal

Replace the deployed authenticated synthetic preview with the Core-backed review
path, then admit the already authorized May 2026 photo and Bank of China sources
as evidence-bound candidates that remain pending human review.

## Scope

- Database-backed Candidate read and decision persistence in Core.
- A dedicated mTLS ingress whose only upstream path to Core is a protected Unix
  socket; Core accepts the exact verified client certificate identity supplied
  by that ingress and no browser identity headers.
- A production KeyProvider and encrypted evidence read path.
- An offline, idempotent import command for the authorized photo/BOC sources.
- A Web Core-backed deployment overlay and mode-aware production/preview labels.
- Backup, isolated PostgreSQL 15 replay, rollback evidence, and online read-only
  acceptance before any real import.

## Non-goals

- Automatic posting, automatic account classification, workbook publication, or
  treating any imported candidate as approved.
- Browser-to-Core access, business facts in Web SQLite, or financial values in Git.
- Live mailbox collection. The first cutover consumes only the already prepared,
  source-hashed artifacts authorized by the user.

## Fail-closed gates

1. Production Core cannot enable internal reads without the database reader,
   current policy generation, persistent audit/receipt sinks, and reviewed mTLS
   policy.
2. Candidate decisions require the database command backend, request-bound user
   assertion, UUID idempotency key, optimistic revision, and append-only audit.
3. Evidence is unavailable without a production KeyProvider and verified
   encrypted descriptor/content binding.
4. The importer validates a manifest digest and source hashes, defaults every
   item to `PENDING` or `INCOMPLETE`, and is safe to replay.
5. Web remains in preview mode until Core health, migration, mTLS, read, decision,
   and candidate-count acceptance all pass.

## Acceptance

- The production-mode configuration and mTLS verifier have negative tests for
  forged headers, direct TCP access, wrong SAN/serial, stale policy, duplicate
  headers, and insecure policy files.
- Database decision tests cover confirm, ignore, correction, conflict resolution,
  replay, key reuse, stale revision, assertion binding, and transaction rollback.
- The isolated importer reports the authorized source counts without logging raw
  financial fields; a second run creates zero additional facts.
- The Web session exposes its runtime mode and never labels preview facts as Core
  data or Core facts as synthetic data.
- The original live gate turns green only when the deployed Web container reports
  `core-backed` and Core returns the imported pending candidates.

## Controlled evidence unlock checkpoint (2026-08-30)

- Core now has a default-disabled, request-bound `POST /internal/v1/evidence/unlocks` seam matching
  the Web BFF contract. It requires the `evidence:unlock` workload capability, exact entity/unit
  scope, a short-lived body-bound user assertion, and one-to-one operation/JTI replay identity.
- Password processing is isolated in a no-network, no-database `evidence-unlocker` sidecar over a
  private Unix domain socket. Core retains a read-only artifact mount; only the sidecar can write,
  and it publishes encrypted Evidence Object material after bounded ZIP validation.
- The public evidence projection distinguishes `NOT_REQUIRED`, `PASSWORD_REQUIRED`, and
  `UNLOCKED`. `UNLOCKED` means successful source processing only; it does not imply import,
  review, confirmation, or posting.
- Production remains disabled (`closed` U1 gate and an explicit compose profile). No source,
  password, credential, financial value, or production activation is stored in Git.
- Migration `20260830_0025` now follows the linear `0022 -> 0023 -> 0024` chain. It installs
  append-only reviewed-source, operation, receipt, and encrypted-output facts and makes the
  audit-horizon candidate projection authoritative. Runtime roles receive no direct fact-table
  grants; only the worker registration, API command, and reader projection entry points execute.
- The backup/restore gate observes and verifies the 0025 row counts, owners, function signatures,
  append-only triggers, ACLs, and effective privileges. Empty rollback is supported outside
  production; production rollback and any rollback after facts exist fail closed.

## OCR and managed-account preprocessing checkpoint (2026-08-29)

- The user expanded the authorized preprocessing scope to offline OCR and declared every
  personal/company account with supplied statement evidence a Managed Account.
- `bill_preprocessing.py` uses field-level confidence and deterministic layout parsers. It
  refuses to guess cropped periods, treats annual summaries as context-only, and binds each
  ready candidate to its single source image.
- The optional RapidOCR/ONNX Runtime versions are pinned. The one-shot OCR container has no
  network, environment secrets, database dependency, or writable application filesystem.
- Same-owner Managed Account movement is an Internal Transfer; cross-owner/company movement
  is a Related-Party Transfer. Both require equal opposite bilateral statement evidence.
- The existing controlled-bundle command accepts private OCR observations and bypasses its
  legacy fixed-cell photo candidates when they are supplied. Production candidates are not
  silently replaced until the superseding import gate is run and reviewed.

## May financial foundation checkpoint (2026-08-29)

- The authorized WeChat Pay and Alipay annual exports are now normalized together with the
  controlled May BOC rows by `scripts/build_financial_foundation_workbook.mjs`.
- `scripts/run_financial_foundation.ps1` is the single local build entry point. It writes a
  concise review workbook plus a non-sensitive validation manifest and fails closed on duplicate
  record ids, missing evidence references, or invalid internal-transfer links.
- A payment-method or same-holder counterparty account is recorded immediately, but becomes a
  Managed Account only after its own statement is supplied. Until then it is listed in
  `待补佐证` and cannot be automatically cleared.
- The real May rehearsal normalized 208 rows and registered 13 accounts. Five accounts still need
  independent statements. No automatic posting, candidate confirmation, or production Web
  replacement occurred at this checkpoint.

## Manual-review writeback handoff (2026-08-30)

- Core branch `ai/chatgpt/manual-review-writeback-core` owns migration
  `20260830_0022` with `down_revision=20260830_0021`; later 0023/0024 migrations
  are deliberately outside this slice.
- The browser/BFF contract uses stable `business_unit_ref` and `category_code`.
  Core resolves them through the entity-authorized active accounting-dimensions
  catalog and appends one audited CORRECT_AND_CONFIRM event for a PENDING
  candidate. It never reverse-maps display labels or accepts a browser-supplied
  entity/UUID identity.
- Every final business unit and category is revalidated active and same-entity,
  including amount/month-only corrections whose prior Candidate snapshot refers
  to a retired dimension. Historical snapshots remain readable but cannot be a
  new write target. Duplicate active labels make the whole catalog unavailable
  until the registry is governed.
- The catalog SECURITY DEFINER function grants EXECUTE only to
  `ledgerbridge_reader`; PUBLIC, API, worker, compatibility app, and backup roles
  have no execute path. The 0022 downgrade is limited to an empty isolated R1
  database—including all durable 0017–0021 import, evidence-link, hotel,
  counterparty, managed-account, and bank-statement facts—and is not a
  production rollback.
- Non-PostgreSQL red/green tests cover the stopped writeback, retired-current
  dimension, synthetic conflict correction parity, downgrade ordering, and ACL
  declarations. The full local suite is 778 passed / 200 skipped / one existing
  warning; the focused Core slice is 141 passed / 43 PostgreSQL skips. Ruff,
  mypy, sensitive-path, single-head, and diff checks pass. PostgreSQL tests cover
  the real command/event chain and role ACLs, but this local host lacks a
  disposable PostgreSQL runtime, so that gate must run in the PostgreSQL 15
  CI/service before merge. No production data was modified and no deployment is
  authorized by this handoff.

## MYbank reviewed one-shot runner checkpoint (2026-08-31)

- The runner accepts private values only through one strict operator-confirmed
  mode-`0600` JSON plan; its tracked example contains synthetic fields only.
- `--preflight-only` requires an isolated database target and executes the exact
  statement import, idempotent replay, overlapping-fact rejection, and full
  cutover-inventory acceptance inside one outer transaction before rollback.
- Production execution requires the unchanged plan, its bound private preflight
  receipt, the exact reviewed/deployed revision, a production database target,
  and the explicit `execute-reviewed-cutover-v1` switch. A failed acceptance
  rolls back database facts and aborts unpublished encrypted evidence.
- No owner, entity, business-unit, account, source, credential, private plan,
  deployment, or production write was added. The next gate is user confirmation
  of the exact owner/entity/business-unit/account mapping through the website or
  private plan, followed by the isolated preflight.
