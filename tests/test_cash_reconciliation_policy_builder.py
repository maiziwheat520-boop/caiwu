from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_service import DatabaseInternalReadService
from ledgerbridge.production_mtls import (
    MtlsWorkloadIdentity,
    MtlsWorkloadPolicyV2,
    UnixSocketMtlsVerifier,
    load_mtls_workload_policy,
)
from scripts.build_cash_reconciliation_mtls_policy import (
    CashReconciliationIdentityInput,
    CashReconciliationPolicyError,
    build_candidate_policy,
)


def _grant(ordinal: int, *, bound: bool = True) -> EntityGrant:
    entity = UUID(f"10000000-0000-4000-8000-{ordinal:012d}")
    if not bound:
        return EntityGrant(entity_ref=entity, allow_unassigned_candidates=True)
    unit = UUID(f"11000000-0000-4000-8000-{ordinal:012d}")
    ref = f"unit-{ordinal}"
    return EntityGrant(
        entity_ref=entity,
        business_unit_refs=frozenset({ref}),
        business_unit_ids=frozenset({unit}),
        business_unit_bindings=((ref, unit),),
        allow_unassigned_candidates=True,
    )


def _identity(
    serial: str,
    principal_ref: str,
    san_uri: str,
    capabilities: frozenset[Capability],
    grants: tuple[EntityGrant, ...],
) -> MtlsWorkloadIdentity:
    return MtlsWorkloadIdentity(
        certificate_serial=serial,
        principal=WorkloadPrincipal(
            principal_ref=principal_ref,
            san_uri=san_uri,
            policy_generation=9,
            capabilities=capabilities,
            grants=grants,
        ),
    )


def _current_policy() -> MtlsWorkloadPolicyV2:
    return MtlsWorkloadPolicyV2(
        policy_generation=9,
        identities=(
            _identity(
                "71A09F2C",
                "workload:ledgerbridge-web",
                "spiffe://ledgerbridge.local/web-review",
                frozenset(
                    {
                        Capability.CANDIDATE_READ,
                        Capability.CANDIDATE_DECIDE,
                        Capability.EVIDENCE_READ,
                        Capability.RECONCILIATION_READ,
                        Capability.LEDGER_READ,
                    }
                ),
                (_grant(1), _grant(99, bound=False)),
            ),
            _identity(
                "81B10A3D",
                "workload:ledgerbridge-company-reports",
                "spiffe://ledgerbridge.local/web/company-reports",
                frozenset({Capability.COMPANY_REPORT_READ}),
                tuple(_grant(ordinal) for ordinal in range(2, 9)),
            ),
            _identity(
                "91C20B4E",
                "workload:ledgerbridge-company-bank-review",
                "spiffe://ledgerbridge.local/web/company-bank-review",
                frozenset({Capability.LEDGER_READ}),
                tuple(_grant(ordinal) for ordinal in range(2, 7)),
            ),
        ),
    )


def _new_identity() -> CashReconciliationIdentityInput:
    return CashReconciliationIdentityInput(
        certificate_serial="A1D30C5F",
        principal_ref="workload:ledgerbridge-cash-reconciliation",
        san_uri="spiffe://ledgerbridge.local/web/cash-reconciliation",
    )


class _ProjectionResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def scalar_one(self) -> dict[str, Any]:
        return self._payload


class _ProjectionSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.parameters: dict[str, Any] = {}

    def __enter__(self) -> _ProjectionSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, _statement: object, parameters: dict[str, Any]) -> _ProjectionResult:
        self.parameters = parameters
        return _ProjectionResult(self._payload)


def _projection_payload() -> dict[str, Any]:
    facts = [
        {
            "fact_ref": f"fact-{ordinal}",
            "occurred_on": f"2026-08-{ordinal:02d}",
            "amount_minor": ordinal * 100,
        }
        for ordinal in range(1, 9)
    ]
    return {
        "contract_version": "ledgerbridge.cash-reconciliation.v2",
        "accounting_month": "2026-08",
        "rules": [],
        "rows": [
            {
                "rule_key": "stored-reporting-item",
                "flow_kind": "CURRENT",
                "business_unit_label": "authorized-scope",
                "item_label": "stored-classification",
                "source_kind": "BANK_TRANSACTION",
                "source_ref": "stored-reporting-item",
                "transaction_count": 8,
                "amount_minor": 3600,
                "facts": facts,
            }
        ],
        "issues": [],
        "eligible_fact_count": 8,
        "matched_fact_count": 8,
        "unmatched_fact_count": 0,
        "conflicted_fact_count": 0,
        "issue_count": 0,
        "issues_truncated": False,
        "totals": {"income_minor": 0, "expense_minor": 0, "current_minor": 3600},
    }


