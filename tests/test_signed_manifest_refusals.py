"""Every way a runner manifest or its verification key file can be refused.

The manifest is the only thing that decides which Connectors a worker will
run, so it is accepted solely as a canonical, detached-Ed25519-signed JSON
envelope whose bytes have not moved between the two stats around the read.
Every refusal below closes one path by which an unsigned, re-encoded, or
partially trusted manifest could otherwise enable a Connector.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ledgerbridge.runner_composition import RUNNER_FACTORY_ID
from ledgerbridge.signed_manifest import (
    MAX_MANIFEST_BYTES,
    MAX_VERIFICATION_KEYS,
    ManifestVerificationError,
    canonical_manifest_bytes,
    load_signed_runner_manifest,
    load_verification_keys,
)

KEY_ID = "test-key-1"
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")


def _connector(**overrides: object) -> dict[str, object]:
    connector: dict[str, object] = {
        "execution_mode": "runner",
        "factory_id": RUNNER_FACTORY_ID,
        "name": "bank.synthetic",
        "source_system": "synthetic_bank",
        "version": "1.0",
    }
    connector.update(overrides)
    return connector


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "connectors": [_connector()],
        "generation": "gen-1",
        "key_id": KEY_ID,
        "schema_version": 1,
    }
    payload.update(overrides)
    return payload


def _write(
    path: Path,
    private_key: Ed25519PrivateKey,
    *,
    payload: dict[str, object] | None = None,
    signed_payload: dict[str, object] | None = None,
    envelope_overrides: dict[str, object] | None = None,
    raw: bytes | None = None,
) -> bytes:
    """Write one envelope and return the raw public key that signs it."""

    values = payload if payload is not None else _payload()
    signature = private_key.sign(canonical_manifest_bytes(signed_payload or values))
    envelope: dict[str, object] = {
        **values,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    if envelope_overrides is not None:
        envelope.update(envelope_overrides)
    path.write_bytes(raw if raw is not None else canonical_manifest_bytes(envelope))
    return private_key.public_key().public_bytes_raw()


def _load(path: Path, public_key: bytes, **kwargs: object) -> object:
    return load_signed_runner_manifest(path, {KEY_ID: public_key}, **kwargs)  # type: ignore[arg-type]


# --- reading the file -------------------------------------------------------


def test_a_manifest_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestVerificationError, match="manifest file is unavailable"):
        load_signed_runner_manifest(tmp_path / "absent.json", {KEY_ID: b"\0" * 32})


def test_a_directory_is_not_a_manifest(tmp_path: Path) -> None:
    directory = tmp_path / "manifest.json"
    directory.mkdir()
    with pytest.raises(ManifestVerificationError, match=r"unavailable|invalid or too large"):
        load_signed_runner_manifest(directory, {KEY_ID: b"\0" * 32})


def test_a_manifest_larger_than_the_bound_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"{" + b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ManifestVerificationError, match="invalid or too large"):
        load_signed_runner_manifest(path, {KEY_ID: b"\0" * 32})


@_POSIX_ONLY
def test_a_manifest_reached_through_a_symlink_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    real = tmp_path / "manifest.json"
    public_key = _write(real, private_key)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ManifestVerificationError, match="manifest file is unavailable"):
        _load(link, public_key)


# --- the envelope -----------------------------------------------------------


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe{}", b"[]"])
def test_a_manifest_that_is_not_a_json_object_is_refused(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(body)
    with pytest.raises(
        ManifestVerificationError, match=r"manifest JSON is invalid|fields are inval"
    ):
        load_signed_runner_manifest(path, {KEY_ID: b"\0" * 32})


def test_an_envelope_with_an_unexpected_field_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, envelope_overrides={"extra": 1})
    with pytest.raises(ManifestVerificationError, match="manifest fields are invalid"):
        _load(path, public_key)


def test_an_envelope_from_a_future_schema_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, payload=_payload(schema_version=2))
    with pytest.raises(ManifestVerificationError, match="schema version is unsupported"):
        _load(path, public_key)


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 1},
        {"key_id": None},
        {"connectors": {}},
    ],
)
def test_an_envelope_whose_types_are_wrong_is_refused(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, payload=_payload(**overrides))
    with pytest.raises(ManifestVerificationError, match="envelope types are invalid"):
        _load(path, public_key)


def test_an_envelope_with_a_non_string_signature_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, envelope_overrides={"signature": 1})
    with pytest.raises(ManifestVerificationError, match="envelope types are invalid"):
        _load(path, public_key)


def test_a_manifest_from_another_generation_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key)
    with pytest.raises(ManifestVerificationError, match="generation does not match deployment"):
        _load(path, public_key, expected_generation="gen-2")


def test_a_manifest_signed_by_an_untrusted_key_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key)
    with pytest.raises(ManifestVerificationError, match="signing key is not trusted"):
        load_signed_runner_manifest(path, {"other-key": public_key})


@pytest.mark.parametrize("signature", ["not base64!", "AAAA"])
def test_a_signature_that_is_not_sixty_four_bytes_of_base64_is_refused(
    tmp_path: Path, signature: str
) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, envelope_overrides={"signature": signature})
    with pytest.raises(ManifestVerificationError, match=r"signature (encoding|length) is invalid"):
        _load(path, public_key)


def test_a_re_encoded_envelope_is_not_the_bytes_that_were_signed(tmp_path: Path) -> None:
    # The canonical form is the only accepted encoding, so pretty-printing the
    # same object has to be refused before the signature is even checked.
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    payload = _payload()
    signature = private_key.sign(canonical_manifest_bytes(payload))
    envelope = {**payload, "signature": base64.b64encode(signature).decode("ascii")}
    path.write_bytes(json.dumps(envelope, indent=2).encode("utf-8"))
    with pytest.raises(ManifestVerificationError, match="manifest is not canonical JSON"):
        _load(path, private_key.public_key().public_bytes_raw())


def test_a_verification_key_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    _write(path, private_key)
    with pytest.raises(ManifestVerificationError, match="verification key is invalid"):
        load_signed_runner_manifest(path, {KEY_ID: b"\0" * 31})


def test_a_signature_over_different_content_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(
        path, private_key, payload=_payload(), signed_payload=_payload(generation="gen-9")
    )
    with pytest.raises(ManifestVerificationError, match="manifest signature is invalid"):
        _load(path, public_key)


def test_a_payload_that_is_not_canonical_json_is_refused() -> None:
    with pytest.raises(ManifestVerificationError, match="manifest is not canonical JSON"):
        canonical_manifest_bytes({"value": float("nan")})


# --- the connector list -----------------------------------------------------


@pytest.mark.parametrize(
    "connector",
    [
        {"execution_mode": "runner"},
        {**_connector(), "extra": 1},
        "not-a-mapping",
    ],
)
def test_a_connector_whose_fields_are_wrong_is_refused(tmp_path: Path, connector: object) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key, payload=_payload(connectors=[connector]))
    with pytest.raises(ManifestVerificationError, match="connector fields are invalid"):
        _load(path, public_key)


def test_a_connector_with_an_unknown_execution_mode_is_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(
        path, private_key, payload=_payload(connectors=[_connector(execution_mode="inline")])
    )
    with pytest.raises(ManifestVerificationError, match="manifest connector is invalid"):
        _load(path, public_key)


def test_a_production_manifest_must_run_every_connector_in_the_runner(tmp_path: Path) -> None:
    # Outside the runner a Connector shares the worker's network and secrets.
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(
        path, private_key, payload=_payload(connectors=[_connector(execution_mode="in_process")])
    )
    with pytest.raises(ManifestVerificationError, match="must use runner mode"):
        _load(path, public_key, production=True)


def test_two_connectors_that_cannot_compose_are_refused(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(
        path, private_key, payload=_payload(connectors=[_connector(), _connector()])
    )
    with pytest.raises(ManifestVerificationError, match="connector composition is invalid"):
        _load(path, public_key)


# --- the verification key file ---------------------------------------------


def _write_keys(path: Path, value: object) -> None:
    path.write_bytes(json.dumps(value).encode("utf-8"))


def test_a_verification_key_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    material = base64.b64encode(b"k" * 32).decode("ascii")
    _write_keys(path, {KEY_ID: material})
    assert load_verification_keys(path) == {KEY_ID: b"k" * 32}


def test_a_verification_key_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestVerificationError, match="manifest file is unavailable"):
        load_verification_keys(tmp_path / "absent.json")


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe{}"])
def test_a_verification_key_file_that_is_not_json_is_refused(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "keys.json"
    path.write_bytes(body)
    with pytest.raises(ManifestVerificationError, match="verification key file is invalid"):
        load_verification_keys(path)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"": "AAAA"},
        {"k": 1},
    ],
)
def test_a_verification_key_file_of_the_wrong_shape_is_refused(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "keys.json"
    _write_keys(path, value)
    with pytest.raises(ManifestVerificationError, match="verification key file is invalid"):
        load_verification_keys(path)


def test_a_verification_key_file_with_too_many_keys_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    material = base64.b64encode(b"k" * 32).decode("ascii")
    _write_keys(path, {f"key-{index}": material for index in range(MAX_VERIFICATION_KEYS + 1)})
    with pytest.raises(ManifestVerificationError, match="verification key file is invalid"):
        load_verification_keys(path)


def test_a_verification_key_that_is_not_base64_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    _write_keys(path, {KEY_ID: "not base64!"})
    with pytest.raises(ManifestVerificationError, match="key encoding is invalid"):
        load_verification_keys(path)


def test_a_verification_key_of_the_wrong_length_is_refused_at_load(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    _write_keys(path, {KEY_ID: base64.b64encode(b"short").decode("ascii")})
    with pytest.raises(ManifestVerificationError, match="key length is invalid"):
        load_verification_keys(path)


def test_a_duplicate_key_id_is_refused_rather_than_resolved(tmp_path: Path) -> None:
    # Two entries for one id would leave which key is trusted up to the parser.
    path = tmp_path / "keys.json"
    material = base64.b64encode(b"k" * 32).decode("ascii")
    path.write_bytes(f'{{"{KEY_ID}":"{material}","{KEY_ID}":"{material}"}}'.encode())
    with pytest.raises(ManifestVerificationError, match="verification key file is invalid"):
        load_verification_keys(path)


def test_a_descriptor_that_never_became_a_file_object_is_still_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader owns the descriptor until `fdopen` takes it, and must release it."""

    private_key = Ed25519PrivateKey.generate()
    path = tmp_path / "manifest.json"
    public_key = _write(path, private_key)
    closed: list[int] = []
    real_close = os.close

    def refuse_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("no file object")

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)
    monkeypatch.setattr(os, "close", record_close)
    with pytest.raises(ManifestVerificationError, match="manifest file is unavailable"):
        _load(path, public_key)
    assert len(closed) == 1
