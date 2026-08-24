# LedgerBridge R1 Migration C Security Remediation

Date: 2026-08-24

## Scope

This report responds to the independent Codex Security review in
`2026-08-24-r1-migration-c-security-codex.md` (1 HIGH, 2 MEDIUM). Work was
performed on the local R1 branch with a disposable Hermes PostgreSQL 15
container only. No production database, real ledger rows, or credentials were
used.

## Remediation status

| Finding | Result | Evidence |
| --- | --- | --- |
| Reader projection views bypass entity/as-of authorization | Fixed fail-closed | Migration C revokes reader SELECT on all eight projection views; the reader retains only scoped SECURITY DEFINER function execution. |
| POSTED attribution enforcement is opt-in for legacy entries | Fixed fail-closed | R1 hardening no longer returns early for zero-attribution POSTED entries; the legacy upgrade regression requires the incomplete entry to be rejected. |
| Backup role privilege drift is not fail-closed | Fixed | Migration C includes `ledgerbridge_backup` in role, membership, ownership, ACL, default-ACL, and internal-read privilege checks. The role remains optional and receives CONNECT only when present. |

## Additional correctness fix

The candidate contract literal `ledgerbridge.candidate.v1` is 25 bytes while
Migration 0012 declared `VARCHAR(24)`. The fresh schema now uses 32 characters;
the 0014 compatibility alteration remains for already-deployed 24-character
schemas. This prevents valid candidate inserts from being rejected by the
schema width before the R1 closure checks run.

## Verification

Passed locally on Windows:

- `uv lock --offline`
- `uv run mypy` (40 source files)
- Ruff format/check, Python compileall, and `git diff --check`
- Full local suite: **475 passed, 189 skipped, 1 warning**

Passed individually against the disposable Hermes PostgreSQL 15 replay:

- reader/database ACL minimality
- internal-read objects fixed-owner SECURITY DEFINER and direct-view fail-closed behavior
- privileged backup role rejection
- clean backup role receives CONNECT only
- legacy POSTED entry without complete attribution is rejected

The complete Hermes R1 migration file was replayed against a disposable
PostgreSQL 15 container after the legacy fixtures were made self-consistent:
**48 passed**. The replay covers candidate history and audit atomicity,
POSTED attribution and primary-leg invariants, reconciliation scope, blob
lineage, role/default-ACL drift, downgrade guards, reader horizon/as-of
queries, and evidence-read audit receipts. The fixture harness now separates
database-admin bootstrap from the migration owner, uses fresh head databases
for tests that require an empty reader surface, and keeps the migration's
public-first search path from resolving later unqualified DDL into
`pg_catalog`.

## Deployment boundary

No production migration, role change, merge, or deployment was performed. The
Hermes container and SSH tunnel used for replay are disposable and must be
removed after this report is finalized.
