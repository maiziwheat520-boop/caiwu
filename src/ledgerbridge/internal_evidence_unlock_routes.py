"""Disabled-by-default one-request evidence archive unlock route."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.evidence_unlocker_client import EvidenceUnlockerClient
from ledgerbridge.internal_evidence_unlock import (
    EVIDENCE_UNLOCK_CONTRACT_VERSION,
    DatabaseEvidenceUnlockService,
    EvidenceUnlockAssertionError,
    EvidenceUnlockCoordinator,
    EvidenceUnlockIdempotencyConflict,
    EvidenceUnlockRejected,
    EvidenceUnlockResult,
    EvidenceUnlockService,
    EvidenceUnlockTooLarge,
    EvidenceUnlockUnavailable,
    verify_evidence_unlock_assertion,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    AuthorizationDenied,
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)

MAX_UNLOCK_REQUEST_BYTES = 8192
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024


class InternalEvidenceUnlockProblem(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _problem_response(status_code: int, code: str) -> JSONResponse:
    titles = {
        status.HTTP_400_BAD_REQUEST: "Bad Request",
        status.HTTP_401_UNAUTHORIZED: "Unauthorized",
        status.HTTP_403_FORBIDDEN: "Forbidden",
        status.HTTP_404_NOT_FOUND: "Not Found",
        status.HTTP_409_CONFLICT: "Conflict",
        status.HTTP_413_CONTENT_TOO_LARGE: "Content Too Large",
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "Unsupported Media Type",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "Unprocessable Content",
        status.HTTP_503_SERVICE_UNAVAILABLE: "Service Unavailable",
    }
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
        content={
            "type": f"urn:ledgerbridge:problem:{code.lower().replace('_', '-')}",
            "title": titles[status_code],
            "status": status_code,
            "code": code,
        },
    )


class InternalEvidenceUnlockRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except InternalEvidenceUnlockProblem as exc:
                return _problem_response(exc.status_code, exc.code)
            except AuthenticationDenied:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")
            except EvidenceUnlockAssertionError:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "USER_ASSERTION_INVALID")
            except AuthorizationDenied:
                return _problem_response(status.HTTP_403_FORBIDDEN, "CAPABILITY_REQUIRED")
            except ResourceNotVisible:
                return _problem_response(status.HTTP_404_NOT_FOUND, "RESOURCE_NOT_FOUND")
            except EvidenceUnlockIdempotencyConflict:
                return _problem_response(status.HTTP_409_CONFLICT, "IDEMPOTENCY_CONFLICT")
            except EvidenceUnlockTooLarge:
                return _problem_response(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "EVIDENCE_UNLOCK_ARCHIVE_TOO_LARGE",
                )
            except EvidenceUnlockRejected:
                return _problem_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "UNLOCK_REJECTED")
            except EvidenceUnlockUnavailable:
                return _problem_response(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "EVIDENCE_UNLOCK_UNAVAILABLE",
                )

        return route_handler


def require_internal_evidence_unlock(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.enable_internal_evidence_unlock:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_404_NOT_FOUND,
            "EVIDENCE_UNLOCK_DISABLED",
        )


def get_evidence_unlock_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvidenceUnlockCoordinator:
    """Default composition contains no reviewed source and therefore fails closed."""

    if settings.internal_evidence_unlock_backend == "database":
        socket_path = settings.internal_evidence_unlock_socket_path
        if settings.internal_evidence_unlock_transport != "unix-socket" or socket_path is None:
            raise EvidenceUnlockUnavailable("evidence unlocker transport is unavailable")
        return DatabaseEvidenceUnlockService(
            get_session_factory(settings.resolved_api_database_url()),
            EvidenceUnlockerClient(
                socket_path,
                timeout_seconds=settings.internal_evidence_unlock_timeout_seconds,
            ),
            max_archive_bytes=settings.artifact_max_bytes,
        )
    return EvidenceUnlockService(
        source_lookup=lambda _source_ref: None,
        processor=lambda _source, _password, _operation_id: None,
        max_archive_bytes=MAX_ARCHIVE_BYTES,
    )


def require_evidence_unlock(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.EVIDENCE_UNLOCK)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-evidence-unlock"],
    dependencies=[Depends(require_internal_evidence_unlock)],
    route_class=InternalEvidenceUnlockRoute,
)


@router.post(
    "/evidence/unlocks",
    response_model=EvidenceUnlockResult,
)
async def unlock_evidence(
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_evidence_unlock)],
    service: Annotated[EvidenceUnlockCoordinator, Depends(get_evidence_unlock_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvidenceUnlockResult:
    if request.scope.get("query_string"):
        raise InternalEvidenceUnlockProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    _reject_duplicate_security_headers(request)
    body_buffer = await _read_bounded_body(request)
    parsed: dict[str, object] = {}
    try:
        parsed = _parse_unlock_request(body_buffer)
        source_ref = cast(UUID, parsed["source_ref"])
        password = cast(str, parsed["password"])
        operation_id = _canonical_header_uuid(request, "Idempotency-Key")
        assertion = _single_header(request, "X-LedgerBridge-User-Assertion") or ""
        if not 1 <= len(assertion) <= 4096:
            raise EvidenceUnlockAssertionError("evidence unlock assertion is missing")
        assertion_key = settings.internal_command_assertion_key
        issuer = settings.internal_command_assertion_issuer
        audience = settings.internal_command_assertion_audience
        if assertion_key is None or issuer is None or audience is None:
            raise EvidenceUnlockAssertionError("evidence unlock assertion verifier unavailable")
        claims = verify_evidence_unlock_assertion(
            assertion,
            key=assertion_key.get_secret_value().encode("utf-8"),
            issuer=issuer,
            audience=audience,
            body=bytes(body_buffer),
            source_ref=source_ref,
            operation_id=operation_id,
            workload_principal=principal,
        )
        return service.unlock(
            principal,
            source_ref=source_ref,
            password=password,
            operation_id=operation_id,
            body_sha256=claims.body_sha256,
            assertion_jti=claims.jti,
            actor_ref=claims.subject,
            authentication_generation=claims.authentication_generation,
        )
    finally:
        if "password" in parsed:
            parsed["password"] = None
        for index in range(len(body_buffer)):
            body_buffer[index] = 0


async def _read_bounded_body(request: Request) -> bytearray:
    media_type = (_single_header(request, "Content-Type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise InternalEvidenceUnlockProblem(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "INVALID_CONTENT_TYPE",
        )
    content_length = _single_header(request, "Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise InternalEvidenceUnlockProblem(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_CONTENT_LENGTH",
            ) from exc
        if declared_length < 0:
            raise InternalEvidenceUnlockProblem(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_CONTENT_LENGTH",
            )
        if declared_length > MAX_UNLOCK_REQUEST_BYTES:
            raise InternalEvidenceUnlockProblem(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "EVIDENCE_UNLOCK_REQUEST_TOO_LARGE",
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_UNLOCK_REQUEST_BYTES:
            for index in range(len(body)):
                body[index] = 0
            raise InternalEvidenceUnlockProblem(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "EVIDENCE_UNLOCK_REQUEST_TOO_LARGE",
            )
        body.extend(chunk)
    return body


def _parse_unlock_request(body: bytearray) -> dict[str, object]:
    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(bytes(body), object_pairs_hook=closed_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        ) from exc
    if not isinstance(value, dict) or set(value) != {"contract_version", "source_ref", "password"}:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        )
    if value["contract_version"] != EVIDENCE_UNLOCK_CONTRACT_VERSION:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        )
    raw_source_ref = value["source_ref"]
    try:
        source_ref = UUID(raw_source_ref) if isinstance(raw_source_ref, str) else UUID(int=0)
    except ValueError as exc:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        ) from exc
    if not isinstance(raw_source_ref, str) or str(source_ref) != raw_source_ref:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        )
    password = value["password"]
    if not isinstance(password, str) or not 1 <= len(password) <= 1024 or "\x00" in password:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_EVIDENCE_UNLOCK_REQUEST",
        )
    return {
        "contract_version": EVIDENCE_UNLOCK_CONTRACT_VERSION,
        "source_ref": source_ref,
        "password": password,
    }


def _canonical_header_uuid(request: Request, name: str) -> UUID:
    value = _single_header(request, name) or ""
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_IDEMPOTENCY_KEY",
        ) from exc
    if str(parsed) != value.lower():
        raise InternalEvidenceUnlockProblem(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_IDEMPOTENCY_KEY",
        )
    return parsed


def _reject_duplicate_security_headers(request: Request) -> None:
    for name in (
        "Content-Type",
        "Content-Length",
        "Idempotency-Key",
        "X-LedgerBridge-User-Assertion",
    ):
        _single_header(request, name)


def _single_header(request: Request, name: str) -> str | None:
    encoded_name = name.lower().encode("ascii")
    values = [
        value.decode("latin-1")
        for raw_name, value in request.scope.get("headers", ())
        if raw_name.lower() == encoded_name
    ]
    if len(values) > 1:
        raise InternalEvidenceUnlockProblem(
            status.HTTP_400_BAD_REQUEST,
            "DUPLICATE_SECURITY_HEADER",
        )
    return values[0] if values else None
