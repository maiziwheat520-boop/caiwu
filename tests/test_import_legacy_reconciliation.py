"""The one-time original workbook import must not widen worker privileges."""

from pathlib import Path

import pytest

from ledgerbridge.config import Settings
from ledgerbridge.reconciliation_legacy_archive import LegacyArchiveError
from scripts.import_legacy_reconciliation import migration_database_url


def test_import_uses_migration_owner(tmp_path: Path) -> None:
    url = "postgresql+psycopg://ledgerbridge_owner@localhost/ledgerbridge"
    settings = Settings(
        env="test", runtime_role="migrate", database_url=url, artifact_root=tmp_path
    )
    assert migration_database_url(settings) == url


@pytest.mark.parametrize("role", ["ledgerbridge_worker", "ledgerbridge_api"])
def test_import_refuses_runtime_database_roles(role: str, tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        runtime_role="migrate",
        database_url=f"postgresql+psycopg://{role}@localhost/ledgerbridge",
        artifact_root=tmp_path,
    )
    with pytest.raises(LegacyArchiveError, match="迁移专用数据库身份"):
        migration_database_url(settings)
