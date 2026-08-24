"""Disabled-by-default R1 synthetic Core read routes.

Every route enforces one ordering: feature gate, verified workload identity,
route capability, closed parameter validation, then scope-aware lookup.  The
module never derives identity from headers or cookies and does not install CORS.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_read_audit import (
    AuditSinkUnavailable,
    EvidenceReadAuditEvent,
    InternalReadAuditSink,
    InternalReadReceiptSink,
    get_internal_read_audit_sink,
    get_internal_read_receipt_sink,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    AuthorizationDenied,
    CandidatePage,
    CapabilitiesResponse,
    Capability,
    LedgerSummary,
    ReconciliationProjection,
    ResourceNotVisible,
    WorkloadPrincipal,
    authorize_collection_read,
    authorize_read,
    require_capability,
)
from ledgerbridge.internal_read_cursor import CursorInvalid, ReadCursorSigner
from ledgerbridge.internal_read_service import (
    DatabaseInternalReadService,
    InternalReadBackendUnavailable,
    SyntheticInternalReadService,
    SyntheticResourceIntegrityError,
)
from ledgerbridge.text import contains_unstorable_text

_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")
_PROBLEM_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad Request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not Found",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service Unavailable",
}


class InternalReadProblem(RuntimeError):
    """Route-local error rendered as the closed four-field problem contract."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _problem_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
        content={
            "type": f"urn:ledgerbridge:problem:{code.lower().replace('_', '-')}",
            "title": _PROBLEM_TITLES[status_code],
            "status": status_code,
            "code": code,
        },
    )


class InternalReadRoute(APIRoute):
    """Keep dependency and endpoint errors inside the R1 problem contract."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except InternalReadProblem as exc:
                return _problem_response(exc.status_code, exc.code)
            except CursorInvalid:
                return _problem_response(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
            except AuthenticationDenied:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")
            except AuthorizationDenied:
                return _problem_response(status.HTTP_403_FORBIDDEN, "CAPABILITY_REQUIRED")
            except ResourceNotVisible:
                return _problem_response(status.HTTP_404_NOT_FOUND, "RESOURCE_NOT_FOUND")
            except AuditSinkUnavailable:
                return _problem_response(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "AUDIT_SINK_UNAVAILABLE",
                )
            except SyntheticResourceIntegrityError:
                return _problem_response(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "SYNTHETIC_RESOURCE_UNAVAILABLE",
                )
            except InternalReadBackendUnavailable:
                return _problem_response(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "INTERNAL_READ_UNAVAILABLE",
                )

        return route_handler


class InternalReadNoStoreMiddleware:
    """Prevent caching even when Starlette rejects a method/path before route dispatch."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        if (
            scope.get("type") != "http"
            or not isinstance(path, str)
            or not (path == "/internal/v1" or path.startswith("/internal/v1/"))
        ):
            await self.app(scope, receive, send)
            return

        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_no_store)


