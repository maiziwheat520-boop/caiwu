from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import Request

from ledgerbridge.config import Settings
from ledgerbridge.internal_read_auth import (
    VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY,
    VerifiedInternalReadPrincipalMiddleware,
    VerifiedMtlsPrincipal,
    get_internal_read_principal,
    principal_from_scope,
)
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
    authorize_candidate_read,
    filter_visible_scopes,
)

ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")
ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
POLICY_GENERATION = 11


def _principal(*, allow_unassigned: bool = False) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:r1-reader",
        san_uri="spiffe://ledgerbridge.test/r1-reader",
        policy_generation=POLICY_GENERATION,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY_A,
                business_unit_refs=frozenset({"unit-demo-a"}),
                allow_unassigned_candidates=allow_unassigned,
            ),
        ),
    )


def _verified(
    *,
    principal: WorkloadPrincipal | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    policy_generation: int = POLICY_GENERATION,
) -> VerifiedMtlsPrincipal:
    now = datetime.now(UTC)
    return VerifiedMtlsPrincipal(
        principal=principal or _principal(),
        issued_at=issued_at or now - timedelta(seconds=1),
        expires_at=expires_at or now + timedelta(minutes=5),
        policy_generation=policy_generation,
    )


def test_unassigned_candidate_requires_separate_explicit_entity_grant() -> None:
    ordinary = _principal()
    with pytest.raises(ResourceNotVisible, match="not found"):
        authorize_candidate_read(
            ordinary,
            entity_ref=ENTITY_A,
            business_unit_ref=None,
        )

    explicit = _principal(allow_unassigned=True)
    authorize_candidate_read(
        explicit,
        entity_ref=ENTITY_A,
        business_unit_ref=None,
    )
    authorize_candidate_read(
        explicit,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )
    for entity, unit in ((ENTITY_A, "unit-demo-b"), (ENTITY_B, None)):
        with pytest.raises(ResourceNotVisible, match="not found"):
            authorize_candidate_read(
                explicit,
                entity_ref=entity,
                business_unit_ref=unit,
            )


def test_unassigned_candidate_can_be_granted_without_any_business_unit() -> None:
    unassigned_only = WorkloadPrincipal(
        principal_ref="workload:r1-unassigned-only",
        san_uri="spiffe://ledgerbridge.test/r1-unassigned-only",
        policy_generation=POLICY_GENERATION,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY_A,
                allow_unassigned_candidates=True,
            ),
        ),
    )
    authorize_candidate_read(
        unassigned_only,
        entity_ref=ENTITY_A,
        business_unit_ref=None,
    )
    with pytest.raises(ResourceNotVisible, match="not found"):
        authorize_candidate_read(
            unassigned_only,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
        )
    with pytest.raises(ValueError, match="business unit or unassigned"):
        EntityGrant(entity_ref=ENTITY_A)


def test_candidate_collection_filter_does_not_infer_unassigned_visibility() -> None:
    values = (
        (ENTITY_A, "unit-demo-a"),
        (ENTITY_A, "unit-demo-b"),
        (ENTITY_A, None),
        (ENTITY_B, None),
    )
    assert filter_visible_scopes(_principal(), values) == ((ENTITY_A, "unit-demo-a"),)
    assert filter_visible_scopes(_principal(allow_unassigned=True), values) == (
        (ENTITY_A, "unit-demo-a"),
        (ENTITY_A, None),
    )


def test_scope_resolver_accepts_only_current_typed_verifier_output() -> None:
    verified = _verified()
    scope = {VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: verified}
    assert (
        principal_from_scope(
            scope,
            current_policy_generation=POLICY_GENERATION,
        )
        == verified.principal
    )

    for invalid in (
        {},
        {VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: "attacker"},
        {VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: verified.principal},
        {"state": {VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: verified}},
        {"headers": [(b"x-principal", b"workload:r1-reader")]},
        {"cookies": {"principal": "workload:r1-reader"}},
    ):
        with pytest.raises(AuthenticationDenied):
            principal_from_scope(
                invalid,
                current_policy_generation=POLICY_GENERATION,
            )


def test_scope_resolver_rejects_expired_future_and_stale_generations() -> None:
    now = datetime.now(UTC)
    invalid = (
        _verified(
            issued_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
        ),
        _verified(
            issued_at=now + timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        ),
        _verified(policy_generation=POLICY_GENERATION - 1),
        _verified(
            principal=_principal().model_copy(update={"policy_generation": POLICY_GENERATION - 1})
        ),
    )
    for verified in invalid:
        with pytest.raises(AuthenticationDenied):
            principal_from_scope(
                {VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: verified},
                current_policy_generation=POLICY_GENERATION,
                now=now,
            )


def test_verified_envelope_rejects_invalid_types_and_time_windows() -> None:
    now = datetime.now(UTC)
    with pytest.raises(AuthenticationDenied, match="principal type"):
        VerifiedMtlsPrincipal(
            principal="attacker",  # type: ignore[arg-type]
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
            policy_generation=POLICY_GENERATION,
        )
    with pytest.raises(AuthenticationDenied, match="timezone-aware"):
        _verified(issued_at=now.replace(tzinfo=None))
    with pytest.raises(AuthenticationDenied, match="validity window"):
        _verified(issued_at=now, expires_at=now)
    with pytest.raises(AuthenticationDenied, match="too long"):
        _verified(issued_at=now, expires_at=now + timedelta(hours=1, seconds=1))


@pytest.mark.asyncio
async def test_middleware_uses_verifier_and_replaces_preexisting_scope_identity() -> None:
    seen: dict[str, object] = {}
    verified = _verified()

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    def verifier(scope: Mapping[str, object]) -> VerifiedMtlsPrincipal:
        assert scope["headers"] == [(b"x-principal", b"attacker")]
        return verified

    middleware = VerifiedInternalReadPrincipalMiddleware(app, verifier)  # type: ignore[arg-type]
    scope: dict[str, object] = {
        "type": "http",
        "headers": [(b"x-principal", b"attacker")],
        "state": {"authenticated_principal": "attacker"},
        VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: "attacker",
    }
    await middleware(scope, object(), object())  # type: ignore[arg-type]
    assert seen[VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY] is verified
    assert seen["state"] == {"authenticated_principal": "attacker"}


@pytest.mark.asyncio
async def test_middleware_clears_forged_scope_identity_when_verifier_fails() -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    def verifier(_scope: Mapping[str, object]) -> VerifiedMtlsPrincipal:
        raise RuntimeError("synthetic verifier unavailable")

    middleware = VerifiedInternalReadPrincipalMiddleware(app, verifier)  # type: ignore[arg-type]
    await middleware(
        {
            "type": "http",
            VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: _verified(),
        },
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY not in seen


def test_fastapi_dependency_uses_scope_only_and_fails_closed() -> None:
    verified = _verified()
    request = Request(
        {
            "type": "http",
            VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: verified,
        }
    )
    settings = Settings(
        database_url="postgresql+psycopg://ledgerbridge_owner@localhost/ledgerbridge",
        internal_read_policy_generation=POLICY_GENERATION,
        artifact_root=Path.cwd().resolve(),
    )
    assert get_internal_read_principal(request, settings) == verified.principal

    raw = Request(
        {
            "type": "http",
            "headers": [(b"x-principal", b"attacker")],
            "state": {"authenticated_principal": "attacker"},
        }
    )
    with pytest.raises(AuthenticationDenied):
        get_internal_read_principal(raw, settings)
