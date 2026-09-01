"""Request-bound human assertions for the protected payroll integration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.internal_read_contract import WorkloadPrincipal

PAYROLL_USER_ASSERTION_VERSION = "ledgerbridge.payroll-bff-user-assertion.v1"
PROVIDER_WORKLOAD_ASSERTION_HEADER = "X-LedgerBridge-Workload-Assertion"
PROVIDER_USER_ASSERTION_HEADER = "X-LedgerBridge-User-Assertion"
MAX_ASSERTION_LIFETIME_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5

PayrollAction = Literal[
    "payroll.status.read",
    "payroll.dashboard.read",
    "payroll.materials.list",
    "payroll.material.read",
    "payroll.batches.list",
    "payroll.verification.list",
    "payroll.material.review",
    "payroll.batch.submit-review",
    "payroll.batch.review",
    "payroll.batch.approve",
    "payroll.batch.verify-receipts",
    "payroll.publication.read",
    "payroll.test_workspace.read",
    "payroll.test_workspace.create",
    "payroll.test_workspace.organize",
    "payroll.test_workspace.validate",
    "payroll.test_workspace.clear",
    "payroll.test_workspace.legacy.read",
    "payroll.test_workspace.legacy.command",
]


class PayrollUserAssertionClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["ledgerbridge.payroll-bff-user-assertion.v1"] = (
        "ledgerbridge.payroll-bff-user-assertion.v1"
    )
    issuer: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    session_ref: str = Field(min_length=1, max_length=200)
    authentication_generation: int = Field(ge=1)
    method: Literal["GET", "POST"]
    canonical_path: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^/internal/v1/(?:payroll(?:/|$)|payroll-publications/)",
    )
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    entity_ref: UUID
    action: PayrollAction
    resource_ref: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0)
    operation_id: UUID | None = None
    workload_principal: str = Field(min_length=1, max_length=200)
    policy_generation: int = Field(ge=1)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    jti: UUID

    @model_validator(mode="after")
    def command_fields_match_method(self) -> PayrollUserAssertionClaims:
        if self.method == "GET":
            if self.expected_revision is not None or self.operation_id is not None:
                raise ValueError("GET assertions cannot bind command revision fields")
        elif self.expected_revision is None or self.operation_id is None:
            raise ValueError("POST assertions require command revision fields")
        return self


class PayrollUserAssertionError(RuntimeError):
    """A payroll assertion is malformed, stale, or request-mismatched."""


def sign_payroll_user_assertion(claims: PayrollUserAssertionClaims, key: bytes) -> str:
    _validate_key(key)
    return _sign_envelope(claims.model_dump(mode="json"), key)


def verify_payroll_user_assertion(
    token: str,
    *,
    key: bytes,
    issuer: str,
    audience: str,
    method: str,
    canonical_path: str,
    body: bytes,
    entity_ref: UUID,
    action: PayrollAction,
    resource_ref: str,
    expected_revision: int | None,
    operation_id: UUID | None,
    workload_principal: WorkloadPrincipal,
    now: datetime | None = None,
) -> PayrollUserAssertionClaims:
    _validate_key(key)
    try:
        claims = PayrollUserAssertionClaims.model_validate_json(
            _verify_envelope(token, key),
            strict=True,
        )
    except (UnicodeError, ValueError) as exc:
        raise PayrollUserAssertionError("payroll user assertion is malformed") from exc

    body_digest = hashlib.sha256(body).hexdigest()
    if (
        claims.issuer != issuer
        or claims.audience != audience
        or claims.method != method
        or claims.canonical_path != canonical_path
        or not hmac.compare_digest(claims.body_sha256, body_digest)
        or claims.entity_ref != entity_ref
        or claims.action != action
        or claims.resource_ref != resource_ref
        or claims.expected_revision != expected_revision
        or claims.operation_id != operation_id
        or claims.workload_principal != workload_principal.principal_ref
        or claims.policy_generation != workload_principal.policy_generation
    ):
        raise PayrollUserAssertionError("payroll user assertion does not match the request")
    current = int((now or datetime.now(UTC)).timestamp())
    if (
        claims.expires_at <= claims.issued_at
        or claims.expires_at - claims.issued_at > MAX_ASSERTION_LIFETIME_SECONDS
        or current + MAX_CLOCK_SKEW_SECONDS < claims.issued_at
        or current - MAX_CLOCK_SKEW_SECONDS >= claims.expires_at
    ):
        raise PayrollUserAssertionError("payroll user assertion is outside its validity window")
    return claims


class HmacPayrollProviderAssertionSigner:
    """Create request-bound workload and user assertions for PayrollVerification."""

    def __init__(
        self,
        *,
        workload_key: bytes,
        user_key: bytes,
        issuer: str,
        audience: str,
        service_subject: str,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], UUID] = uuid4,
        lifetime_seconds: int = 30,
    ) -> None:
        _validate_key(workload_key)
        _validate_key(user_key)
        if hmac.compare_digest(workload_key, user_key):
            raise PayrollUserAssertionError(
                "provider workload and user assertion keys must be independent"
            )
        if not all(
            isinstance(value, str) and value == value.strip() and value
            for value in (issuer, audience, service_subject)
        ):
            raise PayrollUserAssertionError("provider assertion identity is unavailable")
        if not 1 <= lifetime_seconds <= MAX_ASSERTION_LIFETIME_SECONDS:
            raise PayrollUserAssertionError("provider assertion lifetime is invalid")
        self._workload_key = workload_key
        self._user_key = user_key
        self._issuer = issuer
        self._audience = audience
        self._service_subject = service_subject
        self._clock = clock or (lambda: datetime.now(UTC))
        self._nonce_factory = nonce_factory
        self._lifetime_seconds = lifetime_seconds

    def headers(
        self,
        *,
        user: PayrollUserAssertionClaims,
        company_id: str,
        provider_action: str,
        method: Literal["GET", "POST"],
        path: str,
        body: bytes,
    ) -> Mapping[str, str]:
        if (
            not company_id
            or company_id != company_id.strip()
            or not provider_action
            or provider_action != provider_action.strip()
            or not path.startswith("/api/v1/")
            or "?" in path
            or "#" in path
        ):
            raise PayrollUserAssertionError("provider request binding is invalid")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PayrollUserAssertionError("provider assertion clock must be timezone-aware")
        issued_at = int(now.timestamp())
        expires_at = issued_at + self._lifetime_seconds
        body_digest = hashlib.sha256(body).hexdigest()
        common: dict[str, object] = {
            "version": "ledgerbridge.payroll-workload-assertion.v1",
            "issuer": self._issuer,
            "audience": self._audience,
            "method": method,
            "path": path,
            "body_sha256": body_digest,
            "iat": issued_at,
            "exp": expires_at,
        }
        workload = {
            **common,
            "subject": self._service_subject,
            "entity": str(user.entity_ref),
            "company": company_id,
            "action": provider_action,
            "jti": str(self._nonce_factory()),
        }
        provider_user = {
            **common,
            "version": "ledgerbridge.payroll-user-assertion.v1",
            "subject": user.subject,
            "session": user.session_ref,
            "entity": str(user.entity_ref),
            "company": company_id,
            "action": provider_action,
            "jti": str(self._nonce_factory()),
        }
        return MappingProxyType(
            {
                PROVIDER_WORKLOAD_ASSERTION_HEADER: _sign_envelope(
                    workload,
                    self._workload_key,
                ),
                PROVIDER_USER_ASSERTION_HEADER: _sign_envelope(
                    provider_user,
                    self._user_key,
                ),
            }
        )


def _sign_envelope(claims: Mapping[str, object], key: bytes) -> str:
    payload = json.dumps(
        claims,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _b64url_encode(payload)
    signed = f"v1.{encoded}".encode("ascii")
    signature = hmac.new(key, signed, hashlib.sha256).digest()
    return f"v1.{encoded}.{_b64url_encode(signature)}"


def _verify_envelope(token: str, key: bytes) -> bytes:
    version, encoded, encoded_signature = token.split(".")
    if version != "v1":
        raise ValueError("unsupported envelope")
    signed = f"v1.{encoded}".encode("ascii")
    supplied = _b64url_decode(encoded_signature)
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("signature mismatch")
    return _b64url_decode(encoded)


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 256:
        raise PayrollUserAssertionError("payroll assertion key is unavailable")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
