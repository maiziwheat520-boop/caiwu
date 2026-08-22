# Task: Phase 2 Evidence and Import

- Status: implementation, fixed-SHA self-audit, and protected CI complete; merge decision pending
- Preflight date: 2026-08-21
- Implementation owner: Codex
- Review owner: Codex self-audit; Claude fixed-SHA audit hook preserved
- Base commit: `f892f8a2e62759bbb44f85a561d386cb22ad79fa`
- Implementation base: preflight merge `232378ef70f3cfa24324dc6add61ce6089d107b4`
- Preflight branch: `ai/chatgpt/phase-2-prep`
- Implementation branch: `ai/chatgpt/phase-2-evidence-import`
- Planned migration: `20260821_0003`

## Goal

Build the evidence-preservation and import framework required before real
financial exports or OAuth attachments enter LedgerBridge. Phase 2 creates the
durable evidence identity, permanent source records, observable import jobs,
and a narrow connector SDK while keeping all uncertain or ambiguous results out
of the ledger.

Phase 2 does not implement real Alipay, WeChat, Bank of China, CSV, XLSX, ZIP,
or EML parsers. It uses synthetic connectors and fixtures only. It must not
create POSTED entries, classify spending, guess accounts, or ingest production
financial data during development or deployment validation.

## Preflight result

| Gate | Result |
| --- | --- |
| Frozen baseline and deferred Phase 1 requirements recovered | Passed |
| F-4 encrypted backup and isolated restore rehearsal | Passed at deployed merge SHA `0c5616f648d720da88dd37deac94610486e7e611` |
| Production schema/data starting point | Alembic `20260821_0002`; Phase 1 rows/source placeholders/artifact files are zero; Phase 2 tables are absent |
| Git source of truth | Public `maiziwheat520-boop/caiwu`; `main` at `f892f8a2e62759bbb44f85a561d386cb22ad79fa` |
| F-6 branch protection | Passed: PR required, strict `secrets`/`quality`/`compose`, admins enforced, force-push/delete disabled |
| Write ownership | Codex owns only `LedgerBridge-Codex` on `ai/chatgpt/phase-2-prep`; Claude remains review-only |
| Real evidence/credentials in repository or database | None |
| Phase 1 placeholder | `journal_entry.source_record_id` exists as nullable UUID without FK and must be bound in Phase 2 |
| M8 POSTED-transition audit decision | User confirmed inclusion in Phase 2 |

Required approvals are intentionally zero because this is currently a
single-human repository. Branch protection still requires a pull request, all
three current status checks against latest `main`, and resolved conversations;
the administrator cannot bypass the rule.

## Frozen evidence semantics

- `RawArtifact` is immutable evidence metadata plus a content-addressed blob.
- SHA-256 is the artifact identity and is computed by the core while streaming;
  caller-supplied digests are never trusted.
- The original filename is display metadata only and is never used as a storage path.
- A later retention policy may remove blob bytes, but it must retain the
  `RawArtifact` metadata row and every `SourceRecord`.
- `SourceRecord` is permanent. Its identity is exactly
  `(artifact_id, record_locator)`.
- External transaction identity is unique by
  `(account_id, source, external_transaction_id)` when all nullable components
  required by that identity are present.
- A fingerprint remains heuristic evidence. It cannot delete, merge, or silently
  suppress a source record.
- Connectors implement only `detect()` and `parse()`. They cannot write the
  database, artifact store, audit chain, classification rules, or postings.
- Ambiguity produces `NEEDS_REVIEW`; it never selects the first connector or
  guesses an account.

## Database contract

### `raw_artifact`

The Phase 2 migration introduces a model with at least:

- UUID primary key;
- 32-byte SHA-256 value with a database length check and unique constraint;
- non-blank source, original filename, and media type;
- non-negative byte size;
- UTC received timestamp;
- unique, content-derived relative storage key;
- authorizing `AuditEvent` reference for evidence ingestion.

The storage key uses a fixed content-addressed form derived only from the
verified digest, for example `sha256/ab/cd/<64-lowercase-hex>`. Absolute paths,
`..`, separators from user filenames, and symlink traversal are invalid.

RawArtifact metadata is immutable in v0.1. Runtime receives SELECT/INSERT only;
database triggers also reject UPDATE/DELETE by an owner. Future retention removes
only blob bytes through an explicit audited policy and does not delete this row.

