from __future__ import annotations

import argparse
import json
from pathlib import Path

from server.core_backed_cutover import prepare_core_backed_state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Back up Web state and remove synthetic preview business rows."
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-directory", type=Path, required=True)
    parser.add_argument(
        "--acknowledge-preview-data-removal",
        action="store_true",
        help="Required acknowledgement for the one-way preview cleanup.",
    )
    args = parser.parse_args()
    if not args.acknowledge_preview_data_removal:
        raise SystemExit("Refusing preview cleanup without the explicit acknowledgement")
    result = prepare_core_backed_state(args.database, args.backup_directory)
    print(
        json.dumps(
            {
                "backup_path": str(result.backup_path),
                "backup_sha256": result.backup_sha256,
                "removed_preview_rows": result.removed_rows,
                "preserved_auth_rows": result.preserved_auth_rows,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
