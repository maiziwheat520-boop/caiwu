import asyncio
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, BinaryIO, Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge import __version__
from ledgerbridge.artifacts import (
    ArtifactIntegrityError,
    ArtifactPublishedQuotaError,
    ArtifactQuotaStateError,
    ArtifactStagingQuotaError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTooLargeError,
    PublishedArtifact,
)
from ledgerbridge.auth import (
    EVIDENCE_WRITE,
    AuthenticatedPrincipal,
    authorize_principal,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.connectors import Connector
from ledgerbridge.db import get_session, get_session_factory
from ledgerbridge.dispatch import (
    DispatchConflict,
    DispatchError,
    DispatchNotFound,
    DispatchService,
)
from ledgerbridge.imports import EvidenceImporter, EvidenceIngestionError, IngestMetadata
from ledgerbridge.internal_candidate_command_routes import (
    router as internal_candidate_command_router,
)
from ledgerbridge.internal_evidence_unlock_routes import (
    router as internal_evidence_unlock_router,
)
from ledgerbridge.internal_payroll_routes import router as internal_payroll_router
from ledgerbridge.internal_read_auth import VerifiedInternalReadPrincipalMiddleware
from ledgerbridge.internal_read_routes import (
    InternalReadNoStoreMiddleware,
)
from ledgerbridge.internal_read_routes import (
    router as internal_read_router,
)
from ledgerbridge.models import DispatchState, ImportJobStatus, ReviewItemKind
from ledgerbridge.production_mtls import verify_configured_mtls_principal
from ledgerbridge.review_service import ReviewConflict, ReviewNotFound, ReviewService
from ledgerbridge.secure_spool import EncryptedSpool
from ledgerbridge.text import contains_unstorable_text
from ledgerbridge.upload import (
    MAX_MULTIPART_FIELD_BYTES,
    MAX_MULTIPART_HEADER_BYTES,
    MultipartComplete,
    MultipartError,
    MultipartField,
    MultipartFileChunk,
    MultipartFileStart,
    MultipartLimitError,
    parse_multipart,
)

app = FastAPI(
    title="LedgerBridge API",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(InternalReadNoStoreMiddleware)
app.add_middleware(
    VerifiedInternalReadPrincipalMiddleware,
    verifier=lambda scope: verify_configured_mtls_principal(scope, get_settings()),
)
app.include_router(internal_read_router)
app.include_router(internal_candidate_command_router)
app.include_router(internal_evidence_unlock_router)
app.include_router(internal_payroll_router)


class UploadReadTimeoutError(TimeoutError):
    """The request body did not arrive within the configured read deadline."""


class UploadConcurrencyError(RuntimeError):
    """The bounded upload-body admission pool is currently full."""


class _UploadAdmission:
    def __init__(self, limit: int) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)

    def acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


class _AdmittedBody:
    """A temporary request body that releases its loop-independent admission slot."""

    def __init__(self, body: BinaryIO, release: Callable[[], None]) -> None:
        self._body = body
        self._release = release
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._body.seek(offset, whence)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._body.close()
        finally:
            self._release()

    @property
    def closed(self) -> bool:
        return self._closed


_UPLOAD_ADMISSIONS: dict[int, _UploadAdmission] = {}
_UPLOAD_ADMISSIONS_LOCK = threading.Lock()


def _get_upload_admission(limit: int) -> _UploadAdmission:
    if limit <= 0:
        raise ValueError("upload concurrency must be positive")
    with _UPLOAD_ADMISSIONS_LOCK:
        admission = _UPLOAD_ADMISSIONS.get(limit)
        if admission is None:
            admission = _UploadAdmission(limit)
            _UPLOAD_ADMISSIONS[limit] = admission
        return admission


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    return {"status": "ok"}


DatabaseSession = Annotated[Session, Depends(get_session)]


@app.get("/health/ready", tags=["health"])
def readiness(session: DatabaseSession) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    return {"status": "ready"}


class InternalUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    job_id: UUID
    status: ImportJobStatus
    parsed_count: int
    created_count: int
    duplicate_count: int
    error_code: str | None


class AsyncDispatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    artifact_id: UUID
    status: DispatchState


class AsyncDispatchStatusResponse(AsyncDispatchResponse):
    job_id: UUID | None
    result_status: ImportJobStatus | None
    error_code: str | None


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    kind: ReviewItemKind
    status: str
    source_record_id: UUID | None
    summary: str
    payload: dict[str, object]
    candidate_key: str | None
    created_at: datetime
    decided_at: datetime | None
    decision_actor: str | None
    decision_reason: str | None


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["RESOLVED", "REJECTED"]
    reason: str
    resolution_account_id: UUID | None = None


def get_artifact_store(settings: Annotated[Settings, Depends(get_settings)]) -> ArtifactStore:
    return ArtifactStore(
        settings.artifact_root,
        max_bytes=settings.artifact_max_bytes,
        total_max_bytes=settings.artifact_total_max_bytes,
        staging_max_bytes=settings.artifact_staging_max_bytes,
        staging_ttl_seconds=settings.artifact_staging_ttl_seconds,
    )


def get_evidence_importer(
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
) -> EvidenceImporter:
    return EvidenceImporter(
        get_session_factory(settings.resolved_api_database_url()),
        store,
        production=settings.env == "production",
    )


def get_dispatch_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DispatchService:
    return DispatchService(
        get_session_factory(settings.resolved_api_database_url()),
        lease_seconds=settings.dispatch_lease_seconds,
        max_attempts=settings.dispatch_max_attempts,
    )


def get_review_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewService:
    return ReviewService(get_session_factory(settings.resolved_api_database_url()))


def get_internal_connectors() -> Sequence[Connector]:
    """Return the reviewed internal connector manifest, empty until one exists."""

    return ()


def require_internal_upload(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if settings.env == "production" or not settings.enable_internal_upload:
        raise _route_error("INTERNAL_UPLOAD_DISABLED", status.HTTP_404_NOT_FOUND)


def require_internal_async_dispatch(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if settings.env == "production" or not settings.enable_internal_async_dispatch:
        raise _route_error("ASYNC_DISPATCH_DISABLED", status.HTTP_404_NOT_FOUND)


def require_review_api(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Keep Review operations explicitly disabled until their auth gate is enabled."""

    if settings.env == "production" or not settings.enable_review_api:
        raise _route_error("REVIEW_API_DISABLED", status.HTTP_404_NOT_FOUND)


def get_async_dispatch_manifest() -> tuple[str, bytes] | None:
    """Return the verified manifest identity; empty until a reviewed loader exists."""

    return None


def get_authenticated_principal(
    request: Request,
    settings: Annotated[Settings | None, Depends(get_settings)] = None,
) -> str:
    """Read a verifier-owned principal, never a client-supplied identity header."""

    principal = getattr(request.state, "authenticated_principal", None)
    if isinstance(principal, AuthenticatedPrincipal):
        if not authorize_principal(
            principal,
            EVIDENCE_WRITE,
            expected_policy_generation=(
                getattr(settings, "auth_policy_generation", None) if settings is not None else None
            ),
            clock_skew_seconds=(
                getattr(settings, "auth_clock_skew_seconds", 30) if settings is not None else 30
            ),
        ):
            raise _route_error("AUTH_REQUIRED", status.HTTP_401_UNAUTHORIZED)
        return principal.actor
    if getattr(settings, "auth_provider", "disabled") == "trusted_gateway":
        raise _route_error("AUTH_REQUIRED", status.HTTP_401_UNAUTHORIZED)
    if (
        not isinstance(principal, str)
        or not principal
        or len(principal) > 200
        or contains_unstorable_text(principal)
    ):
        raise _route_error("AUTH_REQUIRED", status.HTTP_401_UNAUTHORIZED)
    return principal


@app.post(
    "/v1/evidence/imports",
    response_model=InternalUploadResponse,
    response_model_exclude_none=False,
    tags=["internal-evidence"],
    dependencies=[Depends(require_internal_upload)],
)
async def upload_evidence(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[str, Depends(get_authenticated_principal)],
    connectors: Annotated[Sequence[Connector], Depends(get_internal_connectors)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    importer: Annotated[EvidenceImporter, Depends(get_evidence_importer)],
) -> InternalUploadResponse:
    if not connectors:
        raise _route_error(
            "CONNECTOR_REGISTRY_UNAVAILABLE",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    declared_length = _declared_length(request)
    max_body_bytes = _max_body_bytes(settings.artifact_max_bytes)
    if declared_length is not None and declared_length > max_body_bytes:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        request_body = await _read_bounded_request(
            request,
            max_body_bytes,
            timeout_seconds=settings.upload_read_timeout_seconds,
            concurrency=settings.upload_concurrency,
        )
        try:
            handoff = store.begin_handoff()
            try:
                channel: str | None = None
                filename: str | None = None
                media_type: str | None = None
                parser_complete = False
                for event in parse_multipart(
                    iter(lambda: request_body.read(64 * 1024), b""),
                    request.headers.get("content-type", ""),
                    max_file_bytes=settings.artifact_max_bytes,
                    max_body_bytes=max_body_bytes,
                    declared_length=declared_length,
                ):
                    if isinstance(event, MultipartField):
                        channel = event.value
                    elif isinstance(event, MultipartFileStart):
                        filename = event.filename
                        media_type = event.media_type
                    elif isinstance(event, MultipartFileChunk):
                        handoff.write(event.data)
                    elif isinstance(event, MultipartComplete):
                        parser_complete = True
                if channel is None or filename is None or media_type is None or not parser_complete:
                    raise MultipartError("multipart body is incomplete")
                importer.validate_ingest_channel(channel)
                metadata = IngestMetadata(
                    source=channel,
                    original_filename=filename,
                    media_type=media_type,
                )
                published = handoff.complete(parser_complete=True)
            except BaseException:
                handoff.abort()
                raise
        finally:
            request_body.close()
    except UploadReadTimeoutError as exc:
        raise _route_error("EVIDENCE_READ_TIMEOUT", status.HTTP_408_REQUEST_TIMEOUT) from exc
    except UploadConcurrencyError as exc:
        raise _route_error("EVIDENCE_UPLOAD_BUSY", status.HTTP_429_TOO_MANY_REQUESTS) from exc
    except MultipartLimitError as exc:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except MultipartError as exc:
        raise _route_error("INVALID_MULTIPART", status.HTTP_400_BAD_REQUEST) from exc
    except ValueError as exc:
        raise _route_error("INVALID_METADATA", status.HTTP_422_UNPROCESSABLE_CONTENT) from exc
    except ArtifactTooLargeError as exc:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except ArtifactStagingQuotaError as exc:
        raise _route_error("ARTIFACT_STAGING_QUOTA", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except ArtifactPublishedQuotaError as exc:
        raise _route_error("ARTIFACT_TOTAL_QUOTA", status.HTTP_507_INSUFFICIENT_STORAGE) from exc
    except ArtifactQuotaStateError as exc:
        raise _route_error("ARTIFACT_QUOTA_STATE", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except ArtifactIntegrityError as exc:
        raise _route_error("EVIDENCE_INTEGRITY", status.HTTP_500_INTERNAL_SERVER_ERROR) from exc
    except (ArtifactStoreError, OSError) as exc:
        raise _route_error("EVIDENCE_STORAGE", status.HTTP_500_INTERNAL_SERVER_ERROR) from exc
    except EvidenceIngestionError as exc:
        raise _map_ingestion_error(exc) from exc
    except SQLAlchemyError as exc:
        raise _route_error("IMPORT_DATABASE", status.HTTP_503_SERVICE_UNAVAILABLE) from exc

    try:
        outcome = importer.ingest_published(
            published,
            metadata,
            connectors,
            actor=principal,
            reason="internal evidence upload",
        )
    except EvidenceIngestionError as exc:
        raise _map_ingestion_error(exc) from exc
    return InternalUploadResponse(
        artifact_id=outcome.artifact_id,
        job_id=outcome.job_id,
        status=outcome.status,
        parsed_count=outcome.parsed_count,
        created_count=outcome.created_count,
        duplicate_count=outcome.duplicate_count,
        error_code=outcome.error_code,
    )


@app.post(
    "/v1/evidence/import-requests",
    response_model=AsyncDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=False,
    tags=["internal-evidence"],
    dependencies=[Depends(require_internal_async_dispatch)],
)
async def enqueue_evidence_import(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[str, Depends(get_authenticated_principal)],
    manifest: Annotated[tuple[str, bytes] | None, Depends(get_async_dispatch_manifest)],
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    dispatch: Annotated[DispatchService, Depends(get_dispatch_service)],
) -> AsyncDispatchResponse:
    if manifest is None or len(manifest[1]) != 32:
        raise _route_error(
            "CONNECTOR_MANIFEST_UNAVAILABLE",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    manifest_generation, manifest_digest = manifest
    declared_length = _declared_length(request)
    max_body_bytes = _max_body_bytes(settings.artifact_max_bytes)
    if declared_length is not None and declared_length > max_body_bytes:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE)
    try:
        published, channel, filename, media_type = await _receive_handoff(
            request,
            settings=settings,
            store=store,
            declared_length=declared_length,
            max_body_bytes=max_body_bytes,
            dispatch=dispatch,
        )
        snapshot = dispatch.enqueue_published(
            published,
            ingest_channel=channel,
            original_filename=filename,
            media_type=media_type,
            manifest_generation=manifest_generation,
            manifest_digest=manifest_digest,
            actor=principal,
            reason="internal async evidence import request",
        )
    except UploadReadTimeoutError as exc:
        raise _route_error("EVIDENCE_READ_TIMEOUT", status.HTTP_408_REQUEST_TIMEOUT) from exc
    except UploadConcurrencyError as exc:
        raise _route_error("EVIDENCE_UPLOAD_BUSY", status.HTTP_429_TOO_MANY_REQUESTS) from exc
    except MultipartLimitError as exc:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except MultipartError as exc:
        raise _route_error("INVALID_MULTIPART", status.HTTP_400_BAD_REQUEST) from exc
    except DispatchConflict as exc:
        raise _route_error("DISPATCH_CONFLICT", status.HTTP_409_CONFLICT) from exc
    except DispatchError as exc:
        raise _route_error("DISPATCH_UNAVAILABLE", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except ValueError as exc:
        raise _route_error("INVALID_METADATA", status.HTTP_422_UNPROCESSABLE_CONTENT) from exc
    except ArtifactTooLargeError as exc:
        raise _route_error("EVIDENCE_LIMIT", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except ArtifactStagingQuotaError as exc:
        raise _route_error("ARTIFACT_STAGING_QUOTA", status.HTTP_413_CONTENT_TOO_LARGE) from exc
    except ArtifactPublishedQuotaError as exc:
        raise _route_error("ARTIFACT_TOTAL_QUOTA", status.HTTP_507_INSUFFICIENT_STORAGE) from exc
    except ArtifactQuotaStateError as exc:
        raise _route_error("ARTIFACT_QUOTA_STATE", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except ArtifactIntegrityError as exc:
        raise _route_error("EVIDENCE_INTEGRITY", status.HTTP_500_INTERNAL_SERVER_ERROR) from exc
    except (ArtifactStoreError, OSError) as exc:
        raise _route_error("EVIDENCE_STORAGE", status.HTTP_500_INTERNAL_SERVER_ERROR) from exc
    except SQLAlchemyError as exc:
        raise _route_error("IMPORT_DATABASE", status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    response.headers["Location"] = f"/v1/evidence/import-requests/{snapshot.operation_id}"
    return AsyncDispatchResponse(
        operation_id=snapshot.operation_id,
        artifact_id=snapshot.artifact_id,
        status=snapshot.state,
    )


@app.get(
    "/v1/evidence/import-requests/{operation_id}",
    response_model=AsyncDispatchStatusResponse,
    response_model_exclude_none=False,
    tags=["internal-evidence"],
    dependencies=[Depends(require_internal_async_dispatch)],
)
def get_evidence_import_status(
    operation_id: UUID,
    principal: Annotated[str, Depends(get_authenticated_principal)],
    dispatch: Annotated[DispatchService, Depends(get_dispatch_service)],
) -> AsyncDispatchStatusResponse:
    try:
        snapshot = dispatch.get_for_actor(operation_id, principal)
    except DispatchNotFound as exc:
        raise _route_error("OPERATION_NOT_FOUND", status.HTTP_404_NOT_FOUND) from exc
    return AsyncDispatchStatusResponse(
        operation_id=snapshot.operation_id,
        artifact_id=snapshot.artifact_id,
        status=snapshot.state,
        job_id=snapshot.job_id,
        result_status=snapshot.result_status,
        error_code=snapshot.error_code,
    )


@app.get(
    "/v1/reviews",
    response_model=list[ReviewResponse],
    tags=["review"],
    dependencies=[Depends(require_review_api)],
)
def list_reviews(
    principal: Annotated[str, Depends(get_authenticated_principal)],
    review_service: Annotated[ReviewService, Depends(get_review_service)],
    review_status: str | None = None,
    kind: ReviewItemKind | None = None,
) -> list[ReviewResponse]:
    _ = principal
    items = review_service.list_items(
        status=review_status,
        kind=kind.value if kind is not None else None,
    )
    return [ReviewResponse.model_validate(item) for item in items]


@app.post(
    "/v1/reviews/{review_id}/decision",
    response_model=ReviewResponse,
    tags=["review"],
    dependencies=[Depends(require_review_api)],
)
def decide_review(
    review_id: UUID,
    body: ReviewDecisionRequest,
    principal: Annotated[str, Depends(get_authenticated_principal)],
    review_service: Annotated[ReviewService, Depends(get_review_service)],
) -> ReviewResponse:
    try:
        item = review_service.decide(
            review_id,
            actor=principal,
            decision=body.decision,
            reason=body.reason,
            resolution_account_id=body.resolution_account_id,
        )
    except ReviewNotFound as exc:
        raise _route_error("REVIEW_NOT_FOUND", status.HTTP_404_NOT_FOUND) from exc
    except ReviewConflict as exc:
        raise _route_error("REVIEW_CONFLICT", status.HTTP_409_CONFLICT) from exc
    except ValueError as exc:
        raise _route_error(
            "INVALID_REVIEW_DECISION", status.HTTP_422_UNPROCESSABLE_CONTENT
        ) from exc
    return ReviewResponse.model_validate(item)


async def _receive_handoff(
    request: Request,
    *,
    settings: Settings,
    store: ArtifactStore,
    declared_length: int | None,
    max_body_bytes: int,
    dispatch: DispatchService,
) -> tuple[PublishedArtifact, str, str, str]:
    request_body = await _read_bounded_request(
        request,
        max_body_bytes,
        timeout_seconds=settings.upload_read_timeout_seconds,
        concurrency=settings.upload_concurrency,
    )
    channel: str | None = None
    filename: str | None = None
    media_type: str | None = None
    try:
        handoff = store.begin_handoff()
        try:
            parser_complete = False
            for event in parse_multipart(
                iter(lambda: request_body.read(64 * 1024), b""),
                request.headers.get("content-type", ""),
                max_file_bytes=settings.artifact_max_bytes,
                max_body_bytes=max_body_bytes,
                declared_length=declared_length,
            ):
                if isinstance(event, MultipartField):
                    channel = event.value
                elif isinstance(event, MultipartFileStart):
                    filename = event.filename
                    media_type = event.media_type
                elif isinstance(event, MultipartFileChunk):
                    handoff.write(event.data)
                elif isinstance(event, MultipartComplete):
                    parser_complete = True
            if channel is None or filename is None or media_type is None or not parser_complete:
                raise MultipartError("multipart body is incomplete")
            dispatch.validate_ingest_channel(channel)
            published = handoff.complete(parser_complete=True)
        except BaseException:
            handoff.abort()
            raise
    finally:
        request_body.close()
    if channel is None or filename is None or media_type is None:
        raise MultipartError("multipart metadata is incomplete")
    return published, channel, filename, media_type


async def _read_bounded_request(
    request: Request,
    maximum: int,
    *,
    timeout_seconds: float = 120.0,
    concurrency: int = 2,
) -> BinaryIO:
    admission = _get_upload_admission(concurrency)
    if not admission.acquire():
        raise UploadConcurrencyError("upload body admission pool is full")
    body: EncryptedSpool | None = None
    total = 0
    try:
        body = EncryptedSpool()
        try:
            async with asyncio.timeout(timeout_seconds):
                async for chunk in request.stream():
                    if not isinstance(chunk, bytes):
                        raise MultipartError("multipart chunks must be bytes")
                    total += len(chunk)
                    if total > maximum:
                        raise MultipartLimitError("multipart body exceeds its configured limit")
                    if chunk:
                        body.write(chunk)
        except TimeoutError as exc:
            raise UploadReadTimeoutError("multipart body read timed out") from exc
        body.seal()
        return cast(BinaryIO, _AdmittedBody(cast(BinaryIO, body), admission.release))
    except BaseException:
        try:
            if body is not None:
                body.close()
        finally:
            admission.release()
        raise


def _declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise _route_error("INVALID_MULTIPART", status.HTTP_400_BAD_REQUEST) from exc


def _max_body_bytes(max_file_bytes: int) -> int:
    return max_file_bytes + (2 * MAX_MULTIPART_HEADER_BYTES) + MAX_MULTIPART_FIELD_BYTES + 1024


def _route_error(code: str, http_status: int) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error_code": code})


def _map_ingestion_error(error: EvidenceIngestionError) -> HTTPException:
    if error.error_code == "IMPORT_DATABASE":
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    elif error.error_code.startswith("ARTIFACT_"):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _route_error(error.error_code, http_status)
