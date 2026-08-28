from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ledgerbridge.file_key_provider import (
    FILE_KEY_SCHEMA,
    FileKeyProvider,
    FileKeyProviderError,
    bootstrap_file_key,
)


def test_bootstrap_load_and_round_trip_without_overwrite(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    path = (key_dir / "evidence-key.json").resolve()

    bootstrap_file_key(path, generation="prod-20260828-1")
    provider = FileKeyProvider(path)
    dek = b"d" * 32
    wrapped = provider.wrap_key(dek, purpose="artifact", aad=b"object")

    assert provider.active_generation == "prod-20260828-1"
    assert provider.unwrap_key(wrapped, purpose="artifact", aad=b"object") == dek
    with pytest.raises(FileKeyProviderError, match="already exists"):
        bootstrap_file_key(path, generation="prod-20260828-2")


def test_loader_rejects_unknown_fields_and_symlink(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    path = (key_dir / "evidence-key.json").resolve()
    payload = {
        "schema": FILE_KEY_SCHEMA,
        "active_generation": "prod-1",
        "generations": {"prod-1": "AA=="},
        "unexpected": True,
    }
    path.write_text(json.dumps(payload), encoding="ascii")
    if os.name != "nt":
        path.chmod(0o600)
    with pytest.raises(FileKeyProviderError, match="fields"):
        FileKeyProvider(path)

    link = key_dir / "link.json"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(FileKeyProviderError, match="regular file"):
        FileKeyProvider(link.resolve(strict=False) if False else link.absolute())


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_loader_rejects_group_readable_key_file(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    path = (key_dir / "evidence-key.json").resolve()
    bootstrap_file_key(path, generation="prod-1")
    path.chmod(0o640)

    with pytest.raises(FileKeyProviderError, match="permissions"):
        FileKeyProvider(path)
