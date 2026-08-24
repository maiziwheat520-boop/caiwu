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
- Migration 0015 makes that function a trusted-writer endpoint: only
  `ledgerbridge_api` receives `internal_read` schema `USAGE` and the exact
  receipt-function `EXECUTE` grant. `ledgerbridge_reader` has no receipt
  `EXECUTE`, and the API role has no direct receipt-table or fact-table write
  grant. The function remains `SECURITY DEFINER` with a fixed
  `search_path`, so principal, SAN, and policy-generation parameters are
  writer assertions rather than claims a database reader can manufacture.
- The receipt sink's session factory must therefore use the authenticated API
  writer connection. The database ACL is the enforcement boundary; comments,
  test doubles, and caller-supplied identity fields are not authorization.
- `DatabaseInternalReadService` appends the receipt after successful decryption
  and digest verification, before returning content. Sink exceptions fail closed
  as `InternalReadBackendUnavailable`.

## Verification

```text
uv run --frozen --extra dev pytest -q tests/test_r1_internal_read_audit.py tests/test_r1_internal_read_database_service.py
48 passed
uv run --frozen --extra dev pytest -q tests/test_r1_internal_read_audit.py tests/test_r1_internal_read_database_service.py tests/test_r1_database_migration.py
58 passed, 41 skipped (PostgreSQL integration URL not configured)
ruff format --check: 5 files already formatted
ruff check: All checks passed
mypy: Success: no issues found in 5 source files
git diff --check
All static checks passed.
```

The sink is not enabled by default or in production. A test-only database
route may opt in with `enable_internal_read_persistent_receipt`; the API writer
sink is then injected while the reader role remains read-only. A future
production enablement still requires a real KeyProvider, reader bootstrap,
mTLS verifier, PostgreSQL 15 replay, and an independent security review.
