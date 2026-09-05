"""Deployment-owned file key provider for an already encrypted host volume.

The key file is a root/service-owned bootstrap artifact, not application
configuration.  Runtime loading is read-only and fails closed on symlinks,
unexpected permissions, unknown fields, malformed base64, or key drift.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Final, cast

from nacl import bindings

from ledgerbridge.keyring import (
    KeyProviderError,
    SyntheticKeyProvider,
    WrappedKey,
)

FILE_KEY_SCHEMA: Final = "ledgerbridge.file-key-provider.v1"
_TOP_LEVEL_FIELDS: Final = frozenset({"active_generation", "generations", "schema"})
_KEY_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES


class FileKeyProviderError(KeyProviderError):
    """A deployment key file could not be loaded without ambiguity."""


class FileKeyProvider:
    """Read-only adapter over one private, deployment-managed key file."""

    __slots__ = ("_delegate", "_path")

    def __init__(self, path: Path) -> None:
        resolved = _require_absolute(path)
        payload = _read_private_file(resolved)
        generations, active_generation = _decode_payload(payload)
        self._delegate = SyntheticKeyProvider(
            generations,
            active_generation=active_generation,
        )
        self._path = resolved

    @property
    def active_generation(self) -> str:
        return self._delegate.active_generation

    @property
    def path(self) -> Path:
        return self._path

    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        return self._delegate.wrap_key(dek, purpose=purpose, aad=aad)

    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes:
        return self._delegate.unwrap_key(wrapped, purpose=purpose, aad=aad)

    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        return self._delegate.rewrap_key(wrapped, purpose=purpose, aad=aad)

    def self_test(self) -> None:
        self._delegate.self_test()


def bootstrap_file_key(path: Path, *, generation: str) -> None:
    """Create one new key file atomically; existing files are never replaced."""

    resolved = _require_absolute(path)
    parent = resolved.parent
    _require_private_directory(parent)
    payload = json.dumps(
        {
            "schema": FILE_KEY_SCHEMA,
            "active_generation": generation,
            "generations": {generation: base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError as exc:
        raise FileKeyProviderError("deployment key file already exists") from exc
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise FileKeyProviderError("deployment key file write made no progress")
            written += count
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            resolved.unlink()
        raise
    else:
        os.close(descriptor)
    _fsync_directory(parent)
    FileKeyProvider(resolved).self_test()


def _require_absolute(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise FileKeyProviderError("deployment key path must be absolute")
    return path


def _read_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
    except OSError as exc:
        raise FileKeyProviderError("deployment key file is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise FileKeyProviderError("deployment key file must be a regular file")
    _require_private_metadata(before, "deployment key file")
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FileKeyProviderError("deployment key file cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            os.name != "nt" and (opened.st_dev != before.st_dev or opened.st_ino != before.st_ino)
        ):
            raise FileKeyProviderError("deployment key file identity changed")
        _require_private_metadata(opened, "deployment key file")
        if opened.st_size <= 0 or opened.st_size > 16_384:
            raise FileKeyProviderError("deployment key file size is invalid")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise FileKeyProviderError("deployment key file was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise FileKeyProviderError("deployment key file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_payload(payload: bytes) -> tuple[dict[str, bytes], str]:
    try:
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FileKeyProviderError("deployment key file is not valid JSON") from exc
    if not isinstance(decoded, dict) or set(decoded) != _TOP_LEVEL_FIELDS:
        raise FileKeyProviderError("deployment key file fields are invalid")
    if decoded.get("schema") != FILE_KEY_SCHEMA:
        raise FileKeyProviderError("deployment key file schema is invalid")
    active = decoded.get("active_generation")
    raw_generations = decoded.get("generations")
    if not isinstance(active, str) or not isinstance(raw_generations, dict):
        raise FileKeyProviderError("deployment key generations are invalid")
    if not 1 <= len(raw_generations) <= 32:
        raise FileKeyProviderError("deployment key generation count is invalid")
    generations: dict[str, bytes] = {}
    for generation, encoded in raw_generations.items():
        if not isinstance(generation, str) or not isinstance(encoded, str):
            raise FileKeyProviderError("deployment key generation is invalid")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileKeyProviderError("deployment key material is invalid") from exc
        if len(key) != _KEY_BYTES:
            raise FileKeyProviderError("deployment key material has an invalid length")
        generations[generation] = key
    if active not in generations:
        raise FileKeyProviderError("active deployment key generation is unavailable")
    return generations, active


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FileKeyProviderError("deployment key directory is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise FileKeyProviderError("deployment key directory must be a regular directory")
    _require_private_metadata(metadata, "deployment key directory")


def _require_private_metadata(metadata: os.stat_result, label: str) -> None:
    if os.name == "nt":
        return
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise FileKeyProviderError(f"{label} permissions are too broad")
    geteuid = cast(Callable[[], int], getattr(os, "geteuid", lambda: metadata.st_uid))
    if metadata.st_uid not in {0, geteuid()}:
        raise FileKeyProviderError(f"{label} owner is invalid")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
