import base64
import os
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import ledgerbridge.signed_manifest as signed_manifest
from ledgerbridge.runner_composition import RUNNER_FACTORY_ID
from ledgerbridge.signed_manifest import (
    MAX_MANIFEST_BYTES,
    ManifestVerificationError,
    _decode_signature,
    _parse_connector,
    canonical_manifest_bytes,
    load_signed_runner_manifest,
    load_verification_keys,
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


def _write_payload(path: Path, private_key: Ed25519PrivateKey, payload: dict[str, object]) -> bytes:
    signature = private_key.sign(canonical_manifest_bytes(payload))
    path.write_bytes(
        canonical_manifest_bytes(
            {**payload, "signature": base64.b64encode(signature).decode("ascii")}
        )
    )
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


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.update(schema_version=2), "schema version"),
        (lambda p: p.update(generation=1), "envelope types"),
        (lambda p: p.update(connectors={}), "envelope types"),
        (lambda p: p.update(connectors=[{"bad": 1}]), "connector fields"),
        (
            lambda p: p.update(
                connectors=[
                    {
                        "execution_mode": "in_process",
                        "factory_id": RUNNER_FACTORY_ID,
                        "name": "bank.synthetic",
                        "source_system": "synthetic_bank",
                        "version": "1.0",
                    }
                ]
            ),
            "runner mode",
        ),
    ],
)
def test_signed_manifest_rejects_schema_and_connector_policy_errors(
    tmp_path: Path,
    change: object,
    message: str,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    payload: dict[str, object] = {
        "connectors": [],
        "generation": "gen-1",
        "key_id": "test-key-1",
        "schema_version": 1,
    }
    change(payload)  # type: ignore[operator]
    public_key = _write_payload(path, private_key, payload)
    with pytest.raises(ManifestVerificationError, match=message):
        load_signed_runner_manifest(path, {"test-key-1": public_key}, production=True)


def test_signature_and_key_file_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(ManifestVerificationError, match="signature encoding"):
        _decode_signature("not-base64")
    with pytest.raises(ManifestVerificationError, match="signature length"):
        _decode_signature(base64.b64encode(b"short").decode("ascii"))

    key_path = tmp_path / "keys.json"
    key_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ManifestVerificationError, match="key file is invalid"):
        load_verification_keys(key_path)
    key_path.write_text('{"key":"not-base64"}', encoding="utf-8")
    with pytest.raises(ManifestVerificationError, match="key encoding"):
        load_verification_keys(key_path)
    key_path.write_text(
        '{"key":"' + base64.b64encode(b"short").decode("ascii") + '"}', encoding="utf-8"
    )
    with pytest.raises(ManifestVerificationError, match="key length"):
        load_verification_keys(key_path)
    key_path.write_text('{"key":1}', encoding="utf-8")
    with pytest.raises(ManifestVerificationError, match="key file is invalid"):
        load_verification_keys(key_path)


def test_manifest_file_and_public_key_fail_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ManifestVerificationError, match="unavailable"):
        load_verification_keys(missing)
    too_large = tmp_path / "large.json"
    too_large.write_bytes(b"x" * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ManifestVerificationError, match="too large"):
        load_verification_keys(too_large)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ManifestVerificationError, match=r"unavailable|invalid"):
        load_verification_keys(directory)
    with pytest.raises(ManifestVerificationError, match="connector fields"):
        _parse_connector(object(), production=False)
    with pytest.raises(ManifestVerificationError, match="unavailable"):
        load_signed_runner_manifest(tmp_path / "missing.json", {"key": b"short"})
    assert os.path.exists(tmp_path)


def test_signed_manifest_helper_error_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ManifestVerificationError, match="canonical JSON"):
        canonical_manifest_bytes({1: object()})  # type: ignore[dict-item]

    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    payload: dict[str, object] = {
        "connectors": [
            {
                "execution_mode": "runner",
                "factory_id": RUNNER_FACTORY_ID,
                "name": "bank.synthetic",
                "source_system": "synthetic_bank",
                "version": "1.0",
            },
            {
                "execution_mode": "runner",
                "factory_id": RUNNER_FACTORY_ID,
                "name": "bank.synthetic",
                "source_system": "synthetic_bank",
                "version": "1.0",
            },
        ],
        "generation": "gen-1",
        "key_id": "test-key-1",
        "schema_version": 1,
    }
    public_key = _write_payload(path, private_key, payload)
    with pytest.raises(ManifestVerificationError, match="composition"):
        load_signed_runner_manifest(path, {"test-key-1": public_key})

    key_path = tmp_path / "keys.json"
    key_path.write_text(
        '{"test-key-1":"' + base64.b64encode(public_key).decode("ascii") + '"}',
        encoding="utf-8",
    )
    assert load_verification_keys(key_path) == {"test-key-1": public_key}
    key_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ManifestVerificationError, match="key file is invalid"):
        load_verification_keys(key_path)

    with pytest.raises(ManifestVerificationError, match="verification key"):
        signed_manifest._verify_signature({}, b"s" * 64, b"short")
    with pytest.raises(ManifestVerificationError, match="connector is invalid"):
        signed_manifest._parse_connector(
            {
                "execution_mode": "invalid",
                "factory_id": RUNNER_FACTORY_ID,
                "name": "bank.synthetic",
                "source_system": "synthetic_bank",
                "version": "1.0",
            },
            production=False,
        )

    real_fstat = signed_manifest.os.fstat  # type: ignore[attr-defined]
    calls = 0

    def changed_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            values = list(result)
            values[9] += 1
            return os.stat_result(values)
        return cast(os.stat_result, result)

    monkeypatch.setattr(signed_manifest.os, "fstat", changed_fstat)  # type: ignore[attr-defined]
    with pytest.raises(ManifestVerificationError, match="changed"):
        signed_manifest._read_stable_file(key_path)

    def fail_fdopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("fdopen failed")

    monkeypatch.setattr(signed_manifest.os, "fdopen", fail_fdopen)  # type: ignore[attr-defined]
    with pytest.raises(ManifestVerificationError, match="unavailable"):
        signed_manifest._read_stable_file(key_path)
