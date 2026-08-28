"""Encrypt a controlled review source bundle and persist its descriptor."""

from __future__ import annotations

import argparse
from pathlib import Path

from ledgerbridge.controlled_import import prepare_source_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--prepared-manifest", type=Path, required=True)
    args = parser.parse_args()
    prepared = prepare_source_manifest(
        args.source_manifest.resolve(),
        key_file=args.key_file.resolve(),
        artifact_root=args.artifact_root.resolve(),
        prepared_manifest_path=args.prepared_manifest.resolve(),
    )
    print(
        "CONTROLLED_REVIEW_PREPARED_OK "
        f"evidence={len(prepared.evidence)} candidates={len(prepared.candidates)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
