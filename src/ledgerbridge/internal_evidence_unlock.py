"""Ephemeral, source-bound evidence archive unlock contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn, Protocol, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.evidence_unlocker_protocol import (
    UnlockerRequest,
    UnlockerResponse,
    UnlockerSourceDescriptor,
    UnlockerStatus,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
    require_visible_scope,
)

EVIDENCE_UNLOCK_CONTRACT_VERSION = "ledgerbridge.evidence-unlock.v1"
MAX_ASSERTION_LIFETIME_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 5
EVIDENCE_UNLOCK_REQUEST_NAMESPACE = UUID("6fdf7155-a764-5a5f-a29e-46ccbce930bf")
_COMMAND_SQL = {
    "prepare_evidence_unlock": text(
        "SELECT * FROM internal_command.prepare_evidence_unlock(CAST(:request AS jsonb))"
    ),
    "complete_evidence_unlock": text(
        "SELECT * FROM internal_command.complete_evidence_unlock(CAST(:request AS jsonb))"
    ),
    "reject_evidence_unlock": text(
        "SELECT * FROM internal_command.reject_evidence_unlock(CAST(:request AS jsonb))"
    ),
}


class EvidenceUnlockUserAssertionClaims(BaseModel):
    """Short-lived human assertion matching the existing Web adapter contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["ledgerbridge.bff-user-assertion.v1"] = "ledgerbridge.bff-user-assertion.v1"
    issuer: str = Field(min_length=1, max_length=200)
    audience: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    authentication_generation: int = Field(ge=1)
    method: Literal["POST"] = "POST"
    canonical_path: Literal["/internal/v1/evidence/unlocks"] = "/internal/v1/evidence/unlocks"
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_ref: UUID
    operation_id: UUID
    workload_principal: str = Field(min_length=1, max_length=200)
    policy_generation: int = Field(ge=1)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)
    jti: UUID


class EvidenceUnlockAssertionError(RuntimeError):
    """The Web user assertion is missing, stale, or request-mismatched."""


class EvidenceUnlockIdempotencyConflict(RuntimeError):
    """An operation id was reused for a different source or request body."""


class EvidenceUnlockRejected(RuntimeError):
    """The approved source processor rejected the supplied password."""


class EvidenceUnlockTooLarge(RuntimeError):
    """The reviewed archive exceeds the configured one-request limit."""


class EvidenceUnlockUnavailable(RuntimeError):
    """The reviewed source or one-shot processor cannot safely run."""


@dataclass(frozen=True, slots=True)
class EvidenceUnlockSource:
    source_ref: UUID
    entity_ref: UUID
    business_unit_ref: str
    archive_size_bytes: int
    reviewed: bool

    def __post_init__(self) -> None:
        if self.source_ref.int == 0 or self.entity_ref.int == 0:
            raise ValueError("evidence unlock source identities must be non-zero")
        if not 1 <= len(self.business_unit_ref) <= 100:
            raise ValueError("evidence unlock business-unit reference is invalid")
        if type(self.archive_size_bytes) is not int or self.archive_size_bytes < 0:
            raise ValueError("evidence unlock archive size is invalid")
        if type(self.reviewed) is not bool:
            raise ValueError("evidence unlock review state is invalid")


class EvidenceUnlockResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.evidence-unlock-result.v1"] = (
        "ledgerbridge.evidence-unlock-result.v1"
    )
    source_ref: UUID
    unlock_status: Literal["UNLOCKED"] = "UNLOCKED"


@dataclass(frozen=True, slots=True)
class _IdempotencyReceipt:
    source_ref: UUID
    assertion_jti: UUID
    actor_ref: str
    authentication_generation: int
    workload_principal_ref: str
    result: EvidenceUnlockResult


EvidenceUnlockSourceLookup = Callable[[UUID], EvidenceUnlockSource | None]
EvidenceUnlockProcessor = Callable[[EvidenceUnlockSource, str, UUID], None]


