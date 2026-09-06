"""Prepare a revision-bound repair of omitted, already authorized bank owners.

No activation, certificate issuance, ledger write or database operation. Only the
cash reader's missing entity grants may change; all other identities are retained.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ledgerbridge.internal_read_contract import Capability
from ledgerbridge.production_mtls import MtlsWorkloadPolicyV2, load_mtls_workload_policy
from scripts.build_cash_reconciliation_mtls_policy import (
    CashReconciliationPolicyError,
    _derived_grants,
    _single_identity,
    _write_new_candidate,
)

PRINCIPAL = "workload:ledgerbridge-cash-reconciliation"
SAN = "spiffe://ledgerbridge.local/web/cash-reconciliation"


def repair_policy(
    current: MtlsWorkloadPolicyV2, *, expected_generation: int, target_generation: int
) -> MtlsWorkloadPolicyV2:
    if (
        current.policy_generation != expected_generation
        or target_generation != expected_generation + 1
    ):
        raise CashReconciliationPolicyError("POLICY_GENERATION_TRANSITION_INVALID")
    identity = _single_identity(current, PRINCIPAL, error_code="CASH_IDENTITY_INVALID")
    if identity.principal.san_uri != SAN or identity.principal.capabilities != frozenset(
        {Capability.RECONCILIATION_READ, Capability.LEDGER_READ}
    ):
        raise CashReconciliationPolicyError("CASH_IDENTITY_INVALID")
    desired = _derived_grants(current)
    existing = identity.principal.grants
    # Reject stale or manually broadened scopes, not just mismatched entity IDs.
    if any(grant not in desired for grant in existing):
        raise CashReconciliationPolicyError("CASH_SCOPE_DRIFT")
    additions = tuple(grant for grant in desired if grant not in existing)
    if not additions:
        raise CashReconciliationPolicyError("CASH_SCOPE_ALREADY_COMPLETE")
    if any(
        grant.business_unit_refs
        or grant.business_unit_ids
        or grant.business_unit_bindings
        or not grant.allow_account_registry
        or grant.allow_unassigned_candidates
        for grant in additions
    ):
        raise CashReconciliationPolicyError("CASH_SCOPE_DRIFT")
    identities = tuple(
        item.model_copy(
            update={
                "principal": item.principal.model_copy(
                    update={
                        "policy_generation": target_generation,
                        **({"grants": desired} if item == identity else {}),
                    }
                )
            }
        )
        for item in current.identities
    )
    # Revalidate nested objects rather than relying on unvalidated model_copy.
    return MtlsWorkloadPolicyV2.model_validate(
        {
            "policy_generation": target_generation,
            "identities": [item.model_dump(mode="json") for item in identities],
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--target-generation", type=int, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        current = load_mtls_workload_policy(
            args.current_policy,
            expected_policy_generation=args.expected_generation,
            require_root_owner=False,
        )
        if not isinstance(current, MtlsWorkloadPolicyV2):
            raise CashReconciliationPolicyError("CURRENT_POLICY_V2_REQUIRED")
        candidate = repair_policy(
            current,
            expected_generation=args.expected_generation,
            target_generation=args.target_generation,
        )
        if args.write:
            _write_new_candidate(args.output, candidate)
        print(
            f"{'POLICY_WRITTEN' if args.write else 'PLAN_READY'} "
            f"generation={candidate.policy_generation} identities={len(candidate.identities)}"
        )
    except (CashReconciliationPolicyError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, CashReconciliationPolicyError) else "POLICY_BUILD_FAILED")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
