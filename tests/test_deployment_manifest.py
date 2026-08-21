from pathlib import Path

import pytest

from scripts.deployment_manifest import create_manifest, verify_manifest

REVISION = "a" * 40


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "deploy"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=excluded\n", encoding="utf-8")
    manifest = root / "MANIFEST.sha256"
    return root, manifest


def test_manifest_round_trip_excludes_runtime_secret(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)

    assert create_manifest(root, manifest, REVISION) == 2
    assert ".env" not in manifest.read_text(encoding="utf-8")
    assert verify_manifest(root, manifest, REVISION) == 2


def test_manifest_detects_hash_and_file_set_drift(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    create_manifest(root, manifest, REVISION)

    (root / "src" / "app.py").write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash differs"):
        verify_manifest(root, manifest, REVISION)

    create_manifest(root, manifest, REVISION)
    (root / "unexpected.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="file set differs"):
        verify_manifest(root, manifest, REVISION)


def test_manifest_binds_full_revision(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    create_manifest(root, manifest, REVISION)

    with pytest.raises(ValueError, match="does not match"):
        verify_manifest(root, manifest, "b" * 40)
