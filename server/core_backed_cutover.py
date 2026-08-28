"""One-way preparation of the Web authentication database for Core-backed mode."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


PREVIEW_TABLES = (
    "idempotency_responses",
    "workbook_drafts",
    "review_events",
    "candidates",
    "metadata",
    "schema_migrations",
)
PREVIEW_TRIGGERS = (
    "review_events_no_update",
    "review_events_no_delete",
)
AUTH_TABLES = (
    "auth_schema",
    "auth_user",
    "passkey_credentials",
    "recovery_codes",
    "auth_sessions",
    "auth_state",
)


@dataclass(frozen=True, slots=True)
class CutoverResult:
    backup_path: Path
    backup_sha256: str
    removed_rows: int
    preserved_auth_rows: int


def prepare_core_backed_state(database: Path, backup_directory: Path) -> CutoverResult:
    """Back up the enrolled database, then remove only preview business storage."""

    database = database.resolve(strict=True)
    if not database.is_file():
        raise ValueError("Web state database must be a regular file")
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_directory = backup_directory.resolve(strict=True)
    if database.parent == backup_directory and database.name.startswith("pre-core-backed-"):
        raise ValueError("refusing to prepare a backup as the active database")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = backup_directory / f"pre-core-backed-{timestamp}.sqlite3"
    if backup_path.exists():
        raise FileExistsError("cutover backup already exists")

    with closing(sqlite3.connect(database, timeout=5)) as source:
        source.execute("PRAGMA foreign_keys = ON")
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Web state database failed integrity_check")
        tables = _tables(source)
        missing_auth = sorted(set(AUTH_TABLES) - tables)
        if missing_auth:
            raise RuntimeError(
                "Web authentication schema is incomplete: " + ", ".join(missing_auth)
            )
        auth_before = _row_count(source, AUTH_TABLES)
        preview_before = _row_count(source, tuple(table for table in PREVIEW_TABLES if table in tables))

        with closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Web state backup failed integrity_check")

        source.execute("BEGIN IMMEDIATE")
        try:
            for trigger in PREVIEW_TRIGGERS:
                source.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            for table in PREVIEW_TABLES:
                source.execute(f'DROP TABLE IF EXISTS "{table}"')
            source.execute("PRAGMA user_version = 0")
            if _row_count(source, AUTH_TABLES) != auth_before:
                raise RuntimeError("authentication rows changed during preview cleanup")
            source.commit()
        except BaseException:
            source.rollback()
            raise

        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Web state database failed post-cutover integrity_check")
        if set(PREVIEW_TABLES) & _tables(source):
            raise RuntimeError("preview business tables remain after cutover")

    try:
        os.chmod(backup_path, 0o600)
        os.chmod(database, 0o600)
    except OSError:
        pass
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    return CutoverResult(backup_path, digest, preview_before, auth_before)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _row_count(connection: sqlite3.Connection, tables: tuple[str, ...]) -> int:
    return sum(
        int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
    )