def require_internal_read_api(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """First route dependency: disabled and production modes are indistinguishable."""

    if settings.env == "production" or not settings.enable_internal_read_api:
        raise InternalReadProblem(status.HTTP_404_NOT_FOUND, "INTERNAL_READ_DISABLED")


def get_synthetic_internal_read_service(
    settings: Annotated[Settings, Depends(get_settings)],
    receipt_sink: Annotated[
        InternalReadReceiptSink | None, Depends(get_internal_read_receipt_sink)
    ],
) -> SyntheticInternalReadService | DatabaseInternalReadService:
    if settings.internal_read_backend == "database":
        cursor_key = settings.internal_read_cursor_key
        if cursor_key is None:
            raise InternalReadBackendUnavailable("signed cursor key is unavailable")
        return DatabaseInternalReadService(
            get_session_factory(settings.resolved_reader_database_url()),
            ReadCursorSigner(cursor_key),
            receipt_sink=receipt_sink,
        )
    return SyntheticInternalReadService()


@dataclass(frozen=True, slots=True)
class _CandidateListParams:
    month: str | None
    status: CandidateStatus | None
    business_unit: str | None
    cursor: str | None


@dataclass(frozen=True, slots=True)
class _ReconciliationParams:
    month: str
    entity_ref: UUID
    business_unit: str


@dataclass(frozen=True, slots=True)
class _LedgerParams:
    entity_ref: UUID
    business_unit: str
    from_month: str
    to_month: str


def _validate_no_query(request: Request) -> None:
    _closed_query(request, allowed=frozenset())


def _parse_candidate_list_params(request: Request) -> _CandidateListParams:
    query = _closed_query(
        request,
        allowed=frozenset({"month", "status", "business_unit", "cursor"}),
    )
    cursor = query.get("cursor")
    if cursor is not None and not 1 <= len(cursor) <= 512:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return _CandidateListParams(
        month=_optional_month(query, "month"),
        status=_optional_candidate_status(query, "status"),
        business_unit=_optional_business_unit(query, "business_unit"),
        cursor=cursor,
    )


def _parse_closed_resource_uuid(id: str, request: Request) -> UUID:
    _closed_query(request, allowed=frozenset())
    return _parse_resource_uuid(id)


def _parse_reconciliation_params(month: str, request: Request) -> _ReconciliationParams:
    query = _closed_query(
        request,
        allowed=frozenset({"entity_ref", "business_unit"}),
        required=frozenset({"entity_ref", "business_unit"}),
    )
    return _ReconciliationParams(
        month=_parse_resource_month(month),
        entity_ref=_parse_uuid(query["entity_ref"]),
        business_unit=_parse_business_unit(query["business_unit"]),
    )


def _parse_ledger_params(request: Request) -> _LedgerParams:
    query = _closed_query(
        request,
        allowed=frozenset({"entity_ref", "business_unit", "from_month", "to_month"}),
        required=frozenset({"entity_ref", "business_unit", "from_month", "to_month"}),
    )
    from_month = _parse_month(query["from_month"])
    to_month = _parse_month(query["to_month"])
    if from_month > to_month:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return _LedgerParams(
        entity_ref=_parse_uuid(query["entity_ref"]),
        business_unit=_parse_business_unit(query["business_unit"]),
        from_month=from_month,
        to_month=to_month,
    )


VerifiedPrincipal = Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)]


def require_system_read(principal: VerifiedPrincipal) -> WorkloadPrincipal:
    require_capability(principal, Capability.SYSTEM_READ)
    return principal


def require_candidate_read(principal: VerifiedPrincipal) -> WorkloadPrincipal:
    require_capability(principal, Capability.CANDIDATE_READ)
    return principal


def require_evidence_read(principal: VerifiedPrincipal) -> WorkloadPrincipal:
    require_capability(principal, Capability.EVIDENCE_READ)
    return principal


def require_reconciliation_read(principal: VerifiedPrincipal) -> WorkloadPrincipal:
    require_capability(principal, Capability.RECONCILIATION_READ)
    return principal


def require_ledger_read(principal: VerifiedPrincipal) -> WorkloadPrincipal:
    require_capability(principal, Capability.LEDGER_READ)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-read"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)

SystemPrincipal = Annotated[WorkloadPrincipal, Depends(require_system_read)]
CandidatePrincipal = Annotated[WorkloadPrincipal, Depends(require_candidate_read)]
EvidencePrincipal = Annotated[WorkloadPrincipal, Depends(require_evidence_read)]
ReconciliationPrincipal = Annotated[
    WorkloadPrincipal,
    Depends(require_reconciliation_read),
]
LedgerPrincipal = Annotated[WorkloadPrincipal, Depends(require_ledger_read)]
NoQuery = Annotated[None, Depends(_validate_no_query)]
CandidateListParams = Annotated[_CandidateListParams, Depends(_parse_candidate_list_params)]
ResourceRef = Annotated[UUID, Depends(_parse_closed_resource_uuid)]
ReconciliationParams = Annotated[_ReconciliationParams, Depends(_parse_reconciliation_params)]
LedgerParams = Annotated[_LedgerParams, Depends(_parse_ledger_params)]
Service = Annotated[
    SyntheticInternalReadService | DatabaseInternalReadService,
    Depends(get_synthetic_internal_read_service),
]
AuditSink = Annotated[InternalReadAuditSink, Depends(get_internal_read_audit_sink)]


@router.get("/capabilities", response_model=CapabilitiesResponse)
def get_capabilities(
    principal: SystemPrincipal,
    _validated: NoQuery,
    service: Service,
) -> CapabilitiesResponse:
    return service.capabilities(principal)


