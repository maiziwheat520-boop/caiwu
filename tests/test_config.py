from pathlib import Path

import pytest
from pydantic import ValidationError

from ledgerbridge.config import Settings, escape_alembic_ini_value, get_settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="database_url"):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_artifact_root_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        Settings(database_url="sqlite+pysqlite:///:memory:", artifact_root=Path("relative"))


def test_artifact_quota_defaults_are_production_safe(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:", artifact_root=tmp_path.resolve()
    )

    assert settings.artifact_max_bytes == 50 * 1024 * 1024
    assert settings.artifact_total_max_bytes == 10 * 1024 * 1024 * 1024
    assert settings.artifact_staging_max_bytes == 512 * 1024 * 1024
    assert settings.artifact_staging_ttl_seconds == 60 * 60
    assert settings.enable_internal_upload is False

    with pytest.raises(ValidationError, match="less than or equal"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            artifact_total_max_bytes=2**63,
        )


def test_alembic_url_escapes_config_interpolation() -> None:
    assert escape_alembic_ini_value("postgresql://user:p%ss@db/name") == (
        "postgresql://user:p%%ss@db/name"
    )
