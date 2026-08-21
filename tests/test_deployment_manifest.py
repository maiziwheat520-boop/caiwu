import sys
from pathlib import Path

import pytest

from scripts import deployment_manifest
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


def test_manifest_excludes_root_runtime_dirs_but_includes_nested_code_dirs(
    tmp_path: Path,
) -> None:
    root, manifest = _tree(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "runtime.db").write_text("excluded\n", encoding="utf-8")
    (root / "src" / "data").mkdir()
    (root / "src" / "data" / "schema.py").write_text("VERSION = 1\n", encoding="utf-8")

    create_manifest(root, manifest, REVISION)
    contents = manifest.read_text(encoding="utf-8")
    assert "data/runtime.db" not in contents
    assert "src/data/schema.py" in contents
    assert verify_manifest(root, manifest, REVISION) == 3


def test_manifest_rejects_symlink(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    try:
        (root / "linked.py").symlink_to(root / "src" / "app.py")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="symlinks"):
        create_manifest(root, manifest, REVISION)


def test_manifest_rejects_symlink_to_excluded_manifest(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    try:
        (root / "linked.py").symlink_to(manifest)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="symlinks"):
        create_manifest(root, manifest, REVISION)


@pytest.mark.parametrize("relative", ["../outside.py", "/absolute.py"])
def test_manifest_rejects_unsafe_paths(tmp_path: Path, relative: str) -> None:
    root, manifest = _tree(tmp_path)
    manifest.write_text(
        "\n".join(
            [
                "# ledgerbridge-deployment-manifest-v1",
                f"# revision {REVISION}",
                f"{'0' * 64}  {relative}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid deployment manifest entry"):
        verify_manifest(root, manifest, REVISION)


def test_manifest_rejects_invalid_create_revision(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    with pytest.raises(ValueError, match="40-character Git SHA"):
        create_manifest(root, manifest, "short")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("not-a-manifest\n", "version"),
        (
            "# ledgerbridge-deployment-manifest-v1\n# wrong header\n",
            "revision header",
        ),
        (
            "# ledgerbridge-deployment-manifest-v1\n# revision invalid\n",
            "revision is invalid",
        ),
    ],
)
def test_manifest_rejects_malformed_headers(
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    root, manifest = _tree(tmp_path)
    manifest.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        verify_manifest(root, manifest)


def test_manifest_skips_blank_lines_and_rejects_duplicates(tmp_path: Path) -> None:
    root, manifest = _tree(tmp_path)
    create_manifest(root, manifest, REVISION)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_manifest(root, manifest, REVISION) == 2

    lines.insert(3, lines[3])
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid deployment manifest entry"):
        verify_manifest(root, manifest, REVISION)


def test_manifest_cli_create_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _tree(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deployment_manifest",
            "create",
            "--root",
            str(root),
            "--output",
            str(manifest),
            "--revision",
            REVISION,
        ],
    )
    deployment_manifest.main()
    assert "created deployment manifest for 2 files" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deployment_manifest",
            "verify",
            "--root",
            str(root),
            "--manifest",
            str(manifest),
            "--expected-revision",
            REVISION,
        ],
    )
    deployment_manifest.main()
    assert "verified deployment manifest for 2 files" in capsys.readouterr().out
