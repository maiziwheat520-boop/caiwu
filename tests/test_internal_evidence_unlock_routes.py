from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4, uuid5

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.evidence_unlocker_protocol import (
    UnlockerOutputDescriptor,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerStatus,
)
from ledgerbridge.internal_evidence_unlock import (
    EVIDENCE_UNLOCK_REQUEST_NAMESPACE,
    DatabaseEvidenceUnlockService,
    EvidenceUnlockCoordinator,
    EvidenceUnlockRejected,
    EvidenceUnlockService,
    EvidenceUnlockSource,
    EvidenceUnlockUserAssertionClaims,
    sign_evidence_unlock_assertion,
)
from ledgerbridge.internal_evidence_unlock_routes import (
    get_evidence_unlock_service,
    router,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.main import app as production_application

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
BUSINESS_UNIT_ID = UUID("11000000-0000-4000-8000-000000000001")
SOURCE = UUID("21000000-0000-4000-8000-000000000001")
KEY = b"k" * 32


def _settings(*, enabled: bool = False) -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=enabled,
        internal_read_policy_generation=(1 if enabled else None),
        enable_internal_evidence_unlock=enabled,
        internal_command_assertion_key=(SecretStr("k" * 32) if enabled else None),
        internal_command_assertion_issuer=("ledgerbridge-web-test" if enabled else None),
        internal_command_assertion_audience=("ledgerbridge-core-test" if enabled else None),
    )


def _principal(*, entity_ref: UUID = ENTITY) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:evidence-unlock-test",
        san_uri="spiffe://ledgerbridge.test/evidence-unlock-test",
        policy_generation=1,
        capabilities=frozenset({Capability.EVIDENCE_UNLOCK}),
        grants=(
            EntityGrant(
                entity_ref=entity_ref,
                business_unit_refs=frozenset({"unit-a"}),
                business_unit_ids=frozenset({BUSINESS_UNIT_ID}),
                business_unit_bindings=(("unit-a", BUSINESS_UNIT_ID),),
            ),
        ),
    )


def _signed_headers(
    *,
    body: bytes,
    operation_id: UUID,
    principal: WorkloadPrincipal,
    source_ref: UUID = SOURCE,
    assertion_jti: UUID | None = None,
) -> dict[str, str]:
    issued_at = datetime.now(UTC)
    assertion = sign_evidence_unlock_assertion(
        EvidenceUnlockUserAssertionClaims(
            issuer="ledgerbridge-web-test",
            audience="ledgerbridge-core-test",
            subject="ledgerbridge-owner",
            authentication_generation=4,
            canonical_path="/internal/v1/evidence/unlocks",
            body_sha256=hashlib.sha256(body).hexdigest(),
            resource_ref=source_ref,
            operation_id=operation_id,
            workload_principal=principal.principal_ref,
            policy_generation=principal.policy_generation,
            issued_at=int(issued_at.timestamp()),
            expires_at=int((issued_at + timedelta(seconds=45)).timestamp()),
            jti=assertion_jti or uuid4(),
        ),
        KEY,
    )
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": str(operation_id),
        "X-LedgerBridge-User-Assertion": assertion,
    }


def _enabled_client(
    service: EvidenceUnlockCoordinator,
    principal: WorkloadPrincipal,
) -> TestClient:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: _settings(enabled=True)
    application.dependency_overrides[get_internal_read_principal] = lambda: principal
    application.dependency_overrides[get_evidence_unlock_service] = lambda: service
    return TestClient(application)