class EvidenceUnlockerPort(Protocol):
    def process(self, request: UnlockerRequest) -> UnlockerResponse: ...


class EvidenceUnlockCoordinator(Protocol):
    def unlock(
        self,
        principal: WorkloadPrincipal,
        *,
        source_ref: UUID,
        password: str,
        operation_id: UUID,
        body_sha256: str,
        assertion_jti: UUID,
        actor_ref: str,
        authentication_generation: int,
    ) -> EvidenceUnlockResult: ...


class EvidenceUnlockService:
    """Run one reviewed archive unlock without retaining its password."""

    def __init__(
        self,
        *,
        source_lookup: EvidenceUnlockSourceLookup,
        processor: EvidenceUnlockProcessor,
        max_archive_bytes: int,
        max_idempotency_records: int = 1024,
    ) -> None:
        if type(max_archive_bytes) is not int or max_archive_bytes <= 0:
            raise ValueError("max archive bytes must be positive")
        if type(max_idempotency_records) is not int or max_idempotency_records <= 0:
            raise ValueError("max idempotency records must be positive")
        self._source_lookup = source_lookup
        self._processor = processor
        self._max_archive_bytes = max_archive_bytes
        self._max_idempotency_records = max_idempotency_records
        self._receipts: dict[UUID, _IdempotencyReceipt] = {}
        self._assertion_uses: dict[UUID, UUID] = {}
        self._lock = threading.Lock()

    def unlock(
        self,
        principal: WorkloadPrincipal,
        *,
        source_ref: UUID,
        password: str,
        operation_id: UUID,
        body_sha256: str,
        assertion_jti: UUID,
        actor_ref: str,
        authentication_generation: int,
    ) -> EvidenceUnlockResult:
        require_capability(principal, Capability.EVIDENCE_UNLOCK)
        _ = body_sha256  # Verified by the signed route and never retained as a password verifier.
        try:
            source = self._source_lookup(source_ref)
        except Exception as exc:
            raise EvidenceUnlockUnavailable("approved source lookup failed") from exc
        if (
            type(source) is not EvidenceUnlockSource
            or source.source_ref != source_ref
            or not source.reviewed
        ):
            raise ResourceNotVisible("approved evidence source was not found")
        require_visible_scope(
            principal,
            entity_ref=source.entity_ref,
            business_unit_ref=source.business_unit_ref,
        )
        if source.archive_size_bytes > self._max_archive_bytes:
            raise EvidenceUnlockTooLarge("approved evidence source exceeds the unlock limit")

        with self._lock:
            assertion_operation = self._assertion_uses.get(assertion_jti)
            if assertion_operation is not None and assertion_operation != operation_id:
                raise EvidenceUnlockIdempotencyConflict(
                    "evidence unlock assertion was reused for another operation"
                )
            if (
                assertion_operation is None
                and len(self._assertion_uses) >= self._max_idempotency_records
            ):
                raise EvidenceUnlockUnavailable("evidence unlock assertion ledger is full")
            previous = self._receipts.get(operation_id)
            if previous is not None:
                if (
                    previous.source_ref != source_ref
                    or previous.assertion_jti != assertion_jti
                    or previous.actor_ref != actor_ref
                    or previous.authentication_generation != authentication_generation
                    or previous.workload_principal_ref != principal.principal_ref
                ):
                    raise EvidenceUnlockIdempotencyConflict(
                        "evidence unlock operation does not match its first request"
                    )
                return previous.result
            if len(self._receipts) >= self._max_idempotency_records:
                raise EvidenceUnlockUnavailable("evidence unlock idempotency ledger is full")
            self._assertion_uses[assertion_jti] = operation_id
            try:
                self._processor(source, password, operation_id)
            except EvidenceUnlockRejected:
                raise
            except Exception as exc:
                raise EvidenceUnlockUnavailable("evidence unlock processor failed") from exc
            result = EvidenceUnlockResult(source_ref=source_ref)
            self._receipts[operation_id] = _IdempotencyReceipt(
                source_ref=source_ref,
                assertion_jti=assertion_jti,
                actor_ref=actor_ref,
                authentication_generation=authentication_generation,
                workload_principal_ref=principal.principal_ref,
                result=result,
            )
            return result


