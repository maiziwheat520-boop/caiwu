# R1 durable evidence-read receipt boundary (2026-08-25)

## Goal

Make the database reader's evidence-read acknowledgement explicit and durable
when a deployment injects a reviewed sink, while keeping the default synthetic
and production compositions unchanged.

## Implementation

- `EvidenceReadReceipt` is an immutable, closed Pydantic payload containing the
  operation id, verified principal policy generation, evidence/entity/business
  unit/blob bindings, byte size, and plaintext SHA-256. The envelope's external
  key generation remains independently bound by the decryptor descriptor and is
  not misrepresented as the authorization policy generation.
- `DatabaseInternalReadReceiptSink` invokes the fixed Migration 0015 function
  `internal_read.append_internal_evidence_read_audit` with positional arguments,
  converts the digest to `bytea`, and commits only after a returned audit id.
- `DatabaseInternalReadService` appends the receipt after successful decryption
  and digest verification, before returning content. Sink exceptions fail closed
  as `InternalReadBackendUnavailable`.

## Verification

```text
uv run --frozen --extra dev pytest -q tests/test_r1_internal_read_audit.py tests/test_r1_internal_read_database_service.py
48 passed
uv run --frozen --extra dev mypy
Success: no issues found in 41 source files
```

The sink is not wired into the default HTTP route or production settings. A
future production enablement still requires a real KeyProvider, reader
bootstrap, mTLS verifier, PostgreSQL 15 replay, and an independent security
review.