def test_closed_gate_precedes_authentication_and_unlock_service() -> None:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: _settings()
    calls = 0

    def unexpected_dependency() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("closed unlock route must not resolve later dependencies")

    application.dependency_overrides[get_internal_read_principal] = unexpected_dependency
    application.dependency_overrides[get_evidence_unlock_service] = unexpected_dependency

    response = TestClient(application).post(
        "/internal/v1/evidence/unlocks",
        headers={"Idempotency-Key": "25000000-0000-4000-8000-000000000001"},
        json={
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": "21000000-0000-4000-8000-000000000001",
            "password": "synthetic-secret",
        },
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "EVIDENCE_UNLOCK_DISABLED"
    assert calls == 0


def test_main_application_exposes_the_closed_unlock_route() -> None:
    production_application.dependency_overrides[get_settings] = lambda: _settings()
    try:
        response = TestClient(production_application).post(
            "/internal/v1/evidence/unlocks",
            headers={"Idempotency-Key": "25000000-0000-4000-8000-000000000001"},
            json={
                "contract_version": "ledgerbridge.evidence-unlock.v1",
                "source_ref": str(SOURCE),
                "password": "synthetic-secret",
            },
        )
    finally:
        production_application.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "EVIDENCE_UNLOCK_DISABLED"


def test_reviewed_source_in_entity_scope_is_unlocked_once_without_secret_echo() -> None:
    password_was_present = False
    processor_calls = 0

    def process(source: EvidenceUnlockSource, password: str, operation_id: UUID) -> None:
        nonlocal password_was_present, processor_calls
        assert source.source_ref == SOURCE
        assert operation_id.int != 0
        password_was_present = password == "synthetic-one-request-password"
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=True,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = WorkloadPrincipal(
        principal_ref="workload:evidence-unlock-test",
        san_uri="spiffe://ledgerbridge.test/evidence-unlock-test",
        policy_generation=1,
        capabilities=frozenset({Capability.EVIDENCE_UNLOCK}),
        grants=(EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"unit-a"})),),
    )
    settings = _settings(enabled=True)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_internal_read_principal] = lambda: principal
    application.dependency_overrides[get_evidence_unlock_service] = lambda: service
    client = TestClient(application)

    operation_id = uuid4()
    payload = {
        "contract_version": "ledgerbridge.evidence-unlock.v1",
        "source_ref": str(SOURCE),
        "password": "synthetic-one-request-password",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    issued_at = datetime.now(UTC)
    assertion = sign_evidence_unlock_assertion(
        EvidenceUnlockUserAssertionClaims(
            issuer="ledgerbridge-web-test",
            audience="ledgerbridge-core-test",
            subject="ledgerbridge-owner",
            authentication_generation=4,
            canonical_path="/internal/v1/evidence/unlocks",
            body_sha256=hashlib.sha256(body).hexdigest(),
            resource_ref=SOURCE,
            operation_id=operation_id,
            workload_principal=principal.principal_ref,
            policy_generation=principal.policy_generation,
            issued_at=int(issued_at.timestamp()),
            expires_at=int((issued_at + timedelta(seconds=45)).timestamp()),
            jti=uuid4(),
        ),
        KEY,
    )

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": str(operation_id),
            "X-LedgerBridge-User-Assertion": assertion,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "ledgerbridge.evidence-unlock-result.v1",
        "source_ref": str(SOURCE),
        "unlock_status": "UNLOCKED",
    }
    assert response.headers["cache-control"] == "no-store"
    assert "synthetic-one-request-password" not in response.text
    assert password_was_present is True
    assert processor_calls == 1


def test_reviewed_archive_over_size_limit_is_rejected_before_password_processing() -> None:
    processor_calls = 0

    def process(_source: EvidenceUnlockSource, _password: str, _operation_id: UUID) -> None:
        nonlocal processor_calls
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=8193,
            reviewed=True,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 413
    assert response.json()["code"] == "EVIDENCE_UNLOCK_ARCHIVE_TOO_LARGE"
    assert processor_calls == 0


def test_reviewed_source_outside_principal_entity_is_not_visible() -> None:
    processor_calls = 0

    def process(_source: EvidenceUnlockSource, _password: str, _operation_id: UUID) -> None:
        nonlocal processor_calls
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=True,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = _principal(entity_ref=UUID("10000000-0000-4000-8000-000000000099"))
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert processor_calls == 0


