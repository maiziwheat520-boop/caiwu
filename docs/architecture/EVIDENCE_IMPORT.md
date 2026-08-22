# Phase 2 Evidence and Import operations

Status: implementation contract for Phase 2
Date: 2026-08-21

## Evidence identity and retention

The core computes SHA-256 while streaming every upload into a private staging
file. The caller never supplies the digest or storage path. Publication uses a
same-filesystem hard-link create operation, which is atomic and refuses to
overwrite an existing destination. The final key is always
`sha256/<2 hex>/<2 hex>/<64 hex>` and never contains the display filename.

An existing blob is accepted only after its size and digest are recomputed.
Symbolic links, non-regular destinations, size mismatches, digest mismatches,
oversized streams, interrupted reads, and failed synchronization all fail
closed. A database failure after publication may leave a verified unreferenced
blob. That blob is retained for a future explicit orphan scan; it is not deleted
automatically because a concurrent transaction may be adopting the same digest.

`RawArtifact` metadata and `SourceRecord` rows are permanent and immutable. The
v0.1 retention policy keeps bytes forever. A future audited retention policy may
remove bytes, but it must not delete either database row or rewrite provenance.

## Connector boundary

Connectors receive only immutable metadata, a bounded prefix for detection, and
a read-only stream with no host path, file descriptor, or write method. Their
interface is limited to:

- `detect(metadata, bounded_prefix)` returning `MATCH`, `NO_MATCH`, or `AMBIGUOUS`;
- `parse(read_only_stream)` yielding typed `ParsedSourceRecord` values.

The core validates connector identity, parser provenance, JSON shape, record
limits, unique locators, and normalized money after values are returned. Money
uses signed integer minor units and `CNY`; normalized floats are rejected. A
connector cannot choose the database transaction, artifact destination, audit
chain, or ledger posting behavior.

Exactly one deterministic match proceeds. Zero matches, multiple matches, or an
explicit ambiguous result create an observable router job in `NEEDS_REVIEW`.
Detection exceptions and connector contract violations become bounded error
codes and generic summaries; raw rows, display filenames, and exception messages
are not copied to logs or job diagnostics.

## Job lifecycle and idempotency

The database enforces these transitions:

```text
PENDING -> RUNNING -> SUCCEEDED | FAILED | NEEDS_REVIEW
       \------------> FAILED | NEEDS_REVIEW
```

Terminal jobs are immutable. One effective attempt exists for each
`(artifact_id, connector_name, connector_version)`. A repeated import of the
same bytes and connector version returns the existing terminal outcome. A new
connector version receives a distinct job but cannot overwrite an existing
`(artifact_id, record_locator)` SourceRecord. Provenance disagreement becomes
`NEEDS_REVIEW` and publishes no partial batch.

The core validates the complete batch before starting its publication
transaction. SourceRecords and the terminal success state commit together under
a row lock on the job. Unique locator races converge as duplicates; other
identity conflicts roll back the entire batch. Phase 2 never creates a
JournalEntry from imported evidence.

## Database permissions

`ledgerbridge_app` receives SELECT/INSERT on `raw_artifact` and `source_record`,
SELECT/INSERT plus updates to the explicit lifecycle columns on `import_job`, and
no direct AuditEvent write privilege. Database triggers reject RawArtifact or
SourceRecord UPDATE/DELETE even from the migration owner and reject illegal job
transitions. The SourceRecord and external transaction identities use unique
constraints, and every evidence relationship uses `ON DELETE RESTRICT`.

The API keeps the artifact volume read-only. The worker is the only service with
a read-write artifact mount. Both continue to use a read-only root filesystem,
dropped capabilities, no-new-privileges, the unprivileged UID, and the
least-privileged database login. Phase 2 adds no business endpoint and performs
no production ingestion.

## POSTED audit binding

`journal_entry.posted_audit_event_id` is nullable for DRAFT/manual lifecycle
work and mandatory exactly when status is POSTED. A valid DRAFT-to-POSTED
transition references a newly appended event in the same PostgreSQL transaction.
The event must:

- be distinct from every creation event;
- have action `journal.post`;
- target the exact JournalEntry UUID in `payload.journal_entry_id`;
- be used by only one JournalEntry.

The database checks the event transaction ID in addition to its action and
target. A stale pre-created event cannot be attached later. The application
helper appends the event and updates the entry without committing; the caller's
commit evaluates the existing deferred balance, completeness, and entity
constraints. Any failure rolls back both the status transition and audit append.
The audit chain therefore reconstructs the entry, actor, time, reason, and target
for every POSTED transition.

## Recovery and deployment gate

The database backup already includes all three Phase 2 tables, audit events, and
bindings after migration. Artifact backup includes both referenced evidence and
any retained verified orphan. Restore validation must check migration
`20260821_0003`, grants, functions, triggers, blob hashes, SourceRecord counts,
and the worker/API mount split.

No production migration is implied by merging Phase 2. Before an authorized
deployment, create a fresh encrypted backup and pass an isolated restore
rehearsal. After deployment, create another encrypted backup and repeat the
restore verification. Real financial evidence and real connectors remain out of
scope until separately approved.