### `import_job`

The migration introduces a permanent job record with:

- UUID primary key and non-null RawArtifact FK using `ON DELETE RESTRICT`;
- connector name and version;
- status enum: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `NEEDS_REVIEW`;
- created, started, and completed timestamps with state-consistent checks;
- non-negative parsed/created/duplicate counters;
- a bounded machine error code and sanitized diagnostic summary, never raw fields;
- uniqueness for one effective attempt per
  `(artifact_id, connector_name, connector_version)`.

Database checks/trigger logic reject illegal state transitions and success with
an error code. Retrying the same artifact with the same connector version is
idempotent; a new parser version is a distinct job.

### `source_record`

The migration introduces a permanent record with:

- UUID primary key;
- non-null RawArtifact and ImportJob FKs using `ON DELETE RESTRICT`;
- non-blank `record_locator`, `source`, and parser version;
- JSONB `raw_fields` and `normalized_fields` objects;
- optional Account FK using `ON DELETE RESTRICT`;
- optional external transaction ID;
- UTC creation timestamp;
- unique `(artifact_id, record_locator)` identity;
- partial unique external identity over
  `(account_id, source, external_transaction_id)` when account and external ID
  are present.

SourceRecord rows are not deleted. Identity and raw fields are immutable. Phase 2
does not implement parser reruns against real connectors; a later parser task
must introduce audited/versioned parse results rather than overwrite provenance.

### Existing ledger bindings

- Add a real nullable FK from `journal_entry.source_record_id` to
  `source_record.id` using `ON DELETE RESTRICT`.
- Do not make the column non-null: manual and correction entries may have no
  source record.
- Migration must fail closed if an existing non-null placeholder is orphaned;
  it may not create a synthetic SourceRecord to make the FK pass.

### M8: POSTED transition audit binding

The user confirmed that Phase 2 closes the deferred M8 audit gap. Add a unique,
nullable `posted_audit_event_id` FK (or an exactly equivalent explicit binding)
and database enforcement with these semantics:

1. DRAFT creation retains its existing unique creation/authorization AuditEvent.
2. A DRAFT-to-POSTED transition must atomically reference a different append-only
   AuditEvent whose action is `journal.post` and whose payload targets that exact
   JournalEntry ID.
3. The transition and audit append are one transaction; if balance, entity,
   completeness, or audit validation fails, neither is committed.
4. Directly setting POSTED without the binding, reusing another entry's event,
   using the creation event, or targeting the wrong ID fails at the database layer.
5. No importer in Phase 2 automatically performs this transition.

The audit chain alone must be sufficient to reconstruct which entry was posted,
by which actor, at what time, and for what reason.

## Artifact-store contract

- Only the worker-side evidence service writes the artifact volume. API remains
  read-only. The worker mount becomes read-write only when the implementation
  actually invokes this service; all other container hardening remains.
- Ingestion accepts a bounded read-only binary stream plus metadata. A connector
  never receives an arbitrary host path.
- Bytes stream into a randomly named private staging file below the configured
  artifact root while SHA-256 and size are calculated.
- Publication uses fsync plus same-filesystem atomic rename into the
  content-addressed destination. Published blobs are non-executable and
  read-only to normal application code.
- An existing destination is accepted only after digest and size verification.
  Mismatch fails closed and never overwrites either file.
- Database rows may never reference a missing or unverified blob. If a crash
  leaves an unreferenced content-addressed blob after publication but before
  database commit, retain it for an explicit orphan scan; do not blindly delete
  a path that a concurrent importer may have adopted.
- Phase 2 stores ZIP/EML/XLSX/CSV bytes but never expands or parses their real
  contents. Archive expansion limits belong to the real connector phase.

## Connector SDK contract

Define typed, side-effect-free interfaces equivalent to:

- `detect(metadata, bounded_prefix) -> MATCH | NO_MATCH | AMBIGUOUS`;
- `parse(read_only_stream) -> iterable[ParsedSourceRecord]`.

The core owns streaming, hashing, storage publication, transaction boundaries,
idempotency, database writes, job state, audit events, and log redaction.
Connectors return typed values only. Normalized monetary values, when present,
use signed integer minor units and `CNY`; floats are rejected. Connector and
parser version identifiers are mandatory and enter provenance.

