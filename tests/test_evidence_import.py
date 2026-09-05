from __future__ import annotations

import hashlib
import io
import json
import os
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.audit import append_audit_event
from ledgerbridge.connectors import (
    ArtifactMetadata,
    DetectionResult,
    ParsedSourceRecord,
    ReadableBinary,
)
from ledgerbridge.db import build_engine
from ledgerbridge.imports import (
    EvidenceImporter,
    EvidenceIngestionError,
    ImportOutcome,
    IngestMetadata,
)
from ledgerbridge.ledger import post_journal_entry
from ledgerbridge.models import (
    Account,
    AccountClass,
    Entity,
    EntityType,
    ImportJob,
    ImportJobStatus,
    JournalEntry,
    JournalStatus,
    Posting,
    SourceRecord,
)
from ledgerbridge.runner_client import RunnerClientError, RunnerConnector

# ``20260901_0027`` and every bank-statement import revision after it refuse to
# downgrade on purpose, so a chain that includes them cannot be walked back.
# Reversibility is therefore proven against the last revision that still has a
# downgrade path.
LAST_REVERSIBLE_REVISION = "20260831_0026"


def _run_alembic(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


def _test_admin_url() -> URL:
    value = os.environ.get("LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL")
    if value is None:
        pytest.skip(
            "PostgreSQL integration tests require LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL "
            "for temporary database bootstrap and cleanup"
        )
    return make_url(value)


def _maintenance_engine(admin_url: URL) -> Engine:
    return create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def _migration_owner_name(owner_url: URL) -> str:
    username = owner_url.username
    if not isinstance(username, str) or not username:
        pytest.skip("LEDGERBRIDGE_MIGRATION_DATABASE_URL must identify a database owner")
    return username


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_DATABASE_URL")
    return value


@pytest.fixture(scope="session")
def migration_database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    return value


@pytest.fixture(scope="module")
def isolated_legacy_urls(
    database_url: str,
    migration_database_url: str,
) -> Iterator[tuple[str, str]]:
    """Run the pre-R1 evidence lifecycle against a disposable 0013 database.

    Other integration modules intentionally exercise the R1 head on the
    shared CI database.  Alembic upgrades are monotonic, so an in-place
    ``upgrade 0013`` would silently leave this legacy suite on head and make
    its valid POSTED fixtures fail the mandatory R1 attribution trigger.
    """
    owner_url = make_url(migration_database_url)
    runtime_url = make_url(database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_evidence_legacy_{uuid4().hex[:12]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_owner_url = owner_url.set(database=database_name)
    temporary_runtime_url = runtime_url.set(database=database_name)
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        _run_alembic(temporary_owner_url.render_as_string(hide_password=False), "20260824_0013")
        yield (
            temporary_owner_url.render_as_string(hide_password=False),
            temporary_runtime_url.render_as_string(hide_password=False),
        )
    finally:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


@pytest.fixture(scope="module")
def admin_engine(isolated_legacy_urls: tuple[str, str]) -> Iterator[Engine]:
    engine = create_engine(isolated_legacy_urls[0], pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def runtime_engine(
    isolated_legacy_urls: tuple[str, str],
    admin_engine: Engine,
) -> Iterator[Engine]:
    del admin_engine
    engine = build_engine(isolated_legacy_urls[1])
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_database(admin_engine: Engine) -> Iterator[None]:
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE source_record, import_job, raw_artifact, posting, "
                "journal_entry, account, entity, audit_event RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def importer(runtime_engine: Engine, tmp_path: Path) -> EvidenceImporter:
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000, chunk_size=127)
    return EvidenceImporter(sessions, store, detection_prefix_bytes=64, max_records=1_000)


@dataclass
class SyntheticConnector:
    name: str = "synthetic"
    version: str = "1"
    source_system: str = "synthetic"
    detection: DetectionResult = DetectionResult.MATCH
    records: list[ParsedSourceRecord] = field(default_factory=list)
    parse_error: bool = False
    detect_error: bool = False
    saw_path: bool = False
    parsed_bytes: bytes = b""

    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        assert len(bounded_prefix) <= 64
        assert len(metadata.sha256_hex) == 64
        if self.detect_error:
            raise ValueError("secret-looking detection input 6222020000000000")
        return self.detection

    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        self.saw_path = any(hasattr(stream, attribute) for attribute in ("name", "fileno", "write"))
        self.parsed_bytes = stream.read()
        if self.parse_error:
            raise ValueError("secret-looking raw row: 6222020000000000")
        return list(self.records)


class InvalidDetectConnector(SyntheticConnector):
    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        super().detect(metadata, bounded_prefix)
        return cast(DetectionResult, "INVALID")


class InvalidParseConnector(SyntheticConnector):
    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        stream.read()
        return cast(Iterable[ParsedSourceRecord], [{"raw": "dictionary"}])


class NestedMutationConnector(SyntheticConnector):
    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        stream.read()
        nested: dict[str, object] = {"amount_minor": 1, "currency": "CNY"}
        normalized: dict[str, object] = {"nested": nested}

        def values() -> Iterator[ParsedSourceRecord]:
            yield ParsedSourceRecord(
                record_locator="row:nested",
                source=self.name,
                parser_version=self.version,
                raw_fields={"nested": {"memo": "original"}},
                normalized_fields=normalized,
            )
            nested["amount_minor"] = 1.5

        return values()


class PropertyDriftConnector:
    def __init__(self) -> None:
        self.name_reads = 0
        self.version_reads = 0
        self.source_system_reads = 0

    @property
    def name(self) -> str:
        self.name_reads += 1
        return "synthetic" if self.name_reads == 1 else "x" * 150

    @property
    def version(self) -> str:
        self.version_reads += 1
        return "1" if self.version_reads == 1 else "y" * 150

    @property
    def source_system(self) -> str:
        self.source_system_reads += 1
        return "synthetic" if self.source_system_reads == 1 else "invalid-source"

    def detect(
        self,
        metadata: ArtifactMetadata,
        bounded_prefix: bytes,
    ) -> DetectionResult:
        del metadata, bounded_prefix
        return DetectionResult.MATCH

    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        stream.read()
        return [_record("row:stable-identity")]


class TamperingConnector(SyntheticConnector):
    def __init__(self, destination: Path, replacement: bytes) -> None:
        super().__init__(records=[_record("row:tampered")])
        self.destination = destination
        self.replacement = replacement

    def detect(
        self,
        metadata: ArtifactMetadata,
        bounded_prefix: bytes,
    ) -> DetectionResult:
        del metadata, bounded_prefix
        self.destination.chmod(0o600)
        self.destination.write_bytes(self.replacement)
        return DetectionResult.MATCH


class IdentityMutatingConnector(SyntheticConnector):
    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        stream.read()

        def values() -> Iterator[ParsedSourceRecord]:
            yield _record("row:before-mutation")
            self.name = "mutated"
            self.version = "2"
            yield ParsedSourceRecord(
                record_locator="row:after-mutation",
                source=self.name,
                parser_version=self.version,
                raw_fields={},
                normalized_fields={},
            )

        return values()


def _record(
    locator: str,
    *,
    version: str = "1",
    external_id: str | None = None,
) -> ParsedSourceRecord:
    return ParsedSourceRecord(
        record_locator=locator,
        source="synthetic",
        parser_version=version,
        raw_fields={"amount": "12.34", "memo": "synthetic only"},
        normalized_fields={"amount_minor": 1234, "currency": "CNY"},
        external_transaction_id=external_id,
    )


def _ingest(
    importer: EvidenceImporter,
    content: bytes,
    connectors: list[SyntheticConnector],
    *,
    filename: str = "synthetic.txt",
) -> ImportOutcome:
    return importer.ingest_and_import(
        io.BytesIO(content),
        IngestMetadata(
            source="synthetic_upload",
            original_filename=filename,
            media_type="text/plain",
        ),
        connectors,
        actor="pytest",
        reason="synthetic Phase 2 acceptance",
    )


def test_ingest_published_continues_from_an_artifact_store_commit(
    importer: EvidenceImporter,
) -> None:
    content = b"already committed evidence"
    published = importer._store.publish(io.BytesIO(content))
    connector = SyntheticConnector(records=[_record("row:published")])

    outcome = importer.ingest_published(
        published,
        IngestMetadata(
            source="synthetic_upload",
            original_filename="committed.txt",
            media_type="text/plain",
        ),
        [connector],
        actor="pytest",
        reason="published handoff test",
    )

    assert outcome.status is ImportJobStatus.SUCCEEDED
    assert outcome.parsed_count == 1
    assert connector.parsed_bytes == content


def test_importer_and_connector_batch_guardrails(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    store = ArtifactStore((tmp_path / "guardrails").resolve(), max_bytes=10_000)
    with pytest.raises(ValueError, match="positive"):
        EvidenceImporter(sessions, store, detection_prefix_bytes=0)
    with pytest.raises(ValueError, match="positive"):
        EvidenceImporter(sessions, store, max_records=0)
    with pytest.raises(ValueError, match="source"):
        IngestMetadata(source=" ", original_filename="x", media_type="text/plain")

    normal = EvidenceImporter(sessions, store, max_records=10)
    limited = EvidenceImporter(sessions, store, max_records=1)
    wrong_source = ParsedSourceRecord(
        record_locator="row:wrong-source",
        source="other",
        parser_version="1",
        raw_fields={},
        normalized_fields={},
    )
    cases: list[tuple[EvidenceImporter, list[SyntheticConnector]]] = [
        (normal, [SyntheticConnector(), SyntheticConnector()]),
        (normal, [InvalidDetectConnector()]),
        (normal, [InvalidParseConnector()]),
        (
            normal,
            [SyntheticConnector(records=[_record("same"), _record("same")])],
        ),
        (normal, [SyntheticConnector(records=[wrong_source])]),
        (
            limited,
            [SyntheticConnector(records=[_record("one"), _record("two")])],
        ),
    ]
    for index, (subject, connectors) in enumerate(cases):
        outcome = _ingest(subject, f"guardrail-{index}".encode(), connectors)
        assert outcome.status is ImportJobStatus.FAILED
        assert outcome.error_code in {"DETECTION_ERROR", "CONNECTOR_CONTRACT"}

    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM journal_entry")).scalar_one() == 0


def test_production_importer_rejects_in_process_connectors(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    subject = EvidenceImporter(
        sessionmaker(bind=runtime_engine, expire_on_commit=False),
        ArtifactStore(tmp_path / "production-mode", max_bytes=1_000),
        production=True,
    )
    outcome = _ingest(subject, b"production mode", [SyntheticConnector()])
    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "CONNECTOR_CONTRACT"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0


def test_canonical_source_registries_are_append_only_and_runtime_read_only(
    runtime_engine: Engine,
    admin_engine: Engine,
) -> None:
    with runtime_engine.connect() as connection:
        channels = (
            connection.execute(text("SELECT id FROM ingest_channel ORDER BY id")).scalars().all()
        )
        systems = (
            connection.execute(text("SELECT id FROM source_system ORDER BY id")).scalars().all()
        )
        assert channels == ["manual_upload", "synthetic_upload"]
        assert systems == ["synthetic"]
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO ingest_channel (id, description) "
                    "VALUES ('runtime_added', 'not allowed')"
                )
            )
        connection.rollback()

    with admin_engine.connect() as connection:
        for statement in (
            "UPDATE ingest_channel SET description = 'changed' WHERE id = 'manual_upload'",
            "DELETE FROM source_system WHERE id = 'synthetic'",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                connection.execute(text(statement))
            connection.rollback()
        function_config = connection.execute(
            text(
                "SELECT proconfig FROM pg_proc JOIN pg_namespace "
                "ON pg_namespace.oid = pg_proc.pronamespace "
                "WHERE nspname = 'public' AND proname = 'registry_block_mutation'"
            )
        ).scalar_one()
        assert function_config == ["search_path=pg_catalog"]


def test_unknown_ingest_channel_fails_before_artifact_publication(
    runtime_engine: Engine,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "unknown-channel").resolve()
    subject = EvidenceImporter(
        sessionmaker(bind=runtime_engine, expire_on_commit=False),
        ArtifactStore(root, max_bytes=1_000),
    )

    with pytest.raises(EvidenceIngestionError) as captured:
        subject.ingest_and_import(
            io.BytesIO(b"must not publish"),
            IngestMetadata(
                source="unknown_channel",
                original_filename="synthetic.txt",
                media_type="text/plain",
            ),
            [],
            actor="pytest",
            reason="unknown channel acceptance",
        )

    assert captured.value.error_code == "INGEST_CHANNEL_UNKNOWN"
    assert not root.exists()


def test_unknown_connector_source_system_publishes_no_records(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    connector = SyntheticConnector(
        source_system="unknown_system",
        records=[_record("row:must-not-publish")],
    )

    outcome = _ingest(importer, b"unknown source system", [connector])

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "CONNECTOR_CONTRACT"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0


def test_successful_import_is_idempotent_and_never_creates_ledger_rows(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    connector = SyntheticConnector(records=[_record("row:1"), _record("row:2")])
    first = _ingest(importer, b"synthetic import", [connector], filename="../secret-name.csv")
    second = _ingest(importer, b"synthetic import", [connector], filename="different.csv")

    assert first.status is ImportJobStatus.SUCCEEDED
    assert first.created_count == 2
    assert first.duplicate_count == 0
    assert first.artifact_created
    assert second.job_id == first.job_id
    assert second.artifact_id == first.artifact_id
    assert second.status is ImportJobStatus.SUCCEEDED
    assert not second.artifact_created
    assert not connector.saw_path
    assert connector.parsed_bytes == b"synthetic import"

    with admin_engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM raw_artifact), "
                "(SELECT count(*) FROM import_job), "
                "(SELECT count(*) FROM source_record), "
                "(SELECT count(*) FROM journal_entry)"
            )
        ).one()
        assert counts == (1, 1, 2, 0)
        artifact = connection.execute(
            text("SELECT original_filename, storage_key FROM raw_artifact")
        ).one()
        assert artifact.original_filename == "../secret-name.csv"
        assert "secret-name" not in artifact.storage_key
        audit_text = "\n".join(
            connection.execute(
                text("SELECT payload::text FROM audit_event ORDER BY sequence")
            ).scalars()
        )
        assert "secret-name" not in audit_text
        assert "different.csv" not in audit_text


def test_concurrent_identical_import_converges_on_one_artifact_job_and_batch(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    parse_barrier = Barrier(2)

    class ConcurrentConnector(SyntheticConnector):
        def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
            parse_barrier.wait(timeout=5)
            return super().parse(stream)

    connectors = [
        ConcurrentConnector(records=[_record("row:1"), _record("row:2")]),
        ConcurrentConnector(records=[_record("row:1"), _record("row:2")]),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_ingest, importer, b"concurrent synthetic import", [connector])
            for connector in connectors
        ]
        outcomes = [future.result(timeout=15) for future in futures]

    assert {outcome.status for outcome in outcomes} == {ImportJobStatus.SUCCEEDED}
    assert len({outcome.artifact_id for outcome in outcomes}) == 1
    assert len({outcome.job_id for outcome in outcomes}) == 1
    assert sum(outcome.artifact_created for outcome in outcomes) == 1
    with admin_engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM raw_artifact), "
                "(SELECT count(*) FROM import_job), "
                "(SELECT count(*) FROM source_record), "
                "(SELECT count(*) FROM journal_entry)"
            )
        ).one()
        assert counts == (1, 1, 2, 0)


