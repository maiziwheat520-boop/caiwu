# Task: original reconciliation workflow integrity

- Status: module contract and synthetic proof complete; production adapter pending integration
- Branch: `ai/chatgpt/original-reconciliation-workflow-core`
- Base: production Core `5d03d93fe43670ec4136754050eab06e4dab2b0c`
- Migration: none; shared persistence and production data were not modified
- Deployment: not performed
- Contract: `ledgerbridge.original-reconciliation-workflow.v1`

## Boundary

This workflow applies only to items that belong to the user's original monthly reconciliation
scope. It does not ingest every bank income or expense, replace Company Reconciliation, post
ledger facts, or re-enable screenshot/history-workbook submission. Private account mappings,
real names, statement values, and evidence IDs remain outside Git. The later June/July/August
statement scan must supply reviewed private mappings instead of adding source-specific guesses to
this module.

The existing `ledgerbridge.original-reconciliation.v1` grid remains a read-only projection. This
new module supplies the missing identity, source-account, evidence-relocation, concurrency, and
monthly-close semantics. Its process-local service is test-only and is not composed into FastAPI.

## Stable item and authoritative source contracts

An item receives a deterministic UUIDv5 from Entity, business unit, month, and an explicit
`stable_item_key` supplied by an authoritative importer or adapter. Amount, classification,
review status, and evidence links are deliberately excluded, so correcting those fields cannot
rename the item. Reusing one stable key within the same scope is rejected.

Every item has one or more authoritative sources. Each source contains a stable upstream
authority reference and immutable fact references. Bank-statement and WeChat-transfer sources
must also contain the Shared Foundation `managed_account_ref`; a display label or transaction
summary is never accepted as an account identity. Payroll publications may be authoritative
without pretending that a summary row is a bank transaction.

## Commands and close receipt

Evidence relinking requires both `expected_month_revision` and `expected_item_revision`, a bounded
add/remove set, an actor-bound idempotency key, a reason, and a timezone-aware timestamp. The
command rejects stale revisions, duplicate additions, missing removals, and changes after close.
It returns an immutable event containing the before/after revisions and exact resulting links.

Month close requires `expected_month_revision`. It fails closed when the item set is empty, any
item is not confirmed, or any item lacks evidence. The receipt binds the exact source-account,
fact, classification, amount, review, item-revision, and evidence state in a canonical SHA-256;
it includes exact integer-minor-unit income, expense, current-account net, and overall net totals.
The close receipt identity is independent of request ordering and operation ID. Exact command
replay returns the stored receipt; changed content or actor under the same operation ID conflicts.
No later mutation or second close is permitted.

## Production persistence port required from the main integration task

The module exposes `OriginalReconciliationWorkflowPort`. A production adapter must provide these
operations in one database transaction each:

1. `get_month(entity_ref, business_unit_ref, month)` returns the exact aggregate revision and its
   immutable close receipt, if present.
2. `relink_evidence(...)` locks/compares both expected revisions, validates Evidence Registry
   references and account scope, appends the event, updates the item/month snapshots, and records
   the operation fingerprint plus actor atomically.
3. `close_month(...)` locks/compares the month revision, revalidates every source fact, Managed
   Account assignment, Evidence Registry link, and review terminal state, then appends the close
   receipt and operation record atomically.

The persistence design must enforce unique operation IDs, immutable event/receipt rows, one
closed receipt per scoped month, unique stable item refs, foreign keys to the existing Evidence
and Managed Account registries, and historical account-assignment validity at the source fact's
effective time. It must not create posting rows. These constraints require a shared schema review;
this branch intentionally adds no Alembic migration.

After the adapter exists, the internal HTTP/BFF seam needs one read and two commands:

- `GET /internal/v1/original-reconciliation-workflows/{month}` with Entity and business-unit scope;
- `POST .../items/{item_ref}/evidence:relink` with `Idempotency-Key` and both expected revisions;
- `POST .../{month}:close` with `Idempotency-Key` and the expected month revision.

The Web may remove its current stable-account and formal-close blockers only after it reads these
contracts from the production adapter and re-reads the resulting revision/receipt. CSV review
export remains an export and cannot be presented as a formal close.

## Verification

Synthetic tests cover stable identity, required account refs for bank and WeChat sources,
cross-scope/duplicate rejection, optimistic concurrency, exact actor-bound idempotency, append-only
evidence events, close blockers, exact signed totals, canonical close digest and receipt identity,
and rejection of all post-close mutation. Real names, values, and source mappings are absent from
the fixtures. The final local run passed the 12 new workflow tests, 33 original-reconciliation
focused tests, and the full Core suite (`1317 passed`, `212 skipped` for documented platform/
database conditions). Ruff, strict MyPy on the new module, compileall, Bandit, and diff checks also
passed.
