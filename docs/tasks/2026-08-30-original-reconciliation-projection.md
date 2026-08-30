# Task: original reconciliation read projection

- Status: implementation complete; central review pending
- Branch: `ai/chatgpt/original-reconciliation-core`
- Migration: none; this slice adds no schema, role, grant, credential, or real-data path
- Contract: `ledgerbridge.original-reconciliation.v1`

## Boundary and ownership

This module is distinct from the existing monthly reconciliation draft, Personal Finance,
Company Reconciliation, and Payroll modules. Shared Financial Foundation remains authoritative
for entity/company/business-unit/Managed Account identity and authorization. The private
original-reconciliation layout owns only legacy aliases, legacy slot/coordinate rules, labels,
derived-cell placement, `economic_effect`, and display sign. No shared ownership table or mapping
is copied into this module.

The deployed caller injects one bounded `layout_version` and `mapping_version`; the browser
cannot select an arbitrary version. A layout contains exactly the private label, source-to-slot,
fact-cell, and derived-cell rules for that version. F:G are immutable spacer columns. Duplicate
slot refs or any label/fact/derived coordinate collision fail closed. Real legacy labels and
amounts are not in source control; tests use synthetic names and minor-unit values.
The route returns 503 until that reviewed private layout dependency is injected; an
`unconfigured` layout never degrades into a misleading blank report.

## Facts, source layers, and money

- `CONFIRMED_CANDIDATE` identity is the latest `candidate_ref`. It remains confirmed but pending
  posting, contributes only the signed `confirmed_candidate_amount_minor` audit total and source
  counts, and never enters formal income/expense/profit cells or totals.
- `POSTED_LEDGER` identity must be the unique primary `posting.id`. It alone may populate formal
  amount cells and formal totals after an explicit private slot and `economic_effect` mapping.
- `ACCOUNT_STATEMENT` identity is the stable `transaction_ref`. It remains a source/gap until the
  Shared Foundation read projection supplies explicit entity/account/business-unit attribution.
- De-duplication is `(source_kind, canonical_fact_ref)`. Facts in different source layers are not
  hidden by equal amounts or equal lineage. Conflicting duplicates within one layer fail closed.

`economic_effect` is one of `INCOME`, `EXPENSE`, `EXPENSE_REFUND`, `NO_EFFECT`, or `BALANCE`.
The effect, not the raw sign, controls formal totals. Expense refunds reduce posted expense and
may make the current-period posted expense total negative; they never become income. Internal
transfers and repayments use `NO_EFFECT`; they may occupy an explicitly configured adjustment
slot but never enter income, expense, or profit. The cell sign comes only from the explicit
private `sign_multiplier`, so the Web must render it without flipping it again. Derived cells
read already-computed formal totals and never feed back into them.

The current Candidate adapter has no proper POSTED fact projection because the existing
category aggregate omits primary `posting.id`. It therefore returns `posted_ledger_complete=false`
and null `posted_income_minor`, `posted_expense_minor`, `posted_profit_minor`, and
`posted_amount_minor`; it does not claim a false zero. Related derived cells use
`POSTED_LEDGER_UNAVAILABLE`. A future adapter may return a complete empty fact set, in which case
and only in which case those formal totals are zero.

The posted read contract preserves three states. A complete immutable posting-identity set may be
empty and then yields formal zero totals. An unavailable posted reader yields null totals and
`POSTED_LEDGER_UNAVAILABLE`. A future company-level reader may still have valid formal totals when
an individual business-unit attribution snapshot is absent; that omission must be represented as
a breakdown GAP. It must not join historical postings to current business-unit labels or make the
company total unavailable. This slice does not yet expose that future breakdown projection.

Global `projection_gaps` keep layout-independent missing dimensions visible. The current
Candidate reader always declares `MISSING_TIME_GRANULARITY` because its facts prove only an
accounting month, not the original workbook's intra-month row. A future immutable POSTED reader
uses `MISSING_BUSINESS_UNIT_ATTRIBUTION` when company totals are sound but an individual posting
lacks its historical business-unit snapshot; it must preserve those totals and leave only the
breakdown unresolved.

## Completeness and carry-forward

`is_complete` is false when any cell or global projection GAP exists, the posted-ledger projection is unavailable, a
confirmed candidate still awaits posting, a confirmed candidate lacks an explicit legacy mapping,
any posted source has fewer mapped facts than facts, pending review exists, a missing material
exists, or opening/closing balance is unavailable.
`confirmed_pending_posting_count` makes the first condition explicit to the Web.

Balance carry-forward priority is frozen but not yet implemented:

1. an immutable published prior-period closing snapshot with the same entity, account,
   reporting dimension, and mapping version;
2. otherwise explicit as-of ledger balance evidence for the configured balance account;
3. otherwise null totals plus `MISSING_BALANCE_MAPPING` GAP cells.

The builder never derives closing balance from opening plus profit.

## Shared blocker taxonomy

The response pins `ledgerbridge.financial-foundation-blocker-taxonomy.v1`. The Shared Foundation
classifier recognizes only existing Core codes: `EVIDENCE_INCOMPLETE`, `ACCOUNT_UNREGISTERED`,
`COUNTERPARTY_STATEMENT_REQUIRED`, `FUNDING_STATEMENT_REQUIRED`,
`RELATED_ACCOUNT_STATEMENT_REQUIRED`, and `HOTEL_PAYOUT_STATEMENT_REQUIRED`. Unknown codes stay
pending and do not increase `missing_material_count`. No password blocker or general account-
attribution blocker exists in the current Candidate contract, so this slice does not invent one.

## HTTP interface

`GET /internal/v1/original-reconciliations/{month}?entity_ref=<uuid>&business_unit=<ref>` uses the
existing verified `WorkloadPrincipal`, requires both `reconciliation:read` and `candidate:read`,
revalidates every returned Candidate against entity, business unit, month, and status, rejects
unknown/duplicate query keys, returns the fixed problem contract, and always emits
`Cache-Control: no-store`. The existing `/internal/v1/reconciliations/{month}` route is unchanged.

## TDD evidence

The implementation followed vertical red-green slices. Recorded red failures included missing
projection, layout, source-rule, taxonomy, reader, and route modules; stale non-negative refund
validation; absent `is_complete`/`confirmed_pending_posting_count`; and absent
`posted_ledger_complete`. Each failure was observed before its implementation. Synthetic tests
cover A:M/40-row order, F:G spacers, labels, derived totals, expense display sign, refund greater
than current-period expense, no-effect exclusion, missing-effect GAP, nullable balances,
same-layer de-duplication, cross-layer retention, pending exclusion, shared taxonomy, strict
entity/business-unit/month isolation, no-store/problem responses, and composite capability checks.