@router.get("/candidates", response_model=CandidatePage)
def list_candidates(
    principal: CandidatePrincipal,
    params: CandidateListParams,
    service: Service,
) -> CandidatePage:
    authorize_collection_read(principal, Capability.CANDIDATE_READ)
    if params.cursor is not None and not isinstance(service, DatabaseInternalReadService):
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return service.list_candidates(
        principal,
        month=params.month,
        status=params.status,
        business_unit=params.business_unit,
        cursor=params.cursor,
    )


@router.get("/candidates/{id}", response_model=CandidateProjection)
def get_candidate(
    principal: CandidatePrincipal,
    candidate_ref: ResourceRef,
    service: Service,
) -> CandidateProjection:
    return service.get_candidate(principal, candidate_ref)


@router.get("/evidence/{id}/content", response_class=Response)
def download_evidence(
    principal: EvidencePrincipal,
    evidence_ref: ResourceRef,
    service: Service,
    audit_sink: AuditSink,
) -> Response:
    evidence = service.get_evidence(principal, evidence_ref)

    content = evidence.content
    digest = hashlib.sha256(content).digest()
    digest_hex = digest.hex()
    if (
        evidence.media_type != "application/octet-stream"
        or evidence.byte_size != len(content)
        or evidence.sha256 != digest_hex
        or not _safe_filename(evidence.filename)
    ):
        raise InternalReadProblem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "EVIDENCE_INTEGRITY_UNAVAILABLE",
        )

    event = EvidenceReadAuditEvent(
        principal_ref=principal.principal_ref,
        principal_san_uri=principal.san_uri,
        policy_generation=principal.policy_generation,
        evidence_ref=evidence_ref,
        entity_ref=evidence.entity_ref,
        business_unit_ref=evidence.business_unit_ref,
        byte_size=len(content),
        sha256=digest_hex,
    )
    try:
        audit_sink.append(event)
    except AuditSinkUnavailable:
        raise
    except Exception as exc:
        raise AuditSinkUnavailable("internal read audit append failed") from exc

    digest_header = base64.b64encode(digest).decode("ascii")
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{evidence.filename}"',
            "Content-Digest": f"sha-256=:{digest_header}:",
        },
    )


@router.get("/reconciliations/{month}", response_model=ReconciliationProjection)
def get_reconciliation(
    principal: ReconciliationPrincipal,
    params: ReconciliationParams,
    service: Service,
) -> ReconciliationProjection:
    authorize_read(
        principal,
        Capability.RECONCILIATION_READ,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit,
    )
    return service.get_reconciliation(
        principal,
        month=params.month,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit,
    )


@router.get("/ledger-summary", response_model=LedgerSummary)
def get_ledger_summary(
    principal: LedgerPrincipal,
    params: LedgerParams,
    service: Service,
) -> LedgerSummary:
    authorize_read(
        principal,
        Capability.LEDGER_READ,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit,
    )
    return service.get_ledger_summary(
        principal,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit,
        from_month=params.from_month,
        to_month=params.to_month,
    )


def _closed_query(
    request: Request,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> Mapping[str, str]:
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in values:
            raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
        values[key] = value
    if not required.issubset(values):
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return values


def _parse_month(value: str) -> str:
    if _MONTH.fullmatch(value) is None:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return value


def _optional_month(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    return None if value is None else _parse_month(value)


def _optional_candidate_status(
    values: Mapping[str, str],
    key: str,
) -> CandidateStatus | None:
    value = values.get(key)
    if value is None:
        return None
    try:
        return CandidateStatus(value)
    except ValueError as exc:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY") from exc


def _parse_business_unit(value: str) -> str:
    if not value or len(value) > 100 or value.strip() != value or contains_unstorable_text(value):
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return value


def _optional_business_unit(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key)
    return None if value is None else _parse_business_unit(value)


def _parse_uuid(value: str) -> UUID:
    if _UUID.fullmatch(value) is None:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    try:
        return UUID(value)
    except ValueError as exc:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY") from exc


def _parse_resource_uuid(value: str) -> UUID:
    if _UUID.fullmatch(value) is None:
        raise ResourceNotVisible("resource was not found")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ResourceNotVisible("resource was not found") from exc


def _parse_resource_month(value: str) -> str:
    if _MONTH.fullmatch(value) is None:
        raise ResourceNotVisible("resource was not found")
    return value


def _safe_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 200
        and value not in {".", ".."}
        and _SAFE_FILENAME.fullmatch(value) is not None
    )
