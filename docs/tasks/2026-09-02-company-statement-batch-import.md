# Task: Atomic company statement batch import

- Status: five complete range statements imported and verified in production
- Date: 2026-09-02
- Integration owner: Codex
- Branch: `ai/chatgpt/company-mybank-production-import`
- Production release: Core `5d03d93fe43670ec4136754050eab06e4dab2b0c`, schema `20260902_0034`

## Goal

Ingest all currently available non-empty official company statements without repeating archive
password discovery, without partial database commits, and without creating Accounting Candidates,
Journal Entries, or Postings. The restricted operator workflow resolves each archive directly to
its registered account credential, decrypts once into a private batch, and persists normalized
statement facts plus encrypted Evidence through Core.

## Superseding full-range batch contract

The operator later supplied one complete official range statement for each of the five managed
company accounts. These five files supersede the incomplete daily-source batch as the production
input. The daily sources remain retained privately and are not imported again.

- The range batch contains five statements and 1,442 source rows.
- Four accounts have no existing statement facts. One account has nine exact facts from the
  representative daily slice; the range statement reuses those facts and adds new observations.
- The accepted production delta is five statements/reviews/Evidence objects, 1,433 transaction
  facts, and 1,442 observations. Candidates, Journal Entries, and Postings remain unchanged.
- `mybank_company_range_xlsx_v3` is a separate parser profile for the three observed official
  9/11-column range-export variants. The strict single-day v2 parser is unchanged.
- A v2 private plan binds the operator-reviewed expected-new-transaction count. PostgreSQL still
  rejects any non-exact overlap; the count gate does not weaken fact comparison.
- Range authorization accepts only gap-free, adjacent effective-date assignments for the same
  business unit. A gap or unit change still fails closed; historical coverage is appended rather
  than rewriting the current assignment.

## Retained daily-batch contract

- The batch contains 18 non-empty statements and 71 transactions.
- Twenty-one valid empty statements are retained in the private manifest as explicit skips.
- One previously imported statement is retained as an explicit idempotent skip.
- The manifest binds every source digest, size, account suffix, date, transaction count, Evidence
  ref, finalized plan digest, and order.
- All plans share one target revision, backup directory, restore report, evidence key, and encrypted
  artifact root.
- One database transaction covers the complete ordered batch.
- Each item performs its own exact replay and overlapping-fact conflict probe.
- Candidate, latest-pending Candidate, account registry, Journal Entry, and Posting counts remain
  unchanged.
- Any item failure, conflict-probe failure, or final acceptance failure rolls back the database and
  aborts all staged encrypted publications.
- Re-executing a fully completed batch is an exact zero-delta replay.

## Local verification

- Batch-focused tests: 13 passed.
- Relevant cutover and plan tests: 59 passed, 5 environment skips.
- Complete Windows suite: 1,294 passed, 212 environment/platform skips.
- Ruff format/check, strict changed-source MyPy, and `git diff --check` passed.
- Private statement files, credentials, identifiers, plans, manifests, and receipts remain ignored
  and outside Git.

## Production result

- Immutable release `5d03d93fe43670ec4136754050eab06e4dab2b0c` and schema
  `20260902_0034` were deployed and verified before data mutation.
- One five-file `commit=false` preflight passed against the pre-import encrypted backup and isolated
  restore proof.
- Production used one guarded transaction per statement, with an immediate exact completed replay
  and a fresh encrypted backup/isolated restore proof before the next item. This avoided weakening
  the append-only evidence/audit constraints after the multi-item transaction path failed closed.
- All five production receipts report `created=true`; all five immediate replays report
  `created=false`, exact zero count delta, and rejected fact-conflict probes.
- The accepted delta is five statements, five reviews, five encrypted Evidence objects, 1,433 new
  transaction facts, and 1,442 observations. The nine existing facts for suffix `3678` were reused
  and received new observations; no prior fact or audit row was deleted.
- Final production totals are 11 statements, 2,447 transaction facts, 2,456 observations, 30
  encrypted Evidence objects/blobs, and 11 reviews. Candidate is unchanged at 259 with zero latest
  pending; Journal Entry and Posting remain zero.
- Final encrypted backup `20260902T093041Z-5d03d93fe436` passed isolated restore with report
  `restore-rehearsal-20260902T093113Z.json`. API, worker, internal reader, and PostgreSQL are healthy
  with zero restart counts.
- Private preflight and five production receipts are retained mode `0600` outside Git. The official
  user-supplied files were not modified.

The remaining engineering follow-up is a small refactor of the optional multi-item outer
transaction runner so its deferred audit/evidence checks share one transaction scope. It is not a
data-repair task and does not affect the five completed imports.

## Rollback

Before the production transaction commits, any failure rolls back automatically. After commit,
restore uses the fresh revision-bound encrypted backup and its verified isolated-restore proof.
The original official encrypted archives remain retained outside the repository.
