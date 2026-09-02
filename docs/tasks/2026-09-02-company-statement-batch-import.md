# Task: Atomic company statement batch import

- Status: implementation verified locally; production execution pending
- Date: 2026-09-02
- Integration owner: Codex
- Branch: `ai/chatgpt/company-mybank-production-import`
- Production baseline: Core `87f88d81267434a90cb335751de97c2fe77d1f26`, schema `20260902_0033`

## Goal

Ingest all currently available non-empty official company statements without repeating archive
password discovery, without partial database commits, and without creating Accounting Candidates,
Journal Entries, or Postings. The restricted operator workflow resolves each archive directly to
its registered account credential, decrypts once into a private batch, and persists normalized
statement facts plus encrypted Evidence through Core.

## Frozen batch contract

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

## Production gate

1. Build and deploy one immutable Core release containing the batch operator capability; schema
   remains `20260902_0033`.
2. Create a fresh encrypted backup at that exact revision and pass isolated restore.
3. Rebind the private batch to the deployed revision and fresh backup proof without re-decrypting
   its source workbooks.
4. Finalize all 18 private plans inside the one-shot container.
5. Run the complete batch against the isolated restored database with `commit=false`; verify the
   database and encrypted artifact inventory return to the initial state.
6. Execute production once; verify 18 new statements/reviews/Evidence objects and 71 new
   transactions/observations, with all prohibited deltas remaining zero.
7. Execute the same production batch again and require every item to report `created=false` with
   zero database, audit, and artifact delta.
8. Create a post-import encrypted backup, pass isolated restore, verify service health/restart
   counts, and only then remove temporary plaintext workbooks from the exact private batch path.

## Rollback

Before the production transaction commits, any failure rolls back automatically. After commit,
restore uses the fresh revision-bound encrypted backup and its verified isolated-restore proof.
The original official encrypted archives remain retained outside the repository.
