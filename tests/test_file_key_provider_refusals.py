"""Every way a deployment key file can be refused.

The key file is a root/service-owned bootstrap artifact that holds the
material every evidence blob is wrapped under, so it is loaded read-only and
fails closed on anything ambiguous: a path that is not absolute, a file that
moved between the stat and the open, permissions or ownership that let a
third party read it, or a payload whose fields, schema, generations or key
material do not decode to exactly one usable active generation.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_KEYBYTES as KEY_BYTES

from ledgerbridge.file_key_provider import (
    FILE_KEY_SCHEMA,
    FileKeyProvider,
    FileKeyProviderError,
    bootstrap_file_key,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics")


def _key_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir(mode=0o700)
    return directory


def _write_key_file(path: Path, body: bytes | str) -> Path:
    """Write one key file with the private permissions the loader demands."""

    path.write_bytes(body.encode("ascii") if isinstance(body, str) else body)
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _material(seed: bytes = b"k") -> str:
    return base64.b64encode(seed * KEY_BYTES).decode("ascii")


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "active_generation": "gen-1",
        "generations": {"gen-1": _material()},
        "schema": FILE_KEY_SCHEMA,
    }
    payload.update(overrides)
    return payload


def _write_payload(tmp_path: Path, payload: object) -> Path:
    return _write_key_file(_key_dir(tmp_path) / "evidence-key.json", json.dumps(payload))


def _fake_stat(
    template: os.stat_result,
    *,
    mode: int | None = None,
    ino: int | None = None,
    uid: int | None = None,
) -> os.stat_result:
    values: list[Any] = list(template)
    if mode is not None:
        values[0] = mode
    if ino is not None:
        values[1] = ino
    if uid is not None:
        values[4] = uid
    return os.stat_result(values)


# --- naming the file --------------------------------------------------------


def test_a_relative_key_path_is_never_resolved_against_the_working_directory() -> None:
    relative = Path("evidence-key.json")
    with pytest.raises(FileKeyProviderError, match="path must be absolute"):
        FileKeyProvider(relative)
    with pytest.raises(FileKeyProviderError, match="path must be absolute"):
        bootstrap_file_key(relative, generation="gen-1")


def test_a_key_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileKeyProviderError, match="key file is unavailable"):
        FileKeyProvider(_key_dir(tmp_path) / "absent.json")


def test_a_directory_is_not_a_key_file(tmp_path: Path) -> None:
    with pytest.raises(FileKeyProviderError, match="must be a regular file"):
        FileKeyProvider(_key_dir(tmp_path))


def test_a_key_file_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_payload(tmp_path, _payload())

    def refuse_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("no descriptor")

    monkeypatch.setattr(os, "open", refuse_open)
    with pytest.raises(FileKeyProviderError, match="cannot be opened"):
        FileKeyProvider(path)


def test_a_key_file_swapped_for_something_else_between_the_stat_and_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is stat-ed twice; the descriptor decides which file was read."""

    path = _write_payload(tmp_path, _payload())
    swapped = _fake_stat(path.lstat(), mode=stat.S_IFDIR | 0o600)
    monkeypatch.setattr(os, "fstat", lambda descriptor: swapped)
    with pytest.raises(FileKeyProviderError, match="identity changed"):
        FileKeyProvider(path)


@_POSIX_ONLY
def test_a_key_file_swapped_for_another_inode_between_the_stat_and_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_payload(tmp_path, _payload())
    original = path.lstat()
    monkeypatch.setattr(
        os, "fstat", lambda descriptor: _fake_stat(original, ino=original.st_ino + 1)
    )
    with pytest.raises(FileKeyProviderError, match="identity changed"):
        FileKeyProvider(path)


@_POSIX_ONLY
def test_a_key_file_owned_by_a_third_party_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_payload(tmp_path, _payload())
    foreign = _fake_stat(path.lstat(), uid=path.lstat().st_uid + 4242)
    monkeypatch.setattr(Path, "lstat", lambda self: foreign)
    with pytest.raises(FileKeyProviderError, match="owner is invalid"):
        FileKeyProvider(path)


@pytest.mark.parametrize("body", [b"", b"x" * 16_385])
def test_a_key_file_whose_size_is_out_of_bounds_is_refused(tmp_path: Path, body: bytes) -> None:
    path = _write_key_file(_key_dir(tmp_path) / "evidence-key.json", body)
    with pytest.raises(FileKeyProviderError, match="size is invalid"):
        FileKeyProvider(path)


def test_a_key_file_that_stops_short_of_its_own_size_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_payload(tmp_path, _payload())
    monkeypatch.setattr(os, "read", lambda descriptor, count: b"")
    with pytest.raises(FileKeyProviderError, match="was truncated"):
        FileKeyProvider(path)


def test_a_key_file_that_grows_while_it_is_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_payload(tmp_path, _payload())
    monkeypatch.setattr(os, "read", lambda descriptor, count: b"x" * count)
    with pytest.raises(FileKeyProviderError, match="changed while reading"):
        FileKeyProvider(path)


