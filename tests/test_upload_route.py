from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from ledgerbridge import main as main_module
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
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.dispatch import DispatchNotFound, DispatchSnapshot
from ledgerbridge.imports import EvidenceIngestionError, ImportOutcome, IngestMetadata
from ledgerbridge.main import (
    _declared_length,
    _map_ingestion_error,
    _read_bounded_request,
    app,
    get_artifact_store,
    get_async_dispatch_manifest,
    get_authenticated_principal,
    get_dispatch_service,
    get_evidence_importer,
    get_internal_connectors,
    require_internal_upload,
)
from ledgerbridge.models import DispatchState, ImportJobStatus


class FakeImporter:
    def __init__(self) -> None:
        self.validated_sources: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.validation_error: EvidenceIngestionError | None = None
        self.import_error: EvidenceIngestionError | None = None

    def validate_ingest_channel(self, source: str) -> None:
        self.validated_sources.append(source)
        if self.validation_error is not None:
            raise self.validation_error
        if source != "manual_upload":
            raise RuntimeError("unexpected source in route test")

    def ingest_published(
        self,
        published: PublishedArtifact,
        metadata: IngestMetadata,
        connectors: object,
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        if self.import_error is not None:
            raise self.import_error
        self.calls.append(
            {
                "published": published,
                "metadata": metadata,
                "connectors": connectors,
                "actor": actor,
                "reason": reason,
            }
        )
        return ImportOutcome(
            artifact_id=uuid4(),
            job_id=uuid4(),
            status=ImportJobStatus.SUCCEEDED,
            parsed_count=1,
            created_count=1,
            duplicate_count=0,
            error_code=None,
            artifact_created=published.created,
        )


class FakeDispatchService:
    def __init__(self) -> None:
        self.validated_sources: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.snapshot = DispatchSnapshot(
            operation_id=uuid4(),
            artifact_id=uuid4(),
            state=DispatchState.PENDING,
            job_id=None,
            result_status=None,
            error_code=None,
        )

    def validate_ingest_channel(self, source: str) -> None:
        self.validated_sources.append(source)
        if source != "manual_upload":
            raise RuntimeError("unexpected source in async route test")

    def enqueue_published(self, published: PublishedArtifact, **kwargs: object) -> DispatchSnapshot:
        self.calls.append({"published": published, **kwargs})
        return self.snapshot

    def get_for_actor(self, operation_id: object, actor: str) -> DispatchSnapshot:
        del actor
        if operation_id != self.snapshot.operation_id:
            raise DispatchNotFound("not found")
        return self.snapshot


def _settings(root: Path, *, enabled: bool = True, max_bytes: int = 1024) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        env="test",
        artifact_root=root.resolve(),
        artifact_max_bytes=max_bytes,
        artifact_total_max_bytes=8 * 1024,
        artifact_staging_max_bytes=8 * 1024,
        enable_internal_upload=enabled,
        enable_internal_async_dispatch=enabled,
    )


def _install_overrides(
    root: Path,
    *,
    enabled: bool = True,
    max_bytes: int = 1024,
    connectors: tuple[object, ...] = (object(),),
    principal: str | None = "auth-service/user-1",
) -> tuple[ArtifactStore, FakeImporter]:
    settings = _settings(root, enabled=enabled, max_bytes=max_bytes)
    store = ArtifactStore(
        root.resolve(),
        max_bytes=max_bytes,
        total_max_bytes=8 * 1024,
        staging_max_bytes=8 * 1024,
    )
    importer = FakeImporter()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_evidence_importer] = lambda: importer
    app.dependency_overrides[get_internal_connectors] = lambda: connectors
    if principal is not None:
        app.dependency_overrides[get_authenticated_principal] = lambda: principal
    return store, importer


