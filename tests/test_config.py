from pathlib import Path

import pytest
from pydantic import ValidationError

from ledgerbridge.config import Settings, escape_alembic_ini_value, get_settings


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("LEDGERBRIDGE_ARTIFACT_ROOT", str(Path.cwd()))
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
    assert settings.enable_internal_read_api is False
    assert settings.enable_real_ingest is False

    with pytest.raises(ValidationError, match="less than or equal"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            artifact_total_max_bytes=2**63,
        )


def test_real_ingest_is_unconditionally_unavailable_during_s1(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="S1 operational and I1 gates"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            enable_real_ingest=True,
        )


def test_internal_read_api_is_generation_bound_and_reader_url_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="internal_read_policy_generation"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_api=True,
        )

    enabled = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path.resolve(),
        enable_internal_read_api=True,
        internal_read_policy_generation=7,
    )
    assert enabled.internal_read_policy_generation == 7

    with pytest.raises(ValidationError, match="reader_database_url"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_api=True,
            internal_read_backend="database",
            internal_read_policy_generation=7,
        )

    database_reader = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        reader_database_url="postgresql+psycopg://ledgerbridge_reader@db/app",
        artifact_root=tmp_path.resolve(),
        enable_internal_read_api=True,
        internal_read_backend="database",
        internal_read_cursor_key="k" * 32,
        internal_read_policy_generation=7,
    )
    assert database_reader.resolved_reader_database_url().endswith("/app")

    with pytest.raises(ValidationError, match="R1 operational gate"):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_api@db/app",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_api=True,
            internal_read_policy_generation=7,
        )

    production_reader = Settings(
        env="production",
        runtime_role="api",
        api_database_url="postgresql://ledgerbridge_api@db/app",
        reader_database_url="postgresql://ledgerbridge_reader@db/app",
        artifact_root=tmp_path.resolve(),
        enable_internal_read_api=True,
        internal_read_backend="database",
        internal_read_cursor_key="c" * 32,
        internal_read_policy_generation=7,
        internal_read_operational_gate="r1-production-v1",
        internal_read_transport="unix-mtls-proxy",
        internal_read_mtls_policy_path=(tmp_path / "policy.json").resolve(),
        enable_internal_read_persistent_audit=True,
        enable_internal_read_persistent_receipt=True,
    )
    assert production_reader.internal_read_backend == "database"

    with pytest.raises(ValidationError, match="requires the database reader backend"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_api=True,
            internal_read_policy_generation=7,
            enable_internal_read_persistent_receipt=True,
        )

    with pytest.raises(ValidationError, match="production internal read persistent receipt"):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_api@db/app",
            artifact_root=tmp_path.resolve(),
            enable_internal_read_persistent_receipt=True,
        )


def test_candidate_command_api_is_complete_synthetic_and_nonproduction(
    tmp_path: Path,
) -> None:
    base = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "artifact_root": tmp_path.resolve(),
        "enable_internal_read_api": True,
        "internal_read_policy_generation": 7,
        "enable_internal_candidate_command_api": True,
    }
    with pytest.raises(ValidationError, match="assertion key, issuer, and audience"):
        Settings(**base)

    enabled = Settings(
        **base,
        internal_command_assertion_key="k" * 32,
        internal_command_assertion_issuer="ledgerbridge-web-test",
        internal_command_assertion_audience="ledgerbridge-core-test",
    )
    assert enabled.enable_internal_candidate_command_api is True

    with pytest.raises(ValidationError, match="synthetic-only"):
        Settings(
            **base,
            internal_read_backend="database",
            reader_database_url="postgresql+psycopg://ledgerbridge_reader@db/app",
            internal_read_cursor_key="c" * 32,
            internal_command_assertion_key="k" * 32,
            internal_command_assertion_issuer="ledgerbridge-web-test",
            internal_command_assertion_audience="ledgerbridge-core-test",
        )

    with pytest.raises(ValidationError, match="production candidate command API"):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_api@db/app",
            artifact_root=tmp_path.resolve(),
            enable_internal_candidate_command_api=True,
            internal_command_assertion_key="k" * 32,
            internal_command_assertion_issuer="ledgerbridge-web-test",
            internal_command_assertion_audience="ledgerbridge-core-test",
        )


