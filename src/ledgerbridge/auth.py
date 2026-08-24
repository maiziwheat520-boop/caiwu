"""Trusted-gateway principal admission for internal routes.

The middleware has no authority to parse client identity headers or bearer
tokens. A separately authenticated loopback/mTLS gateway supplies an immutable
principal through the ASGI scope; missing or malformed state is fail-closed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from ledgerbridge.text import contains_unstorable_text

EVIDENCE_WRITE: Final = "evidence:write"
MAX_PROVIDER_LENGTH: Final = 64
MAX_SUBJECT_LENGTH: Final = 200
MAX_POLICY_GENERATION_LENGTH: Final = 100
MAX_ACTOR_LENGTH: Final = 200
MAX_PRINCIPAL_LIFETIME: Final = timedelta(hours=1)


class AuthenticatedPrincipalError(ValueError):
    """A gateway principal is malformed or outside the trusted policy."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Verifier-owned identity used by internal route dependencies."""

    provider: str
    subject: str
    capabilities: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    policy_generation: str

    def __post_init__(self) -> None:
        _require_text("principal.provider", self.provider, MAX_PROVIDER_LENGTH)
        _require_text("principal.subject", self.subject, MAX_SUBJECT_LENGTH)
        _require_text(
            "principal.policy_generation",
            self.policy_generation,
            MAX_POLICY_GENERATION_LENGTH,
        )
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(value, str) or not value or len(value) > 100
            for value in self.capabilities
        ):
            raise AuthenticatedPrincipalError("principal capabilities are invalid")
        if "/" in self.provider or "/" in self.subject:
            raise AuthenticatedPrincipalError("principal provider and subject must not contain /")
        if len(self.provider) + 1 + len(self.subject) > MAX_ACTOR_LENGTH:
            raise AuthenticatedPrincipalError("principal actor is too long")
        issued = _as_utc(self.issued_at)
        expires = _as_utc(self.expires_at)
        if expires <= issued or expires - issued > MAX_PRINCIPAL_LIFETIME:
            raise AuthenticatedPrincipalError("principal lifetime is invalid")

    @property
    def actor(self) -> str:
        """Stable audit actor; raw credentials are never part of the value."""

        return f"{self.provider}/{self.subject}"


def authorize_principal(
    principal: AuthenticatedPrincipal,
    capability: str,
    *,
    expected_policy_generation: str | None,
    clock_skew_seconds: int = 30,
    now: datetime | None = None,
) -> bool:
    """Return whether a principal is current, scoped, and policy-compatible."""

    if not isinstance(capability, str) or not capability:
        return False
    if expected_policy_generation is not None and (
        principal.policy_generation != expected_policy_generation
    ):
        return False
    if clock_skew_seconds < 0 or clock_skew_seconds > 300:
        return False
    current = _as_utc(now or datetime.now(UTC))
    issued = _as_utc(principal.issued_at)
    expires = _as_utc(principal.expires_at)
    skew = timedelta(seconds=clock_skew_seconds)
    return (
        capability in principal.capabilities
        and issued <= current + skew
        and current <= expires + skew
    )


GatewayPrincipalResolver = Callable[[Mapping[str, object]], AuthenticatedPrincipal | None]


class TrustedPrincipalMiddleware:
    """Install resolver-owned identity in ASGI state without trusting headers."""

    def __init__(self, app: Callable[..., Awaitable[None]], resolver: GatewayPrincipalResolver):
        self.app = app
        self.resolver = resolver

    async def __call__(self, scope: dict[str, object], receive: object, send: object) -> None:
        if scope.get("type") == "http":
            state = scope.get("state")
            state_map = dict(state) if isinstance(state, Mapping) else {}
            state_map.pop("authenticated_principal", None)
            scope["state"] = state_map
            try:
                principal = self.resolver(scope)
            except Exception:
                principal = None
            if isinstance(principal, AuthenticatedPrincipal):
                state_map["authenticated_principal"] = principal
            scope["state"] = state_map
        await self.app(scope, receive, send)


def _require_text(field: str, value: object, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or not all(character.isprintable() for character in value)
        or contains_unstorable_text(value)
    ):
        raise AuthenticatedPrincipalError(f"{field} is invalid")


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AuthenticatedPrincipalError("principal timestamps must be timezone-aware")
    return value.astimezone(UTC)
