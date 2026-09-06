from __future__ import annotations

import pytest

from ledgerbridge.internal_read_contract import Capability, EntityGrant
from ledgerbridge.production_mtls import MtlsWorkloadPolicyV2
from scripts.build_cash_reconciliation_mtls_policy import (
    CashReconciliationPolicyError,
    _derived_grants,
    build_candidate_policy,
)
from scripts.repair_cash_reconciliation_account_scope import repair_policy
from tests.test_cash_reconciliation_policy_builder import _current_policy, _grant, _new_identity


def test_personal_bank_owner_is_not_lost_when_it_has_no_review_batch() -> None:
    current = _current_policy()
    primary = current.identities[0]
    bank = EntityGrant(entity_ref=_grant(99).entity_ref, allow_account_registry=True)
    current = current.model_copy(
        update={
            "identities": (
                primary.model_copy(
                    update={
                        "principal": primary.principal.model_copy(
                            update={"grants": (primary.principal.grants[0], bank)}
                        )
                    }
                ),
                *current.identities[1:],
            )
        }
    )

    result = _derived_grants(current)

    assert bank.entity_ref in {grant.entity_ref for grant in result}
    derived = next(grant for grant in result if grant.entity_ref == bank.entity_ref)
    assert derived == bank
    assert not derived.allow_unassigned_candidates
    assert not derived.business_unit_ids
    assert not derived.business_unit_refs
    assert len(result) == 9


def _deployed_policy() -> MtlsWorkloadPolicyV2:
    old = build_candidate_policy(
        _current_policy(), _new_identity(), expected_generation=9, target_generation=10
    )
    primary = old.identities[0]
    bank = EntityGrant(entity_ref=_grant(99).entity_ref, allow_account_registry=True)
    return old.model_copy(
        update={
            "identities": (
                primary.model_copy(
                    update={
                        "principal": primary.principal.model_copy(
                            update={"grants": (primary.principal.grants[0], bank)}
                        )
                    }
                ),
                *old.identities[1:],
            )
        }
    )


def test_repair_keeps_certificates_and_other_scopes_exact() -> None:
    old = _deployed_policy()
    repaired = repair_policy(old, expected_generation=10, target_generation=11)
    assert len(repaired.identities) == len(old.identities)
    for before, after in zip(old.identities, repaired.identities, strict=True):
        assert after.certificate_serial == before.certificate_serial
        assert after.principal.capabilities == before.principal.capabilities
        assert after.principal.san_uri == before.principal.san_uri
        assert after.principal.policy_generation == 11
        if before == old.identities[-1]:
            assert len(after.principal.grants) == 9
            assert all(grant in after.principal.grants for grant in before.principal.grants)
        else:
            assert after.principal.grants == before.principal.grants
    with pytest.raises(CashReconciliationPolicyError, match="ALREADY_COMPLETE"):
        repair_policy(repaired, expected_generation=11, target_generation=12)


def test_repair_rejects_missing_review_scope_not_just_extra_scope() -> None:
    old = _deployed_policy()
    cash = old.identities[-1]
    old = old.model_copy(
        update={
            "identities": (
                *old.identities[:-1],
                cash.model_copy(
                    update={
                        "principal": cash.principal.model_copy(
                            update={"grants": cash.principal.grants[1:]}
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(CashReconciliationPolicyError, match="SCOPE_DRIFT"):
        repair_policy(old, expected_generation=10, target_generation=11)


def test_repair_rejects_capability_drift() -> None:
    old = _deployed_policy()
    cash = old.identities[-1]
    old = old.model_copy(
        update={
            "identities": (
                *old.identities[:-1],
                cash.model_copy(
                    update={
                        "principal": cash.principal.model_copy(
                            update={
                                "capabilities": cash.principal.capabilities
                                | {Capability.CANDIDATE_READ}
                            }
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(CashReconciliationPolicyError, match="CASH_IDENTITY_INVALID"):
        repair_policy(old, expected_generation=10, target_generation=11)


def test_repair_rejects_stale_generation() -> None:
    with pytest.raises(CashReconciliationPolicyError, match="GENERATION_TRANSITION"):
        repair_policy(_deployed_policy(), expected_generation=9, target_generation=11)
