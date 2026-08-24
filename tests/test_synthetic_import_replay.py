from __future__ import annotations

import os
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.imports import EvidenceImporter, IngestMetadata
from ledgerbridge.synthetic_connector import SyntheticBankConnector

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_bank_statement.json"


@pytest.fixture(scope="module")
def replay_database_url() -> Iterator[str]:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    owner_url = create_engine(value).url
    database_name = f"ledgerbridge_phase6_replay_{uuid4().hex[:12]}"
    maintenance_engine = create_engine(
        owner_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    temporary_url = owner_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        rendered = temporary_url.render_as_string(hide_password=False)
        config = Config("alembic.ini")
        config.attributes["database_url"] = rendered
        command.upgrade(config, "head")
        temporary_engine = create_engine(temporary_url)
        temporary_engine.dispose()
        temporary_engine = None
        yield rendered
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


@pytest.fixture(scope="module")
def replay_runtime_url(replay_database_url: str) -> str:
    value = os.environ.get("LEDGERBRIDGE_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_DATABASE_URL")
    runtime_url = create_engine(value).url
    temporary_database = create_engine(replay_database_url).url.database
    with create_engine(replay_database_url).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_system (id, description) VALUES "
                "('synthetic_bank', 'Synthetic test-only bank source') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
    return runtime_url.set(database=temporary_database).render_as_string(hide_password=False)


def test_synthetic_fixture_replays_through_importer(
    replay_runtime_url: str,
    tmp_path: Path,
) -> None:
    content = FIXTURE.read_bytes()
    importer = EvidenceImporter(
        sessionmaker(bind=create_engine(replay_runtime_url), expire_on_commit=False),
        ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000, chunk_size=127),
        detection_prefix_bytes=64,
        max_records=100,
    )
    metadata = IngestMetadata(
        source="synthetic_upload",
        original_filename="synthetic_bank_statement.json",
        media_type="application/json",
    )

    first = importer.ingest_and_import(
        content_stream(content),
        metadata,
        [SyntheticBankConnector()],
        actor="phase6-test",
        reason="synthetic connector replay",
    )
    second = importer.ingest_and_import(
        content_stream(content),
        metadata,
        [SyntheticBankConnector()],
        actor="phase6-test",
        reason="synthetic connector replay retry",
    )

    assert first.status.value == "SUCCEEDED", first.error_code
    assert first.parsed_count == 2
    assert first.created_count == 2
    assert second.status is first.status
    with create_engine(replay_runtime_url).connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM source_record "
                    "WHERE source = 'synthetic_bank' AND record_locator LIKE 'statement:2026-08:%'"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM import_job WHERE id = :job_id"),
                {"job_id": first.job_id},
            ).scalar_one()
            == 1
        )


def content_stream(content: bytes) -> BytesIO:
    return BytesIO(content)
