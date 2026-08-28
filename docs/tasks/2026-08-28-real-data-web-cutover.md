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