def _install_async_overrides(
    root: Path,
    *,
    enabled: bool = True,
    principal: str | None = "auth-service/user-1",
    manifest: tuple[str, bytes] | None = ("synthetic-test", b"m" * 32),
) -> tuple[ArtifactStore, FakeDispatchService]:
    settings = _settings(root, enabled=enabled)
    store = ArtifactStore(
        root.resolve(),
        max_bytes=settings.artifact_max_bytes,
        total_max_bytes=settings.artifact_total_max_bytes,
        staging_max_bytes=settings.artifact_staging_max_bytes,
    )
    dispatch = FakeDispatchService()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[get_dispatch_service] = lambda: dispatch
    app.dependency_overrides[get_async_dispatch_manifest] = lambda: manifest
    if principal is not None:
        app.dependency_overrides[get_authenticated_principal] = lambda: principal
    return store, dispatch


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def _request(*, body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/evidence/imports",
            "headers": headers or [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "scheme": "http",
            "_body": body,
        }
    )


def test_route_dependency_factories_and_principal_are_explicit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = get_artifact_store(settings)
    importer = get_evidence_importer(settings, store)
    assert importer._store is store
    assert get_internal_connectors() == ()

    request = _request()
    request.state.authenticated_principal = "server/actor"
    assert get_authenticated_principal(request) == "server/actor"
    assert _declared_length(request) is None


def test_internal_upload_is_never_enabled_in_production(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=True).model_copy(update={"env": "production"})
    with pytest.raises(HTTPException) as disabled:
        require_internal_upload(settings)
    assert disabled.value.status_code == 404
    assert isinstance(disabled.value.detail, dict)
    assert disabled.value.detail.get("error_code") == "INTERNAL_UPLOAD_DISABLED"


def test_route_error_mapping_and_declared_length_are_bounded() -> None:
    invalid = _request(headers=[(b"content-length", b"not-a-number")])
    with pytest.raises(HTTPException) as invalid_length:
        _declared_length(invalid)
    assert invalid_length.value.status_code == 400

    assert (
        _map_ingestion_error(EvidenceIngestionError("IMPORT_DATABASE", "secret")).status_code == 500
    )
    assert (
        _map_ingestion_error(EvidenceIngestionError("ARTIFACT_QUOTA_STATE", "secret")).status_code
        == 503
    )
    assert _map_ingestion_error(EvidenceIngestionError("NO_CONNECTOR", "secret")).status_code == 422


def test_bounded_request_spool_enforces_type_and_size() -> None:
    class ByteRequest:
        async def stream(self) -> Any:
            yield b"abc"

    body = asyncio.run(_read_bounded_request(ByteRequest(), 3))  # type: ignore[arg-type]
    try:
        assert body.read() == b"abc"
    finally:
        body.close()

    class InvalidRequest:
        async def stream(self) -> Any:
            yield "not bytes"

    with pytest.raises(ValueError):
        asyncio.run(_read_bounded_request(InvalidRequest(), 3))  # type: ignore[arg-type]

    class LargeRequest:
        async def stream(self) -> Any:
            yield b"abcd"

    with pytest.raises(ValueError):
        asyncio.run(_read_bounded_request(LargeRequest(), 3))  # type: ignore[arg-type]


def test_internal_upload_route_commits_then_calls_importer_with_server_actor(
    tmp_path: Path,
) -> None:
    store, importer = _install_overrides(tmp_path)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("statement.csv", b"amount,1\n", "text/csv")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "SUCCEEDED"
    assert payload["parsed_count"] == 1
    assert importer.validated_sources == ["manual_upload"]
    assert len(importer.calls) == 1
    call = importer.calls[0]
    assert call["actor"] == "auth-service/user-1"
    assert call["reason"] == "internal evidence upload"
    assert call["metadata"].original_filename == "statement.csv"
    assert store.read_prefix(call["published"], 100) == b"amount,1\n"
    assert store.quota_snapshot().staging_bytes == 0


