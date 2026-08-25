# Synthetic data entry/output gateway (2026-08-25)

## Purpose

Provide an immediately usable local contract while the production Core write
path remains gated. The loopback gateway accepts a bounded JSON message plus
base64 evidence and returns a stable candidate projection after Hermes
admission, deterministic synthetic triage, and candidate-intent validation.

## Contract

- `POST /v1/intake` accepts `message_id`, `source_event_ref`, `entity_ref`,
  `text`, and one to 32 evidence objects.
- Evidence is decoded, bounded to 1 MiB per item, and returned with its
  SHA-256 digest and byte size.
- Financial triage returns a generated `candidate_ref`; non-financial and
  ambiguous messages never create one.
- `GET /v1/candidates` returns candidate projections held by the current
  process.
- Every response carries `writes_posting: false`.

## Optional staging persistence

Set `LEDGERBRIDGE_SYNTHETIC_PERSISTENCE_PATH` to an absolute `.sqlite3` path
under the ignored `var/` directory to retain candidate projections across
gateway restarts. The SQLite store contains only the structured response,
source identity, timestamps, and evidence digests/metadata; it never stores
base64 evidence bytes, raw EML, OAuth tokens, or database credentials. The
default remains process-local memory.

## Explicit non-goals

This launcher does not write PostgreSQL, the ArtifactStore, AuditEvent,
JournalEntry, Posting, or real Hermes data. Restarting the process clears the
in-memory candidates. It is a contract/demo adapter for the next Core
persistence implementation, not a production ingestion endpoint.

## Manual verification

```text
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py --check
uv run ruff format --check scripts/r1_synthetic_data_gateway.py
uv run ruff check scripts/r1_synthetic_data_gateway.py
uv run mypy scripts/r1_synthetic_data_gateway.py
python -m compileall -q scripts/r1_synthetic_data_gateway.py
```
