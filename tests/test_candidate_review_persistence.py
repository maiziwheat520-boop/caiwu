from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from ledgerbridge.models import ReviewItemKind
from ledgerbridge.review_service import ReviewService


@pytest.fixture(scope="module")
def isolated_review_urls() -> Iterator[tuple[str, str]]:
    owner_value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    worker_value = os.environ.get("LEDGERBRIDGE_WORKER_DATABASE_URL")
    if owner_value is None or worker_value is None:
        pytest.skip("PostgreSQL integration tests require migration and worker URLs")
    owner_url = create_engine(owner_value).url
    worker_url = create_engine(worker_value).url
    database_name = f"ledgerbridge_candidate_review_{uuid4().hex[:12]}"
    maintenance_engine = create_engine(
        owner_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    temporary_owner_url = owner_url.set(database=database_name)
    temporary_worker_url = worker_url.set(database=database_name)
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        config = Config("alembic.ini")
        config.attributes["database_url"] = temporary_owner_url.render_as_string(
            hide_password=False
        )
        command.upgrade(config, "head")
        temporary_engine = create_engine(temporary_owner_url)
        temporary_engine.dispose()
        temporary_engine = None
        yield (
            temporary_owner_url.render_as_string(hide_password=False),
            temporary_worker_url.render_as_string(hide_password=False),
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


def test_concurrent_worker_review_creation_converges_on_one_row(
    isolated_review_urls: tuple[str, str],
) -> None:
    _owner_url, worker_url = isolated_review_urls
    candidate_key = "a" * 64

    def create() -> UUID:
        service = ReviewService(
            sessionmaker(bind=create_engine(worker_url), expire_on_commit=False)
        )
        return service.create_review_item(
            kind=ReviewItemKind.DEDUP,
            summary="candidate requires review",
            payload={"record_locator": "synthetic:row-1"},
            actor="phase5-worker",
            reason="concurrent candidate admission",
            candidate_key=candidate_key,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = tuple(pool.map(lambda _index: create(), range(8)))

    assert len(set(ids)) == 1
    with create_engine(worker_url).connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM review_item WHERE candidate_key = :candidate_key"),
                {"candidate_key": candidate_key},
            ).scalar_one()
            == 1
        )
