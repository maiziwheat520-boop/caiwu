from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import HTTPException, Request

from ledgerbridge.auth import (
    EVIDENCE_WRITE,
    AuthenticatedPrincipal,
    AuthenticatedPrincipalError,
    TrustedPrincipalMiddleware,
    authorize_principal,
)
from ledgerbridge.main import get_authenticated_principal


def _principal(**changes: object) -> AuthenticatedPrincipal:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "provider": "trusted-gateway",
        "subject": "user-1",
        "capabilities": frozenset({EVIDENCE_WRITE}),
        "issued_at": now - timedelta(seconds=1),
        "expires_at": now + timedelta(minutes=5),
        "policy_generation": "policy-1",
    }
    values.update(changes)
    return AuthenticatedPrincipal(**values)  # type: ignore[arg-type]


def test_principal_authorization_is_scoped_and_time_bounded() -> None:
    principal = _principal()
    assert principal.actor == "trusted-gateway/user-1"
    assert authorize_principal(
        principal,
        EVIDENCE_WRITE,
        expected_policy_generation="policy-1",
    )
    assert not authorize_principal(
        principal,
        "review:write",
        expected_policy_generation="policy-1",
    )
    assert not authorize_principal(
        principal,
        "",
        expected_policy_generation="policy-1",
    )
    assert not authorize_principal(
        principal,
        EVIDENCE_WRITE,
        expected_policy_generation="policy-1",
        clock_skew_seconds=301,
    )


def test_route_dependency_uses_typed_principal_and_rejects_raw_gateway_state() -> None:
    principal = _principal()
    request = Request(
        {
            "type": "http",
            "state": {"authenticated_principal": principal},
        }
    )

    class Settings:
        auth_provider = "trusted_gateway"
        auth_policy_generation = "policy-1"
        auth_clock_skew_seconds = 30

    assert get_authenticated_principal(request, Settings()) == "trusted-gateway/user-1"  # type: ignore[arg-type]
    raw_request = Request(
        {
            "type": "http",
            "state": {"authenticated_principal": "attacker"},
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        get_authenticated_principal(raw_request, Settings())  # type: ignore[arg-type]
    assert exc_info.value.status_code == 401
    assert not authorize_principal(
        principal,
        EVIDENCE_WRITE,
        expected_policy_generation="policy-2",
    )
    assert not authorize_principal(
        principal,
        EVIDENCE_WRITE,
        expected_policy_generation="policy-1",
        now=principal.expires_at + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": ""},
        {"subject": "\x00bad"},
        {"provider": "idp/tenant"},
        {"subject": "user\nforged"},
        {"provider": " user"},
        {"capabilities": {EVIDENCE_WRITE}},
        {"expires_at": datetime.now(UTC) + timedelta(hours=2)},
        {"issued_at": datetime.now()},
    ],
)
def test_principal_rejects_untrusted_shapes(changes: dict[str, object]) -> None:
    with pytest.raises(AuthenticatedPrincipalError):
        _principal(**changes)


def test_principal_actor_boundary_is_enforced_before_route_admission() -> None:
    valid = _principal(provider="p" * 64, subject="s" * 135)
    assert len(valid.actor) == 200
    with pytest.raises(AuthenticatedPrincipalError, match="actor is too long"):
        _principal(provider="p" * 64, subject="s" * 136)


@pytest.mark.asyncio
async def test_trusted_middleware_installs_resolver_identity_without_headers() -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    principal = _principal()

    def resolver(_scope: Mapping[str, object]) -> AuthenticatedPrincipal:
        return principal

    middleware = TrustedPrincipalMiddleware(app, resolver)
    scope: dict[str, object] = {"type": "http", "headers": [(b"x-actor", b"attacker")]}
    await middleware(scope, object(), object())

    assert seen["state"] == {"authenticated_principal": principal}
    assert seen["headers"] == [(b"x-actor", b"attacker")]


@pytest.mark.asyncio
async def test_trusted_middleware_fails_closed_on_resolver_error() -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    def resolver(_scope: Mapping[str, object]) -> AuthenticatedPrincipal:
        raise RuntimeError("gateway unavailable")

    await TrustedPrincipalMiddleware(app, resolver)(
        {
            "type": "http",
            "state": {"untrusted": True, "authenticated_principal": _principal()},
        },
        object(),
        object(),
    )
    assert seen["state"] == {"untrusted": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_result", [None])
async def test_trusted_middleware_removes_stale_raw_principal(
    resolver_result: None,
) -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    def resolver(_scope: Mapping[str, object]) -> None:
        return resolver_result

    await TrustedPrincipalMiddleware(app, resolver)(
        {
            "type": "http",
            "state": {"authenticated_principal": "attacker"},
        },
        object(),
        object(),
    )
    assert seen["state"] == {}


@pytest.mark.asyncio
async def test_trusted_middleware_rejects_invalid_resolver_return() -> None:
    seen: dict[str, object] = {}

    async def app(scope: dict[str, object], _receive: object, _send: object) -> None:
        seen.update(scope)

    def resolver(_scope: Mapping[str, object]) -> AuthenticatedPrincipal | None:
        return cast(AuthenticatedPrincipal | None, object())

    await TrustedPrincipalMiddleware(app, resolver)(
        {"type": "http", "state": {"authenticated_principal": _principal()}},
        object(),
        object(),
    )
    assert seen["state"] == {}
