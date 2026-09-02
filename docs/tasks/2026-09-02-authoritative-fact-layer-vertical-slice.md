# Authoritative fact-layer vertical slice

Status: Data and reporting-service slice complete; Web policy wiring pending
Owner: Codex primary integrator
Branch: `ai/chatgpt/company-mybank-production-import`
Worktree: `D:\repos\_worktrees\ledgerbridge-company-mybank-production-import`
Base commit: `4041588`

## Ownership checkpoint

- Shared interface and migration owner: Codex primary integrator
- Owned code: MYbank cutover schema compatibility, backup/restore schema inventory, focused tests
- Owned control documents: Shared Financial Foundation language, ADR 0003, this task, project status
- Read-only consumers: Company Reporting v1 and its Web window
- Contract versions: `ledgerbridge.bank-statement.v1`, `ledgerbridge.account-registry.v1`,
  `ledgerbridge.company-report.v1`

## Slice

One representative non-empty MYbank company daily Official Source Document follows this path:

`source digest -> encrypted Evidence Object -> registered company Managed Account -> bank statement`
`-> Normalized Financial Facts -> pending statement review -> ACCOUNT_STATEMENT company report`

The slice is deliberately narrower than the complete accounting chain. It must not create or
change a Candidate, Journal Entry, or Posting.

## Acceptance

- Source SHA-256, media type, size, parser profile, account suffix, owner, account, statement period,
  row count, and transaction-set digest are exact and plan-bound.
- Schema 0032, encrypted backup, isolated restore, and rollback readiness pass before production.
- Isolated rollback-only preflight proves the same plan can import, replay without delta, reject a
  conflicting overlapping fact, and return to the exact starting inventory.
- Production import adds exactly one evidence lineage and statement, one pending review, and the
  expected transaction/observation rows. It adds no account, Candidate, Journal Entry, or Posting.
- Exact replay adds zero rows.
- An authorized `ACCOUNT_STATEMENT` company report reads the imported month and reflects exactly
  one additional pending statement. Confirmed counts and cash-flow amounts remain unchanged until
  a supported human statement-review command exists; direct read-only probes reconcile the
  imported transaction count and signed movement to the admitted facts.
- Any failure stops closed. A failed import rolls back its transaction; an anomalous successful
  import is recovered from the target-revision database and artifact backup rather than deleted.

## Not in this slice

- Automatic categorization or business-unit guessing
- Balanced Ledger Draft generation
- Human posting workflow
- Formal posted revenue, expense, profit, or opening/closing balance claims

## Production result

- Core first reached revision `e328019e2aac0bc28fc3ed43fe1d546c8a696d67` and schema
  `20260902_0032`. A target-revision encrypted backup and isolated restore passed before the
  reviewed production command ran.
- The protected runner admitted one representative non-empty official company statement. It
  created one encrypted Evidence lineage, one statement, nine transaction facts, nine
  observations, and one pending review. Its in-transaction replay was zero-delta and its
  overlapping-fact probe was rejected. Candidate, Journal Entry, and Posting counts stayed
  unchanged.
- The first reporting-service check correctly exposed an architectural defect: the
  `ACCOUNT_STATEMENT` projection joined `candidate_source` even though statement imports are
  designed not to create Accounting Candidates. Migration `20260902_0033` removes that unused
  dependency without manufacturing a Candidate or weakening authorization.
- The migration was transactionally preflighted and rolled back, then released as
  `87f88d81267434a90cb335751de97c2fe77d1f26`. The read-only company-report service now returns
  the imported month with one pending statement while confirmed transaction, confirmed
  statement, Candidate, Journal Entry, and Posting counts remain zero for this slice.
- Pre-release recovery point:
  `/srv/ai-center/backups/ledgerbridge/20260902T050538Z-e328019e2aac`, with passing rehearsal
  `restore-rehearsal-20260902T050611Z.json`.
- Final recovery point:
  `/srv/ai-center/backups/ledgerbridge/20260902T050955Z-87f88d812674`, with passing rehearsal
  `restore-rehearsal-20260902T051027Z.json`.
- The current persisted Web workload policy does not grant this representative company, so the
  existing Web window cannot display it yet. The Core fact/reporting seam is proven; adding the
  intended company grant and company selector is a separate authorization/UI change, not a data
  backfill. A supported human statement-review command and all later Candidate/draft/posting
  stages also remain open.