@pytest.mark.parametrize(
    ("connectors", "expected_code"),
    [
        ([], "NO_CONNECTOR"),
        (
            [
                SyntheticConnector(name="one"),
                SyntheticConnector(name="two"),
            ],
            "AMBIGUOUS_CONNECTOR",
        ),
        (
            [SyntheticConnector(detection=DetectionResult.AMBIGUOUS)],
            "AMBIGUOUS_CONNECTOR",
        ),
    ],
)
def test_uncertain_routing_needs_review_without_source_records(
    importer: EvidenceImporter,
    admin_engine: Engine,
    connectors: list[SyntheticConnector],
    expected_code: str,
) -> None:
    outcome = _ingest(importer, expected_code.encode(), connectors)
    assert outcome.status is ImportJobStatus.NEEDS_REVIEW
    assert outcome.error_code == expected_code
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM journal_entry")).scalar_one() == 0


def test_parse_failure_is_sanitized_and_batch_is_empty(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    connector = SyntheticConnector(parse_error=True)
    outcome = _ingest(importer, b"raw value 987654", [connector], filename="token=secret.csv")

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "PARSE_ERROR"
    with admin_engine.connect() as connection:
        job = connection.execute(
            text("SELECT diagnostic_summary, parsed_count, created_count FROM import_job")
        ).one()
        assert job == ("connector parse failed", 0, 0)
        assert "987654" not in job.diagnostic_summary
        assert "secret" not in job.diagnostic_summary
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0


def test_detection_exception_is_sanitized(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    connector = SyntheticConnector(detect_error=True)
    outcome = _ingest(importer, b"detection secret 987654", [connector])

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "DETECTION_ERROR"
    with admin_engine.connect() as connection:
        summary = connection.execute(text("SELECT diagnostic_summary FROM import_job")).scalar_one()
        assert summary == "connector detection failed"
        assert "987654" not in summary
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0


def test_untrusted_runner_error_code_is_terminalized_and_audited(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    class MaliciousRunnerClient:
        def detect(self, request: object, stream: ReadableBinary) -> DetectionResult:
            del request, stream
            raise RunnerClientError("RUNNER_ERROR", "\x00")

        def parse(self, request: object, stream: ReadableBinary) -> tuple[ParsedSourceRecord, ...]:
            del request, stream
            return ()

    connector = RunnerConnector(
        "synthetic",
        "1",
        "synthetic",
        MaliciousRunnerClient(),  # type: ignore[arg-type]
    )
    outcome = importer.ingest_and_import(
        io.BytesIO(b"malicious runner error"),
        IngestMetadata(
            source="synthetic_upload",
            original_filename="runner.txt",
            media_type="text/plain",
        ),
        [connector],  # type: ignore[list-item]
        actor="pytest",
        reason="runner error normalization",
    )

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "RUNNER_ERROR"
    with admin_engine.connect() as connection:
        job = connection.execute(
            text(
                "SELECT status, error_code, diagnostic_summary, terminal_audit_event_id "
                "FROM import_job"
            )
        ).one()
        assert job.status == "FAILED"
        assert job.error_code == "RUNNER_ERROR"
        assert job.diagnostic_summary == "connector runner failed"
        assert job.terminal_audit_event_id is not None
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        assert (
            connection.execute(
                text("SELECT count(*) FROM audit_event WHERE action = 'import.complete'")
            ).scalar_one()
            == 1
        )


def test_runner_capacity_failure_bubbles_for_dispatch_retry(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    class UnavailableRunnerClient:
        def detect(self, request: object, stream: ReadableBinary) -> DetectionResult:
            del request, stream
            raise RunnerClientError("RUNNER_UNAVAILABLE", "runner capacity is unavailable")

        def parse(self, request: object, stream: ReadableBinary) -> tuple[ParsedSourceRecord, ...]:
            del request, stream
            return ()

    connector = RunnerConnector(
        "synthetic",
        "1",
        "synthetic",
        UnavailableRunnerClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(EvidenceIngestionError) as error:
        importer.ingest_and_import(
            io.BytesIO(b"temporary runner capacity failure"),
            IngestMetadata(
                source="synthetic_upload",
                original_filename="runner.txt",
                media_type="text/plain",
            ),
            [connector],  # type: ignore[list-item]
            actor="pytest",
            reason="runner capacity retry",
        )

    assert error.value.error_code == "RUNNER_UNAVAILABLE"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM import_job")).scalar_one() == 0


def test_runner_capacity_during_parse_keeps_job_retryable(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    class UnavailableRunnerClient:
        def detect(self, request: object, stream: ReadableBinary) -> DetectionResult:
            del request, stream
            return DetectionResult.MATCH

        def parse(self, request: object, stream: ReadableBinary) -> tuple[ParsedSourceRecord, ...]:
            del request, stream
            raise RunnerClientError("RUNNER_UNAVAILABLE", "runner capacity is unavailable")

    connector = RunnerConnector(
        "synthetic",
        "1",
        "synthetic",
        UnavailableRunnerClient(),  # type: ignore[arg-type]
    )
    with admin_engine.connect() as connection:
        audit_events_before = connection.execute(
            text("SELECT count(*) FROM audit_event")
        ).scalar_one()
    with pytest.raises(EvidenceIngestionError) as error:
        importer.ingest_and_import(
            io.BytesIO(b"temporary parse capacity failure"),
            IngestMetadata(
                source="synthetic_upload",
                original_filename="runner.txt",
                media_type="text/plain",
            ),
            [connector],  # type: ignore[list-item]
            actor="pytest",
            reason="runner parse capacity retry",
        )

    assert error.value.error_code == "RUNNER_UNAVAILABLE"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM import_job")).scalar_one() == "RUNNING"
        assert (
            connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one()
            == audit_events_before + 1
        )


@pytest.mark.parametrize(
    ("field", "bad_text"),
    [
        ("record_locator", "\x00"),
        ("record_locator", "\ud800"),
        ("raw_fields", "\x00"),
        ("raw_fields", "\ud800"),
    ],
)
def test_untrusted_runner_record_text_is_terminalized_and_audited(
    importer: EvidenceImporter,
    admin_engine: Engine,
    field: str,
    bad_text: str,
) -> None:
    record = object.__new__(ParsedSourceRecord)
    object.__setattr__(record, "record_locator", "row:1")
    object.__setattr__(record, "source", "synthetic")
    object.__setattr__(record, "parser_version", "1")
    object.__setattr__(record, "raw_fields", {"memo": "safe"})
    object.__setattr__(record, "normalized_fields", {})
    object.__setattr__(record, "external_transaction_id", None)
    if field == "record_locator":
        object.__setattr__(record, field, bad_text)
    else:
        object.__setattr__(record, field, {"memo": bad_text})

    class MaliciousRunnerClient:
        def detect(self, request: object, stream: ReadableBinary) -> DetectionResult:
            del request, stream
            return DetectionResult.MATCH

        def parse(self, request: object, stream: ReadableBinary) -> tuple[ParsedSourceRecord, ...]:
            del request, stream
            return (record,)

    connector = RunnerConnector(
        "synthetic",
        "1",
        "synthetic",
        MaliciousRunnerClient(),  # type: ignore[arg-type]
    )
    outcome = importer.ingest_and_import(
        io.BytesIO(b"malicious runner record"),
        IngestMetadata(
            source="synthetic_upload",
            original_filename="runner-record.txt",
            media_type="text/plain",
        ),
        [connector],  # type: ignore[list-item]
        actor="pytest",
        reason="runner record normalization",
    )

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "CONNECTOR_CONTRACT"
    with admin_engine.connect() as connection:
        job = connection.execute(
            text("SELECT status, terminal_audit_event_id FROM import_job")
        ).one()
        assert job.status == "FAILED"
        assert job.terminal_audit_event_id is not None
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        assert (
            connection.execute(
                text("SELECT count(*) FROM audit_event WHERE action = 'import.complete'")
            ).scalar_one()
            == 1
        )


def test_mutated_float_output_is_revalidated_before_publication(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    normalized: dict[str, object] = {"amount_minor": 1, "currency": "CNY"}
    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={},
        normalized_fields=normalized,
    )
    normalized["amount_minor"] = 1.5
    outcome = _ingest(importer, b"mutated connector", [SyntheticConnector(records=[record])])

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "CONNECTOR_CONTRACT"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0


def test_nested_output_is_detached_before_generator_can_mutate_it(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    outcome = _ingest(importer, b"nested mutation", [NestedMutationConnector()])

    assert outcome.status is ImportJobStatus.SUCCEEDED
    with admin_engine.connect() as connection:
        normalized = connection.execute(
            text("SELECT normalized_fields FROM source_record")
        ).scalar_one()
        assert normalized == {"nested": {"amount_minor": 1, "currency": "CNY"}}


def test_connector_identity_is_frozen_before_detection_and_parse(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    outcome = _ingest(importer, b"identity mutation", [IdentityMutatingConnector()])

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "CONNECTOR_CONTRACT"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        identity = connection.execute(
            text("SELECT connector_name, connector_version FROM import_job")
        ).one()
        assert identity == ("synthetic", "1")


def test_connector_identity_properties_are_read_exactly_once(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    connector = PropertyDriftConnector()
    outcome = importer.ingest_and_import(
        io.BytesIO(b"property drift"),
        IngestMetadata(
            source="synthetic_upload",
            original_filename="drift.txt",
            media_type="text/plain",
        ),
        [connector],
        actor="pytest",
        reason="identity snapshot test",
    )

    assert outcome.status is ImportJobStatus.SUCCEEDED
    assert connector.name_reads == 1
    assert connector.version_reads == 1
    assert connector.source_system_reads == 1
    with admin_engine.connect() as connection:
        identity = connection.execute(
            text("SELECT connector_name, connector_version FROM import_job")
        ).one()
        assert identity == ("synthetic", "1")


def test_tampering_after_detection_is_an_evidence_integrity_failure(
    importer: EvidenceImporter,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    content = b"tamper after prefix"
    digest = hashlib.sha256(content).digest()
    digest_hex = digest.hex()
    destination = tmp_path / "sha256" / digest_hex[:2] / digest_hex[2:4] / digest_hex
    connector = TamperingConnector(destination, b"X" * len(content))

    outcome = _ingest(importer, content, [connector])

    assert outcome.status is ImportJobStatus.FAILED
    assert outcome.error_code == "EVIDENCE_INTEGRITY"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 0
        assert (
            connection.execute(text("SELECT error_code FROM import_job")).scalar_one()
            == "EVIDENCE_INTEGRITY"
        )


def test_corrupt_duplicate_publication_raises_a_controlled_ingestion_error(
    importer: EvidenceImporter,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    content = b"corrupt duplicate"
    first = _ingest(importer, content, [SyntheticConnector(records=[_record("row:1")])])
    digest_hex = hashlib.sha256(content).hexdigest()
    destination = tmp_path / "sha256" / digest_hex[:2] / digest_hex[2:4] / digest_hex
    destination.chmod(0o600)
    destination.write_bytes(b"Z" * len(content))

    with pytest.raises(EvidenceIngestionError) as captured:
        _ingest(importer, content, [SyntheticConnector(records=[_record("row:1")])])

    assert captured.value.error_code == "EVIDENCE_INTEGRITY"
    assert "corrupt duplicate" not in str(captured.value)
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM raw_artifact")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM import_job")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 1
        assert (
            connection.execute(
                text("SELECT count(*) FROM raw_artifact WHERE id = :id"),
                {"id": first.artifact_id},
            ).scalar_one()
            == 1
        )


def test_job_creation_database_failure_is_a_controlled_ingestion_error(
    importer: EvidenceImporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_job_creation(*_args: object, **_kwargs: object) -> UUID:
        raise IntegrityError("synthetic job failure", {}, RuntimeError("database failure"))

    monkeypatch.setattr(importer, "_find_or_create_job", fail_job_creation)

    with pytest.raises(EvidenceIngestionError) as captured:
        _ingest(importer, b"controlled database failure", [])

    assert captured.value.error_code == "IMPORT_DATABASE"
    assert str(captured.value) == "evidence import state could not be recorded"


def test_conflicting_source_or_media_type_routes_to_provenance_review(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    content = b"same bytes conflicting provenance"
    first = _ingest(importer, content, [SyntheticConnector(records=[_record("row:1")])])
    second = importer.ingest_and_import(
        io.BytesIO(content),
        IngestMetadata(
            source="manual_upload",
            original_filename="different-display-name.bin",
            media_type="application/x-other",
        ),
        [SyntheticConnector(records=[_record("row:1")])],
        actor="pytest",
        reason="provenance conflict test",
    )

    assert first.status is ImportJobStatus.SUCCEEDED
    assert second.status is ImportJobStatus.NEEDS_REVIEW
    assert second.error_code == "PROVENANCE_CONFLICT"
    assert second.artifact_id == first.artifact_id
    with admin_engine.connect() as connection:
        artifact = connection.execute(
            text("SELECT source, original_filename, media_type FROM raw_artifact")
        ).one()
        assert artifact == ("synthetic_upload", "synthetic.txt", "text/plain")
        assert connection.execute(text("SELECT count(*) FROM raw_artifact")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM import_job")).scalar_one() == 2


def test_new_parser_version_gets_distinct_job_without_overwriting_provenance(
    importer: EvidenceImporter,
    admin_engine: Engine,
) -> None:
    first = _ingest(importer, b"versioned", [SyntheticConnector(records=[_record("row:1")])])
    second_connector = SyntheticConnector(version="2", records=[_record("row:1", version="2")])
    second = _ingest(importer, b"versioned", [second_connector])

    assert first.status is ImportJobStatus.SUCCEEDED
    assert second.job_id != first.job_id
    assert second.status is ImportJobStatus.NEEDS_REVIEW
    assert second.error_code == "IDENTITY_CONFLICT"
    with admin_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT parser_version, raw_fields FROM source_record")
        ).all()
        assert len(rows) == 1
        assert rows[0].parser_version == "1"
        assert rows[0].raw_fields == {"amount": "12.34", "memo": "synthetic only"}
        assert connection.execute(text("SELECT count(*) FROM import_job")).scalar_one() == 2


def test_runtime_and_owner_cannot_mutate_permanent_evidence(
    importer: EvidenceImporter,
    runtime_engine: Engine,
    admin_engine: Engine,
) -> None:
    outcome = _ingest(importer, b"immutable", [SyntheticConnector(records=[_record("row:1")])])

    with runtime_engine.connect() as connection:
        for statement in (
            "UPDATE raw_artifact SET source = 'changed'",
            "DELETE FROM source_record",
            "ALTER TABLE raw_artifact DISABLE TRIGGER raw_artifact_no_update_delete",
            "INSERT INTO audit_event (id, sequence, occurred_at, actor, action, reason, "
            "payload, hash) VALUES (gen_random_uuid(), 999, now(), 'x', 'x', 'x', "
            "'{}'::jsonb, decode(repeat('00', 32), 'hex'))",
        ):
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))
            connection.rollback()

    with admin_engine.connect() as connection:
        before = connection.execute(
            text("SELECT source, original_filename, encode(sha256, 'hex') FROM raw_artifact")
        ).one()
        with pytest.raises(DBAPIError, match="immutable"):
            connection.execute(text("UPDATE raw_artifact SET source = 'owner-change'"))
        connection.rollback()
        with pytest.raises(DBAPIError, match="permanent"):
            connection.execute(text("DELETE FROM source_record"))
        connection.rollback()
        after = connection.execute(
            text("SELECT source, original_filename, encode(sha256, 'hex') FROM raw_artifact")
        ).one()
        assert after == before
        assert connection.execute(text("SELECT count(*) FROM source_record")).scalar_one() == 1
        assert (
            connection.execute(
                text("SELECT count(*) FROM raw_artifact WHERE id = :id"),
                {"id": outcome.artifact_id},
            ).scalar_one()
            == 1
        )


def test_database_failure_leaves_only_an_unreferenced_verified_blob(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "database-failure").resolve()
    store = ArtifactStore(root, max_bytes=1_000)
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    subject = EvidenceImporter(sessions, store)
    content = b"verified orphan after database rollback"

    with pytest.raises(EvidenceIngestionError) as captured:
        subject.ingest_and_import(
            io.BytesIO(content),
            IngestMetadata(
                source="synthetic_upload",
                original_filename="synthetic.txt",
                media_type="text/plain",
            ),
            [],
            actor=" ",
            reason="forced audit failure",
        )
    assert captured.value.error_code == "IMPORT_DATABASE"
    assert str(captured.value) == "evidence import state could not be recorded"

    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM raw_artifact")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one() == 0
    blobs = [path for path in (root / "sha256").rglob("*") if path.is_file()]
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == content
    assert not list((root / ".staging").iterdir())


def test_quota_rejection_is_audited_without_disclosing_filename(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, dict[str, object]]] = []

    def capture_log(message: str, *, extra: dict[str, object]) -> None:
        logged.append((message, extra))

    monkeypatch.setattr("ledgerbridge.imports.logger.error", capture_log)
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    store = ArtifactStore(
        (tmp_path / "quota-audit").resolve(),
        max_bytes=1_000,
        total_max_bytes=4,
        staging_max_bytes=1_000,
    )
    subject = EvidenceImporter(sessions, store)

    with pytest.raises(EvidenceIngestionError) as captured:
        subject.ingest_and_import(
            io.BytesIO(b"12345"),
            IngestMetadata(
                source="synthetic_upload",
                original_filename="private-bank-statement.csv",
                media_type="text/plain",
            ),
            [],
            actor="pytest",
            reason="quota acceptance",
        )

    assert captured.value.error_code == "ARTIFACT_TOTAL_QUOTA"
    with admin_engine.connect() as connection:
        event = connection.execute(
            text(
                "SELECT action, payload FROM audit_event WHERE action = 'artifact.ingest_rejected'"
            )
        ).one()
    assert event.action == "artifact.ingest_rejected"
    assert event.payload["quota_kind"] == "published"
    assert event.payload["limit_bytes"] == 4
    assert event.payload["observed_bytes"] == 0
    assert event.payload["requested_bytes"] == 5
    assert event.payload["ingest_channel"] == "synthetic_upload"
    assert len(UUID(event.payload["intake_id"]).bytes) == 16
    assert "private-bank-statement.csv" not in json.dumps(event.payload)
    assert logged[0][0] == "artifact ingestion rejected by quota control"
    assert logged[0][1]["audit_recorded"] is True
    assert "private-bank-statement.csv" not in json.dumps(logged[0][1])


def test_quota_rejection_keeps_structured_log_when_audit_database_is_unavailable(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[tuple[str, dict[str, object]]] = []

    def capture_log(message: str, *, extra: dict[str, object]) -> None:
        logged.append((message, extra))

    monkeypatch.setattr("ledgerbridge.imports.logger.error", capture_log)
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    store = ArtifactStore(
        (tmp_path / "quota-log-fallback").resolve(),
        max_bytes=1_000,
        total_max_bytes=1,
        staging_max_bytes=1_000,
    )
    subject = EvidenceImporter(sessions, store)

    def fail_audit(*_args: object, **_kwargs: object) -> UUID:
        raise SQLAlchemyError("unavailable")

    monkeypatch.setattr("ledgerbridge.imports.append_audit_event", fail_audit)
    with pytest.raises(EvidenceIngestionError) as captured:
        _ingest(subject, b"too large", [])

    assert captured.value.error_code == "ARTIFACT_TOTAL_QUOTA"
    with admin_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM audit_event")).scalar_one() == 0
    assert logged[0][0] == "artifact ingestion rejected by quota control"
    assert logged[0][1]["audit_recorded"] is False


def test_staging_and_ambiguous_quota_state_have_distinct_audited_errors(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    staging_root = (tmp_path / "staging-pressure").resolve()
    (staging_root / ".staging").mkdir(parents=True)
    (staging_root / ".staging" / "artifact-existing").write_bytes(b"12")
    staging_subject = EvidenceImporter(
        sessions,
        ArtifactStore(
            staging_root,
            max_bytes=100,
            staging_max_bytes=1,
            staging_ttl_seconds=60,
        ),
    )
    with pytest.raises(EvidenceIngestionError) as staging_error:
        _ingest(staging_subject, b"x", [])
    assert staging_error.value.error_code == "ARTIFACT_STAGING_QUOTA"

    ambiguous_root = (tmp_path / "ambiguous-state").resolve()
    ambiguous_root.mkdir()
    (ambiguous_root / "unexpected").write_bytes(b"unknown")
    state_subject = EvidenceImporter(
        sessions,
        ArtifactStore(ambiguous_root, max_bytes=100),
    )
    with pytest.raises(EvidenceIngestionError) as state_error:
        _ingest(state_subject, b"x", [])
    assert state_error.value.error_code == "ARTIFACT_QUOTA_STATE"

    with admin_engine.connect() as connection:
        payloads = (
            connection.execute(
                text(
                    "SELECT payload FROM audit_event "
                    "WHERE action = 'artifact.ingest_rejected' ORDER BY sequence"
                )
            )
            .scalars()
            .all()
        )
    assert [payload["error_code"] for payload in payloads] == [
        "ARTIFACT_STAGING_QUOTA",
        "ARTIFACT_QUOTA_STATE",
    ]
    assert payloads[1]["limit_bytes"] is None
    assert payloads[1]["observed_bytes"] is None
    assert payloads[1]["requested_bytes"] is None


def test_import_job_state_machine_and_column_grants(
    importer: EvidenceImporter,
    runtime_engine: Engine,
    admin_engine: Engine,
) -> None:
    outcome = _ingest(importer, b"job-state", [SyntheticConnector()])
    manual_id: UUID
    with Session(runtime_engine, expire_on_commit=False) as session:
        manual = ImportJob(
            artifact_id=outcome.artifact_id,
            connector_name="manual-state-test",
            connector_version="1",
            source_system="synthetic",
            status=ImportJobStatus.PENDING,
        )
        session.add(manual)
        session.commit()
        manual_id = manual.id

        with pytest.raises(DBAPIError, match="illegal import job transition"):
            session.execute(
                update(ImportJob)
                .where(ImportJob.id == manual.id)
                .values(
                    status=ImportJobStatus.SUCCEEDED,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
        session.rollback()

        session.execute(
            update(ImportJob)
            .where(ImportJob.id == manual.id)
            .values(status=ImportJobStatus.RUNNING, started_at=datetime.now(UTC))
        )
        session.commit()
        # The state check runs before the deferred provenance trigger when the
        # terminal audit binding is omitted.  PostgreSQL therefore reports
        # either invariant depending on the constraint evaluation order.
        with pytest.raises(
            DBAPIError,
            match=r"import_job_state_timestamps|terminal import job requires",
        ):
            session.execute(
                update(ImportJob)
                .where(ImportJob.id == manual.id)
                .values(status=ImportJobStatus.SUCCEEDED, completed_at=datetime.now(UTC))
            )
            session.commit()
        session.rollback()
        audit_event_id = append_audit_event(
            session,
            actor="pytest",
            action="import.complete",
            reason="manual state transition",
            payload={
                "job_id": str(manual.id),
                "artifact_id": str(outcome.artifact_id),
                "status": "SUCCEEDED",
            },
        )
        session.execute(
            update(ImportJob)
            .where(ImportJob.id == manual.id)
            .values(
                status=ImportJobStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                terminal_audit_event_id=audit_event_id,
            )
        )
        session.commit()

        with pytest.raises(DBAPIError, match="terminal import jobs are immutable"):
            session.execute(
                update(ImportJob)
                .where(ImportJob.id == manual.id)
                .values(diagnostic_summary="late mutation")
            )

    with admin_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT has_table_privilege('ledgerbridge_app', 'raw_artifact', 'UPDATE')")
            ).scalar_one()
            is False
        )
        assert (
            connection.execute(
                text("SELECT has_table_privilege('ledgerbridge_app', 'source_record', 'DELETE')")
            ).scalar_one()
            is False
        )
        assert (
            connection.execute(
                text(
                    "SELECT has_column_privilege("
                    "'ledgerbridge_app', 'import_job', 'status', 'UPDATE')"
                )
            ).scalar_one()
            is True
        )
        assert (
            connection.execute(
                text("SELECT status::text FROM import_job WHERE id = :id"),
                {"id": manual_id},
            ).scalar_one()
            == "SUCCEEDED"
        )


def test_partial_external_identity_and_artifact_locator_uniqueness(
    importer: EvidenceImporter,
    runtime_engine: Engine,
) -> None:
    first = _ingest(importer, b"identity-one", [SyntheticConnector()])
    second = _ingest(importer, b"identity-two", [SyntheticConnector()])
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity = Entity(entity_type=EntityType.PERSON, name="Synthetic")
        session.add(entity)
        session.flush()
        account = Account(
            entity_id=entity.id,
            identifier="bank",
            name="Bank",
            account_class=AccountClass.ASSET,
        )
        session.add(account)
        session.flush()
        session.add(
            SourceRecord(
                artifact_id=first.artifact_id,
                import_job_id=first.job_id,
                record_locator="manual:1",
                source="synthetic",
                parser_version="1",
                raw_fields={},
                normalized_fields={},
                account_id=account.id,
                external_transaction_id="external-1",
            )
        )
        session.commit()

        session.add(
            SourceRecord(
                artifact_id=second.artifact_id,
                import_job_id=second.job_id,
                record_locator="manual:2",
                source="synthetic",
                parser_version="1",
                raw_fields={},
                normalized_fields={},
                account_id=account.id,
                external_transaction_id="external-1",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add_all(
            [
                SourceRecord(
                    artifact_id=second.artifact_id,
                    import_job_id=second.job_id,
                    record_locator=f"null:{index}",
                    source="synthetic",
                    parser_version="1",
                    raw_fields={},
                    normalized_fields={},
                    account_id=account.id,
                    external_transaction_id=None,
                )
                for index in range(2)
            ]
        )
        session.commit()

        session.add(
            SourceRecord(
                artifact_id=first.artifact_id,
                import_job_id=first.job_id,
                record_locator="manual:1",
                source="synthetic",
                parser_version="1",
                raw_fields={},
                normalized_fields={},
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def _create_accounts(session: Session) -> tuple[UUID, UUID, UUID]:
    entity = Entity(entity_type=EntityType.PERSON, name="Audit test")
    session.add(entity)
    session.flush()
    bank = Account(
        entity_id=entity.id,
        identifier=f"bank-{uuid4().hex[:8]}",
        name="Bank",
        account_class=AccountClass.ASSET,
    )
    expense = Account(
        entity_id=entity.id,
        identifier=f"expense-{uuid4().hex[:8]}",
        name="Expense",
        account_class=AccountClass.EXPENSE,
    )
    session.add_all([bank, expense])
    session.commit()
    return entity.id, bank.id, expense.id


def _draft_entry(
    session: Session,
    entity_id: UUID,
    bank_id: UUID,
    expense_id: UUID,
    *,
    balanced: bool = True,
) -> JournalEntry:
    entry_id = uuid4()
    creation_event = append_audit_event(
        session,
        actor="pytest",
        action="journal.create",
        reason="synthetic draft",
        payload={"journal_entry_id": str(entry_id)},
    )
    entry = JournalEntry(
        id=entry_id,
        entity_id=entity_id,
        occurred_at=datetime.now(UTC),
        origin="pytest",
        status=JournalStatus.DRAFT,
        primary_account_id=bank_id,
        audit_event_id=creation_event,
    )
    session.add(entry)
    session.flush()
    session.add_all(
        [
            Posting(entry_id=entry.id, account_id=bank_id, amount_minor=-100),
            Posting(
                entry_id=entry.id,
                account_id=expense_id,
                amount_minor=100 if balanced else 99,
            ),
        ]
    )
    session.flush()
    return entry


def test_journal_creation_requires_fresh_semantic_audit(
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine) as session:
        entity_id, bank_id, _expense_id = _create_accounts(session)
        insert_statement = text(
            "INSERT INTO journal_entry "
            "(id, entity_id, occurred_at, origin, status, primary_account_id, "
            "audit_event_id) VALUES "
            "(:entry_id, :entity_id, now(), 'pytest', 'DRAFT', :account_id, "
            ":audit_event_id)"
        )

        wrong_action_id = uuid4()
        wrong_action = append_audit_event(
            session,
            actor="pytest",
            action="artifact.ingest",
            reason="wrong creation action",
            payload={"journal_entry_id": str(wrong_action_id)},
        )
        with pytest.raises(DBAPIError, match=r"action must be journal.create"):
            session.execute(
                insert_statement,
                {
                    "entry_id": wrong_action_id,
                    "entity_id": entity_id,
                    "account_id": bank_id,
                    "audit_event_id": wrong_action,
                },
            )
        session.rollback()

        wrong_target_id = uuid4()
        wrong_target = append_audit_event(
            session,
            actor="pytest",
            action="journal.create",
            reason="wrong creation target",
            payload={"journal_entry_id": str(uuid4())},
        )
        with pytest.raises(DBAPIError, match="target does not match"):
            session.execute(
                insert_statement,
                {
                    "entry_id": wrong_target_id,
                    "entity_id": entity_id,
                    "account_id": bank_id,
                    "audit_event_id": wrong_target,
                },
            )
        session.rollback()

        stale_id = uuid4()
        stale_event = append_audit_event(
            session,
            actor="pytest",
            action="journal.create",
            reason="stale creation event",
            payload={"journal_entry_id": str(stale_id)},
        )
        session.commit()
        with pytest.raises(DBAPIError, match="appended in this transaction"):
            session.execute(
                insert_statement,
                {
                    "entry_id": stale_id,
                    "entity_id": entity_id,
                    "account_id": bank_id,
                    "audit_event_id": stale_event,
                },
            )
        session.rollback()
        assert session.execute(text("SELECT count(*) FROM journal_entry")).scalar_one() == 0


def test_valid_post_transition_is_bound_and_reconstructable(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, bank_id, expense_id = _create_accounts(session)
        entry = _draft_entry(session, entity_id, bank_id, expense_id)
        session.commit()
        event_id = post_journal_entry(
            session,
            entry.id,
            actor="operator-1",
            reason="reviewed synthetic entry",
        )
        session.commit()

        row = session.execute(
            text(
                "SELECT j.id, j.status::text, a.id, a.actor, a.action, a.reason, "
                "a.occurred_at, a.payload ->> 'journal_entry_id' "
                "FROM journal_entry j JOIN audit_event a "
                "ON a.id = j.posted_audit_event_id WHERE j.id = :id"
            ),
            {"id": entry.id},
        ).one()
        assert row == (
            entry.id,
            "POSTED",
            event_id,
            "operator-1",
            "journal.post",
            "reviewed synthetic entry",
            row.occurred_at,
            str(entry.id),
        )
        assert row.occurred_at.tzinfo is not None


def test_post_helper_rejects_missing_and_already_posted_entries(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        with pytest.raises(LookupError, match="does not exist"):
            post_journal_entry(
                session,
                uuid4(),
                actor="pytest",
                reason="missing",
            )
        entity_id, bank_id, expense_id = _create_accounts(session)
        entry = _draft_entry(session, entity_id, bank_id, expense_id)
        session.commit()
        post_journal_entry(
            session,
            entry.id,
            actor="pytest",
            reason="first post",
        )
        session.commit()
        with pytest.raises(ValueError, match="only DRAFT"):
            post_journal_entry(
                session,
                entry.id,
                actor="pytest",
                reason="second post",
            )


def test_post_transition_rejects_missing_wrong_and_stale_evidence(
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, bank_id, expense_id = _create_accounts(session)
        entry = _draft_entry(session, entity_id, bank_id, expense_id)
        session.commit()

        with pytest.raises(DBAPIError, match=r"requires journal.post"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(status=JournalStatus.POSTED)
            )
        session.rollback()

        wrong_action = append_audit_event(
            session,
            actor="pytest",
            action="journal.review",
            reason="wrong action",
            payload={"journal_entry_id": str(entry.id)},
        )
        with pytest.raises(DBAPIError, match=r"action must be journal.post"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(status=JournalStatus.POSTED, posted_audit_event_id=wrong_action)
            )
        session.rollback()

        wrong_target = append_audit_event(
            session,
            actor="pytest",
            action="journal.post",
            reason="wrong target",
            payload={"journal_entry_id": str(uuid4())},
        )
        with pytest.raises(DBAPIError, match="target does not match"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(status=JournalStatus.POSTED, posted_audit_event_id=wrong_target)
            )
        session.rollback()

        with pytest.raises(DBAPIError, match="creation audit"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(
                    status=JournalStatus.POSTED,
                    posted_audit_event_id=entry.audit_event_id,
                )
            )
        session.rollback()

        stale_event = append_audit_event(
            session,
            actor="pytest",
            action="journal.post",
            reason="stale event",
            payload={"journal_entry_id": str(entry.id)},
        )
        session.commit()
        with pytest.raises(DBAPIError, match="this transaction"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(status=JournalStatus.POSTED, posted_audit_event_id=stale_event)
            )


def test_post_audit_trigger_cannot_be_shadowed_by_temporary_tables(
    runtime_engine: Engine,
    admin_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, bank_id, expense_id = _create_accounts(session)
        entry = _draft_entry(session, entity_id, bank_id, expense_id)
        stale_event = append_audit_event(
            session,
            actor="pytest",
            action="unrelated.action",
            reason="must not authorize posting",
            payload={},
        )
        session.commit()
        entry_id = entry.id

    with admin_engine.begin() as connection:
        connection.execute(
            text(
                """
                DO $do$
                BEGIN
                    EXECUTE format(
                        'GRANT TEMPORARY ON DATABASE %I TO ledgerbridge_app',
                        current_database()
                    );
                END
                $do$;
                """
            )
        )

    try:
        runtime_engine.dispose()
        with runtime_engine.connect() as connection:
            transaction = connection.begin()
            try:
                assert connection.execute(
                    text("SELECT has_database_privilege(current_user, current_database(), 'TEMP')")
                ).scalar_one()
                connection.execute(
                    text("CREATE TEMP TABLE audit_event (id uuid, action text, payload jsonb)")
                )
                connection.execute(text("CREATE TEMP TABLE journal_entry (audit_event_id uuid)"))
                connection.execute(
                    text(
                        "INSERT INTO audit_event (id, action, payload) VALUES "
                        "(:id, 'journal.post', "
                        "jsonb_build_object('journal_entry_id', CAST(:target AS text)))"
                    ),
                    {"id": stale_event, "target": str(entry_id)},
                )
                with pytest.raises(DBAPIError, match="this transaction"):
                    connection.execute(
                        text(
                            "UPDATE public.journal_entry SET status = 'POSTED', "
                            "posted_audit_event_id = :event_id WHERE id = :entry_id"
                        ),
                        {"event_id": stale_event, "entry_id": entry_id},
                    )
            finally:
                transaction.rollback()
    finally:
        runtime_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DO $do$
                    BEGIN
                        EXECUTE format(
                            'REVOKE TEMPORARY ON DATABASE %I '
                            'FROM ledgerbridge_app',
                            current_database()
                        );
                    END
                    $do$;
                    """
                )
            )

    with admin_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status::text FROM journal_entry WHERE id = :id"),
            {"id": entry_id},
        ).scalar_one()
        assert status == "DRAFT"


def test_raw_artifact_requires_fresh_semantic_audit_and_digest_key_binding(
    runtime_engine: Engine,
) -> None:
    content = b"direct raw artifact"
    digest = hashlib.sha256(content).digest()
    digest_hex = digest.hex()
    storage_key = f"sha256/{digest_hex[:2]}/{digest_hex[2:4]}/{digest_hex}"
    insert_statement = text(
        "INSERT INTO raw_artifact "
        "(sha256, source, original_filename, media_type, byte_size, "
        "storage_key, audit_event_id) VALUES "
        "(:sha256, 'manual_upload', 'direct.bin', 'application/octet-stream', "
        ":byte_size, :storage_key, :audit_event_id)"
    )

    with Session(runtime_engine) as session:
        payload = {
            "sha256": digest_hex,
            "byte_size": len(content),
            "storage_key": storage_key,
            "source": "manual_upload",
            "original_filename_sha256": hashlib.sha256(b"direct.bin").hexdigest(),
            "media_type": "application/octet-stream",
        }
        wrong_action = append_audit_event(
            session,
            actor="pytest",
            action="unrelated.action",
            reason="wrong artifact action",
            payload=payload,
        )
        with pytest.raises(DBAPIError, match=r"action must be artifact\.ingest"):
            session.execute(
                insert_statement,
                {
                    "sha256": digest,
                    "byte_size": len(content),
                    "storage_key": storage_key,
                    "audit_event_id": wrong_action,
                },
            )
        session.rollback()

        stale_event = append_audit_event(
            session,
            actor="pytest",
            action="artifact.ingest",
            reason="stale artifact event",
            payload=payload,
        )
        session.commit()
        with pytest.raises(DBAPIError, match="appended in this transaction"):
            session.execute(
                insert_statement,
                {
                    "sha256": digest,
                    "byte_size": len(content),
                    "storage_key": storage_key,
                    "audit_event_id": stale_event,
                },
            )
        session.rollback()

        for field, value in (
            ("source", "other-source"),
            ("original_filename_sha256", hashlib.sha256(b"other.bin").hexdigest()),
            ("media_type", "text/plain"),
            ("byte_size", len(content) + 1),
        ):
            mismatch_event = append_audit_event(
                session,
                actor="pytest",
                action="artifact.ingest",
                reason=f"mismatched artifact {field}",
                payload={**payload, field: value},
            )
            with pytest.raises(DBAPIError, match="payload does not match"):
                session.execute(
                    insert_statement,
                    {
                        "sha256": digest,
                        "byte_size": len(content),
                        "storage_key": storage_key,
                        "audit_event_id": mismatch_event,
                    },
                )
            session.rollback()

        mismatched_key = f"sha256/00/00/{'0' * 64}"
        mismatch_event = append_audit_event(
            session,
            actor="pytest",
            action="artifact.ingest",
            reason="mismatched storage key",
            payload={**payload, "storage_key": mismatched_key},
        )
        with pytest.raises(DBAPIError, match="storage_key_matches_sha256"):
            session.execute(
                insert_statement,
                {
                    "sha256": digest,
                    "byte_size": len(content),
                    "storage_key": mismatched_key,
                    "audit_event_id": mismatch_event,
                },
            )


def test_balance_failure_rolls_back_post_audit_atomically(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, bank_id, expense_id = _create_accounts(session)
        before = session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()
        entry = _draft_entry(session, entity_id, bank_id, expense_id, balanced=False)
        event_id = post_journal_entry(
            session,
            entry.id,
            actor="pytest",
            reason="must roll back",
        )
        with pytest.raises(DBAPIError, match="unbalanced"):
            session.commit()
        session.rollback()
        assert session.execute(text("SELECT count(*) FROM audit_event")).scalar_one() == before
        assert session.get(JournalEntry, entry.id) is None
        assert (
            session.execute(
                text("SELECT count(*) FROM audit_event WHERE id = :id"), {"id": event_id}
            ).scalar_one()
            == 0
        )


def test_phase2_migration_round_trip_and_objects(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase2_rt_{uuid4().hex[:12]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, LAST_REVERSIBLE_REVISION)
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            inspector = inspect(connection)
            assert {"raw_artifact", "import_job", "source_record"} <= set(
                inspector.get_table_names()
            )
            expected_phase2_checks = {
                "ck_raw_artifact_raw_artifact_sha256_length",
                "ck_raw_artifact_raw_artifact_storage_key_matches_sha256",
                "ck_import_job_import_job_state_timestamps",
                "ck_source_record_source_record_raw_fields_object",
                "ck_journal_entry_journal_entry_posted_audit_binding",
            }
            phase2_table_checks = {
                str(constraint["name"])
                for table in ("raw_artifact", "import_job", "source_record")
                for constraint in inspector.get_check_constraints(table)
                if constraint["name"] is not None
            }
            journal_checks = {
                str(constraint["name"])
                for constraint in inspector.get_check_constraints("journal_entry")
                if constraint["name"] is not None
            }
            observed_phase2_checks = phase2_table_checks | journal_checks
            assert expected_phase2_checks <= observed_phase2_checks
            assert not any("_ck_" in name for name in phase2_table_checks)
            assert not any(
                "_ck_" in name and "posted_audit_binding" in name for name in journal_checks
            )
            journal_columns = {item["name"] for item in inspector.get_columns("journal_entry")}
            assert "posted_audit_event_id" in journal_columns
            source_fks = {item["name"] for item in inspector.get_foreign_keys("journal_entry")}
            assert "fk_journal_entry_source_record_id_source_record" in source_fks
            trigger_names = set(
                connection.execute(
                    text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
                ).scalars()
            )
            assert {
                "raw_artifact_no_update_delete",
                "raw_artifact_audit_binding",
                "source_record_no_update_delete",
                "import_job_state_machine",
                "journal_entry_post_audit_binding",
            } <= trigger_names

        temporary_engine.dispose()
        temporary_engine = None
        _run_alembic(rendered, "20260821_0002", downgrade=True)
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            inspector = inspect(connection)
            assert not (
                {"raw_artifact", "import_job", "source_record"} & set(inspector.get_table_names())
            )
            journal_columns = {item["name"] for item in inspector.get_columns("journal_entry")}
            assert "source_record_id" in journal_columns
            assert "posted_audit_event_id" not in journal_columns

        temporary_engine.dispose()
        temporary_engine = None
        _run_alembic(rendered, "head")
        temporary_engine = create_engine(temporary_url)
        assert {"raw_artifact", "import_job", "source_record"} <= set(
            inspect(temporary_engine).get_table_names()
        )
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_phase2_downgrade_refuses_to_delete_evidence(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase2_downgrade_{uuid4().hex[:10]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, LAST_REVERSIBLE_REVISION)
        temporary_engine = create_engine(temporary_url)
        content = b"downgrade evidence"
        digest = hashlib.sha256(content).digest()
        digest_hex = digest.hex()
        storage_key = f"sha256/{digest_hex[:2]}/{digest_hex[2:4]}/{digest_hex}"
        payload = {
            "sha256": digest_hex,
            "byte_size": len(content),
            "storage_key": storage_key,
            "source": "manual_upload",
            "original_filename_sha256": hashlib.sha256(b"downgrade.bin").hexdigest(),
            "media_type": "application/octet-stream",
        }
        with temporary_engine.begin() as connection:
            event_id = connection.execute(
                text(
                    "SELECT append_audit_event("
                    "'pytest', 'artifact.ingest', 'downgrade guard', NULL, "
                    "CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, sort_keys=True)},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO raw_artifact "
                    "(sha256, source, original_filename, media_type, byte_size, "
                    "storage_key, audit_event_id) VALUES "
                    "(:sha256, 'manual_upload', 'downgrade.bin', "
                    "'application/octet-stream', :byte_size, :storage_key, :event_id)"
                ),
                {
                    "sha256": digest,
                    "byte_size": len(content),
                    "storage_key": storage_key,
                    "event_id": event_id,
                },
            )

        temporary_engine.dispose()
        temporary_engine = None
        with pytest.raises(Exception, match="prevents destructive downgrade"):
            _run_alembic(rendered, "20260821_0002", downgrade=True)

        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260824_0015"
            )
            assert connection.execute(text("SELECT count(*) FROM raw_artifact")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM ingest_channel")).scalar_one() == 2
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_phase3_registry_migration_round_trip_preserves_security_controls(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase3_roundtrip_{uuid4().hex[:10]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, LAST_REVERSIBLE_REVISION)
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM ingest_channel")).scalar_one() == 2
            assert connection.execute(text("SELECT count(*) FROM source_system")).scalar_one() == 1
        temporary_engine.dispose()
        temporary_engine = None

        _run_alembic(rendered, "20260821_0003", downgrade=True)
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260821_0003"
            )
            assert (
                connection.execute(text("SELECT to_regclass('public.ingest_channel')")).scalar_one()
                is None
            )
        temporary_engine.dispose()
        temporary_engine = None

        _run_alembic(rendered, "head")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT proconfig FROM pg_proc JOIN pg_namespace "
                    "ON pg_namespace.oid = pg_proc.pronamespace "
                    "WHERE nspname = 'public' AND proname = 'registry_block_mutation'"
                )
            ).scalar_one() == ["search_path=pg_catalog"]
            grants = connection.execute(
                text(
                    "SELECT table_name, privilege_type "
                    "FROM information_schema.role_table_grants "
                    "WHERE grantee = 'ledgerbridge_app' "
                    "AND table_name IN ('ingest_channel', 'source_system') "
                    "ORDER BY table_name, privilege_type"
                )
            ).all()
            assert len(grants) == 2
            assert grants[0][0] == "ingest_channel"
            assert grants[0][1] == "SELECT"
            assert grants[1][0] == "source_system"
            assert grants[1][1] == "SELECT"
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_phase3_upgrade_rolls_back_on_unregistered_legacy_provenance(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase3_legacy_{uuid4().hex[:10]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, "20260821_0003")
        temporary_engine = create_engine(temporary_url)
        content = b"legacy provenance"
        digest = hashlib.sha256(content).digest()
        digest_hex = digest.hex()
        storage_key = f"sha256/{digest_hex[:2]}/{digest_hex[2:4]}/{digest_hex}"
        payload = {
            "sha256": digest_hex,
            "byte_size": len(content),
            "storage_key": storage_key,
            "source": "legacy-source",
            "original_filename_sha256": hashlib.sha256(b"legacy.bin").hexdigest(),
            "media_type": "application/octet-stream",
        }
        with temporary_engine.begin() as connection:
            event_id = connection.execute(
                text(
                    "SELECT append_audit_event("
                    "'pytest', 'artifact.ingest', 'legacy provenance', NULL, "
                    "CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(payload, sort_keys=True)},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO raw_artifact "
                    "(sha256, source, original_filename, media_type, byte_size, "
                    "storage_key, audit_event_id) VALUES "
                    "(:sha256, 'legacy-source', 'legacy.bin', 'application/octet-stream', "
                    ":byte_size, :storage_key, :event_id)"
                ),
                {
                    "sha256": digest,
                    "byte_size": len(content),
                    "storage_key": storage_key,
                    "event_id": event_id,
                },
            )
        temporary_engine.dispose()
        temporary_engine = None

        with pytest.raises(Exception, match="registered channel"):
            _run_alembic(rendered, "head")

        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260821_0003"
            )
            assert (
                connection.execute(text("SELECT to_regclass('public.ingest_channel')")).scalar_one()
                is None
            )
            assert connection.execute(text("SELECT count(*) FROM raw_artifact")).scalar_one() == 1
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_phase2_migration_fails_closed_on_existing_posted_entry(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase2_posted_{uuid4().hex[:10]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, "20260821_0002")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as connection:
            audit_id = connection.execute(
                text(
                    "SELECT append_audit_event('pytest', 'journal.create', 'posted test', "
                    "NULL, '{}'::jsonb)"
                )
            ).scalar_one()
            entity_id = connection.execute(
                text(
                    "INSERT INTO entity (entity_type, name) VALUES ('PERSON', 'Posted') "
                    "RETURNING id"
                )
            ).scalar_one()
            account_ids = (
                connection.execute(
                    text(
                        "INSERT INTO account (entity_id, identifier, name, account_class) VALUES "
                        "(:entity_id, 'bank', 'Bank', 'ASSET'), "
                        "(:entity_id, 'expense', 'Expense', 'EXPENSE') RETURNING id"
                    ),
                    {"entity_id": entity_id},
                )
                .scalars()
                .all()
            )
            entry_id = connection.execute(
                text(
                    "INSERT INTO journal_entry (entity_id, occurred_at, origin, status, "
                    "primary_account_id, audit_event_id) VALUES "
                    "(:entity_id, now(), 'pytest', 'DRAFT', :account_id, :audit_id) RETURNING id"
                ),
                {
                    "entity_id": entity_id,
                    "account_id": account_ids[0],
                    "audit_id": audit_id,
                },
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO posting (entry_id, account_id, amount_minor, currency) VALUES "
                    "(:entry_id, :bank_id, -100, 'CNY'), "
                    "(:entry_id, :expense_id, 100, 'CNY')"
                ),
                {
                    "entry_id": entry_id,
                    "bank_id": account_ids[0],
                    "expense_id": account_ids[1],
                },
            )
            connection.execute(
                text("UPDATE journal_entry SET status = 'POSTED' WHERE id = :id"),
                {"id": entry_id},
            )
        temporary_engine.dispose()
        temporary_engine = None
        with pytest.raises(Exception, match="explicit audit binding"):
            _run_alembic(rendered, "head")
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_phase2_migration_fails_closed_on_orphan_source_placeholder(
    migration_database_url: str,
) -> None:
    owner_url = make_url(migration_database_url)
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_phase2_orphan_{uuid4().hex[:10]}"
    maintenance_engine = _maintenance_engine(_test_admin_url())
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, "20260821_0002")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as connection:
            audit_id = connection.execute(
                text(
                    "SELECT append_audit_event('pytest', 'journal.create', 'orphan test', "
                    "NULL, '{}'::jsonb)"
                )
            ).scalar_one()
            entity_id = connection.execute(
                text(
                    "INSERT INTO entity (entity_type, name) VALUES ('PERSON', 'Orphan') "
                    "RETURNING id"
                )
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO journal_entry (entity_id, occurred_at, origin, status, "
                    "source_record_id, audit_event_id) VALUES "
                    "(:entity_id, now(), 'pytest', 'DRAFT', :source_id, :audit_id)"
                ),
                {"entity_id": entity_id, "source_id": uuid4(), "audit_id": audit_id},
            )
        temporary_engine.dispose()
        temporary_engine = None
        with pytest.raises(Exception, match="source_record"):
            _run_alembic(rendered, "head")
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()
