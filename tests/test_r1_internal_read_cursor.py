from __future__ import annotations

import base64
import json
import zlib
from datetime import UTC, datetime
from uuid import UUID

import pytest

import ledgerbridge.internal_read_cursor as cursor_module
from ledgerbridge.internal_read_contract import (
    READ_CONTRACT_VERSION,
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
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


@pytest.mark.parametrize(
    ("key", "message"),
    [("short", "32"), ("x" * 257, "32")],
)
def test_cursor_signer_rejects_weak_keys(key: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ReadCursorSigner(key)


def test_cursor_issue_rejects_invalid_horizon_and_naive_timestamp() -> None:
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    candidate_id = UUID("30000000-0000-4000-8000-000000000002")
    with pytest.raises(CursorInvalid, match="horizon"):
        signer.issue(
            principal,
            horizon_sequence=0,
            horizon_hash=b"h" * 32,
            last_created_at=datetime(2026, 8, 24, tzinfo=UTC),
            month=None,
            status=None,
            business_unit=None,
            last_candidate_id=candidate_id,
        )
    with pytest.raises(CursorInvalid, match="horizon"):
        signer.issue(
            principal,
            horizon_sequence=1,
            horizon_hash=b"h" * 31,
            last_created_at=datetime(2026, 8, 24, tzinfo=UTC),
            month=None,
            status=None,
            business_unit=None,
            last_candidate_id=candidate_id,
        )
    with pytest.raises(CursorInvalid, match="timezone"):
        signer.issue(
            principal,
            horizon_sequence=1,
            horizon_hash=b"h" * 32,
            last_created_at=datetime(2026, 8, 24),
            month=None,
            status=None,
            business_unit=None,
            last_candidate_id=candidate_id,
        )


def test_cursor_verify_rejects_length_encoding_and_claim_shapes() -> None:
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    verify_kwargs = {"month": None, "status": None, "business_unit": None}

    for token in ("", "x" * 513, "not-a-token"):
        with pytest.raises(CursorInvalid):
            signer.verify(token, principal, **verify_kwargs)

    token = signer.issue(
        principal,
        **verify_kwargs,
        horizon_sequence=1,
        horizon_hash=b"h" * 32,
        last_created_at=datetime(2026, 8, 24, tzinfo=UTC),
        last_candidate_id=UUID("30000000-0000-4000-8000-000000000002"),
    )
    _, _mac = token.split(".")
    malformed = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode()
    # The signature must cover the replacement body before claims are parsed.
    signed = f"{malformed}.{cursor_module._encode(signer._mac(cursor_module._decode(malformed)))}"
    with pytest.raises(CursorInvalid, match="claims"):
        signer.verify(signed, principal, **verify_kwargs)

    payload = {
        "v": 1,
        "contract": READ_CONTRACT_VERSION,
        "principal_digest": cursor_module._digest_text(principal.principal_ref),
        "policy_generation": principal.policy_generation,
        "grant_digest": cursor_module.grant_digest(principal),
        "filters": [None, None, None],
        "horizon": [1, "00"],
        "last": ["2026-08-24T00:00:00+00:00", str(UUID("30000000-0000-4000-8000-000000000002"))],
    }
    raw = zlib.compress(json.dumps(payload, separators=(",", ":")).encode())
    encoded = cursor_module._encode(raw)
    malformed_claims = f"{encoded}.{cursor_module._encode(signer._mac(raw))}"
    with pytest.raises(CursorInvalid, match="claims"):
        signer.verify(malformed_claims, principal, **verify_kwargs)
