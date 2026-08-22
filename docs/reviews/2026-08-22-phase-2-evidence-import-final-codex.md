# LedgerBridge Phase 2 final Codex self-audit

- Date: 2026-08-22
- Reviewer: Codex
- Repository: maiziwheat520-boop/caiwu
- Base revision: 232378ef70f3cfa24324dc6add61ce6089d107b4
- Initial implementation: 1044d66c03960ee9a4c5e03d4024186123c46778
- Security hardening: 7ab9e52aa09ea4465db6061002c2859ff579788f
- Final reviewed executable commit: b092eb88772d30964524c7475ee96b0ccc86c395
- Verdict: **APPROVED FOR MERGE; USER DECISION REQUIRED**
- Open validated findings: **0 blocker, 0 high, 0 medium, 0 low**

This report reviews the complete executable diff from the merged Phase 2
preflight through b092eb8. It does not authorize production migration,
deployment, real connector traffic, or real financial evidence ingestion.

## Threat model and review coverage

Assets and invariants reviewed:

- immutable raw evidence bytes and metadata, with digest-derived identity;
- permanent SourceRecord provenance and versioned ImportJob outcomes;
- the append-only AuditEvent chain and exact JournalEntry post authorization;
- owner/migration authority versus the least-privileged runtime role;
- connector isolation from paths, database writes, artifact writes, and ledger posting;
- crash-safe publication, duplicate convergence, and fail-closed batch transactions.

Relevant attacker capabilities included a compromised runtime database client,
malicious or defective connector code, concurrent import attempts, crafted filenames
and record locators, symbolic-link manipulation inside an artifact tree, and SQL
objects created in schemas available to a runtime session.

The audit covered migration DDL, PL/pgSQL functions and triggers, grants and downgrade,
ORM mappings, artifact publication, connector SDK, orchestration and transaction
boundaries, ledger posting, configuration/Compose boundaries, and all new or changed
tests. No real parser, OAuth flow, external endpoint, binary financial fixture, or
production data path exists in this phase.

## Validated finding fixed

### CDX-H1 — Temporary-schema shadowing bypassed POSTED audit enforcement

- Severity: High
- Status: Fixed

The original journal_entry_validate_post_audit() trigger resolved
journal_entry and audit_event without schema qualification. PostgreSQL runtime
sessions retain permission to create temporary tables, and pg_temp precedes ordinary
schemas during name resolution. A compromised runtime client could create attacker-
controlled temporary relations with those names, then make the trigger validate the
temporary rows rather than the trusted ledger and audit tables. That provided a path to
set a JournalEntry to POSTED without a valid append-only journal.post event.

The migration now schema-qualifies both trusted relations with public and pins the
trigger function search_path to pg_catalog. A regression test constructs the
temporary-table attack and proves the transition fails while a correctly targeted,
fresh post event still succeeds.

## Additional defects and invariant gaps fixed

### CDX-M1 — Validated connector JSON remained shallowly mutable

Pydantic validation did not deep-copy nested mappings. A connector could mutate a
nested object after validation and before persistence, so stored provenance could differ
from the batch that core logic approved. The importer now performs a canonical JSON
round trip to detach and revalidate the complete nested value before publication.

### CDX-M2 — Mutable connector identity could split job and record provenance

Core logic previously read connector name/version at multiple times. A stateful or
hostile connector could return different identifiers so the ImportJob and SourceRecord
described different parser provenance. The orchestrator now snapshots immutable
connector identity once and uses that value throughout routing, job identity, records,
and audit evidence.

### Evidence metadata binding and durability hardening

- RawArtifact insertion now requires a fresh, semantically matching ingestion
  AuditEvent in the same transaction; a uniqueness race rolls back the orphan event.
- Database checks bind the lowercase digest-derived storage_key to the stored SHA-256,
  rather than trusting application construction alone.