Exactly one deterministic `MATCH` may proceed. Zero, multiple, or explicit
ambiguous matches produce `NEEDS_REVIEW` and zero SourceRecords. Detection and
parse failures use bounded error codes; logs must not include raw rows, statement
contents, filenames containing secrets, database URLs, or OAuth tokens.

## Import orchestration and idempotency

1. Persist/locate the verified RawArtifact by digest.
2. Resolve exactly one connector without guessing.
3. Create or resume the versioned ImportJob.
4. Parse into typed values without database side effects.
5. Validate the complete batch before any SourceRecord publication.
6. Insert SourceRecords in one transaction, enforcing source and external identities.
7. Mark the job terminal and append non-secret audit evidence with counts/digests.

Re-importing identical evidence with the same connector version returns the
existing artifact/job outcome and creates zero RawArtifacts, SourceRecords, or
JournalEntries. A uniqueness race is handled as idempotency, not as an internal
error. Conflicting external identity or record locator goes to review/failure
with no partial batch.

## In scope

- One reversible Phase 2 migration and model registry updates.
- RawArtifact, ImportJob, and SourceRecord models and database invariants.
- The existing JournalEntry SourceRecord FK.
- M8 POSTED-transition audit binding and behavior-sensitive tests.
- A content-addressed evidence store with atomic publication and fail-closed paths.
- Connector SDK, import orchestration, and synthetic connectors/fixtures.
- Runtime grants and worker/API artifact-volume boundary required by the framework.
- Documentation for provenance, retention, idempotency, and recovery behavior.

## Out of scope

- Real or customer-derived financial files, even if redacted informally.
- Real Alipay, WeChat, Bank of China, CSV, XLSX, ZIP, or EML parsing.
- Outlook OAuth, Graph, mailbox collection, refresh-token storage, or network calls.
- Deduplication beyond exact artifact/source/external identity gates.
- ReconciliationGroup, Suspense cleanup, classification rules, EntryTag, ReviewItem,
  period close, credit-card/loan/refund semantics, or Hermes business endpoints.
- Creating POSTED entries from imported evidence.
- UI, dashboard, LLM calls, or connector-specific classification logic.
- Production migration/deployment without a later explicit authorization.

## Acceptance tests

### Migration and permissions

- Upgrade creates all three tables, enums, indexes, FKs, immutability/state
  triggers, SourceRecord FK, and M8 binding; downgrade removes only Phase 2
  objects/bindings; upgrade after downgrade recreates them.
- Object absence/presence is asserted in an isolated database, not inferred from
  Alembic version alone.
- `ledgerbridge_app` can perform the intended insert/select/job-transition path
  but cannot delete evidence/source rows, mutate raw evidence, alter triggers,
  or directly insert/update/delete AuditEvent.
- RawArtifact delete with dependent SourceRecords is rejected and SourceRecords
  remain byte-for-byte unchanged.

### Evidence storage

- Arbitrary chunk boundaries produce the same SHA-256 and storage key.
- Duplicate bytes under different filenames publish one blob and one RawArtifact.
- Concurrent identical ingestion converges on one verified blob/row/job.
- User filenames containing separators, `..`, absolute paths, Unicode, device
  names, or shell metacharacters never influence the destination path.
- Symlink and destination-mismatch attempts fail without overwriting evidence.
- Simulated read, fsync, rename, database, and interruption failures leave no
  database reference to missing/unverified bytes and no reusable partial file.

### Source and import identity

- Duplicate `(artifact_id, record_locator)` inserts create zero new records.
- The partial external identity rejects a duplicate only when the relevant
  account/source/external ID is present; null external IDs remain allowed.
- A failed or ambiguous batch publishes zero SourceRecords and zero JournalEntries.
- Re-importing identical evidence with the same connector version adds zero
  records and zero ledger transactions.
- Different parser versions produce distinct ImportJobs without overwriting
  existing provenance.

### Connector boundary

- Synthetic connectors prove exactly-one-match routing and no-match/multi-match/
  ambiguous `NEEDS_REVIEW` behavior.
- A malicious synthetic connector cannot receive a host path, write the artifact
  store/database, or smuggle float money into normalized output.
