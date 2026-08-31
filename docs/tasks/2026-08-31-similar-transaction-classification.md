# Task: Similar-transaction classification groups

- Status: Core group/batch slice implemented; Web and learned-rule management pending
- Implementation owner: Codex
- Review owner: central LedgerBridge task
- Core branch: `ai/chatgpt/similar-transaction-groups-core`
- Web branch: `ai/chatgpt/similar-transaction-groups-web`

## Goal

Turn repeated manual review into one explicit, auditable group action without
turning similarity into automatic posting. A reviewer still owns the first
classification. They may then preview and explicitly apply only the accounting
classification to the remaining PENDING members of the same stable group.

## Frozen boundaries

- Core remains the only owner of Candidate, decision, group, and learned-rule facts.
- The default action affects one Candidate only. Group scope is never inferred from a
  button click or silently expanded by Web.
- Every member decision still uses the existing append-only Candidate event command,
  its own derived idempotency operation, expected revision, and authorization check.
  The batch is all-or-nothing: Core preflights and locks every member in UUID order,
  then appends all member events and one batch receipt in a single transaction.
  Any stale revision, scope failure, terminal state, group-key drift, or invalid
  dimension rolls the whole request back.
- A group action propagates only `business_unit_ref` and `category_code`. Amount,
  accounting month, conflict resolution, evidence, and source facts never propagate.
- Group members must share Entity, source system, source kind, direction, transaction
  type, normalized counterparty, funding instrument/account bucket, transaction
  status, currency, risk signature, and the source Candidate's accounting month.
- Amount is not a grouping key. A robust group-level amount outlier is reported as a
  separate risk and is never included in one-click approval or a learned rule.
- Refund/reversal, internal or related-party movement, hotel payout, unsettled,
  blocker, low-confidence, and ambiguous records remain explicit review cases.
- Learned-rule mutation and automatic suggestion are not exposed in this slice.
  The group contract keeps read-only eligibility/block reasons and an `active_rule`
  placeholder that remains `null`; a later append-only rule slice must still bind the
  source approval fact and may only pre-classify, never confirm or post.

## Similarity-key sources

1. Prefer the registry-backed `counterparty_ref` when available.
2. Until production counterparty/account registries are populated, accept only the
   exact seven-field platform summary shape
   `platform | date | direction | transaction type | counterparty | funding instrument | status`.
   The versioned `EXACT_PLATFORM_SUMMARY_V1` parser excludes date and amount from the
   stable key, rejects incomplete or unknown direction shapes, and reports its degraded
   evidence basis in the public group contract. It is not fuzzy text matching.

## Acceptance scenarios

- Multiple Alipay Yu'e Bao fund-income Candidates group despite different amounts.
- The same counterparty with different direction, transaction type, Entity, source,
  funding account, currency, or risk signature does not share a group.
- Refund/reversal, internal transfer, hotel payout, and large bank-transfer examples
  are grouped only under their exact risk/evidence conditions and are not auto-confirmed.
- A group with conflicting terminal history (for example one CONFIRMED and one IGNORED)
  can still be explicitly reviewed, but cannot produce a learned rule.
- A successful batch receipt contains one result per member. A replay appends no new
  Candidate event, and a reused operation with different content fails closed. A
  failed preflight may identify member-specific reasons, but no member succeeds and
  no batch receipt or Candidate event is written.
- Rule eligibility and block reasons remain visible. Rule CRUD, match-count execution,
  automatic suggestions, and disabled events are explicitly deferred.

## Deployment boundary

Implementation, tests, commits, and pushes are in scope. Production migration,
production data mutation, and deployment remain owned by the central task after review.
