# Task: Payroll publication read adapter

- Status: review
- Implementation owner: Codex
- Review owners: Copernicus and Franklin
- Branch: `ai/chatgpt/real-data-cutover`
- Owned files: `src/ledgerbridge/payroll_integration.py`,
  `src/ledgerbridge/internal_payroll_routes.py`, `src/ledgerbridge/config.py`,
  `src/ledgerbridge/internal_read_contract.py`, `src/ledgerbridge/main.py`, and the matching
  payroll integration tests

## Goal

Let LedgerBridge consume one versioned, read-only PayrollVerification publication through its
existing authenticated company scope without sharing databases, pages, credentials, or payment
capabilities.

## In scope

- A configuration-injected provider base URL and bounded request timeout.
- Existing `WorkloadPrincipal` to single-`EntityGrant` authorization and an explicit
  `entity_ref` to `company_id` mapping.
- Read-only `payroll-ledgerbridge-publication/v1` retrieval with deterministic replay handling.
- Fail-closed validation for identity scope, integer minor-unit money, verification and material
  v1 projections, audit-chain/approval proof, payload integrity, and no-payment flags.
- A disabled-by-default internal route and contract tests for unavailable providers, unknown
  versions, cross-company data, idempotency conflicts, and forbidden payment semantics.

## Out of scope

- Reading the PayrollVerification database or copying its UI.
- Publishing or accepting formal payroll data, showing a production page entry, or enabling the
  feature flag.
- Bank connections, payment instructions, payment submission, or payment execution.
- Exposing the Windows development service to VM103 without authenticated service transport.
- Reorganizing existing production tables during the 0021 deployment. The accepted modular
  monolith target is recorded separately and will be migrated incrementally.

## Frozen invariants

- `payable`, `submission_supported`, and every payment execution/submission capability remain
  strictly `false`.
- Money exchanged with LedgerBridge uses signed JSON-safe integer `_minor` fields only.
- `company_id`, `account_id`, and `employee_id` are provider-issued stable identifiers; the
  adapter never guesses, rewrites, or derives them from raw account data.
- Company scope comes only from authenticated LedgerBridge grants and configured mapping.
- Unknown major versions and malformed or incomplete audit/verification closure fail closed.

## Acceptance tests

- Default-disabled and missing-configuration routes reject before provider access.
- Timeout, unavailable provider, invalid JSON, unknown version, cross-company identity, duplicate
  idempotency mismatch, and payment-enabled payloads fail closed.
- Float, boolean, string, and JSON-unsafe `_minor` amounts are rejected.
- Verification/material objects reject unsupported schemas, raw file fields, wrong account or
  amount mappings, and incomplete result coverage.
- Audit events verify ordering, previous/content hashes, three distinct submit/review/approve
  actors, approved locked snapshot binding, and the chain proof; empty or broken chains reject.
- Existing internal-read authentication and unrelated Core behavior remain unchanged.

## Implementation evidence

- Initial adapter commit: `e2b7225392a1d1f1b17fe04959a41cab4306418a`.
- Contract remediation commit: `2980023`.
- Focused contract/auth/config verification: 65 passed; Ruff and strict mypy passed.
- Deployment revision and runtime smoke are recorded in
  `docs/reviews/2026-08-30-payroll-integration-handoff-codex.md`.

## Review findings

- Both independent reviews reported zero blockers.
- Pre-push findings for nested money/verification/material validation, strict audit action data,
  complete audit-chain validation, and v1 safe optional fields are closed in `2980023` with
  behavior-sensitive negative tests; final independent re-review is the push gate.
- Low-priority refactors are intentionally deferred because they do not change the read-only
  contract or deployment safety.

## Module boundary

Payroll is one independently maintained business module in the accepted modular-monolith target.
It shares the same PostgreSQL database and Financial Foundation with Personal Finance and Hotel
Reconciliation while owning its private schema and service interface. The current HTTP source is
a transitional provider implementation; a future same-database implementation replaces the
source behind the unchanged `payroll-ledgerbridge-publication/v1` contract.

## TEST_ONLY workspace extension (2026-09-01)

- Core adds authenticated read, organize, validate, clear, and material-preview operations for
  the disposable historical payroll workspace without importing formal payroll data.
- The provider response must match the exact requested company, batch, workspace revision,
  material, period, and material type.  A syntactically valid but different provider receipt is
  rejected before Web can show success.
- Previewed account identifiers cannot resemble raw account numbers.  Gross pay is recomputed;
  a net-pay mismatch is visible only when the provider also returns the exact blocking
  `NET_PAY_MISMATCH` evidence and marks the material for human review.
- The extension remains TEST_ONLY and cannot publish, pay, submit, export to a bank, or change a
  formal payroll batch.