- Parse exceptions produce a sanitized bounded error and no raw financial value in logs.
- Hypothesis covers content chunking, record locators, filenames, and idempotent
  batch retry with synthetic data only.

### Audit binding

- DRAFT-to-POSTED without `journal.post` evidence fails.
- Wrong target ID, wrong action, reused event, or creation event reuse fails.
- A valid transition commits the post event and status atomically; forced balance
  failure rolls both back.
- The append-only chain can reconstruct actor/time/reason/JournalEntry for every
  POSTED transition created in tests.

### Quality and deployment gates

- No production evidence, secrets, OAuth material, or binary statement fixture is tracked.
- Ruff, format, strict mypy, Bandit, sensitive-path scan, full-history Gitleaks,
  strict dependency audit, migration round-trip, and Compose build pass.
- Coverage includes evidence storage, import orchestration, connector SDK, models,
  migration helpers, and worker changes; the CI threshold is not reduced and no
  new omit entry is allowed.
- Before any production migration, create a fresh encrypted backup and pass an
  isolated restore rehearsal; production deployment requires separate user authorization.
- After an authorized deployment, verify migration head, grants, API/worker/
  PostgreSQL health, worker write/API read-only mounts, manifest/image revision,
  row counts, and a new encrypted backup plus isolated restore.

## Implementation evidence

- Migration `20260821_0003` creates RawArtifact, ImportJob, and SourceRecord,
  binds the deferred SourceRecord FK, adds POSTED AuditEvent evidence, and has a
  real `head -> 0002 -> head` object-presence round trip.
- The content-addressed store streams and verifies SHA-256, publishes without
  overwrite, rejects intermediate/final symlinks, and retains verified orphan
  blobs when the database transaction fails.
- The Connector SDK receives no path or write API. Core routing, output
  revalidation, record limits, transaction ownership, idempotency, bounded
  errors, and audit events are behavior-tested with synthetic values only.
- PostgreSQL 15 isolated acceptance completed with 109 tests and 96.88%
  coverage at hardened commit `7ab9e52aa09ea4465db6061002c2859ff579788f`.
  Existing Phase 1 behavior, migration failure on orphan placeholders and
  pre-existing POSTED entries, permissions, state transitions, audit
  reconstruction, and atomic rollback all passed. The final executable commit
  `b092eb88772d30964524c7475ee96b0ccc86c395` adds only parent-directory fsync and
  Compose limit wiring; its new regression test and Linux production-image smoke
  passed, and protected CI will run all 110 collected tests.
- Ruff, format, strict mypy, sensitive-path scan, Bandit, strict dependency
  audit (zero known vulnerabilities), Compose config/build, image revision label,
  and worker-writable/API-read-only artifact mount tests passed. The 95% coverage
  threshold and existing omit list were not changed.
- No real parser, binary financial fixture, OAuth material, JournalEntry import,
  production migration, or Hermes deployment was added. Hermes remains on
  `0c5616f648d720da88dd37deac94610486e7e611`.
- Full-history Gitleaks and protected CI passed at review head
  `c3497b868d8564be33688aa9ac5d0b4764480843`: push run `32551808678` and
  pull-request run `32551835286` completed `secrets`, `quality`, and `compose`
  successfully (6/6 jobs total).
- Alembic autogenerate comparison reports no Phase 2 object drift. It still
  reports inherited Phase 1 check-constraint naming and audit-index metadata
  drift; Phase 2 does not rewrite that deployed historical schema.
## Review and handoff gate

- Preflight/task-card work may merge before implementation only after its own CI.
- Implementation starts from the merged preflight SHA on
  `ai/chatgpt/phase-2-evidence-import`.
- Codex develops independently to conserve Claude quota and records a complete
  fixed-SHA self-audit. The separate Claude clone and report-only branch remain
  available for a later immutable audit; Claude does not share write ownership.
- Any change to the frozen evidence identity, deletion semantics, connector
  boundary, POSTED audit binding, or real-data scope requires a new user-approved
  append-only decision rather than a silent task-card edit.

## Implementation verdict

FIXED-SHA SELF-AUDIT AND PROTECTED CI COMPLETE; APPROVED FOR MERGE. The final
reviewed executable SHA is `b092eb88772d30964524c7475ee96b0ccc86c395`, with
no open validated security finding. Merge requires an explicit user decision;
migration and production deployment remain separately authorized.
