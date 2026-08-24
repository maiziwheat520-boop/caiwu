from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_cursor import CursorInvalid, ReadCursorSigner


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:cursor-test",
        san_uri="spiffe://ledgerbridge.test/cursor-test",
        policy_generation=4,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=UUID("10000000-0000-4000-8000-000000000001"),
                business_unit_refs=frozenset({"unit-demo-a"}),
                business_unit_ids=frozenset({UUID("11000000-0000-4000-8000-000000000001")}),
            ),
        ),
    )


def test_cursor_round_trip_binds_horizon_filters_and_principal() -> None:
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    token = signer.issue(
        principal,
        month="2026-08",
        status="PENDING",
        business_unit="unit-demo-a",
        horizon_sequence=19,
        horizon_hash=b"h" * 32,
        last_created_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        last_candidate_id=UUID("30000000-0000-4000-8000-000000000002"),
    )

    claims = signer.verify(
        token,
        principal,
        month="2026-08",
        status="PENDING",
        business_unit="unit-demo-a",
    )

    assert claims["horizon_sequence"] == 19
    assert claims["horizon_hash"] == b"h" * 32
    assert claims["last_candidate_id"] == UUID("30000000-0000-4000-8000-000000000002")

    with pytest.raises(CursorInvalid):
        signer.verify(
            token, principal, month="2026-09", status="PENDING", business_unit="unit-demo-a"
        )


def test_cursor_rejects_tampering_and_changed_grants() -> None:
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    token = signer.issue(
        principal,
        month=None,
        status=None,
        business_unit=None,
        horizon_sequence=1,
        horizon_hash=b"h" * 32,
        last_created_at=datetime(2026, 8, 24, tzinfo=UTC),
        last_candidate_id=UUID("30000000-0000-4000-8000-000000000002"),
    )
    body, mac = token.split(".")
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + mac

    with pytest.raises(CursorInvalid):
        signer.verify(tampered, principal, month=None, status=None, business_unit=None)

    changed = principal.model_copy(update={"policy_generation": 5})
    with pytest.raises(CursorInvalid):
        signer.verify(token, changed, month=None, status=None, business_unit=None)