# --- decoding the payload ---------------------------------------------------


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe{}", b"{"])
def test_a_key_file_that_is_not_ascii_json_is_refused(tmp_path: Path, body: bytes) -> None:
    path = _write_key_file(_key_dir(tmp_path) / "evidence-key.json", body)
    with pytest.raises(FileKeyProviderError, match="not valid JSON"):
        FileKeyProvider(path)


@pytest.mark.parametrize("payload", [[], {}, {"schema": FILE_KEY_SCHEMA}])
def test_a_key_file_without_exactly_the_expected_fields_is_refused(
    tmp_path: Path, payload: object
) -> None:
    path = _write_payload(tmp_path, payload)
    with pytest.raises(FileKeyProviderError, match="fields are invalid"):
        FileKeyProvider(path)


def test_a_key_file_from_another_schema_is_refused(tmp_path: Path) -> None:
    path = _write_payload(tmp_path, _payload(schema="ledgerbridge.file-key-provider.v2"))
    with pytest.raises(FileKeyProviderError, match="schema is invalid"):
        FileKeyProvider(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_generation": 1},
        {"active_generation": None},
        {"generations": []},
        {"generations": "gen-1"},
    ],
)
def test_a_key_file_whose_generation_types_are_wrong_is_refused(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    path = _write_payload(tmp_path, _payload(**overrides))
    with pytest.raises(FileKeyProviderError, match="generations are invalid"):
        FileKeyProvider(path)


@pytest.mark.parametrize(
    "generations",
    [{}, {f"gen-{index}": _material() for index in range(33)}],
)
def test_a_key_file_must_carry_between_one_and_thirty_two_generations(
    tmp_path: Path, generations: dict[str, str]
) -> None:
    path = _write_payload(tmp_path, _payload(generations=generations))
    with pytest.raises(FileKeyProviderError, match="generation count is invalid"):
        FileKeyProvider(path)


def test_a_generation_whose_material_is_not_a_string_is_refused(tmp_path: Path) -> None:
    path = _write_payload(tmp_path, _payload(generations={"gen-1": 1}))
    with pytest.raises(FileKeyProviderError, match="generation is invalid"):
        FileKeyProvider(path)


@pytest.mark.parametrize("encoded", ["not base64!", "AA=A", "AAAAA"])
def test_key_material_that_is_not_strict_base64_is_refused(tmp_path: Path, encoded: str) -> None:
    path = _write_payload(tmp_path, _payload(generations={"gen-1": encoded}))
    with pytest.raises(FileKeyProviderError, match="key material is invalid"):
        FileKeyProvider(path)


@pytest.mark.parametrize("length", [KEY_BYTES - 1, KEY_BYTES + 1, 0])
def test_key_material_of_the_wrong_length_is_refused(tmp_path: Path, length: int) -> None:
    encoded = base64.b64encode(b"k" * length).decode("ascii")
    path = _write_payload(tmp_path, _payload(generations={"gen-1": encoded}))
    with pytest.raises(FileKeyProviderError, match="has an invalid length"):
        FileKeyProvider(path)


def test_an_active_generation_with_no_material_is_refused(tmp_path: Path) -> None:
    path = _write_payload(tmp_path, _payload(active_generation="gen-2"))
    with pytest.raises(
        FileKeyProviderError, match="active deployment key generation is unavailable"
    ):
        FileKeyProvider(path)


# --- creating the file ------------------------------------------------------


def test_bootstrap_refuses_a_directory_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(FileKeyProviderError, match="key directory is unavailable"):
        bootstrap_file_key(tmp_path / "absent" / "evidence-key.json", generation="gen-1")


def test_bootstrap_refuses_a_directory_that_is_a_file(tmp_path: Path) -> None:
    occupied = _write_key_file(tmp_path / "keys", b"{}")
    with pytest.raises(FileKeyProviderError, match="must be a regular directory"):
        bootstrap_file_key(occupied / "evidence-key.json", generation="gen-1")


def test_bootstrap_removes_a_key_file_it_could_not_finish_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written key file would silently lose evidence, so it is unlinked."""

    path = _key_dir(tmp_path) / "evidence-key.json"
    monkeypatch.setattr(os, "write", lambda descriptor, data: 0)
    with pytest.raises(FileKeyProviderError, match="write made no progress"):
        bootstrap_file_key(path, generation="gen-1")
    assert not path.exists()


def test_bootstrap_removes_a_key_file_whose_write_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _key_dir(tmp_path) / "evidence-key.json"

    def refuse_write(descriptor: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", refuse_write)
    with pytest.raises(OSError, match="disk full"):
        bootstrap_file_key(path, generation="gen-1")
    assert not path.exists()


# --- the loaded provider ----------------------------------------------------


def test_the_provider_reports_the_file_it_loaded(tmp_path: Path) -> None:
    path = (_key_dir(tmp_path) / "evidence-key.json").resolve()
    bootstrap_file_key(path, generation="gen-1")
    assert FileKeyProvider(path).path == path


def test_a_wrapped_key_survives_a_rewrap_under_the_same_generation(tmp_path: Path) -> None:
    path = (_key_dir(tmp_path) / "evidence-key.json").resolve()
    bootstrap_file_key(path, generation="gen-1")
    provider = FileKeyProvider(path)
    dek = b"d" * KEY_BYTES

    wrapped = provider.wrap_key(dek, purpose="artifact", aad=b"object")
    rewrapped = provider.rewrap_key(wrapped, purpose="artifact", aad=b"object")

    assert provider.unwrap_key(rewrapped, purpose="artifact", aad=b"object") == dek
