"""Bootstrap the non-rotating controlled-import evidence key."""

from __future__ import annotations

import argparse
from pathlib import Path

from ledgerbridge.file_key_provider import FileKeyProvider, bootstrap_file_key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    path = args.key_file.resolve()
    bootstrap_file_key(path, generation=args.generation)
    provider = FileKeyProvider(path)
    print(f"CONTROLLED_IMPORT_KEY_OK generation={provider.active_generation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
