"""Import one audited hotel payout OCR/bank-evidence cutover."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from sqlalchemy import create_engine

from ledgerbridge.hotel_payout_cutover import import_hotel_payout_cutover


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--prepared-manifest", type=Path, required=True)
    parser.add_argument("--cutover-manifest", type=Path, required=True)
    args = parser.parse_args()
    database_url = os.environ.get("LEDGERBRIDGE_IMPORT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("LEDGERBRIDGE_IMPORT_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = import_hotel_payout_cutover(
            engine,
            source_manifest_path=args.source_manifest.resolve(),
            prepared_manifest_path=args.prepared_manifest.resolve(),
            cutover_manifest_path=args.cutover_manifest.resolve(),
        )
    finally:
        engine.dispose()
    print(
        "HOTEL_PAYOUT_IMPORT_OK "
        f"replayed={str(result.replayed).lower()} "
        f"ignored={result.ignored_candidate_count} "
        f"imported={result.imported_candidate_count} links={result.link_count} "
        f"horizon={result.audit_horizon_sequence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
