"""Synthetic S1 encrypted evidence store composed over ``ArtifactStore``.

The legacy store remains available for old synthetic tests, but this adapter
ensures every byte it hands to the durable staging/published layer is already a
versioned secretstream envelope.  Plaintext identity is kept in the returned
typed metadata and is never used as the durable storage path.
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from ledgerbridge.artifacts import ArtifactStore, BinarySource, PublishedArtifact
from ledgerbridge.crypto import CryptoError, SecretStreamCipher, _parse_envelope
from ledgerbridge.keyring import KeyProviderError, WrappedKey
from ledgerbridge.secure_spool import EncryptedSpool

_ARTIFACT_PURPOSE = "ledgerbridge-artifact-v2"
_OBJECT_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class EncryptedArtifactError(RuntimeError):
    """Encrypted artifact publication or authentication failed."""


class EncryptedArtifactIntegrityError(EncryptedArtifactError):
    """Ciphertext did not authenticate to the expected plaintext identity."""


@dataclass(frozen=True, slots=True)
class EncryptedPublishedArtifact:
    object_ref: str
    plaintext_sha256: bytes
    plaintext_size: int
    ciphertext: PublishedArtifact

    def __post_init__(self) -> None:
        if _OBJECT_REF_PATTERN.fullmatch(self.object_ref) is None:
            raise ValueError("encrypted artifact object reference is invalid")
        if len(self.plaintext_sha256) != hashlib.sha256().digest_size:
            raise ValueError("encrypted artifact plaintext digest is invalid")
        if type(self.plaintext_size) is not int or self.plaintext_size < 0:
            raise ValueError("encrypted artifact plaintext size is invalid")

    @property
    def storage_key(self) -> str:
        """Opaque ciphertext identity; never the plaintext digest."""

        return self.ciphertext.storage_key

    @property
    def created(self) -> bool:
        return self.ciphertext.created


@dataclass(frozen=True, slots=True)
class EncryptedEnvelopeMetadata:
    """Immutable envelope header fields recorded beside a ciphertext blob."""

    chunk_size: int
    stream_header: bytes
    wrapped_key: WrappedKey

    def __post_init__(self) -> None:
        if type(self.chunk_size) is not int or not 1 <= self.chunk_size <= 1_048_576:
            raise ValueError("encrypted envelope chunk size is invalid")
        if type(self.stream_header) is not bytes or len(self.stream_header) != 24:
            raise ValueError("encrypted envelope stream header is invalid")


PublicationState = Literal["open", "committed", "aborted"]


class EncryptedArtifactPublication:
    """A new encrypted blob held until its database reference commits."""

    __slots__ = ("_artifact", "_state", "_store")

    def __init__(
        self,
        store: EncryptedArtifactStore,
        artifact: EncryptedPublishedArtifact,
    ) -> None:
        self._store = store
        self._artifact = artifact
        self._state: PublicationState = "open"

    @property
    def artifact(self) -> EncryptedPublishedArtifact:
        return self._artifact

    @property
    def state(self) -> PublicationState:
        return self._state

    def commit(self) -> None:
        if self._state == "aborted":
            raise EncryptedArtifactError("encrypted artifact publication is aborted")
        self._state = "committed"

    def abort(self) -> None:
        if self._state != "open":
            return
        self._store._discard_uncommitted(self._artifact)
        self._state = "aborted"

    def __enter__(self) -> EncryptedArtifactPublication:
        if self._state != "open":
            raise EncryptedArtifactError(f"encrypted artifact publication is {self._state}")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._state == "open":
            self.abort()


class EncryptedArtifactHandoff:
    """Bounded upload handoff whose transient backing file is ciphertext-only."""

    def __init__(self, store: EncryptedArtifactStore) -> None:
        self._store = store
        self._spool = EncryptedSpool()
        self._size = 0
        self._state = "open"

    @property
    def state(self) -> str:
        return self._state

    @property
    def byte_size(self) -> int:
        return self._size

    def write(self, chunk: bytes) -> None:
        self._require_open()
        if type(chunk) is not bytes:
            raise TypeError("encrypted artifact chunks must be bytes")
        if self._size + len(chunk) > self._store.max_plaintext_bytes:
            self.abort()
            raise EncryptedArtifactError("artifact exceeds configured plaintext limit")
        try:
            self._spool.write(chunk)
        except BaseException:
            self.abort()
            raise
        self._size += len(chunk)

    def complete(self, *, parser_complete: bool) -> EncryptedPublishedArtifact:
        self._require_open()
        if not parser_complete:
            raise EncryptedArtifactError("encrypted artifact handoff requires parser completion")
        try:
            self._spool.seal()
            artifact = self._store.publish(self._spool)
        except BaseException:
            self.abort()
            raise
        self._spool.close()
        self._state = "committed"
        return artifact

    def abort(self) -> None:
        if self._state in {"aborted", "committed"}:
            return
        self._spool.close()
        self._state = "aborted"

    def __enter__(self) -> EncryptedArtifactHandoff:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._state == "open":
            self.abort()

    def _require_open(self) -> None:
        if self._state != "open":
            raise EncryptedArtifactError(f"encrypted artifact handoff is {self._state}")


class EncryptedArtifactStore:
    """Encrypt evidence before delegating durable storage to ``ArtifactStore``."""

    def __init__(
        self,
        durable_store: ArtifactStore,
        cipher: SecretStreamCipher,
        *,
        max_plaintext_bytes: int,
    ) -> None:
        if type(max_plaintext_bytes) is not int or max_plaintext_bytes <= 0:
            raise ValueError("max_plaintext_bytes must be positive")
        self._durable = durable_store
        self._cipher = cipher
        self.max_plaintext_bytes = max_plaintext_bytes

    def begin_handoff(self) -> EncryptedArtifactHandoff:
        return EncryptedArtifactHandoff(self)

    def publish(self, stream: BinarySource) -> EncryptedPublishedArtifact:
        publication = self.begin_publication(stream)
        publication.commit()
        return publication.artifact

    def begin_publication(self, stream: BinarySource) -> EncryptedArtifactPublication:
        return EncryptedArtifactPublication(self, self._publish(stream))

    def _publish(self, stream: BinarySource) -> EncryptedPublishedArtifact:
        object_ref = secrets.token_hex(32)
        hasher = hashlib.sha256()
        plaintext_size = 0

        def plaintext_chunks() -> Iterator[bytes]:
            nonlocal plaintext_size
            while True:
                chunk = stream.read(self._cipher.chunk_size)
                if not chunk:
                    return
                if type(chunk) is not bytes:
                    raise EncryptedArtifactError("artifact stream must return bytes")
                plaintext_size += len(chunk)
                if plaintext_size > self.max_plaintext_bytes:
                    raise EncryptedArtifactError("artifact exceeds configured plaintext limit")
                hasher.update(chunk)
                yield chunk

        handoff = self._durable.begin_handoff()
        try:
            for ciphertext in self._cipher.encrypt_chunks(
                plaintext_chunks(),
                purpose=_ARTIFACT_PURPOSE,
                aad=_artifact_aad(object_ref),
            ):
                handoff.write(ciphertext)
            published = handoff.complete(parser_complete=True)
        except BaseException:
            handoff.abort()
            raise
        return EncryptedPublishedArtifact(
            object_ref=object_ref,
            plaintext_sha256=hasher.digest(),
            plaintext_size=plaintext_size,
            ciphertext=published,
        )

    def _discard_uncommitted(self, artifact: EncryptedPublishedArtifact) -> None:
        _require_artifact(artifact)
        self._durable._discard_created(artifact.ciphertext)

    @contextmanager
    def open_verified(
        self,
        artifact: EncryptedPublishedArtifact,
        *,
        envelope_metadata: EncryptedEnvelopeMetadata | None = None,
    ) -> Iterator[io.BytesIO]:
        _require_artifact(artifact)
        with self._durable.open_verified(artifact.ciphertext) as ciphertext_stream:
            ciphertext = ciphertext_stream.read()
        try:
            if envelope_metadata is None:
                plaintext = self._cipher.decrypt(
                    ciphertext,
                    purpose=_ARTIFACT_PURPOSE,
                    aad=_artifact_aad(artifact.object_ref),
                )
            else:
                plaintext = self._cipher.decrypt_verified_metadata(
                    ciphertext,
                    purpose=_ARTIFACT_PURPOSE,
                    aad=_artifact_aad(artifact.object_ref),
                    expected_chunk_size=envelope_metadata.chunk_size,
                    expected_stream_header=envelope_metadata.stream_header,
                    expected_wrapped_key=envelope_metadata.wrapped_key,
                )
        except (CryptoError, KeyProviderError) as exc:
            raise EncryptedArtifactIntegrityError(
                "encrypted artifact authentication failed"
            ) from exc
        if (
            len(plaintext) != artifact.plaintext_size
            or hashlib.sha256(plaintext).digest() != artifact.plaintext_sha256
        ):
            raise EncryptedArtifactIntegrityError("encrypted artifact plaintext identity mismatch")
        stream = io.BytesIO(plaintext)
        try:
            yield stream
        finally:
            stream.close()

    def read_prefix(self, artifact: EncryptedPublishedArtifact, limit: int) -> bytes:
        if type(limit) is not int or limit < 0:
            raise ValueError("prefix limit must be non-negative")
        with self.open_verified(artifact) as stream:
            return stream.read(limit)

    def envelope_metadata(self, artifact: EncryptedPublishedArtifact) -> EncryptedEnvelopeMetadata:
        """Return the authenticated envelope fields persisted beside one blob."""

        _require_artifact(artifact)
        with self._durable.open_verified(artifact.ciphertext) as ciphertext_stream:
            ciphertext = ciphertext_stream.read()
        try:
            header = _parse_envelope(ciphertext).header
        except CryptoError as exc:
            raise EncryptedArtifactIntegrityError(
                "encrypted artifact envelope metadata is invalid"
            ) from exc
        return EncryptedEnvelopeMetadata(
            chunk_size=header.chunk_size,
            stream_header=header.stream_header,
            wrapped_key=header.wrapped_key,
        )


def _artifact_aad(object_ref: str) -> bytes:
    if _OBJECT_REF_PATTERN.fullmatch(object_ref) is None:
        raise EncryptedArtifactIntegrityError("encrypted artifact object reference is invalid")
    return b"ledgerbridge.artifact.object.v2\x00" + bytes.fromhex(object_ref)


def _require_artifact(artifact: object) -> None:
    if not isinstance(artifact, EncryptedPublishedArtifact):
        raise EncryptedArtifactIntegrityError("encrypted artifact metadata is invalid")
