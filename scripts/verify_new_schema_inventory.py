"""Run the release backup inventory gate against an isolated PostgreSQL copy."""

from __future__ import annotations

import json
import os

import psycopg

from scripts.backup_restore import (
    _new_schema_inventory_sql,
    _validate_new_schema_inventory,
)


def main() -> None:
    database_url = os.getenv("LEDGERBRIDGE_DATABASE_URL")
    if not database_url:
        raise SystemExit("LEDGERBRIDGE_DATABASE_URL is required")
    with psycopg.connect(database_url) as conn, conn.cursor() as cursor:
        for schema in ("payroll", "reconciliation_legacy"):
            cursor.execute(_new_schema_inventory_sql(schema))
            row = cursor.fetchone()
            if row is None or not isinstance(row[0], str):
                raise RuntimeError(f"{schema} inventory query returned no JSON")
            inventory = json.loads(row[0])
            _validate_new_schema_inventory(inventory, schema)
            print(f"{schema}: {len(inventory['row_counts'])} tables and ACLs verified")


if __name__ == "__main__":
    main()
