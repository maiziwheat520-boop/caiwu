# Task: Company financial reporting projection

- Status: implementation complete; upstream migration integration pending
- Implementation owner: Codex
- Review owner: central coordination task
- Branch: `ai/chatgpt/company-reporting-core`
- Owned files: company-reporting contract, read adapter, route, migration, and tests

## Goal

Expose a versioned, read-only company financial summary that LedgerBridge-Web can render without
querying PostgreSQL or inventing company ownership.

## In scope

- `ledgerbridge.company-report.v1` summaries for the companies and business units explicitly
  granted to the authenticated `WorkloadPrincipal`.
- Three explicitly separate read bases: confirmed candidate facts, confirmed account-statement
  facts, and posted ledger facts. A response selects exactly one basis.
- Basis-specific integer-minor-unit metrics grouped by structured accounting month and, only where
  there is explicit attribution, business unit.
- Review-pending and attribution-pending counts alongside nullable material-taxonomy fields and an
  explicit unavailable authoritative-balance object.
- A same-database, independently owned `company_reporting_read` PostgreSQL projection seam and
  the authenticated Core `/internal/v1/company-reports` HTTP interface.

## Out of scope

- Browser-supplied company scope, direct Web database reads, production deployment, or writes to
  financial facts.
- Company inference from counterparty names, summaries, source labels, bank names, or account
  suffixes.
- Combining confirmed candidate facts with posted ledger facts in one total, which could count the
  same economic event twice.

## Frozen invariants

- A reportable company is an `entity` whose type is `COMPANY` and which is present in the verified
  principal's explicit entity grants.
- `CONFIRMED_CANDIDATE` treats the immutable candidate `entity_id` as the authoritative company.
  It exposes signed `confirmed_positive_minor`, `confirmed_negative_minor`, and their sum, never
  calling candidate direction income or expense.
- `ACCOUNT_STATEMENT` requires composite source lineage through
  `(candidate_source.source_system_id, candidate_source.source_event_ref)`, statement observation,
  statement, and a company-owned managed account for the same entity. Because no authoritative
  business-unit attribution exists, this basis keeps company totals while resolving detail with
  the frozen precedence `single-item 10000bp fact allocation > transaction-date account
  assignment > attribution pending`. Multi-item allocations stay pending in v1; no per-cent
  rounding algorithm is invented. Detail labels come only from registry write-time snapshots.
- `POSTED_LEDGER` is the only basis that exposes formal revenue, expense, and profit. It requires
  `POSTED` entries, explicit journal-entry business-unit attribution, and ledger account classes.
- `PENDING`, `INCOMPLETE`, `CONFLICTED`, `IGNORED`, and `SUPERSEDED` never contribute money.
  Reviewable states may contribute only to the review-pending count.
- Account-statement cash outflow and posted-ledger expense are non-negative magnitudes. Candidate
  negative values remain signed. No branch converts one basis into another.
- Money and counts remain bounded JSON-safe integers. One request is limited to 50 companies, 50
  business units per company, and 24 inclusive accounting months.
- Missing attribution is reported, not guessed. Unknown or unauthorized company filters are
  indistinguishable. Month membership is derived only from structured date fields, never summaries.
- Opening and closing balance stay null with `AUTHORITATIVE_BALANCE_UNAVAILABLE` until an immutable
  published closing snapshot or explicit audited as-of ledger balance exists; net cash flow is not
  a balance substitute.
- `missing_material_count` and `taxonomy_version` remain null until a shared versioned taxonomy is
  available. This slice does not copy a review-risk allowlist or the accounting-dimension catalogue.
- Business-unit rows carry an explicit breakdown status. `EMPTY` is distinct from
  `UNAVAILABLE_ATTRIBUTION_PENDING` and `UNAVAILABLE_MISSING_SNAPSHOT`; unavailable rows are null,
  never empty or reconstructed from a current dimension label.
- **Shared-write boundary for central review:** revision `0024` captures immutable business-unit
  ref/label snapshots when a journal-entry attribution is inserted and rejects a newly posted entry
  whose attribution lacks them. Existing history is not backfilled or guessed. Company-level posted
  totals remain available when legacy detail snapshots are missing.
- The PostgreSQL reader sees only the versioned `company_reporting_read` function. It receives no
  base-table grant, and Web never connects to PostgreSQL.

## Acceptance tests

- Cross-company and cross-business-unit isolation, including response revalidation.
- A valid company with no qualifying facts returns zero totals and no month rows.
- Non-confirmed facts never contribute money, while reviewable states contribute only to the
  appropriate pending count.
- Positive, negative, and zero amounts retain stable, basis-specific semantics.
- Unknown or unauthorized company scope returns the same not-found result.
- Company, business-unit, and month quantity bounds fail closed.
- Web renders real Core company cards, month/business-unit summaries, empty/error states, and
  review/attribution notices without fake production data.

## Implementation evidence

- Core exposes one strict, collection-scoped HTTP read route and revalidates every database
  payload against the principal's immutable company/business-unit grants. An unfiltered
  collection omits non-company entity grants; an explicit unknown or unauthorized company
  remains the same not-found response.
- Revision `0024` owns the private report reader, its exact reader-role grant, basis-separated
  SQL, account/fact attribution precedence, and the explicitly reviewed journal-attribution
  snapshot write boundary. Resolved account-statement facts are filtered by granted business
  units before aggregation; out-of-scope facts cannot be relabelled as unassigned.
- Read-only production sampling found one company, one business unit, 61 confirmed candidates,
  146 pending candidates, and no managed accounts, statement observations, or posted journal
  entries. This supports an immediately useful candidate-source layer and explicit empty
  statement/posted layers without manufacturing formal totals.
- Local Core validation: 72 focused company-reporting/route/contract tests passed; the complete
  Windows suite passed 823 tests with 198 environment-gated skips and one pre-existing Starlette
  deprecation warning. Focused Ruff and mypy, both OpenAPI YAML parses, Web lint/type/build, 77
  Web backend tests (one skip), and 36 Web component tests passed.
- No production migration, mutation, posting, deployment, or credential change was performed.

## Review findings

- Independent cross-repository review found and fixed a missing company-level
  `business_unit_breakdown_status` check in Web. Web now validates the same month-to-company
  status reduction as Core, including the distinct attribution-pending and missing-snapshot
  states.
- Independent scope review found and fixed two fail-open edges: a mixed person/company grant set
  no longer fails the whole company collection, and a statement fact assigned to an ungranted
  business unit can no longer enter company totals as apparently unassigned.
- PostgreSQL migration replay remains environment-gated because this worktree has no disposable
  migration database URL. Before merge, central coordination must integrate the separately owned
  `0022` and `0023` revisions in order and run the PostgreSQL migration suite; `0024` deliberately
  does not copy their uncommitted implementation.