def test_mail_provider_defaults_disabled_and_bounded(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:", artifact_root=tmp_path.resolve()
    )
    assert settings.mail_provider == "disabled"
    assert settings.mail_max_messages == 100
    assert settings.mail_graph_page_size == 20
    assert settings.mail_timeout_seconds == 30.0

    enabled = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path.resolve(),
        mail_provider="microsoft_graph",
        mailbox_id="ops@example.test",
    )
    assert enabled.mailbox_id == "ops@example.test"

    with pytest.raises(ValidationError, match="mailbox_id"):
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            artifact_root=tmp_path.resolve(),
            mail_provider="microsoft_graph",
        )

    with pytest.raises(ValidationError, match="remains disabled"):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_api@db/app",
            artifact_root=tmp_path.resolve(),
            mail_provider="microsoft_graph",
            mailbox_id="ops@example.test",
        )


def test_database_role_urls_fallback_outside_production_and_split_in_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "LEDGERBRIDGE_API_DATABASE_URL",
        "LEDGERBRIDGE_WORKER_DATABASE_URL",
        "LEDGERBRIDGE_READER_DATABASE_URL",
    ):
        monkeypatch.delenv(variable, raising=False)
    shared = "sqlite+pysqlite:///:memory:"
    settings = Settings(database_url=shared, artifact_root=tmp_path.resolve())
    assert settings.resolved_api_database_url() == shared
    assert settings.resolved_worker_database_url() == shared

    migration = Settings(
        env="production",
        runtime_role="migrate",
        database_url="postgresql://ledgerbridge_owner@db/app",
        artifact_root=tmp_path.resolve(),
    )
    assert migration.database_url == "postgresql://ledgerbridge_owner@db/app"

    with pytest.raises(ValidationError, match="production runtime_role must be explicit"):
        Settings(
            env="production",
            database_url="postgresql://ledgerbridge_app@db/app",
            artifact_root=tmp_path.resolve(),
        )

    with pytest.raises(ValidationError, match="ledgerbridge_owner"):
        Settings(
            env="production",
            runtime_role="migrate",
            database_url="postgresql://ledgerbridge_app@db/app",
            artifact_root=tmp_path.resolve(),
        )

    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            env="production",
            runtime_role="api",
            database_url=shared,
            api_database_url="postgresql://same",
            worker_database_url="postgresql://same",
            artifact_root=tmp_path.resolve(),
        )

    production_api = Settings(
        env="production",
        runtime_role="api",
        api_database_url="postgresql://ledgerbridge_api@db/app",
        artifact_root=tmp_path.resolve(),
    )
    assert production_api.resolved_api_database_url() == "postgresql://ledgerbridge_api@db/app"

    production_worker = Settings(
        env="production",
        runtime_role="worker",
        worker_database_url="postgresql://ledgerbridge_worker@db/app",
        artifact_root=tmp_path.resolve(),
    )
    assert production_worker.resolved_worker_database_url() == (
        "postgresql://ledgerbridge_worker@db/app"
    )

    with pytest.raises(ValueError, match="production API requires runtime_role=api"):
        migration.resolved_api_database_url()
    with pytest.raises(ValueError, match="production worker requires runtime_role=worker"):
        migration.resolved_worker_database_url()

    with pytest.raises(
        ValidationError,
        match="dedicated runtime roles: ledgerbridge_api",
    ):
        Settings(
            env="production",
            runtime_role="api",
            api_database_url="postgresql://ledgerbridge_app@db/app",
            artifact_root=tmp_path.resolve(),
        )

    with pytest.raises(ValidationError, match="worker_database_url"):
        Settings(env="production", runtime_role="worker", artifact_root=tmp_path.resolve())


def test_alembic_url_escapes_config_interpolation() -> None:
    assert escape_alembic_ini_value("postgresql://user:p%ss@db/name") == (
        "postgresql://user:p%%ss@db/name"
    )