- The artifact store rejects intermediate/final symlinks and destination mismatches,
  never overwrites a published path, fsyncs staged bytes, and now fsyncs every newly
  created parent directory plus the final hard-link directory.
- Compose explicitly forwards LEDGERBRIDGE_ARTIFACT_MAX_BYTES with a 50 MiB default to
  API and worker runtime configuration.

## Verification evidence

At security-hardening commit 7ab9e52aa09ea4465db6061002c2859ff579788f, an
isolated PostgreSQL 15 run on Hermes completed:

- 109 tests passed with 96.88% statement coverage against the unchanged 95% gate;
- migration head -> 20260821_0002 -> head passed with object checks;
- the pg_temp trigger-shadow attack regression passed;
- Ruff, formatting, strict mypy, Bandit, and sensitive-path checks passed;
- strict locked dependency audit reported no known vulnerabilities;
- the production image built with the expected revision label;
- PostgreSQL data checksums reported version 1.

The final executable commit b092eb88772d30964524c7475ee96b0ccc86c395
changes only directory fsync coverage and Compose size-limit wiring. On that SHA:

- all local gates passed; 57 environment-independent tests passed and 53 PostgreSQL or
  Windows-symlink cases were skipped locally, for 110 collected tests total;
- a Linux production image built on Hermes with the exact full-SHA revision label;
- a read-only, capability-dropped disposable container created a content-addressed blob,
  verified the storage key, and converged a duplicate publication;
- rendered Compose configuration contained the 52,428,800-byte default;
- every temporary image, container, archive, and directory was removed.

Production was not rebuilt, restarted, migrated, or written. Hermes remained on
ledgerbridge-app:0c5616f; API, worker, and PostgreSQL were healthy before and after
the isolated smoke.

GitHub push run 32551808678 and pull-request run 32551835286 both passed at
review head c3497b868d8564be33688aa9ac5d0b4764480843. Each completed secrets,
quality, and compose successfully (6/6 jobs total), including full-history
Gitleaks and the complete PostgreSQL-backed 110-test suite.

## Security scan record

A standard security-diff review was run for the immutable initial implementation SHA.
The native Codex Security launcher failed before producing a scan ID because its Windows
process decoded the Chinese workspace path using GBK. The prescribed terminal workflow
was therefore used: a threat model, independent database/import/storage discovery passes,
candidate reconciliation, and canonical manifest/findings/coverage validation completed.
The old SHA retains the High finding above as an immutable historical result; remediation
was verified separately at the fixed SHA. No workbench or TAC-backed claim is made.

## Residual scope and follow-up

These are not open vulnerabilities in the Phase 2 framework, but remain gates for later
work:

- SourceRecord/entity semantics need an explicit design decision before records can
  influence entity-scoped ledger workflows.
- The 50 MiB limit is per artifact; an aggregate tenant/storage quota is required before
  unattended production ingestion.
- Real connector formats, archive expansion limits, OAuth/token handling, classification,
  deduplication heuristics, and automatic posting remain out of scope.
- No real evidence may be ingested and no production migration may run without a fresh
  backup/restore rehearsal plus separate user authorization.

## Claude fixed-SHA audit hook

To preserve Claude quota, no Claude run is required for this PR. If an independent audit
is requested later, Claude should use its separate clone and review only this immutable
range:

    Repository: G:\我的云端硬盘\AI\LedgerBridge-Claude
    Base: 232378ef70f3cfa24324dc6add61ce6089d107b4
    Target: b092eb88772d30964524c7475ee96b0ccc86c395
    Mode: read-only; do not modify the target branch or production
    Scope: git diff Base..Target plus this report's closed findings and residual gates
    Output: a new immutable docs/reviews/*-phase-2-*-claude.md report only after explicit authorization

The protected PR may be merged only after its latest head passes secrets, quality,
and compose, and after the user explicitly authorizes merge. Merge does not authorize
Hermes deployment.
