from __future__ import annotations

from importlib import resources
from pathlib import Path
from uuid import UUID

import pytest

import ledgerbridge.internal_read_service as service_module
from ledgerbridge.candidate_contract import CandidateStatus
from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import (
    SyntheticInternalReadService,
    SyntheticResourceIntegrityError,
)

ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")
ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
UNASSIGNED = UUID("30000000-0000-4000-8000-000000000001")
CANDIDATE_A = UUID("30000000-0000-4000-8000-000000000002")
CANDIDATE_B = UUID("30000000-0000-4000-8000-000000000004")
EVIDENCE_A = UUID("20000000-0000-4000-8000-000000000001")
EVIDENCE_B = UUID("20000000-0000-4000-8000-000000000002")
EVIDENCE_MAIL = UUID("20000000-0000-4000-8000-000000000003")
MISSING = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
R0_FIXTURES = Path(__file__).parent / "fixtures"
PACKAGED_DATA = resources.files("ledgerbridge.synthetic_read_data")


def _principal(
    *capabilities: Capability,
    entity_ref: UUID = ENTITY_A,
    business_unit_ref: str = "unit-demo-a",
    allow_unassigned: bool = False,
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:r1-test",
        san_uri="spiffe://ledgerbridge.test/r1-test",
        policy_generation=7,
        capabilities=frozenset(capabilities),
        grants=(
            EntityGrant(
                entity_ref=entity_ref,
                business_unit_refs=frozenset({business_unit_ref}),
                allow_unassigned_candidates=allow_unassigned,
            ),
        ),
    )


def test_packaged_resources_are_exact_copies_of_the_r0_golden_data() -> None:
    names = (
        "r0_contract_fixture.json",
        "r0_evidence_note.txt",
        "r0_evidence_active.svg",
        "r0_evidence_mail.eml",
    )
    for name in names:
        assert PACKAGED_DATA.joinpath(name).read_bytes() == (R0_FIXTURES / name).read_bytes()


def test_fixture_digest_is_verified_before_dto_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "_read_resource_bytes", lambda _: b"{}")
    with pytest.raises(SyntheticResourceIntegrityError, match="fixture failed validation"):
        SyntheticInternalReadService()


def test_candidate_collection_scopes_before_filters_and_orders_stably() -> None:
    service = SyntheticInternalReadService()
    scoped = _principal(Capability.CANDIDATE_READ)

    page = service.list_candidates(scoped)
    assert [item.candidate_ref for item in page.items] == [
        UUID("30000000-0000-4000-8000-000000000002"),
        UUID("30000000-0000-4000-8000-000000000003"),
        UUID("30000000-0000-4000-8000-000000000005"),
    ]
    assert list(page.items) == sorted(
        page.items,
        key=lambda item: (item.created_at, item.candidate_ref.int),
    )
    assert page.next_cursor is None
    assert len(page.items) <= 100

    filtered = service.list_candidates(
        scoped,
        month="2026-08",
        status=CandidateStatus.PENDING,
        business_unit="unit-demo-a",
    )
    assert [item.short_id for item in filtered.items] == ["C-R0A003"]


def test_unassigned_candidates_require_the_explicit_entity_grant() -> None:
    service = SyntheticInternalReadService()
    ordinary = _principal(Capability.CANDIDATE_READ)
    explicit = _principal(Capability.CANDIDATE_READ, allow_unassigned=True)

    assert UNASSIGNED not in {
        item.candidate_ref for item in service.list_candidates(ordinary).items
    }
    assert UNASSIGNED in {item.candidate_ref for item in service.list_candidates(explicit).items}
    with pytest.raises(ResourceNotVisible, match="not found"):
        service.get_candidate(ordinary, UNASSIGNED)
    assert service.get_candidate(explicit, UNASSIGNED).business_unit_ref is None


def test_candidate_absence_and_cross_scope_access_are_indistinguishable() -> None:
    service = SyntheticInternalReadService()
    scoped = _principal(Capability.CANDIDATE_READ)

    for candidate_ref in (MISSING, CANDIDATE_B):
        with pytest.raises(ResourceNotVisible, match="resource was not found"):
            service.get_candidate(scoped, candidate_ref)
    assert service.get_candidate(scoped, CANDIDATE_A).candidate_ref == CANDIDATE_A


