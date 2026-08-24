"""Deployment-neutral envelope-key provider contracts.

Only the in-memory ``SyntheticKeyProvider`` is implemented here.  Production
key discovery, files, environment variables, and KMS/HSM clients belong to a
deployment adapter and must not be added to this module.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from nacl import bindings, exceptions, utils

_GENERATION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_WRAP_DOMAIN: Final = b"ledgerbridge.dek-wrap.v1\x00"
_KEY_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES
_NONCE_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
_TAG_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES
_DEK_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_KEYBYTES


class KeyProviderError(RuntimeError):
    """A key provider could not safely complete an operation."""


class UnknownKeyGenerationError(KeyProviderError):
    """A wrapped DEK refers to a generation unavailable to this provider."""


class KeyUnwrapError(KeyProviderError):
    """A wrapped DEK failed authenticated decryption."""


class KeyProviderSelfTestError(KeyProviderError):
    """A provider failed its startup cryptographic self-test."""


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """Opaque wrapped data-encryption key and its external generation pin."""

    generation: str
    nonce: bytes
    ciphertext: bytes

    def __post_init__(self) -> None:
        _validate_generation(self.generation)
        if type(self.nonce) is not bytes or len(self.nonce) != _NONCE_BYTES:
            raise KeyProviderError("wrapped-key nonce is invalid")
        if type(self.ciphertext) is not bytes or len(self.ciphertext) != _DEK_BYTES + _TAG_BYTES:
            raise KeyProviderError("wrapped-key ciphertext is invalid")


class KeyProvider(Protocol):
    """Minimal contract implemented by a deployment-owned key authority."""

    @property
    def active_generation(self) -> str: ...

    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey: ...

    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes: ...

    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey: ...

    def self_test(self) -> None: ...


class SyntheticKeyProvider:
    """In-memory key provider for synthetic tests only.

    Callers supply random 32-byte KEKs.  This class intentionally has no file,
    environment, command, or network loading path, so it cannot accidentally
    become a production key store.
    """

    __slots__ = ("_active_generation", "_keys")

    def __init__(self, generations: Mapping[str, bytes], *, active_generation: str) -> None:
        if not generations:
            raise KeyProviderError("at least one synthetic key generation is required")
        if len(generations) > 32:
            raise KeyProviderError("too many synthetic key generations")
        copied: dict[str, bytes] = {}
        for generation, key in generations.items():
            _validate_generation(generation)
            if type(key) is not bytes or len(key) != _KEY_BYTES:
                raise KeyProviderError("synthetic wrapping keys must be 32 bytes")
            copied[generation] = key
        _validate_generation(active_generation)
        if active_generation not in copied:
            raise UnknownKeyGenerationError("active key generation is unavailable")
        self._keys = copied
        self._active_generation = active_generation

    @property
    def active_generation(self) -> str:
        return self._active_generation

    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        return self._wrap_with_generation(
            dek,
            generation=self._active_generation,
            purpose=purpose,
            aad=aad,
        )

    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes:
        _validate_context(purpose, aad)
        if not isinstance(wrapped, WrappedKey):
            raise KeyProviderError("wrapped key has an invalid type")
        key = self._keys.get(wrapped.generation)
        if key is None:
            raise UnknownKeyGenerationError("wrapped key generation is unavailable")
        try:
            dek = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                wrapped.ciphertext,
                _wrap_aad(wrapped.generation, purpose, aad),
                wrapped.nonce,
                key,
            )
        except (exceptions.CryptoError, ValueError) as exc:
            raise KeyUnwrapError("wrapped key authentication failed") from exc
        if len(dek) != _DEK_BYTES:
            raise KeyUnwrapError("unwrapped data key has an invalid length")
        return dek

    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        dek = self.unwrap_key(wrapped, purpose=purpose, aad=aad)
        if wrapped.generation == self._active_generation:
            return wrapped
        return self.wrap_key(dek, purpose=purpose, aad=aad)

    def self_test(self) -> None:
        """Exercise every configured generation and fail closed on any anomaly."""

        purpose = "synthetic-key-provider-self-test"
        aad = b"ledgerbridge.synthetic.self-test"
        try:
            for generation in self._keys:
                dek = utils.random(_DEK_BYTES)
                wrapped = self._wrap_with_generation(
                    dek,
                    generation=generation,
                    purpose=purpose,
                    aad=aad,
                )
                recovered = self.unwrap_key(wrapped, purpose=purpose, aad=aad)
                if not hmac.compare_digest(dek, recovered):
                    raise KeyProviderSelfTestError("synthetic provider self-test mismatch")
                tampered = WrappedKey(
                    generation=wrapped.generation,
                    nonce=wrapped.nonce,
                    ciphertext=wrapped.ciphertext[:-1] + bytes((wrapped.ciphertext[-1] ^ 1,)),
                )
                try:
                    self.unwrap_key(tampered, purpose=purpose, aad=aad)
                except KeyUnwrapError:
                    pass
                else:
                    raise KeyProviderSelfTestError(
                        "synthetic provider accepted a tampered wrapped key"
                    )
        except KeyProviderSelfTestError:
            raise
        except Exception as exc:
            raise KeyProviderSelfTestError("synthetic provider self-test failed") from exc

    def _wrap_with_generation(
        self,
        dek: bytes,
        *,
        generation: str,
        purpose: str,
        aad: bytes,
    ) -> WrappedKey:
        _validate_context(purpose, aad)
        if type(dek) is not bytes or len(dek) != _DEK_BYTES:
            raise KeyProviderError("data-encryption key must be 32 bytes")
        key = self._keys.get(generation)
        if key is None:
            raise UnknownKeyGenerationError("wrapping key generation is unavailable")
        nonce = utils.random(_NONCE_BYTES)
        try:
            ciphertext = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
                dek,
                _wrap_aad(generation, purpose, aad),
                nonce,
                key,
            )
        except (exceptions.CryptoError, ValueError) as exc:
            raise KeyProviderError("data key wrapping failed") from exc
        return WrappedKey(generation=generation, nonce=nonce, ciphertext=ciphertext)


def _validate_generation(generation: str) -> None:
    if type(generation) is not str or _GENERATION_PATTERN.fullmatch(generation) is None:
        raise KeyProviderError("key generation is invalid")


def _validate_context(purpose: str, aad: bytes) -> None:
    if type(purpose) is not str or not purpose:
        raise KeyProviderError("key purpose is invalid")
    try:
        purpose_bytes = purpose.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise KeyProviderError("key purpose is invalid") from exc
    if len(purpose_bytes) > 256:
        raise KeyProviderError("key purpose is invalid")
    if type(aad) is not bytes or len(aad) > 1_048_576:
        raise KeyProviderError("key associated data is invalid")


def _wrap_aad(generation: str, purpose: str, aad: bytes) -> bytes:
    generation_bytes = generation.encode("ascii")
    purpose_bytes = purpose.encode("utf-8")
    return b"".join(
        (
            _WRAP_DOMAIN,
            len(generation_bytes).to_bytes(1, "big"),
            generation_bytes,
            len(purpose_bytes).to_bytes(2, "big"),
            purpose_bytes,
            len(aad).to_bytes(4, "big"),
            aad,
        )
    )
