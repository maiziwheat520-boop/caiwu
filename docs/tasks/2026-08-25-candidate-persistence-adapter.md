# Core Candidate persistence adapter (2026-08-25)

## Scope

`ledgerbridge.candidate_persistence.persist_initial_candidate` is the first
database-backed write seam after the synthetic intake boundary. It accepts a
validated `CandidateAggregate`, writes only revision 1, links already-stored
evidence objects, and appends the corresponding `candidate.create` audit event
and `candidate_event` CREATE receipt in the same caller-owned transaction.

## Safety boundary

- The SQLAlchemy `Session` is injected; the adapter does not create an engine,
  read a URL, commit, or select credentials.
- Evidence bytes are not accepted. `evidence_object` rows must already exist;
  raw content remains in the ArtifactStore/encryption pipeline.
- Source IDs are explicit registered `ingest_channel` and `source_system`
  values; free-text source identity is not derived from a message.
- Only initial Candidate state is supported. Review commands, supersession,
  JournalEntry, Posting, and automatic decisions are out of scope.
- The adapter is not wired into the synthetic gateway or any production route.
  Current migration grants keep runtime business writes disabled until a later
  reviewed write API, mTLS workload identity, and deployment gate are complete.

## Manual static verification

```text
uv run ruff format --check src/ledgerbridge/candidate_persistence.py
uv run ruff check src/ledgerbridge/candidate_persistence.py
uv run mypy src/ledgerbridge/candidate_persistence.py
python -m compileall -q src/ledgerbridge/candidate_persistence.py
```

No production database or credentials were used for this slice.

## Isolated PostgreSQL replay

On 2026-08-25 a disposable PostgreSQL 15 container on Hermes was used only for
verification. After creating test-only roles and applying Alembic through
`20260824_0015`, one synthetic entity/business unit/category/evidence fixture
was inserted and `persist_initial_candidate` committed successfully. The
observed counts were one candidate, one revision, one evidence link, one typed
CREATE event, and two audit events (evidence + candidate). The tunnel and
container were removed immediately afterward; the running LedgerBridge
database was not accessed.
