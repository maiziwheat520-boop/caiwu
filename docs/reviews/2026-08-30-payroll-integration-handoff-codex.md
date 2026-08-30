# Handoff: Payroll publication read adapter

- Date: 2026-08-30
- Implementation owner: Codex
- Review owners: Copernicus and Franklin
- Branch: `ai/chatgpt/real-data-cutover`
- Base commit: `0a8530d3cbaa50d24ea3ab11369db90d0e5b9324`
- Initial adapter commit: `e2b7225392a1d1f1b17fe04959a41cab4306418a`
- Contract remediation commit: `2980023`
- Deployment revision: pending final re-review and deployment

## Outcome

LedgerBridge has a thin, default-disabled read client for
`payroll-ledgerbridge-publication/v1`. It uses LedgerBridge authentication and company grants,
does not read the PayrollVerification database, and exposes no payment capability. Production
enablement is deliberately separate from deploying the disabled code.

## Files changed

- `src/ledgerbridge/payroll_integration.py`
- `src/ledgerbridge/internal_payroll_routes.py`
- `src/ledgerbridge/config.py`
- `src/ledgerbridge/internal_read_contract.py`
- `src/ledgerbridge/main.py`
- `tests/test_payroll_integration.py`
- `tests/test_internal_payroll_routes.py`
- this task, handoff, and project-status checkpoint

## Verification

- Focused payroll contract/auth/config regression: 65 passed, with one existing Starlette
  deprecation warning.
- Ruff format/check and strict mypy passed for the payroll adapter, route, and tests.
- The release diff contains only source, tests, migration, and documentation paths; the sensitive
  path gate reports no tracked real-data path.
- Final full test, push, deployment revision, and production smoke remain the release gate.

## Review status

- BLOCKER: 0 in both independent reviews.
- HIGH: implementation remediation is complete in `2980023`; two independent re-reviews are the
  final closure evidence.
- MEDIUM: this project-status and handoff update closes the missing-documentation finding; v1
  safe optional-field compatibility is included in the validation remediation. Per the explicit
  product rule, float values remain forbidden anywhere in the exchange projection even when an
  unknown optional field is otherwise safe.
- LOW: duplicate helper and primitive error-code refactors deferred; no runtime safety impact.

## Risks and rollback

- The feature is disabled by default, so deploying it does not contact PayrollVerification.
- Production enablement requires an authenticated, VM103-reachable provider endpoint and a
  trusted provider-side publication authorizer; the Windows `127.0.0.1:4318` development service
  is not reachable from VM103 and will not be exposed unauthenticated.
- Application rollback uses the previous immutable LedgerBridge revision. The payroll adapter
  adds no database migration and stores no payroll or financial source data in Git.
- The accepted same-database modular-monolith target is documented in `CONTEXT-MAP.md` and ADR
  0001. It changes only the future provider implementation, not the v1 exchange contract.

## Next exact step

Close and re-review all hard high findings, run the focused and related regression suites, push
the reviewed commits, then deploy the release with payroll integration still disabled.
