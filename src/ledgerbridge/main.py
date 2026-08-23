import tempfile
from collections.abc import Sequence
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
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
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.connectors import Connector
from ledgerbridge.db import get_session, get_session_factory
from ledgerbridge.imports import EvidenceImporter, EvidenceIngestionError, IngestMetadata
from ledgerbridge.models import ImportJobStatus
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
        get_session_factory(settings.database_url),
        store,
        production=settings.env == "production",
    )


def get_internal_connectors() -> Sequence[Connector]:
    """Return the reviewed internal connector manifest, empty until one exists."""

    return ()


def require_internal_upload(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if settings.env == "production" or not settings.enable_internal_upload:
        raise _route_error("INTERNAL_UPLOAD_DISABLED", status.HTTP_404_NOT_FOUND)


def get_authenticated_principal(request: Request) -> str:
    """Read the principal installed by trusted auth middleware, never a header."""

    principal = getattr(request.state, "authenticated_principal", None)
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
        request_body = await _read_bounded_request(request, max_body_bytes)
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


async def _read_bounded_request(request: Request, maximum: int) -> BinaryIO:
    body = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b"))  # noqa: SIM115
    total = 0
    try:
        async for chunk in request.stream():
            if not isinstance(chunk, bytes):
                raise MultipartError("multipart chunks must be bytes")
            total += len(chunk)
            if total > maximum:
                raise MultipartLimitError("multipart body exceeds its configured limit")
            if chunk:
                body.write(chunk)
        body.seek(0)
        return body
    except BaseException:
        body.close()
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
