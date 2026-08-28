"""Import one prepared controlled-review batch as the database owner."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import create_engine

from ledgerbridge.controlled_import import import_prepared_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared-manifest", type=Path, required=True)
    args = parser.parse_args()
    database_url = os.environ.get("LEDGERBRIDGE_IMPORT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("LEDGERBRIDGE_IMPORT_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = import_prepared_manifest(engine, args.prepared_manifest.resolve())
    finally:
        engine.dispose()
    print(
        "CONTROLLED_REVIEW_IMPORT_OK "
        f"replayed={str(result.replayed).lower()} evidence={result.evidence_count} "
        f"candidates={result.candidate_count} horizon={result.audit_horizon_sequence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
