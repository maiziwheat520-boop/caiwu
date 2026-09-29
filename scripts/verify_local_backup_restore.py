"""Compare a restored local ledger with its sealed backup row inventory.

Run against an isolated restore or the new target before exposing its readers.
The database URL is read from the environment, never accepted on the command line.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql


def verify(inventory_path: Path, database_url: str, expected_revision: str) -> None:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    counts = inventory.get("row_counts")
    if not isinstance(counts, dict) or not counts:
        raise ValueError("backup row inventory is missing")
    if not all(isinstance(name, str) and name.isidentifier() and isinstance(count, int)
               and count >= 0 for name, count in counts.items()):
        raise ValueError("backup row inventory contains invalid entries")

    differences: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            row = cursor.fetchone()
            revision = row[0] if row else None
            if revision != expected_revision:
                differences.append(f"revision: expected {expected_revision}, observed {revision}")
            for name, expected in sorted(counts.items()):
                cursor.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier("public"), sql.Identifier(name)
                    )
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(f"missing count for {name}")
                observed = row[0]
                if observed != expected:
                    differences.append(f"{name}: expected {expected}, observed {observed}")
            if expected_revision >= "20260929_0053":
                for schema, table in (("payroll", "batch"), ("reconciliation_legacy", "source")):
                    if schema == "reconciliation_legacy" and expected_revision < "20260929_0054":
                        continue
                    cursor.execute(
                        sql.SQL("SELECT count(*) FROM {}.{}").format(
                            sql.Identifier(schema), sql.Identifier(table)
                        )
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError(f"missing count for {schema}.{table}")
                    observed = row[0]
                    if observed != 0:
                        differences.append(
                            f"{schema}.{table}: expected empty new table, observed {observed}"
                        )
    if differences:
        raise ValueError("restore inventory mismatch: " + "; ".join(differences))
    print(f"verified {len(counts)} historical tables at {expected_revision}; new tables empty")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    database_url = os.getenv("LEDGERBRIDGE_DATABASE_URL")
    if not database_url:
        raise SystemExit("LEDGERBRIDGE_DATABASE_URL is required")
    verify(args.inventory, database_url, args.revision)


if __name__ == "__main__":
    main()
