"""Fail-closed verification for the declarative runner manifest.

The verifier is deliberately separate from the runner composition layer.  It
accepts only a canonical, detached-Ed25519-signed JSON envelope and returns the
immutable ``VerifiedRunnerManifest`` consumed by the worker.  Verification
keys are injected by deployment; this module never discovers keys, imports
Python objects, or enables a Connector by itself.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ledgerbridge.connectors import ConnectorExecutionMode
from ledgerbridge.runner_composition import (
    RunnerCompositionError,
    RunnerConnectorSpec,
    VerifiedRunnerManifest,
)

MAX_MANIFEST_BYTES: Final = 1_048_576
MAX_VERIFICATION_KEYS: Final = 16
MANIFEST_SCHEMA_VERSION: Final = 1
_ENVELOPE_FIELDS: Final = frozenset(
    {"connectors", "generation", "key_id", "schema_version", "signature"}
)
_CONNECTOR_FIELDS: Final = frozenset(
    {"execution_mode", "factory_id", "name", "source_system", "version"}
)


class ManifestVerificationError(ValueError):
    """A manifest failed canonical, schema, key, or signature verification."""


def canonical_manifest_bytes(payload: Mapping[str, object]) -> bytes:
    """Return the only accepted UTF-8 JSON representation for a manifest."""

    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ManifestVerificationError("manifest is not canonical JSON") from exc


def load_signed_runner_manifest(
    path: Path,
    verification_keys: Mapping[str, bytes],
    *,
    expected_generation: str | None = None,
    production: bool = False,
) -> VerifiedRunnerManifest:
    """Load and verify one immutable runner manifest generation.

    ``verification_keys`` contains raw 32-byte Ed25519 public keys keyed by a
    deployment-owned key id.  It is intentionally not read from the manifest
    directory or repository.  The file is read through one descriptor and
    checked before/after reading so a replacement cannot change the verified
    bytes.
    """

    raw = _read_stable_file(path)
    try:
        envelope = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestVerificationError) as exc:
        raise ManifestVerificationError("manifest JSON is invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
        raise ManifestVerificationError("manifest fields are invalid")
    if envelope.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ManifestVerificationError("manifest schema version is unsupported")
    generation = envelope.get("generation")
    key_id = envelope.get("key_id")
    signature_text = envelope.get("signature")
    connectors = envelope.get("connectors")
    if (
        not isinstance(generation, str)
        or not isinstance(key_id, str)
        or not isinstance(signature_text, str)
        or not isinstance(connectors, list)
    ):
        raise ManifestVerificationError("manifest envelope types are invalid")
    if expected_generation is not None and generation != expected_generation:
        raise ManifestVerificationError("manifest generation does not match deployment")
    if key_id not in verification_keys:
        raise ManifestVerificationError("manifest signing key is not trusted")
    signature = _decode_signature(signature_text)
    payload: dict[str, object] = {
        "connectors": connectors,
        "generation": generation,
        "key_id": key_id,
        "schema_version": MANIFEST_SCHEMA_VERSION,
    }
    canonical_envelope = canonical_manifest_bytes({**payload, "signature": signature_text})
    if raw != canonical_envelope:
        raise ManifestVerificationError("manifest is not canonical JSON")
    _verify_signature(payload, signature, verification_keys[key_id])
    specs = tuple(_parse_connector(value, production=production) for value in connectors)
    try:
        return VerifiedRunnerManifest.from_connectors(generation, specs)
    except RunnerCompositionError as exc:
        raise ManifestVerificationError("manifest connector composition is invalid") from exc


def load_verification_keys(path: Path) -> dict[str, bytes]:
    """Load deployment-owned raw Ed25519 public keys from a bounded JSON file."""

    raw = _read_stable_file(path)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestVerificationError) as exc:
        raise ManifestVerificationError("verification key file is invalid") from exc
    if not isinstance(value, dict) or not value or len(value) > MAX_VERIFICATION_KEYS:
        raise ManifestVerificationError("verification key file is invalid")
    keys: dict[str, bytes] = {}
    for key_id, encoded in value.items():
        if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
            raise ManifestVerificationError("verification key file is invalid")
        try:
            key = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise ManifestVerificationError("verification key encoding is invalid") from exc
        if len(key) != 32:
            raise ManifestVerificationError("verification key length is invalid")
        keys[key_id] = key
    return keys


def _read_stable_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_MANIFEST_BYTES:
                raise ManifestVerificationError("manifest file is invalid or too large")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
    except ManifestVerificationError:
        raise
    except OSError as exc:
        raise ManifestVerificationError("manifest file is unavailable") from exc
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if len(raw) > MAX_MANIFEST_BYTES or before_identity != after_identity:
        raise ManifestVerificationError("manifest file changed while reading")
    return raw


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestVerificationError("manifest contains duplicate fields")
        result[key] = value
    return result


def _decode_signature(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ManifestVerificationError("manifest signature encoding is invalid") from exc
    if len(decoded) != 64:
        raise ManifestVerificationError("manifest signature length is invalid")
    return decoded


def _verify_signature(payload: Mapping[str, object], signature: bytes, public_key: bytes) -> None:
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ManifestVerificationError("manifest verification key is invalid")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            canonical_manifest_bytes(payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ManifestVerificationError("manifest signature is invalid") from exc


def _parse_connector(value: object, *, production: bool) -> RunnerConnectorSpec:
    if not isinstance(value, dict) or set(value) != _CONNECTOR_FIELDS:
        raise ManifestVerificationError("manifest connector fields are invalid")
    try:
        mode = ConnectorExecutionMode(value["execution_mode"])
        if production and mode is not ConnectorExecutionMode.RUNNER:
            raise ManifestVerificationError("production manifest connectors must use runner mode")
        return RunnerConnectorSpec(
            factory_id=value["factory_id"],
            name=value["name"],
            version=value["version"],
            source_system=value["source_system"],
            execution_mode=mode,
        )
    except (KeyError, TypeError, ValueError, RunnerCompositionError) as exc:
        if isinstance(exc, ManifestVerificationError):
            raise
        raise ManifestVerificationError("manifest connector is invalid") from exc
