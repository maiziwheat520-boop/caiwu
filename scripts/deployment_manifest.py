"""Create and verify a deterministic LedgerBridge deployment manifest."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import re
from pathlib import Path, PurePosixPath

MANIFEST_VERSION = "ledgerbridge-deployment-manifest-v1"
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
ROOT_EXCLUDED_DIRECTORIES = {"data", "secrets", "var"}
EXCLUDED_FILENAMES = {".env", ".coverage", "DEPLOYED_REVISION", "MANIFEST.sha256"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_files(root: Path, manifest_path: Path) -> dict[str, Path]:
    root = root.resolve()
    manifest_path = manifest_path.resolve()
    files: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if relative.parts[0] in ROOT_EXCLUDED_DIRECTORIES or any(
            part in EXCLUDED_DIRECTORIES for part in relative.parts
        ):
            continue
        if candidate.resolve() == manifest_path or candidate.name in EXCLUDED_FILENAMES:
            continue
        if candidate.is_symlink():
            raise ValueError(f"deployment trees may not contain symlinks: {relative.as_posix()}")
        if candidate.is_file():
            files[relative.as_posix()] = candidate
    return dict(sorted(files.items()))


def create_manifest(root: Path, output: Path, revision: str) -> int:
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("revision must be a full lowercase 40-character Git SHA")
    root = root.resolve()
    output = output if output.is_absolute() else root / output
    files = _manifest_files(root, output)
    lines = [
        f"# {MANIFEST_VERSION}",
        f"# revision {revision}",
        *[f"{_sha256(path)}  {relative}" for relative, path in files.items()],
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return len(files)


def verify_manifest(root: Path, manifest: Path, expected_revision: str | None = None) -> int:
    root = root.resolve()
    manifest = manifest if manifest.is_absolute() else root / manifest
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2 or lines[0] != f"# {MANIFEST_VERSION}":
        raise ValueError("unsupported or missing deployment manifest version")
    prefix = "# revision "
    if not lines[1].startswith(prefix):
        raise ValueError("deployment manifest revision header is missing")
    revision = lines[1][len(prefix) :]
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("deployment manifest revision is invalid")
    if expected_revision is not None and not hmac.compare_digest(revision, expected_revision):
        raise ValueError("deployment manifest revision does not match DEPLOYED_REVISION")

    expected_hashes: dict[str, str] = {}
    for line in lines[2:]:
        if not line:
            continue
        digest, separator, relative = line.partition("  ")
        pure_path = PurePosixPath(relative)
        if (
            separator != "  "
            or HASH_PATTERN.fullmatch(digest) is None
            or pure_path.is_absolute()
            or ".." in pure_path.parts
            or relative in expected_hashes
        ):
            raise ValueError(f"invalid deployment manifest entry: {line!r}")
        expected_hashes[relative] = digest

    actual_files = _manifest_files(root, manifest)
    if set(expected_hashes) != set(actual_files):
        missing = sorted(set(expected_hashes) - set(actual_files))
        unexpected = sorted(set(actual_files) - set(expected_hashes))
        raise ValueError(f"deployment file set differs; missing={missing}, unexpected={unexpected}")

    for relative, expected_hash in expected_hashes.items():
        if not hmac.compare_digest(_sha256(actual_files[relative]), expected_hash):
            raise ValueError(f"deployment file hash differs: {relative}")
    return len(expected_hashes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--root", type=Path, default=Path("."))
    create.add_argument("--output", type=Path, default=Path("MANIFEST.sha256"))
    create.add_argument("--revision", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, default=Path("."))
    verify.add_argument("--manifest", type=Path, default=Path("MANIFEST.sha256"))
    verify.add_argument("--expected-revision")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "create":
        count = create_manifest(args.root, args.output, args.revision)
        print(f"created deployment manifest for {count} files")
        return
    count = verify_manifest(args.root, args.manifest, args.expected_revision)
    print(f"verified deployment manifest for {count} files")


if __name__ == "__main__":
    main()
