import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ledgerbridge.runner_composition import RUNNER_FACTORY_ID
from ledgerbridge.signed_manifest import (
    ManifestVerificationError,
    canonical_manifest_bytes,
    load_signed_runner_manifest,
)


def _write_signed(
    path: Path, private_key: Ed25519PrivateKey, *, generation: str = "gen-1"
) -> bytes:
    payload: dict[str, object] = {
        "connectors": [
            {
                "execution_mode": "runner",
                "factory_id": RUNNER_FACTORY_ID,
                "name": "bank.synthetic",
                "source_system": "synthetic_bank",
                "version": "1.0",
            }
        ],
        "generation": generation,
        "key_id": "test-key-1",
        "schema_version": 1,
    }
    signature = private_key.sign(canonical_manifest_bytes(payload))
    envelope = {**payload, "signature": base64.b64encode(signature).decode("ascii")}
    path.write_bytes(canonical_manifest_bytes(envelope))
    return private_key.public_key().public_bytes_raw()


def test_signed_manifest_round_trip_and_digest(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = _write_signed(tmp_path / "manifest.json", private_key)

    manifest = load_signed_runner_manifest(
        tmp_path / "manifest.json",
        {"test-key-1": public_key},
        expected_generation="gen-1",
    )

    assert manifest.generation == "gen-1"
    assert len(manifest.digest) == 32
    assert manifest.connectors[0].source_system == "synthetic_bank"


def test_signed_manifest_stability_check_ignores_access_time_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write_signed(path, private_key)
    original_fstat = os.fstat
    calls = 0

    def fstat_with_changed_access_time(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        value = original_fstat(descriptor)
        if calls % 2:
            return value
        fields = {
            name: getattr(value, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
        fields["st_atime_ns"] = value.st_atime_ns + 1
        return SimpleNamespace(**fields)  # type: ignore[return-value]

    monkeypatch.setattr(os, "fstat", fstat_with_changed_access_time)
    manifest = load_signed_runner_manifest(path, {"test-key-1": public_key})
    assert manifest.generation == "gen-1"


@pytest.mark.parametrize("changed_field", ["st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"])
def test_signed_manifest_stability_check_rejects_stable_fingerprint_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write_signed(path, private_key)
    original_fstat = os.fstat
    calls = 0

    def fstat_with_changed_field(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        value = original_fstat(descriptor)
        if calls % 2:
            return value
        fields = {
            name: getattr(value, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
        fields[changed_field] += 1
        return SimpleNamespace(**fields)  # type: ignore[return-value]

    monkeypatch.setattr(os, "fstat", fstat_with_changed_field)
    with pytest.raises(ManifestVerificationError, match="changed while reading"):
        load_signed_runner_manifest(path, {"test-key-1": public_key})


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda data: data.replace(b"bank.synthetic", b"bank.tampered"), "signature"),
        (lambda data: data.replace(b'{"connectors"', b'{ "connectors"'), "canonical"),
    ],
)
def test_signed_manifest_rejects_tamper_and_noncanonical(
    tmp_path: Path,
    mutator: object,
    message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write_signed(path, private_key)
    path.write_bytes(mutator(path.read_bytes()))  # type: ignore[operator]

    with pytest.raises(ManifestVerificationError, match=message):
        load_signed_runner_manifest(path, {"test-key-1": public_key})


def test_signed_manifest_rejects_unknown_key_and_generation_mismatch(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write_signed(path, private_key)

    with pytest.raises(ManifestVerificationError, match="not trusted"):
        load_signed_runner_manifest(path, {"other-key": public_key})
    with pytest.raises(ManifestVerificationError, match="generation"):
        load_signed_runner_manifest(path, {"test-key-1": public_key}, expected_generation="gen-2")


def test_signed_manifest_rejects_duplicate_and_unknown_fields(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write_signed(path, private_key)
    duplicate = path.read_text(encoding="utf-8").replace(
        '"schema_version":1,', '"schema_version":1,"schema_version":1,'
    )
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ManifestVerificationError, match="JSON is invalid"):
        load_signed_runner_manifest(path, {"test-key-1": public_key})
    _write_signed(path, private_key)
    path.write_bytes(path.read_bytes().replace(b',"signature"', b',"unexpected":1,"signature"'))
    with pytest.raises(ManifestVerificationError, match="fields are invalid"):
        load_signed_runner_manifest(path, {"test-key-1": public_key})