def test_missing_unlock_capability_is_forbidden_before_source_lookup() -> None:
    lookup_calls = 0

    def lookup(_source_ref: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    service = EvidenceUnlockService(
        source_lookup=lookup,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    granted = _principal()
    principal = granted.model_copy(update={"capabilities": frozenset()})
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CAPABILITY_REQUIRED"
    assert lookup_calls == 0


def test_unreviewed_source_is_indistinguishable_from_an_unknown_source() -> None:
    processor_calls = 0

    def process(_source: EvidenceUnlockSource, _password: str, _operation_id: UUID) -> None:
        nonlocal processor_calls
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=False,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    assert processor_calls == 0


def test_query_string_is_rejected_before_body_or_source_processing() -> None:
    lookup_calls = 0

    def lookup(_source_ref: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    service = EvidenceUnlockService(
        source_lookup=lookup,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks?unexpected=1",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_QUERY"
    assert "synthetic-one-request-password" not in response.text
    assert lookup_calls == 0


def test_wrong_password_returns_fixed_rejection_without_secret_echo() -> None:
    password_seen = False

    def reject(_source: EvidenceUnlockSource, password: str, _operation_id: UUID) -> None:
        nonlocal password_seen
        password_seen = password == "synthetic-wrong-password"
        raise EvidenceUnlockRejected("synthetic extractor detail must stay private")

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=True,
        ),
        processor=reject,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-wrong-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(body=body, operation_id=uuid4(), principal=principal),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "UNLOCK_REJECTED"
    assert "synthetic-wrong-password" not in response.text
    assert "extractor detail" not in response.text
    assert password_seen is True


def test_assertion_body_tamper_is_rejected_before_source_lookup() -> None:
    lookup_calls = 0

    def lookup(_source_ref: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    service = EvidenceUnlockService(
        source_lookup=lookup,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    signed_body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-signed-password",
        },
        separators=(",", ":"),
    ).encode()
    tampered_body = signed_body.replace(b"synthetic-signed", b"synthetic-altered")

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=tampered_body,
        headers=_signed_headers(
            body=signed_body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "USER_ASSERTION_INVALID"
    assert lookup_calls == 0


def test_invalid_and_oversized_content_lengths_fail_before_source_lookup() -> None:
    lookup_calls = 0

    def lookup(_source_ref: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    service = EvidenceUnlockService(
        source_lookup=lookup,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)

    negative = client.post(
        "/internal/v1/evidence/unlocks",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "-1"},
    )
    oversized = client.post(
        "/internal/v1/evidence/unlocks",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "8193"},
    )

    assert negative.status_code == 400
    assert negative.json()["code"] == "INVALID_CONTENT_LENGTH"
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "EVIDENCE_UNLOCK_REQUEST_TOO_LARGE"
    assert lookup_calls == 0


def test_actual_oversized_body_fails_before_assertion_or_source_lookup() -> None:
    lookup_calls = 0

    def lookup(_source_ref: UUID) -> None:
        nonlocal lookup_calls
        lookup_calls += 1
        return None

    service = EvidenceUnlockService(
        source_lookup=lookup,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    client = _enabled_client(service, _principal())

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=b"x" * 8193,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "EVIDENCE_UNLOCK_REQUEST_TOO_LARGE"
    assert lookup_calls == 0


def test_duplicate_security_headers_are_rejected() -> None:
    service = EvidenceUnlockService(
        source_lookup=lambda _source_ref: None,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-password",
        },
        separators=(",", ":"),
    ).encode()
    signed = _signed_headers(body=body, operation_id=operation_id, principal=principal)

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=[
            ("Content-Type", "application/json"),
            ("Idempotency-Key", signed["Idempotency-Key"]),
            ("Idempotency-Key", str(uuid4())),
            ("X-LedgerBridge-User-Assertion", signed["X-LedgerBridge-User-Assertion"]),
        ],
    )

    assert response.status_code == 400
    assert response.json()["code"] == "DUPLICATE_SECURITY_HEADER"


def test_exact_replay_is_idempotent_and_changed_request_conflicts() -> None:
    processor_calls = 0

    def process(_source: EvidenceUnlockSource, _password: str, _operation_id: UUID) -> None:
        nonlocal processor_calls
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=True,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    first_body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-first-password",
        },
        separators=(",", ":"),
    ).encode()
    first_headers = _signed_headers(
        body=first_body,
        operation_id=operation_id,
        principal=principal,
    )

    first = client.post(
        "/internal/v1/evidence/unlocks",
        content=first_body,
        headers=first_headers,
    )
    replay = client.post(
        "/internal/v1/evidence/unlocks",
        content=first_body,
        headers=first_headers,
    )
    changed_body = first_body.replace(b"synthetic-first-password", b"synthetic-other-password")
    conflict = client.post(
        "/internal/v1/evidence/unlocks",
        content=changed_body,
        headers=_signed_headers(
            body=changed_body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert "synthetic-first-password" not in conflict.text
    assert "synthetic-other-password" not in conflict.text
    assert processor_calls == 1


def test_assertion_jti_cannot_authorize_two_operations() -> None:
    processor_calls = 0

    def process(_source: EvidenceUnlockSource, _password: str, _operation_id: UUID) -> None:
        nonlocal processor_calls
        processor_calls += 1

    service = EvidenceUnlockService(
        source_lookup=lambda source_ref: EvidenceUnlockSource(
            source_ref=source_ref,
            entity_ref=ENTITY,
            business_unit_ref="unit-a",
            archive_size_bytes=4096,
            reviewed=True,
        ),
        processor=process,
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    assertion_jti = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()
    first_operation = uuid4()
    second_operation = uuid4()

    first = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=first_operation,
            principal=principal,
            assertion_jti=assertion_jti,
        ),
    )
    conflict = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=second_operation,
            principal=principal,
            assertion_jti=assertion_jti,
        ),
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert processor_calls == 1


