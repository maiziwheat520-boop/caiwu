"""One-shot confirmation of eligible historical Candidates for the disposable test period."""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine

from ledgerbridge.historical_auto_confirmation import (
    HistoricalAutoConfirmationSettings,
    confirm_existing_test_historical_candidates,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute-after-backup",
        action="store_true",
        required=True,
        help="confirm that a current backup and restore check completed before this one-shot",
    )
    parser.parse_args()
    settings = HistoricalAutoConfirmationSettings()
    if not settings.enabled:
        raise RuntimeError("LEDGERBRIDGE_TEST_HISTORICAL_AUTO_IMPORT_ENABLED must be true")
    database_url = os.environ.get("LEDGERBRIDGE_IMPORT_DATABASE_URL")
    if not database_url:
        raise RuntimeError("LEDGERBRIDGE_IMPORT_DATABASE_URL is required")
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        result = confirm_existing_test_historical_candidates(engine, settings)
    finally:
        engine.dispose()
    print(
        "TEST_HISTORICAL_AUTO_CONFIRM_OK "
        f"cutoff={result.cutoff_month} selected={result.selected_count} "
        f"confirmed={result.confirmed_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
