from __future__ import annotations

from uuid import UUID

import pytest

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.production_mtls import (
    MtlsWorkloadPolicy,
)
from scripts.build_company_report_mtls_policy import (
    CompanyReportIdentityInput,
    CompanyReportPolicyError,
    build_candidate_policy,
)


def _grant(ordinal: int) -> EntityGrant:
    entity = UUID(f"10000000-0000-4000-8000-{ordinal:012d}")
    unit = UUID(f"11000000-0000-4000-8000-{ordinal:012d}")
    ref = f"unit-{ordinal}"
    return EntityGrant(
        entity_ref=entity,
        business_unit_refs=frozenset({ref}),
        business_unit_ids=frozenset({unit}),
        business_unit_bindings=((ref, unit),),
        allow_unassigned_candidates=True,
    )


def _current_policy() -> MtlsWorkloadPolicy:
    return MtlsWorkloadPolicy(
        certificate_serial="71A09F2C",
        policy_generation=5,
        principal=WorkloadPrincipal(
            principal_ref="workload:ledgerbridge-web",
            san_uri="spiffe://ledgerbridge.local/web/review",
            policy_generation=5,
            capabilities=frozenset(
                {
                    Capability.CANDIDATE_READ,
                    Capability.CANDIDATE_DECIDE,
                    Capability.EVIDENCE_READ,
                }
            ),
            grants=(_grant(1),),
        ),
    )


def _report_identity() -> CompanyReportIdentityInput:
    return CompanyReportIdentityInput(
        certificate_serial="81B10A3D",
        principal_ref="workload:ledgerbridge-company-reports",
        san_uri="spiffe://ledgerbridge.local/web/company-reports",
        grants=tuple(_grant(ordinal) for ordinal in range(1, 8)),
    )


def test_builder_preserves_primary_authority_and_isolates_company_reports() -> None:
    candidate = build_candidate_policy(
        _current_policy(),
        _report_identity(),
        expected_generation=5,
        target_generation=6,
    )

    primary, report = candidate.identities
    assert candidate.policy_generation == 6
    assert primary.certificate_serial == "71A09F2C"
    assert primary.principal.capabilities == _current_policy().principal.capabilities
    assert primary.principal.grants == _current_policy().principal.grants
    assert primary.principal.policy_generation == 6
    assert report.certificate_serial == "81B10A3D"
    assert report.principal.capabilities == frozenset({Capability.COMPANY_REPORT_READ})
    assert len(report.principal.grants) == 7
    assert not report.principal.capabilities & {
        Capability.CANDIDATE_READ,
        Capability.CANDIDATE_DECIDE,
        Capability.EVIDENCE_READ,
        Capability.LEDGER_READ,
        Capability.PAYROLL_COMMAND,
    }


def test_builder_rejects_generation_drift_and_certificate_reuse() -> None:
    for expected, target in ((4, 5), (5, 7)):
        with pytest.raises(CompanyReportPolicyError, match="GENERATION"):
            build_candidate_policy(
                _current_policy(),
                _report_identity(),
                expected_generation=expected,
                target_generation=target,
            )

    duplicate_certificate = _report_identity().model_copy(
        update={"certificate_serial": _current_policy().certificate_serial}
    )
    with pytest.raises(CompanyReportPolicyError, match="CANDIDATE_POLICY_INVALID"):
        build_candidate_policy(
            _current_policy(),
            duplicate_certificate,
            expected_generation=5,
            target_generation=6,
        )


def test_report_identity_requires_exactly_seven_bound_company_grants() -> None:
    with pytest.raises(ValueError, match="at least 7 items"):
        CompanyReportIdentityInput(
            certificate_serial="81B10A3D",
            principal_ref="workload:ledgerbridge-company-reports",
            san_uri="spiffe://ledgerbridge.local/web/company-reports",
            grants=tuple(_grant(ordinal) for ordinal in range(1, 7)),
        )


    with pytest.raises(ValueError, match="at most 7 items"):
        CompanyReportIdentityInput(
            certificate_serial="81B10A3D",
            principal_ref="workload:ledgerbridge-company-reports",
            san_uri="spiffe://ledgerbridge.local/web/company-reports",
            grants=tuple(_grant(ordinal) for ordinal in range(1, 9)),
        )
    with pytest.raises(ValueError, match="immutable reporting-unit bindings"):
        CompanyReportIdentityInput(
            certificate_serial="81B10A3D",
            principal_ref="workload:ledgerbridge-company-reports",
            san_uri="spiffe://ledgerbridge.local/web/company-reports",
            grants=tuple(
                [
                    EntityGrant(
                        entity_ref=UUID("10000000-0000-4000-8000-000000000001"),
                        allow_unassigned_candidates=True,
                    ),
                    *[_grant(ordinal) for ordinal in range(2, 8)],
                ]
            ),
        )


def test_builder_updates_existing_v2_report_identity_without_dropping_primary() -> None:
    initial = build_candidate_policy(
        _current_policy(),
        _report_identity(),
        expected_generation=5,
        target_generation=6,
    )
    updated = build_candidate_policy(
        initial,
        _report_identity(),
        expected_generation=6,
        target_generation=7,
    )

    assert len(updated.identities) == 2
    assert updated.identities[0].certificate_serial == initial.identities[0].certificate_serial
    assert updated.identities[0].principal.grants == initial.identities[0].principal.grants
    assert updated.identities[1].certificate_serial == initial.identities[1].certificate_serial
    assert updated.identities[1].principal.grants == _report_identity().grants


def test_builder_rejects_v2_report_certificate_drift() -> None:
    initial = build_candidate_policy(
        _current_policy(),
        _report_identity(),
        expected_generation=5,
        target_generation=6,
    )
    mismatched = _report_identity().model_copy(update={"certificate_serial": "99FF"})

    with pytest.raises(CompanyReportPolicyError, match="CURRENT_REPORT_IDENTITY_INVALID"):
        build_candidate_policy(
            initial,
            mismatched,
            expected_generation=6,
            target_generation=7,
        )
