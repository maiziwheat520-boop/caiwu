from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.config import get_settings
from ledgerbridge.db import get_session, get_session_factory
from ledgerbridge.main import app


class HealthySession:
    def execute(self, _statement: object) -> None:
        return None


class FailingSession:
    def execute(self, _statement: object) -> None:
        raise SQLAlchemyError("database unavailable in test")


def _healthy_session() -> Iterator[Session]:
    yield cast(Session, HealthySession())


def _failing_session() -> Iterator[Session]:
    yield cast(Session, FailingSession())


def test_liveness() -> None:
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_schema_is_not_exposed() -> None:
    assert TestClient(app).get("/openapi.json").status_code == 404


def test_readiness_success() -> None:
    app.dependency_overrides[get_session] = _healthy_session
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_returns_503_when_database_fails() -> None:
    app.dependency_overrides[get_session] = _failing_session
    try:
        response = TestClient(app).get("/health/ready")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_real_session_can_be_injected_with_environment() -> None:
    # Exercise the lazy engine path without requiring PostgreSQL.
    get_session_factory.cache_clear()
    factory = get_session_factory("sqlite+pysqlite:///:memory:")
    try:
        with factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        factory.kw["bind"].dispose()
        get_session_factory.cache_clear()


def test_get_session_uses_environment_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LEDGERBRIDGE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("LEDGERBRIDGE_ARTIFACT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    get_session_factory.cache_clear()
    try:
        sessions = list(get_session())
        assert len(sessions) == 1
    finally:
        get_settings.cache_clear()
        get_session_factory.cache_clear()
