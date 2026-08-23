import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import gettempdir
from types import FrameType
from typing import cast
from uuid import UUID

from ledgerbridge.artifacts import ArtifactStore, PublishedArtifact
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.connectors import Connector
from ledgerbridge.db import get_session_factory
from ledgerbridge.dispatch import (
    DispatchClaim,
    DispatchClaimLost,
    DispatchService,
)
from ledgerbridge.imports import EvidenceImporter, EvidenceIngestionError, IngestMetadata
from ledgerbridge.models import ImportJobStatus
from ledgerbridge.runner_composition import (
    VerifiedRunnerManifest,
    build_worker_runner_connectors,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_running = True
HEARTBEAT_PATH = Path(gettempdir()) / "ledgerbridge-worker-heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_MAX_AGE_SECONDS = 30.0
DISPATCH_RENEW_FRACTION = 3


def _stop(_signum: int, _frame: FrameType | None) -> None:
    global _running
    _running = False


def heartbeat_path() -> Path:
    return HEARTBEAT_PATH


def write_heartbeat(path: Path | None = None, now: float | None = None) -> None:
    target = path if path is not None else heartbeat_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.time() if now is None else now
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(f"{timestamp:.6f}\n", encoding="ascii")
    temporary.replace(target)


def heartbeat_is_fresh(
    path: Path | None = None,
    now: float | None = None,
    max_age_seconds: float = HEARTBEAT_MAX_AGE_SECONDS,
) -> bool:
    target = path if path is not None else heartbeat_path()
    current_time = time.time() if now is None else now
    try:
        heartbeat_time = float(target.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    age = current_time - heartbeat_time
    return 0 <= age <= max_age_seconds


def build_evidence_importer() -> EvidenceImporter:
    settings = get_settings()
    return EvidenceImporter(
        get_session_factory(settings.resolved_worker_database_url()),
        ArtifactStore(
            settings.artifact_root,
            max_bytes=settings.artifact_max_bytes,
            total_max_bytes=settings.artifact_total_max_bytes,
            staging_max_bytes=settings.artifact_staging_max_bytes,
            staging_ttl_seconds=settings.artifact_staging_ttl_seconds,
        ),
        production=settings.env == "production",
    )


def build_dispatch_service(settings: Settings | None = None) -> DispatchService:
    current = settings if settings is not None else get_settings()
    return DispatchService(
        get_session_factory(current.resolved_worker_database_url()),
        lease_seconds=current.dispatch_lease_seconds,
        max_attempts=current.dispatch_max_attempts,
    )


def build_worker_connectors(
    manifest: VerifiedRunnerManifest | None = None,
    settings: Settings | None = None,
) -> Sequence[Connector]:
    """Build worker-owned runner facades from an injected verified manifest."""

    if manifest is None:
        return ()
    current = settings if settings is not None else get_settings()
    return cast(
        Sequence[Connector],
        build_worker_runner_connectors(manifest, socket_path=current.runner_socket_path),
    )


def build_worker_manifest() -> VerifiedRunnerManifest | None:
    """Return a verified manifest; no manifest is shipped by default."""

    return None


def worker_id() -> str:
    raw = f"{socket.gethostname()}-{os.getpid()}"
    return raw[:128]


@contextmanager
def renew_dispatch_lease(
    dispatch: DispatchService,
    operation_id: UUID,
    owner: str,
    *,
    lease_seconds: int,
) -> Iterator[None]:
    stop = threading.Event()

    def renew() -> None:
        interval = max(0.1, lease_seconds / DISPATCH_RENEW_FRACTION)
        while not stop.wait(interval):
            try:
                dispatch.renew_lease(operation_id, owner)
            except DispatchClaimLost:
                logger.warning(
                    "dispatch lease lost during import",
                    extra={"operation_id": str(operation_id)},
                )
                return
            except Exception:
                logger.exception(
                    "dispatch lease renewal failed",
                    extra={"operation_id": str(operation_id)},
                )

    thread = threading.Thread(target=renew, name="ledgerbridge-dispatch-lease", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(1.0, lease_seconds / DISPATCH_RENEW_FRACTION))


def process_dispatch_once(
    dispatch: DispatchService,
    importer: EvidenceImporter,
    connectors: Sequence[Connector],
    owner: str,
    *,
    now: datetime | None = None,
    expected_manifest: tuple[str, bytes] | None = None,
) -> bool:
    """Claim and execute at most one dispatch; return whether work was claimed."""

    if not connectors:
        return False
    current = datetime.now(UTC) if now is None else now
    dispatch.recover_expired_leases(now=current, limit=100)
    claim = dispatch.claim_next(owner, now=current)
    if claim is None:
        return False
    try:
        if expected_manifest is not None and (
            claim.manifest_generation != expected_manifest[0]
            or claim.manifest_digest != expected_manifest[1]
        ):
            dispatch.fail(
                claim.operation_id,
                owner,
                error_code="MANIFEST_DRIFT",
                summary="dispatch manifest does not match the worker generation",
                now=current,
            )
            return True
        principal = dispatch.acceptance_principal(claim.operation_id)
        published = _published_from_claim(claim)
        metadata = IngestMetadata(
            source=claim.ingest_channel,
            original_filename=claim.original_filename,
            media_type=claim.media_type,
        )
        with renew_dispatch_lease(
            dispatch,
            claim.operation_id,
            owner,
            lease_seconds=max(1, int((claim.lease_until - current).total_seconds())),
        ):
            outcome = importer.ingest_published(
                published,
                metadata,
                connectors,
                actor=principal.actor,
                reason=principal.reason,
            )
        if outcome.status is ImportJobStatus.FAILED:
            dispatch.fail(
                claim.operation_id,
                owner,
                error_code=outcome.error_code or "IMPORT_FAILED",
                summary="importer reported a terminal failure",
                import_job_id=outcome.job_id,
            )
        else:
            dispatch.complete(
                claim.operation_id,
                owner,
                result_status=outcome.status,
                import_job_id=outcome.job_id,
            )
    except DispatchClaimLost:
        logger.warning(
            "dispatch claim lost before terminalization",
            extra={"operation_id": str(claim.operation_id)},
        )
    except EvidenceIngestionError as exc:
        _handle_dispatch_error(dispatch, claim, owner, exc.error_code, str(exc), current)
    except Exception:
        logger.exception(
            "unexpected dispatch execution failure",
            extra={"operation_id": str(claim.operation_id)},
        )
        _handle_dispatch_error(
            dispatch,
            claim,
            owner,
            "WORKER_INTERNAL",
            "worker execution failed",
            current,
        )
    return True


def _published_from_claim(claim: DispatchClaim) -> PublishedArtifact:
    return PublishedArtifact(
        sha256=_artifact_digest_from_claim(claim),
        byte_size=claim.byte_size,
        storage_key=claim.storage_key,
        created=False,
    )


def _artifact_digest_from_claim(claim: DispatchClaim) -> bytes:
    """The claim carries the artifact identity through its storage key."""

    digest_hex = claim.storage_key.rsplit("/", 1)[-1]
    return bytes.fromhex(digest_hex)


def _handle_dispatch_error(
    dispatch: DispatchService,
    claim: DispatchClaim,
    owner: str,
    error_code: str,
    summary: str,
    now: datetime,
) -> None:
    retryable = error_code in {
        "RUNNER_UNAVAILABLE",
        "IMPORT_DATABASE",
        "EVIDENCE_IO",
        "EVIDENCE_STORAGE",
        "WORKER_INTERNAL",
    }
    try:
        if retryable:
            delay = min(300, 2 ** max(0, claim.attempt_count - 1))
            dispatch.mark_retry(
                claim.operation_id,
                owner,
                available_at=now + timedelta(seconds=delay),
                error_code=error_code,
                summary=summary[:500],
                now=now,
            )
        else:
            dispatch.fail(
                claim.operation_id,
                owner,
                error_code=error_code,
                summary=summary[:500],
                now=now,
            )
    except DispatchClaimLost:
        logger.warning(
            "dispatch claim lost while recording failure",
            extra={"operation_id": str(claim.operation_id)},
        )


def main() -> None:
    global _running
    _running = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    settings = get_settings()
    importer = build_evidence_importer()
    dispatch = build_dispatch_service(settings)
    manifest = build_worker_manifest()
    connectors = build_worker_connectors(manifest, settings) if manifest is not None else ()
    owner = worker_id()
    logger.info("LedgerBridge worker started")
    while _running:
        write_heartbeat()
        if (
            settings.env != "production"
            and settings.enable_internal_async_dispatch
            and manifest is not None
        ):
            process_dispatch_once(
                dispatch,
                importer,
                connectors,
                owner,
                expected_manifest=manifest.identity,
            )
        time.sleep(settings.dispatch_poll_seconds if connectors else HEARTBEAT_INTERVAL_SECONDS)
    logger.info("LedgerBridge worker stopped")


if __name__ == "__main__":
    main()