def test_builder_derives_personal_and_seven_company_scopes_without_broadening() -> None:
    current = _current_policy()

    candidate = build_candidate_policy(
        current,
        _new_identity(),
        expected_generation=9,
        target_generation=10,
    )

    assert candidate.policy_generation == 10
    assert len(candidate.identities) == 4
    assert tuple(identity.certificate_serial for identity in candidate.identities[:3]) == tuple(
        identity.certificate_serial for identity in current.identities
    )
    assert all(identity.principal.policy_generation == 10 for identity in candidate.identities)
    reconciliation = candidate.identities[-1]
    assert reconciliation.principal.capabilities == frozenset(
        {Capability.RECONCILIATION_READ, Capability.LEDGER_READ}
    )
    assert reconciliation.principal.grants == (
        current.identities[0].principal.grants[0],
        *current.identities[1].principal.grants,
    )
    assert len(reconciliation.principal.grants) == 8
    assert not reconciliation.principal.capabilities & {
        Capability.CANDIDATE_READ,
        Capability.CANDIDATE_DECIDE,
        Capability.EVIDENCE_READ,
        Capability.COMPANY_REPORT_READ,
        Capability.PAYROLL_COMMAND,
    }


def test_generated_identity_verifies_and_binds_exact_combined_projection_scope(
    tmp_path: Path,
) -> None:
    candidate = build_candidate_policy(
        _current_policy(),
        _new_identity(),
        expected_generation=9,
        target_generation=10,
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(candidate.model_dump_json(), encoding="utf-8")
    loaded = load_mtls_workload_policy(
        policy_path.absolute(),
        expected_policy_generation=10,
        require_root_owner=False,
    )
    verifier = UnixSocketMtlsVerifier(loaded)
    verified = verifier(
        {
            "type": "http",
            "client": None,
            "headers": [
                (b"x-ledgerbridge-mtls-verified", b"SUCCESS"),
                (
                    b"x-ledgerbridge-client-san",
                    b"spiffe://ledgerbridge.local/web/cash-reconciliation",
                ),
                (b"x-ledgerbridge-client-serial", b"A1D30C5F"),
            ],
        }
    )
    assert verified is not None

    session = _ProjectionSession(_projection_payload())
    projection = DatabaseInternalReadService(
        lambda: cast(Session, session)
    ).get_cash_reconciliation(verified.principal, month="2026-08")

    assert projection.model_dump(mode="json") == _projection_payload()
    assert session.parameters == {
        "month": "2026-08",
        "entity_ids": [UUID(f"10000000-0000-4000-8000-{ordinal:012d}") for ordinal in range(1, 9)],
        "business_unit_ids": [
            UUID(f"11000000-0000-4000-8000-{ordinal:012d}") for ordinal in range(1, 9)
        ],
    }
    assert (
        verifier(
            {
                "type": "http",
                "client": None,
                "headers": [
                    (b"x-ledgerbridge-mtls-verified", b"SUCCESS"),
                    (
                        b"x-ledgerbridge-client-san",
                        b"spiffe://ledgerbridge.local/web-review",
                    ),
                    (b"x-ledgerbridge-client-serial", b"A1D30C5F"),
                ],
            }
        )
        is None
    )


def test_builder_rejects_generation_drift_and_identity_collisions() -> None:
    for expected, target in ((8, 9), (9, 11)):
        with pytest.raises(CashReconciliationPolicyError, match="GENERATION"):
            build_candidate_policy(
                _current_policy(),
                _new_identity(),
                expected_generation=expected,
                target_generation=target,
            )

    for update in (
        {"certificate_serial": "71A09F2C"},
        {"san_uri": "spiffe://ledgerbridge.local/web/company-reports"},
    ):
        with pytest.raises(CashReconciliationPolicyError, match="CANDIDATE_POLICY_INVALID"):
            build_candidate_policy(
                _current_policy(),
                _new_identity().model_copy(update=update),
                expected_generation=9,
                target_generation=10,
            )


def test_builder_rejects_missing_personal_or_incomplete_company_scope() -> None:
    current = _current_policy()
    no_personal = current.model_copy(
        update={
            "identities": (
                current.identities[0].model_copy(
                    update={
                        "principal": current.identities[0].principal.model_copy(
                            update={"grants": (_grant(99, bound=False),)}
                        )
                    }
                ),
                *current.identities[1:],
            )
        }
    )
    with pytest.raises(CashReconciliationPolicyError, match="PRIMARY_SCOPE_INVALID"):
        build_candidate_policy(
            no_personal,
            _new_identity(),
            expected_generation=9,
            target_generation=10,
        )

    incomplete_report = current.model_copy(
        update={
            "identities": (
                current.identities[0],
                current.identities[1].model_copy(
                    update={
                        "principal": current.identities[1].principal.model_copy(
                            update={"grants": current.identities[1].principal.grants[:-1]}
                        )
                    }
                ),
                current.identities[2],
            )
        }
    )
    with pytest.raises(CashReconciliationPolicyError, match="COMPANY_SCOPE_INVALID"):
        build_candidate_policy(
            incomplete_report,
            _new_identity(),
            expected_generation=9,
            target_generation=10,
        )
