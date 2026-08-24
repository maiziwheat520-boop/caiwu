"""Fail-closed mTLS verifier seam for the synthetic internal read API.

This module does not parse certificates, identity headers, cookies, or bearer
tokens. A separately configured transport verifier may inject only the typed
``VerifiedMtlsPrincipal`` envelope into the ASGI scope. Route dependencies then
recheck its lifetime and policy generation before exposing the principal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Final

from fastapi import Depends, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_contract import AuthenticationDenied, WorkloadPrincipal

VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY: Final = "ledgerbridge.verified_internal_read_principal"
MAX_INTERNAL_READ_ASSERTION_LIFETIME: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class VerifiedMtlsPrincipal:
    """Short-lived identity assertion emitted by the trusted mTLS verifier."""

    principal: WorkloadPrincipal
    issued_at: datetime
    expires_at: datetime
    policy_generation: int

    def __post_init__(self) -> None:
        if type(self.principal) is not WorkloadPrincipal:
            raise AuthenticationDenied("mTLS verifier returned an invalid principal type")
        if type(self.policy_generation) is not int or self.policy_generation < 1:
            raise AuthenticationDenied("mTLS verifier returned an invalid policy generation")
        issued_at = _as_utc(self.issued_at)
        expires_at = _as_utc(self.expires_at)
        if expires_at <= issued_at:
            raise AuthenticationDenied("mTLS verifier returned an invalid validity window")
        if expires_at - issued_at > MAX_INTERNAL_READ_ASSERTION_LIFETIME:
            raise AuthenticationDenied("mTLS verifier validity window is too long")


InternalReadPrincipalVerifier = Callable[[Mapping[str, object]], VerifiedMtlsPrincipal | None]


class VerifiedInternalReadPrincipalMiddleware:
    """Install only verifier-owned, typed identity into the ASGI scope."""

    def __init__(self, app: ASGIApp, verifier: InternalReadPrincipalVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Never retain a value installed by an upstream, non-verifier source.
        scope.pop(VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY, None)
        if scope.get("type") == "http":
            try:
                verified = self.verifier(MappingProxyType(scope))
            except Exception:
                verified = None
            if type(verified) is VerifiedMtlsPrincipal:
                scope[VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY] = verified
        await self.app(scope, receive, send)


def principal_from_scope(
    scope: Mapping[str, object],
    *,
    current_policy_generation: int,
    now: datetime | None = None,
) -> WorkloadPrincipal:
    """Resolve a current typed assertion; every malformed shape fails closed."""

    if type(current_policy_generation) is not int or current_policy_generation < 1:
        raise AuthenticationDenied("internal read policy generation is unavailable")
    verified = scope.get(VERIFIED_INTERNAL_READ_PRINCIPAL_SCOPE_KEY)
    if type(verified) is not VerifiedMtlsPrincipal:
        raise AuthenticationDenied("verified mTLS principal is unavailable")
    principal = verified.principal
    if type(principal) is not WorkloadPrincipal:
        raise AuthenticationDenied("verified mTLS principal has an invalid type")
    if (
        verified.policy_generation != current_policy_generation
        or principal.policy_generation != current_policy_generation
    ):
        raise AuthenticationDenied("verified mTLS principal policy is stale")
    current = _as_utc(now or datetime.now(UTC))
    if not (_as_utc(verified.issued_at) <= current < _as_utc(verified.expires_at)):
        raise AuthenticationDenied("verified mTLS principal is outside its validity window")
    return principal


def get_internal_read_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> WorkloadPrincipal:
    """FastAPI dependency returning only a current verifier-owned principal.

    ``AuthenticationDenied`` is intentionally left to the internal read route's
    fixed problem+json exception boundary, which maps it to HTTP 401.
    """

    generation = settings.internal_read_policy_generation
    if generation is None:
        raise AuthenticationDenied("internal read policy generation is unavailable")
    return principal_from_scope(
        request.scope,
        current_policy_generation=generation,
    )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AuthenticationDenied("mTLS verifier timestamps must be timezone-aware")
    return value.astimezone(UTC)
