# Task: Real-data web review cutover

- Status: implementation
- Implementation owner: Codex
- Branch: `ai/chatgpt/real-data-cutover`
- Web branch: `ai/chatgpt/real-data-cutover`

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
