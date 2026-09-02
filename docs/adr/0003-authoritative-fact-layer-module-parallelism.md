---
status: accepted
---

# Keep one authoritative fact layer while modules develop in parallel

LedgerBridge will treat immutable evidence, Official Source Documents, Normalized Financial
Facts, account and owner identity, review decisions, Ledger Drafts, and Posted Entries as one
authoritative PostgreSQL data foundation. Personal Finance, Company Reporting, Hotel
Reconciliation, and Payroll remain independently maintained modules. They consume versioned
read interfaces or submit versioned commands; they do not create competing copies of financial
facts or write another module's private tables.

Development ownership follows those boundaries. Disjoint modules, adapters, pages, and tests may
advance concurrently in separate short-lived worktrees based on the same integration baseline.
One primary integrator owns shared contracts, database migrations, integration commits, and the
production release manifest. A shared contract change is complete only after affected consumers
have synchronized and its compatibility checks pass.

Formal accounting meaning remains layered. An Official Source Document may be automatically and
idempotently admitted as evidence and Normalized Financial Facts. It does not become a Ledger
Draft or Posted Entry. Only balanced, evidence-linked, explicitly approved entries enter the
formal ledger; reports must expose their Reporting Basis and never substitute statement cash flow
or review Candidates for posted revenue, expense, or profit.

## Consequences

- Module teams can ship independently while the shared data interface remains stable.
- Shared schema and contract work can briefly serialize consumers; this is deliberate coordination,
  not a whole-project single-writer rule.
- Importers must prove exact source identity, owner/account binding, transactionality, bounded row
  counts, idempotent replay, and an append-only receipt before production release.
- The first production slice ends at company-report statement facts. Candidate creation,
  classification, balanced draft creation, and human posting remain separate later slices and
  cannot be inferred from a successful statement import.
