# Monthly reconciliation business acceptance repair

Owner: Codex; branch ai/chatgpt/monthly-authoritative-mapping-20260906.
Baseline: production/core e6ec44c; production/web 5ce6a07; schema 0050.
Risk: high, because authorized source visibility and financial classifications affect results.

## Confirmed defects

- The dedicated cash principal copied only candidate-batch-bound personal grants,
  omitting the existing registry-only personal bank owner from the primary Web identity.
  Therefore already mapped personal bank receipts are absent from the live main report.
- The previous Web release grouped old labels and displayed an independent local historical
  report. It did not integrate the reviewed mappings or source coverage into cash computation.
- Annual WeChat parsing and the historical material checks remain local preparation, not a
  complete production intake. Do not use a static prototype total as an authoritative replacement.

## Ownership and implementation scope

The main agent owns policy derivation, repair CLI, Core contracts and integration.
The category_summary agent owns the independent Web category summary and associated tests
on a worktree from the same current production Web baseline. No shared migration is delegated.

The scope repair reuses only the primary Web identity's existing registry-only grant, keeps
the exact two read capabilities and certificate, and rejects policy drift or stale generation.
It cannot create a new entity, review batch, certificate, or command capability. The new CLI
prepares a new private candidate only; activation requires the release lock, backup and rollback.

Reviewed bank category corrections must append audited revisions bound to exact source facts;
no source rewrite or automatic posting. Platform allocations, wage source switching, and
historical adjustments remain separately evidenced; they are not additional cash transactions.

## Acceptance

Regression: a synthetic primary identity with candidate-batch and registry-only personal
grants must retain both in the dedicated projection. The old builder drops the bank owner.
Verify all unchanged identities, scopes, serials, capabilities; reject stale/conflicting repair.
Production read-only comparison must identify each added fact once, preserving amounts/dates.
Source coverage and classification completion must not imply full monthly settlement closure.

Status: implementation in progress, not deployed; annual intake and exact corrections pending.
