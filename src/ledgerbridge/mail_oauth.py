"""Injected Microsoft identity-platform OAuth/PKCE boundary.

This module builds authorization requests and validates token responses, but it
does not perform network I/O, persist refresh tokens, or read client secrets.
Deployments must inject an OAuth transport and an external secret/token store
after the mailbox authentication gates are approved.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")
_MAX_SCOPES = 16
_MAX_SCOPE_LENGTH = 200


class OAuthBoundaryError(RuntimeError):
    """A bounded OAuth request/response failed closed."""


class OAuthTransport(Protocol):
    """The only network seam allowed to exchange an authorization code."""

    def post_form(self, endpoint: str, form: Mapping[str, str]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    tenant: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    authority_host: str = "login.microsoftonline.com"

    def __post_init__(self) -> None:
        _require_token("tenant", self.tenant, 100)
        _require_token("client_id", self.client_id, 200)
        if not self.scopes or len(self.scopes) > _MAX_SCOPES:
            raise OAuthBoundaryError("OAuth scope set is invalid")
        if any(
            not isinstance(scope, str) or not 1 <= len(scope) <= _MAX_SCOPE_LENGTH
            for scope in self.scopes
        ):
            raise OAuthBoundaryError("OAuth scope is invalid")
        parsed = urlsplit(self.redirect_uri)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.fragment:
            raise OAuthBoundaryError(
                "OAuth redirect_uri must be an absolute URL without a fragment"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise OAuthBoundaryError("HTTP OAuth redirect_uri is limited to loopback")
        if self.authority_host != "login.microsoftonline.com":
            raise OAuthBoundaryError("OAuth authority host is not approved")

    @property
    def authorize_endpoint(self) -> str:
        return f"https://{self.authority_host}/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"https://{self.authority_host}/{self.tenant}/oauth2/v2.0/token"


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """Short-lived in-memory access token; never serialize or log this value."""

    access_token: str
    expires_at: datetime
    scope: str

    def __post_init__(self) -> None:
        if not self.access_token.strip() or len(self.access_token) > 8_192:
            raise OAuthBoundaryError("OAuth access token is invalid")
        if self.expires_at.tzinfo is None:
            raise OAuthBoundaryError("OAuth token expiry must be timezone-aware")
        if not self.scope.strip():
            raise OAuthBoundaryError("OAuth token scope is missing")

    def usable(self, *, now: datetime | None = None, skew_seconds: int = 60) -> bool:
        current = now or datetime.now(UTC)
        return current.tzinfo is not None and self.expires_at > current + timedelta(
            seconds=skew_seconds
        )


class MicrosoftOAuthClient:
    """PKCE authorization-code client with an injected token exchange."""

    def __init__(self, config: OAuthClientConfig, transport: OAuthTransport) -> None:
        self._config = config
        self._transport = transport

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        _require_token("state", state, 512, minimum=16)
        _require_token("code_challenge", code_challenge, 128, minimum=43)
        query = urlencode(
            {
                "client_id": self._config.client_id,
                "response_type": "code",
                "redirect_uri": self._config.redirect_uri,
                "response_mode": "query",
                "scope": " ".join(self._config.scopes),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._config.authorize_endpoint}?{query}"

    def exchange_code(self, *, code: str, code_verifier: str) -> OAuthToken:
        _require_token("authorization code", code, 2_000, minimum=1)
        _require_token("code_verifier", code_verifier, 128, minimum=43)
        try:
            payload = self._transport.post_form(
                self._config.token_endpoint,
                {
                    "client_id": self._config.client_id,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._config.redirect_uri,
                    "code_verifier": code_verifier,
                    "scope": " ".join(self._config.scopes),
                },
            )
        except Exception as exc:
            raise OAuthBoundaryError("OAuth token exchange unavailable") from exc
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        token_type = payload.get("token_type", "Bearer")
        scope = payload.get("scope", " ".join(self._config.scopes))
        if (
            not isinstance(access_token, str)
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not 60 <= expires_in <= 86_400
            or token_type != "Bearer"
            or not isinstance(scope, str)
        ):
            raise OAuthBoundaryError("OAuth token response is invalid")
        return OAuthToken(access_token, datetime.now(UTC) + timedelta(seconds=expires_in), scope)


class EphemeralOAuthTokenProvider:
    """Adapt one injected token to ``mail_collector.AccessTokenProvider``."""

    def __init__(self, token: OAuthToken | None = None) -> None:
        self._token = token

    def set_token(self, token: OAuthToken) -> None:
        self._token = token

    def get_access_token(self) -> str:
        token = self._token
        if token is None or not token.usable():
            raise OAuthBoundaryError("OAuth access token is unavailable or expired")
        return token.access_token


def pkce_challenge(verifier: str) -> str:
    """Derive the S256 PKCE challenge without retaining the verifier."""

    _require_token("code_verifier", verifier, 128, minimum=43)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _require_token(field: str, value: str, maximum: int, *, minimum: int = 1) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or not _SAFE_TOKEN.fullmatch(value)
    ):
        raise OAuthBoundaryError(f"{field} is invalid")