def test_async_dispatch_route_publishes_and_returns_202_location(tmp_path: Path) -> None:
    store, dispatch = _install_async_overrides(tmp_path)
    try:
        response = TestClient(app).post(
            "/v1/evidence/import-requests",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("statement.csv", b"amount,1\n", "text/csv")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 202
    payload = response.json()
    assert payload["operation_id"] == str(dispatch.snapshot.operation_id)
    assert payload["artifact_id"] == str(dispatch.snapshot.artifact_id)
    assert payload["status"] == "PENDING"
    assert response.headers["location"].endswith(str(dispatch.snapshot.operation_id))
    assert dispatch.validated_sources == ["manual_upload"]
    assert len(dispatch.calls) == 1
    assert dispatch.calls[0]["actor"] == "auth-service/user-1"
    assert dispatch.calls[0]["reason"] == "internal async evidence import request"
    assert store.quota_snapshot().staging_bytes == 0


def test_async_dispatch_requires_manifest_before_reading_body(tmp_path: Path) -> None:
    store, dispatch = _install_async_overrides(tmp_path, manifest=None)
    try:
        response = TestClient(app).post(
            "/v1/evidence/import-requests",
            content=b"body-not-read",
            headers={"content-type": "multipart/form-data; boundary=unused"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json() == {"detail": {"error_code": "CONNECTOR_MANIFEST_UNAVAILABLE"}}
    assert dispatch.calls == []
    assert not (tmp_path / ".staging").exists()
    del store


def test_async_dispatch_status_is_principal_scoped(tmp_path: Path) -> None:
    _store, dispatch = _install_async_overrides(tmp_path)
    try:
        response = TestClient(app).get(
            f"/v1/evidence/import-requests/{dispatch.snapshot.operation_id}"
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"


def test_async_dispatch_is_disabled_before_authentication(tmp_path: Path) -> None:
    _install_async_overrides(tmp_path, enabled=False, principal=None)
    try:
        response = TestClient(app).post(
            "/v1/evidence/import-requests",
            content=b"not-read",
        )
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json() == {"detail": {"error_code": "ASYNC_DISPATCH_DISABLED"}}


def test_internal_upload_is_disabled_by_default_and_before_auth(tmp_path: Path) -> None:
    _install_overrides(tmp_path, enabled=False, principal=None)
    try:
        response = TestClient(app).post("/v1/evidence/imports", content=b"not-read")
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json() == {"detail": {"error_code": "INTERNAL_UPLOAD_DISABLED"}}


def test_internal_upload_requires_server_authentication_not_a_client_actor(tmp_path: Path) -> None:
    _install_overrides(tmp_path, principal=None)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            headers={"x-ledgerbridge-actor": "client-controlled"},
            content=b"not-read",
        )
    finally:
        _clear_overrides()

    assert response.status_code == 401
    assert response.json() == {"detail": {"error_code": "AUTH_REQUIRED"}}


def test_internal_upload_fails_closed_without_connector_manifest(tmp_path: Path) -> None:
    store, importer = _install_overrides(tmp_path, connectors=())
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            content=b"body-not-read",
            headers={"content-type": "multipart/form-data; boundary=not-used"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json() == {"detail": {"error_code": "CONNECTOR_REGISTRY_UNAVAILABLE"}}
    assert importer.calls == []
    assert not (tmp_path / ".staging").exists()
    del store


def test_internal_upload_malformed_trailing_boundary_leaves_no_artifact(tmp_path: Path) -> None:
    store, importer = _install_overrides(tmp_path)
    boundary = "route-boundary"
    body = (
        b'--route-boundary\r\nContent-Disposition: form-data; name="ingest_channel"\r\n\r\n'
        b"manual_upload\r\n"
        b'--route-boundary\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        b"trusted bytes\r\n--route-boundaryXY"
    )
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert response.json() == {"detail": {"error_code": "INVALID_MULTIPART"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0
    assert not (tmp_path / "sha256").exists()


def test_internal_upload_file_limit_maps_to_413_and_aborts_handoff(tmp_path: Path) -> None:
    store, importer = _install_overrides(tmp_path, max_bytes=3)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("too-large.txt", b"four", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 413
    assert response.json() == {"detail": {"error_code": "EVIDENCE_LIMIT"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0
    assert not (tmp_path / "sha256").exists()


def test_internal_upload_rejects_advertised_body_over_limit_before_reading(
    tmp_path: Path,
) -> None:
    _install_overrides(tmp_path, max_bytes=3)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            content=b"",
            headers={
                "content-type": "multipart/form-data; boundary=unused",
                "content-length": "999999",
            },
        )
    finally:
        _clear_overrides()

    assert response.status_code == 413
    assert response.json() == {"detail": {"error_code": "EVIDENCE_LIMIT"}}


def test_internal_upload_rejects_metadata_that_exceeds_importer_bound(tmp_path: Path) -> None:
    store, importer = _install_overrides(tmp_path)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", f"a/{'b' * 199}")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {"detail": {"error_code": "INVALID_METADATA"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0


def test_internal_upload_maps_unknown_channel_before_publication(tmp_path: Path) -> None:
    store, importer = _install_overrides(tmp_path)
    importer.validation_error = EvidenceIngestionError("INGEST_CHANNEL_UNKNOWN", "bounded")
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {"detail": {"error_code": "INGEST_CHANNEL_UNKNOWN"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0


def test_internal_upload_requires_parser_completion_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, importer = _install_overrides(tmp_path)

    def incomplete_parser(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        from ledgerbridge.upload import MultipartField, MultipartFileStart

        yield MultipartField("ingest_channel", "manual_upload")
        yield MultipartFileStart("x.txt", "text/plain")

    monkeypatch.setattr(main_module, "parse_multipart", incomplete_parser)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert response.json() == {"detail": {"error_code": "INVALID_MULTIPART"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0


@pytest.mark.parametrize(
    ("error", "status_code", "error_code"),
    [
        (ArtifactTooLargeError("bounded"), 413, "EVIDENCE_LIMIT"),
        (
            ArtifactStagingQuotaError("bounded", limit=3, observed=3, requested=1),
            413,
            "ARTIFACT_STAGING_QUOTA",
        ),
        (
            ArtifactPublishedQuotaError("bounded", limit=3, observed=3, requested=1),
            507,
            "ARTIFACT_TOTAL_QUOTA",
        ),
        (ArtifactQuotaStateError("bounded"), 503, "ARTIFACT_QUOTA_STATE"),
        (ArtifactIntegrityError("bounded"), 500, "EVIDENCE_INTEGRITY"),
        (ArtifactStoreError("bounded"), 500, "EVIDENCE_STORAGE"),
        (OSError("bounded"), 500, "EVIDENCE_STORAGE"),
    ],
)
def test_internal_upload_maps_handoff_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    status_code: int,
    error_code: str,
) -> None:
    store, importer = _install_overrides(tmp_path)

    def fail_begin_handoff(self: ArtifactStore) -> Any:
        del self
        raise error

    monkeypatch.setattr(ArtifactStore, "begin_handoff", fail_begin_handoff)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == status_code
    assert response.json() == {"detail": {"error_code": error_code}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0


def test_internal_upload_maps_database_failure_during_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, importer = _install_overrides(tmp_path)

    def fail_begin_handoff(self: ArtifactStore) -> Any:
        del self
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError("bounded")

    monkeypatch.setattr(ArtifactStore, "begin_handoff", fail_begin_handoff)
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json() == {"detail": {"error_code": "IMPORT_DATABASE"}}
    assert importer.calls == []
    assert store.quota_snapshot().staging_bytes == 0


def test_internal_upload_maps_importer_failure_without_raw_summary(tmp_path: Path) -> None:
    _store, importer = _install_overrides(tmp_path)
    importer.import_error = EvidenceIngestionError("NO_CONNECTOR", "do not expose")
    try:
        response = TestClient(app).post(
            "/v1/evidence/imports",
            data={"ingest_channel": "manual_upload"},
            files={"file": ("x.txt", b"ok", "text/plain")},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {"detail": {"error_code": "NO_CONNECTOR"}}
    assert "do not expose" not in response.text
