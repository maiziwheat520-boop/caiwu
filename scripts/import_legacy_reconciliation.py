"""One-time, migration-owner import of an original reconciliation workbook from stdin.

The operator must complete encrypted backup and isolated restore before running
this command in production. No source bytes or cell values are written to logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.reconciliation_legacy_archive import (
    DatabaseLegacyArchiveStore,
    LegacyArchiveError,
    extract_legacy_archive_bytes,
)


def migration_database_url(settings: Settings) -> str:
    """Keep the one-time import separate from long-lived worker credentials."""
    database_url = settings.database_url
    if settings.runtime_role != "migrate" or database_url is None:
        raise LegacyArchiveError("历史对账导入必须使用迁移专用数据库身份")
    try:
        username = make_url(database_url).username
    except (TypeError, ValueError) as exc:
        raise LegacyArchiveError("历史对账迁移数据库地址无效") from exc
    if username != "ledgerbridge_owner":
        raise LegacyArchiveError("历史对账导入必须使用迁移专用数据库身份")
    return database_url


def main() -> int:
    parser = argparse.ArgumentParser(description="Import an original reconciliation workbook")
    parser.add_argument("--filename", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--entity-ref", type=UUID, required=True)
    parser.add_argument(
        "--scope-pair",
        action="append",
        required=True,
        help="entity UUID and business-unit ref separated by a colon",
    )
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        scope_pairs = tuple(
            (UUID(item.split(":", 1)[0]), item.split(":", 1)[1]) for item in args.scope_pair
        )
    except (IndexError, ValueError) as exc:
        raise LegacyArchiveError("历史对账读取范围格式无效") from exc

    source_bytes = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
    digest = hashlib.sha256(source_bytes).hexdigest()
    if digest != args.expected_sha256:
        raise LegacyArchiveError("来源文件与操作员确认的摘要不一致")
    archive = extract_legacy_archive_bytes(source_bytes, args.filename)
    settings = get_settings()
    factory = get_session_factory(migration_database_url(settings))
    with factory() as session, session.begin():
        source_ref = DatabaseLegacyArchiveStore(session).persist(
            entity_ref=args.entity_ref,
            scope_pairs=scope_pairs,
            actor_ref=args.actor,
            archive=archive,
        )
        stored = (
            session.execute(
                text(
                    """
                SELECT COUNT(*) AS sheet_count, SUM(cell_count) AS cell_count
                  FROM reconciliation_legacy.sheet WHERE source_ref = :source_ref
                """
                ),
                {"source_ref": source_ref},
            )
            .mappings()
            .one()
        )
        if stored["sheet_count"] != len(archive.sheets) or stored["cell_count"] != sum(
            sheet.cell_count for sheet in archive.sheets
        ):
            raise LegacyArchiveError("数据库回读数量与来源不一致")
    print(
        json.dumps(
            {
                "source_ref": str(source_ref),
                "source_sha256": archive.source_sha256,
                "sheet_count": len(archive.sheets),
                "cell_count": sum(sheet.cell_count for sheet in archive.sheets),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