class DatabaseEvidenceUnlockService:
    """Coordinate authorization, the Unix-socket unlocker, and atomic database facts."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        unlocker: EvidenceUnlockerPort,
        *,
        max_archive_bytes: int,
    ) -> None:
        if type(max_archive_bytes) is not int or max_archive_bytes <= 0:
            raise ValueError("max archive bytes must be positive")
        self._session_factory = session_factory
        self._unlocker = unlocker
        self._max_archive_bytes = max_archive_bytes

    def unlock(
        self,
        principal: WorkloadPrincipal,
        *,
        source_ref: UUID,
        password: str,
        operation_id: UUID,
        body_sha256: str,
        assertion_jti: UUID,
        actor_ref: str,
        authentication_generation: int,
    ) -> EvidenceUnlockResult:
        require_capability(principal, Capability.EVIDENCE_UNLOCK)
        _ = body_sha256  # Verified at the route; never passed to persistence or the sidecar.
        identity = self._request_identity(
            principal,
            source_ref=source_ref,
            operation_id=operation_id,
            assertion_jti=assertion_jti,
            actor_ref=actor_ref,
            authentication_generation=authentication_generation,
        )
        prepared = self._prepare(identity)
        outcome = prepared.get("outcome")
        if outcome == "REPLAY_UNLOCKED":
            return EvidenceUnlockResult(source_ref=source_ref)
        if outcome == "REPLAY_REJECTED":
            raise EvidenceUnlockRejected("evidence unlock was already rejected")
        if outcome != "READY":
            raise EvidenceUnlockUnavailable("database unlock preparation is invalid")
        source, descriptor = self._prepared_source(prepared, expected_source_ref=source_ref)
        require_visible_scope(
            principal,
            entity_ref=source.entity_ref,
            business_unit_ref=source.business_unit_ref,
        )
        if source.archive_size_bytes > self._max_archive_bytes:
            raise EvidenceUnlockTooLarge("approved evidence source exceeds the unlock limit")
        request = UnlockerRequest(
            request_id=uuid5(
                EVIDENCE_UNLOCK_REQUEST_NAMESPACE,
                f"{operation_id}:{assertion_jti}",
            ),
            operation_id=operation_id,
            request_nonce=assertion_jti,
            source=descriptor,
            password=password,
        )
        try:
            response = self._unlocker.process(request)
        except Exception as exc:
            raise EvidenceUnlockUnavailable("evidence unlocker is unavailable") from exc
        if (
            response.request_id != request.request_id
            or response.operation_id != operation_id
            or response.request_nonce != assertion_jti
            or response.source_ref != source_ref
        ):
            raise EvidenceUnlockUnavailable("evidence unlocker response identity is invalid")
        if response.status == UnlockerStatus.REJECTED:
            self._record_rejection(identity)
            raise EvidenceUnlockRejected("approved source processor rejected the request")
        if response.status != UnlockerStatus.UNLOCKED or not response.outputs:
            raise EvidenceUnlockUnavailable("evidence unlocker did not return encrypted outputs")
        completion = identity | {
            "contract_version": "ledgerbridge.evidence-unlock-completion.v1",
            "outputs": [item.model_dump(mode="json") for item in response.outputs],
        }
        row = self._execute_command("complete_evidence_unlock", completion)
        if row.get("source_ref") != source_ref or row.get("unlock_status") != "UNLOCKED":
            raise EvidenceUnlockUnavailable("database unlock completion is invalid")
        return EvidenceUnlockResult(source_ref=source_ref)

    def _prepare(self, identity: dict[str, object]) -> Mapping[str, object]:
        # Persist the non-secret reservation before invoking the sidecar so concurrent
        # requests cannot process the same operation independently.
        return self._execute_command("prepare_evidence_unlock", identity)

    def _record_rejection(self, identity: dict[str, object]) -> None:
        request = identity | {
            "contract_version": "ledgerbridge.evidence-unlock-rejection.v1",
            "error_code": "UNLOCK_REJECTED",
        }
        row = self._execute_command("reject_evidence_unlock", request)
        if row.get("source_ref") != identity["source_ref"]:
            raise EvidenceUnlockUnavailable("database unlock rejection receipt is invalid")

    def _execute_command(
        self,
        function_name: Literal[
            "prepare_evidence_unlock",
            "complete_evidence_unlock",
            "reject_evidence_unlock",
        ],
        request: dict[str, object],
        *,
        commit: bool = True,
    ) -> Mapping[str, object]:
        try:
            payload = json.dumps(
                request,
                default=str,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            with self._session_factory() as session:
                row = (
                    session.execute(
                        _COMMAND_SQL[function_name],
                        {"request": payload},
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise EvidenceUnlockUnavailable("database unlock command returned no result")
                if commit:
                    session.commit()
                return cast(Mapping[str, object], dict(row))
        except (
            EvidenceUnlockUnavailable,
            EvidenceUnlockIdempotencyConflict,
            EvidenceUnlockRejected,
            ResourceNotVisible,
        ):
            raise
        except SQLAlchemyError as exc:
            self._raise_database_error(exc)
        except (TypeError, ValueError, KeyError) as exc:
            raise EvidenceUnlockUnavailable("database unlock command failed") from exc

    @staticmethod
    def _request_identity(
        principal: WorkloadPrincipal,
        *,
        source_ref: UUID,
        operation_id: UUID,
        assertion_jti: UUID,
        actor_ref: str,
        authentication_generation: int,
    ) -> dict[str, object]:
        scope_bindings = [
            {
                "entity_ref": str(grant.entity_ref),
                "business_unit_id": str(business_unit_id),
            }
            for grant in principal.grants
            for _business_unit_ref, business_unit_id in grant.business_unit_bindings
        ]
        if not scope_bindings:
            raise ResourceNotVisible("approved evidence source was not found")
        return {
            "contract_version": "ledgerbridge.evidence-unlock-command.v1",
            "source_ref": str(source_ref),
            "operation_id": str(operation_id),
            "assertion_jti": str(assertion_jti),
            "actor_ref": actor_ref,
            "authentication_generation": authentication_generation,
            "workload_principal_ref": principal.principal_ref,
            "verified_san": principal.san_uri,
            "policy_generation": principal.policy_generation,
            "scope_bindings": scope_bindings,
        }

    @staticmethod
    def _prepared_source(
        row: Mapping[str, object],
        *,
        expected_source_ref: UUID,
    ) -> tuple[EvidenceUnlockSource, UnlockerSourceDescriptor]:
        try:
            source_ref = _uuid_value(row, "source_ref")
            if source_ref != expected_source_ref:
                raise ValueError("source identity mismatch")
            entity_ref = _uuid_value(row, "entity_ref")
            business_unit_ref = row["business_unit_ref"]
            if not isinstance(business_unit_ref, str):
                raise ValueError("business unit is invalid")
            plaintext_size = _int_value(row, "plaintext_size")
            source = EvidenceUnlockSource(
                source_ref=source_ref,
                entity_ref=entity_ref,
                business_unit_ref=business_unit_ref,
                archive_size_bytes=plaintext_size,
                reviewed=True,
            )
            descriptor = UnlockerSourceDescriptor(
                source_ref=source_ref,
                evidence_ref=_uuid_value(row, "source_evidence_ref"),
                object_ref=_str_value(row, "object_ref"),
                plaintext_sha256=_bytes_value(row, "plaintext_sha256", 32).hex(),
                plaintext_size=plaintext_size,
                ciphertext_sha256=_bytes_value(row, "ciphertext_sha256", 32).hex(),
                ciphertext_size=_int_value(row, "ciphertext_size"),
                storage_key=_str_value(row, "storage_key"),
                chunk_size=_int_value(row, "chunk_size"),
                stream_header=_bytes_value(row, "stream_header", 24).hex(),
                wrapped_key_generation=_str_value(row, "wrapped_key_generation"),
                wrapped_key_nonce=_bytes_value(row, "wrapped_key_nonce", 24).hex(),
                wrapped_key_ciphertext=_bytes_value(
                    row,
                    "wrapped_key_ciphertext",
                    48,
                ).hex(),
            )
            return source, descriptor
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceUnlockUnavailable("database unlock source is invalid") from exc

    @staticmethod
    def _raise_database_error(exc: SQLAlchemyError) -> NoReturn:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "LB005":
            raise EvidenceUnlockIdempotencyConflict("database idempotency conflict") from exc
        if sqlstate == "LB006":
            raise EvidenceUnlockRejected("database rejected evidence unlock") from exc
        if sqlstate == "LB004":
            raise ResourceNotVisible("approved evidence source was not found") from exc
        raise EvidenceUnlockUnavailable("database unlock backend is unavailable") from exc


def _uuid_value(row: Mapping[str, object], field: str) -> UUID:
    value = row[field]
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field} is invalid")
    return value


def _str_value(row: Mapping[str, object], field: str) -> str:
    value = row[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _int_value(row: Mapping[str, object], field: str) -> int:
    value = row[field]
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} is invalid")
    return value


def _bytes_value(row: Mapping[str, object], field: str, length: int) -> bytes:
    value = row[field]
    if isinstance(value, memoryview):
        value = value.tobytes()
    if not isinstance(value, bytes) or len(value) != length:
        raise ValueError(f"{field} is invalid")
    return value


def sign_evidence_unlock_assertion(
    claims: EvidenceUnlockUserAssertionClaims,
    key: bytes,
) -> str:
    _validate_assertion_key(key)
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


def verify_evidence_unlock_assertion(
    token: str,
    *,
    key: bytes,
    issuer: str,
    audience: str,
    body: bytes,
    source_ref: UUID,
    operation_id: UUID,
    workload_principal: WorkloadPrincipal,
    now: datetime | None = None,
) -> EvidenceUnlockUserAssertionClaims:
    _validate_assertion_key(key)
    try:
        version, encoded, encoded_signature = token.split(".")
        if version != "v1":
            raise ValueError("unsupported assertion version")
        signed = f"v1.{encoded}".encode("ascii")
        supplied_signature = _b64url_decode(encoded_signature)
        expected_signature = hmac.new(key, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("assertion signature mismatch")
        claims = EvidenceUnlockUserAssertionClaims.model_validate_json(
            _b64url_decode(encoded),
            strict=True,
        )
    except (UnicodeError, ValueError) as exc:
        raise EvidenceUnlockAssertionError("evidence unlock assertion is malformed") from exc

    current = int((now or datetime.now(UTC)).timestamp())
    if (
        claims.issuer != issuer
        or claims.audience != audience
        or not hmac.compare_digest(claims.body_sha256, hashlib.sha256(body).hexdigest())
        or claims.resource_ref != source_ref
        or claims.operation_id != operation_id
        or claims.workload_principal != workload_principal.principal_ref
        or claims.policy_generation != workload_principal.policy_generation
        or claims.expires_at <= claims.issued_at
        or claims.expires_at - claims.issued_at > MAX_ASSERTION_LIFETIME_SECONDS
        or current + MAX_CLOCK_SKEW_SECONDS < claims.issued_at
        or current - MAX_CLOCK_SKEW_SECONDS >= claims.expires_at
    ):
        raise EvidenceUnlockAssertionError("evidence unlock assertion does not match request")
    return claims


def _validate_assertion_key(key: bytes) -> None:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 256:
        raise EvidenceUnlockAssertionError("evidence unlock assertion key is unavailable")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
