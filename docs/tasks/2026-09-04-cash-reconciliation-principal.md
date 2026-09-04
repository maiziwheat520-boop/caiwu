# Task: Dedicated cash-reconciliation principal

- Status: implementation and independent review complete; production activation pending
- Date: 2026-09-04
- Core baseline: `f7c5544eb9cbf15e3ede264034dcc4177afe7e21`
- Web baseline: `292cdb9b76a1d6122459b718219db8636110460e`
- Schema: unchanged (`20260904_0045`)
- Policy transition: generation 10 to 11

The feature branches passed focused Core checks, the complete Web backend suite,
frontend lint/tests/build, and independent Standards/Spec review. Final release
branches must still be rebuilt from the then-current production revisions under
the unified release lock before activation.

## Goal and production evidence

The monthly cash-reconciliation BFF must read the already authorized personal
scope plus the seven company scopes. Production database projection checks for
September 2026 return 19 eligible and 19 matched company facts, while the live
Web/Core path returns zero because it calls Core with the generic Web principal,
whose grants contain no company Entity.

## Authorization boundary

- A dedicated certificate is bound to
  `workload:ledgerbridge-cash-reconciliation`, proxy SAN
  `spiffe://ledgerbridge.local/web/cash-reconciliation`, and ingress port 8446.
- The principal has exactly `reconciliation:read` and `ledger:read`.
- Its scope is derived from one immutable business-unit-bound personal grant on
  the generic Web principal and exactly seven immutable company-report grants.
- Company facts are already assigned a versioned `reporting_item_code` when an
  automatic import or human review confirms the classification. The monthly
  projection consumes that stored assignment directly; it must not re-run the
  company name/rule matcher at read time.
- Payroll-only, Candidate, evidence, company-report, account-registry, and
  command authority are excluded. The browser cannot enlarge scope.
- Missing or invalid dedicated credentials make only cash reconciliation return
  503; the BFF never falls back to the under-scoped generic client.
- No database migration, financial-data write, Journal Entry, or Posting is part
  of this release.

## Activation and rollback

Central integration must acquire the unified `core,web` release lock, produce an
encrypted backup with isolated restore proof, issue a client-auth certificate
from the existing private CA, build and review the generation-10 candidate
policy, stage both immutable revisions, and switch policy/config/containers in
one rollback-managed window. Rollback restores the prior policy, both deployed
revisions, Web environment, and container set; the database is restored only if
unexpected mutation is detected.
