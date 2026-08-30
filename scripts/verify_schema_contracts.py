"""Verify the deployed 0020/0021 database contracts without mutating data."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, cast

from sqlalchemy import create_engine, text

from scripts.backup_restore import (
    BANK_STATEMENT_SECURITY_REVISION,
    BANK_STATEMENT_SECURITY_SQL,
    COUNTERPARTY_SECURITY_SQL,
    R1_SECURITY_SQL,
    BackupError,
    _validate_bank_statement_security,
    _validate_counterparty_security,
    _validate_r1_database_security,
)


def _decode_object(value: object, *, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise BackupError(f"{label} security query returned an invalid object")
    return cast(dict[str, Any], value)


def main() -> int:
    database_url = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL", "").strip()
    if not database_url and os.environ.get("LEDGERBRIDGE_RUNTIME_ROLE") == "migrate":
        database_url = os.environ.get("LEDGERBRIDGE_DATABASE_URL", "").strip()
    if not database_url:
        raise BackupError("LEDGERBRIDGE_MIGRATION_DATABASE_URL is required")
    engine = create_engine(database_url, hide_parameters=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            current_user, owner = connection.execute(
                text(
                    "SELECT current_user, pg_get_userbyid(datdba) FROM pg_database "
                    "WHERE datname = current_database()"
                )
            ).one()
            if current_user != owner:
                raise BackupError("schema contract verifier must run as the database owner")
            if revision != BANK_STATEMENT_SECURITY_REVISION:
                raise BackupError(
                    f"schema contract verifier requires revision {BANK_STATEMENT_SECURITY_REVISION}"
                )
            metadata: dict[str, Any] = {
                "database_owner": owner,
                "alembic_version": revision,
            }
            metadata.update(
                _decode_object(
                    connection.execute(text(R1_SECURITY_SQL)).scalar_one(),
                    label="R1",
                )
            )
            _validate_r1_database_security(metadata)
            metadata.update(
                _decode_object(
                    connection.execute(text(COUNTERPARTY_SECURITY_SQL)).scalar_one(),
                    label="counterparty",
                )
            )
            _validate_counterparty_security(metadata)
            metadata.update(
                _decode_object(
                    connection.execute(text(BANK_STATEMENT_SECURITY_SQL)).scalar_one(),
                    label="bank statement",
                )
            )
            _validate_bank_statement_security(metadata)
    finally:
        engine.dispose()
    print(f"SCHEMA_SECURITY_CONTRACTS_OK {revision}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackupError as exc:
        print(f"SCHEMA_SECURITY_CONTRACTS_FAILED {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("SCHEMA_SECURITY_CONTRACTS_FAILED unexpected_error", file=sys.stderr)
        raise SystemExit(1) from None
