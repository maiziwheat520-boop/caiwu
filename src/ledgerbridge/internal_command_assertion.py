"""Short-lived, request-bound BFF user assertions for internal commands.

The mTLS workload principal authenticates LedgerBridge-Web as a service.  This
separate envelope binds the human Passkey session to one exact Core command and
never accepts an actor from the command body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ledgerbridge.internal_read_contract import WorkloadPrincipal

ASSERTION_VERSION = "ledgerbridge.bff-user-assertion.v1"
MAX_ASSERTION_LIFETIME_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5


class UserAssertionClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["ledgerbridge.bff-user-assertion.v1"] = "ledgerbridge.bff-user-assertion.v1"
    issuer: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    authentication_generation: int = Field(ge=1)
    method: Literal["POST"] = "POST"
    canonical_path: str = Field(pattern=r"^/internal/v1/candidates/[0-9a-f-]{36}/decisions$")
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_ref: UUID
    expected_revision: int = Field(ge=1)
    operation_id: UUID
    workload_principal: str = Field(min_length=1, max_length=200)
    policy_generation: int = Field(ge=1)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    jti: UUID


class UserAssertionError(RuntimeError):
    """The BFF assertion is missing, malformed, stale, or request-mismatched."""


def sign_user_assertion(claims: UserAssertionClaims, key: bytes) -> str:
    """Create the deterministic v1 envelope used by the Web adapter and tests."""

    _validate_key(key)
    payload = json.dumps(
        claims.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _b64url_encode(payload)
    signed = f"v1.{encoded}".encode("ascii")
    signature = hmac.new(key, signed, hashlib.sha256).digest()
    return f"v1.{encoded}.{_b64url_encode(signature)}"


def verify_user_assertion(
    token: str,
    *,
    key: bytes,
    issuer: str,
    audience: str,
    method: str,
    canonical_path: str,
    body: bytes,
    resource_ref: UUID,
    expected_revision: int,
    operation_id: UUID,
    workload_principal: WorkloadPrincipal,
    now: datetime | None = None,
) -> UserAssertionClaims:
    """Verify signature, lifetime, and every request/workload binding."""

    _validate_key(key)
    try:
        version, encoded, encoded_signature = token.split(".")
        if version != "v1":
            raise ValueError("unsupported version")
        signed = f"v1.{encoded}".encode("ascii")
        supplied_signature = _b64url_decode(encoded_signature)
        expected_signature = hmac.new(key, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature mismatch")
        claims = UserAssertionClaims.model_validate_json(_b64url_decode(encoded), strict=True)
    except (UnicodeError, ValueError) as exc:
        raise UserAssertionError("user assertion is malformed") from exc

    current = int((now or datetime.now(UTC)).timestamp())
    body_digest = hashlib.sha256(body).hexdigest()
    if (
        claims.issuer != issuer
        or claims.audience != audience
        or claims.method != method
        or claims.canonical_path != canonical_path
        or not hmac.compare_digest(claims.body_sha256, body_digest)
        or claims.resource_ref != resource_ref
        or claims.expected_revision != expected_revision
        or claims.operation_id != operation_id
        or claims.workload_principal != workload_principal.principal_ref
        or claims.policy_generation != workload_principal.policy_generation
    ):
        raise UserAssertionError("user assertion does not match the command")
    if (
        claims.expires_at <= claims.issued_at
        or claims.expires_at - claims.issued_at > MAX_ASSERTION_LIFETIME_SECONDS
        or current + MAX_CLOCK_SKEW_SECONDS < claims.issued_at
        or current - MAX_CLOCK_SKEW_SECONDS >= claims.expires_at
    ):
        raise UserAssertionError("user assertion is outside its validity window")
    return claims


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 256:
        raise UserAssertionError("user assertion key is unavailable")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