def test_evidence_scope_precedes_bytes_and_verified_download_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SyntheticInternalReadService()
    scoped = _principal(Capability.EVIDENCE_READ)
    content = service.get_evidence(scoped, EVIDENCE_A)
    assert content.entity_ref == ENTITY_A
    assert content.business_unit_ref == "unit-demo-a"
    assert content.media_type == "application/octet-stream"
    assert content.filename == f"evidence-{EVIDENCE_A.hex}.bin"
    assert content.filename.isascii()
    assert content.byte_size == len(content.content) == 55
    assert content.sha256 == "1606a8c45f9c9143ce1a3aa9512cb32af7ef3cdc70a56836df718f522b15a1be"

    def must_not_read(_: str) -> bytes:
        raise AssertionError("cross-scope evidence bytes were read")

    monkeypatch.setattr(service_module, "_read_resource_bytes", must_not_read)
    with pytest.raises(ResourceNotVisible, match="not found"):
        service.get_evidence(scoped, EVIDENCE_B)
    with pytest.raises(ResourceNotVisible, match="not found"):
        service.get_evidence(scoped, MISSING)


def test_every_evidence_type_is_served_as_verified_octet_stream() -> None:
    service = SyntheticInternalReadService()
    evidence_a = service.get_evidence(_principal(Capability.EVIDENCE_READ), EVIDENCE_A)
    principal_b = _principal(
        Capability.EVIDENCE_READ,
        entity_ref=ENTITY_B,
        business_unit_ref="unit-demo-b",
    )
    evidence_b = service.get_evidence(principal_b, EVIDENCE_B)
    evidence_mail = service.get_evidence(principal_b, EVIDENCE_MAIL)

    for evidence in (evidence_a, evidence_b, evidence_mail):
        assert evidence.media_type == "application/octet-stream"
        assert evidence.filename.isascii()
        assert evidence.byte_size == len(evidence.content)


def test_evidence_requires_complete_size_and_sha256_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SyntheticInternalReadService()
    scoped = _principal(Capability.EVIDENCE_READ)
    monkeypatch.setattr(service_module, "_read_resource_bytes", lambda _: b"tampered")

    with pytest.raises(SyntheticResourceIntegrityError, match="integrity verification"):
        service.get_evidence(scoped, EVIDENCE_A)


def test_reconciliation_and_ledger_are_scoped_and_posted_only() -> None:
    service = SyntheticInternalReadService()
    principal = _principal(Capability.RECONCILIATION_READ, Capability.LEDGER_READ)

    reconciliation = service.get_reconciliation(
        principal,
        month="2026-08",
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )
    assert reconciliation.posted_amount_minor == -12345
    assert reconciliation.posted_amount_minor != -12345 + 999999

    ledger = service.get_ledger_summary(
        principal,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
        from_month="2026-08",
        to_month="2026-08",
    )
    assert ledger.posting_status == "POSTED"
    assert ledger.totals_minor == {"SUPPLIES": -12345}
    empty_range = service.get_ledger_summary(
        principal,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
        from_month="2026-07",
        to_month="2026-07",
    )
    assert empty_range.totals_minor == {}

    cross_scope = _principal(
        Capability.RECONCILIATION_READ,
        Capability.LEDGER_READ,
        entity_ref=ENTITY_B,
        business_unit_ref="unit-demo-b",
    )
    with pytest.raises(ResourceNotVisible, match="not found"):
        service.get_reconciliation(
            cross_scope,
            month="2026-08",
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
        )
    with pytest.raises(ResourceNotVisible, match="not found"):
        service.get_ledger_summary(
            cross_scope,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
            from_month="2026-08",
            to_month="2026-08",
        )


def test_capabilities_and_capabilities_are_non_transitive() -> None:
    service = SyntheticInternalReadService()
    system = _principal(Capability.SYSTEM_READ)
    assert service.capabilities(system).data_mode == "synthetic"

    with pytest.raises(AuthorizationDenied):
        service.list_candidates(system)
    with pytest.raises(AuthorizationDenied):
        service.get_evidence(system, EVIDENCE_A)


@pytest.mark.parametrize("month", ["2026-00", "2026-13", "2026-8", "not-a-month"])
def test_invalid_months_fail_before_results(month: str) -> None:
    service = SyntheticInternalReadService()
    principal = _principal(Capability.CANDIDATE_READ, Capability.LEDGER_READ)
    with pytest.raises(ValueError, match="YYYY-MM"):
        service.list_candidates(principal, month=month)
    with pytest.raises(ValueError, match="YYYY-MM"):
        service.get_ledger_summary(
            principal,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
            from_month=month,
            to_month="2026-08",
        )