def test_database_unlock_persists_only_encrypted_outputs_and_non_secret_request_identity() -> None:
    executed: list[tuple[str, dict[str, object]]] = []
    commits = 0

    class Result:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row

        def mappings(self) -> Result:
            return self

        def one_or_none(self) -> dict[str, object]:
            return self.row

    source_row: dict[str, object] = {
        "outcome": "READY",
        "source_ref": SOURCE,
        "source_evidence_ref": UUID("20000000-0000-4000-8000-000000000001"),
        "entity_ref": ENTITY,
        "business_unit_ref": "unit-a",
        "object_ref": "a" * 64,
        "plaintext_sha256": bytes.fromhex("b" * 64),
        "plaintext_size": 4096,
        "ciphertext_sha256": bytes.fromhex("c" * 64),
        "ciphertext_size": 4608,
        "storage_key": f"sha256/cc/cc/{'c' * 64}",
        "chunk_size": 65536,
        "stream_header": bytes.fromhex("d" * 48),
        "wrapped_key_generation": "test-v1",
        "wrapped_key_nonce": bytes.fromhex("e" * 48),
        "wrapped_key_ciphertext": bytes.fromhex("f" * 96),
    }

    class DatabaseSession:
        def __enter__(self) -> DatabaseSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: object, params: dict[str, object]) -> Result:
            sql = str(statement)
            executed.append((sql, dict(params)))
            if "prepare_evidence_unlock" in sql:
                return Result(source_row)
            if "complete_evidence_unlock" in sql:
                return Result({"source_ref": SOURCE, "unlock_status": "UNLOCKED"})
            raise AssertionError(f"unexpected SQL: {sql}")

        def commit(self) -> None:
            nonlocal commits
            commits += 1
            return None

    class Unlocker:
        def process(self, request: UnlockerRequest) -> UnlockerResponse:
            assert request.password == "synthetic-one-request-password"
            assert request.request_id == uuid5(
                EVIDENCE_UNLOCK_REQUEST_NAMESPACE,
                f"{request.operation_id}:{request.request_nonce}",
            )
            return UnlockerResponse(
                request_id=request.request_id,
                operation_id=request.operation_id,
                request_nonce=request.request_nonce,
                source_ref=request.source.source_ref,
                status=UnlockerStatus.UNLOCKED,
                outputs=(
                    UnlockerOutputDescriptor(
                        evidence_ref=UUID("22000000-0000-4000-8000-000000000001"),
                        media_type="application/pdf",
                        display_name="statement.pdf",
                        object_ref="1" * 64,
                        plaintext_sha256="2" * 64,
                        plaintext_size=2048,
                        ciphertext_sha256="3" * 64,
                        ciphertext_size=2560,
                        storage_key=f"sha256/33/33/{'3' * 64}",
                        chunk_size=65536,
                        stream_header="4" * 48,
                        wrapped_key_generation="test-v1",
                        wrapped_key_nonce="5" * 48,
                        wrapped_key_ciphertext="6" * 96,
                    ),
                ),
            )

    service = DatabaseEvidenceUnlockService(
        lambda: cast(Session, DatabaseSession()),
        Unlocker(),
        max_archive_bytes=8192,
    )
    principal = _principal()
    client = _enabled_client(service, principal)
    operation_id = uuid4()
    body = json.dumps(
        {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": str(SOURCE),
            "password": "synthetic-one-request-password",
        },
        separators=(",", ":"),
    ).encode()

    response = client.post(
        "/internal/v1/evidence/unlocks",
        content=body,
        headers=_signed_headers(
            body=body,
            operation_id=operation_id,
            principal=principal,
        ),
    )

    assert response.status_code == 200
    assert response.json()["unlock_status"] == "UNLOCKED"
    serialized_database_params = json.dumps(
        [params for _sql, params in executed],
        default=str,
        sort_keys=True,
    )
    assert "synthetic-one-request-password" not in serialized_database_params
    assert hashlib.sha256(body).hexdigest() not in serialized_database_params
    assert str(BUSINESS_UNIT_ID) in serialized_database_params
    assert any("complete_evidence_unlock" in sql for sql, _params in executed)
    assert commits == 2
