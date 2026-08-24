"""Versioned envelope encryption built on libsodium secretstream.

Each envelope receives an independent random data-encryption key (DEK).  The
DEK is wrapped by a caller-injected ``KeyProvider``; this module never loads or
persists key-encryption keys itself.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final

from nacl import bindings, exceptions

from ledgerbridge.keyring import KeyProvider, KeyProviderError, WrappedKey

ENVELOPE_SCHEMA: Final = "ledgerbridge.secretstream.v1"
ENVELOPE_ALGORITHM: Final = "xchacha20poly1305-secretstream"
ENVELOPE_MAGIC: Final = b"LBSS\x01"
DEFAULT_CHUNK_SIZE: Final = 65_536
MAX_CHUNK_SIZE: Final = 1_048_576
MAX_HEADER_BYTES: Final = 16_384
MAX_ENVELOPE_BYTES: Final = 268_435_456

_HEADER_FIELDS: Final = frozenset({"algorithm", "chunk_size", "key", "schema", "stream_header"})
_KEY_FIELDS: Final = frozenset({"ciphertext", "generation", "nonce"})
_HEADER_LENGTH_BYTES: Final = 4
_FRAME_LENGTH_BYTES: Final = 4
_STREAM_DOMAIN: Final = b"ledgerbridge.secretstream.payload.v1\x00"
_STREAM_HEADER_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES
_DEK_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_KEYBYTES
_OVERHEAD_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_ABYTES


class CryptoError(RuntimeError):
    """Base class for envelope encryption failures."""


class EnvelopeFormatError(CryptoError):
    """An envelope has unsupported, ambiguous, or malformed framing."""


class AuthenticationError(CryptoError):
    """Ciphertext or associated-data authentication failed."""


class TruncatedCiphertextError(AuthenticationError):
    """An envelope ended before its authenticated FINAL record."""


class CryptoSelfTestError(CryptoError):
    """The encryption stack failed its startup self-test."""


@dataclass(frozen=True, slots=True)
class _EnvelopeHeader:
    chunk_size: int
    stream_header: bytes
    wrapped_key: WrappedKey


@dataclass(frozen=True, slots=True)
class _ParsedEnvelope:
    header: _EnvelopeHeader
    payload: bytes


class SecretStreamCipher:
    """Encrypt and authenticate bounded byte strings with explicit framing."""

    __slots__ = ("_chunk_size", "_provider")

    def __init__(self, provider: KeyProvider, *, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        if type(chunk_size) is not int or not 1 <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("secretstream chunk size is invalid")
        self._provider = provider
        self._chunk_size = chunk_size

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    def encrypt(self, plaintext: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        """Return one opaque, self-framed envelope for ``plaintext``."""

        _validate_inputs(plaintext, purpose, aad)
        return b"".join(self.encrypt_chunks((plaintext,), purpose=purpose, aad=aad))

    def encrypt_chunks(
        self,
        chunks: Iterable[bytes],
        *,
        purpose: str,
        aad: bytes = b"",
    ) -> Iterator[bytes]:
        """Yield an envelope while retaining at most one plaintext chunk.

        The one-chunk lookahead is required so the final plaintext frame can be
        marked with secretstream's authenticated ``TAG_FINAL``.  Empty input
        still emits an authenticated empty final frame.
        """

        _validate_context(purpose, aad)
        dek = bindings.crypto_secretstream_xchacha20poly1305_keygen()
        try:
            wrapped = self._provider.wrap_key(dek, purpose=purpose, aad=aad)
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError("key provider failed while wrapping a data key") from exc
        _require_wrapped_key(wrapped)
        state = bindings.crypto_secretstream_xchacha20poly1305_state()
        try:
            stream_header = bindings.crypto_secretstream_xchacha20poly1305_init_push(state, dek)
        except (exceptions.CryptoError, ValueError) as exc:
            raise CryptoError("secretstream initialization failed") from exc
        header = _EnvelopeHeader(
            chunk_size=self._chunk_size,
            stream_header=stream_header,
            wrapped_key=wrapped,
        )
        encoded_header = _encode_header(header)
        yield ENVELOPE_MAGIC
        yield len(encoded_header).to_bytes(_HEADER_LENGTH_BYTES, "big")
        yield encoded_header

        pending: bytes | None = None
        buffer = bytearray()
        total_plaintext = 0
        index = 0
        for supplied in chunks:
            if type(supplied) is not bytes:
                raise CryptoError("plaintext chunks must be bytes")
            total_plaintext += len(supplied)
            if total_plaintext > MAX_ENVELOPE_BYTES // 2:
                raise CryptoError("plaintext exceeds the supported size")
            offset = 0
            while offset < len(supplied):
                take = min(self._chunk_size - len(buffer), len(supplied) - offset)
                buffer.extend(supplied[offset : offset + take])
                offset += take
                if len(buffer) != self._chunk_size:
                    continue
                if pending is not None:
                    yield self._encrypt_frame(
                        state,
                        header,
                        pending,
                        purpose=purpose,
                        aad=aad,
                        index=index,
                        final=False,
                    )
                    index += 1
                pending = bytes(buffer)
                buffer.clear()
        if buffer:
            if pending is not None:
                yield self._encrypt_frame(
                    state,
                    header,
                    pending,
                    purpose=purpose,
                    aad=aad,
                    index=index,
                    final=False,
                )
                index += 1
            pending = bytes(buffer)
        yield self._encrypt_frame(
            state,
            header,
            pending if pending is not None else b"",
            purpose=purpose,
            aad=aad,
            index=index,
            final=True,
        )

    @staticmethod
    def _encrypt_frame(
        state: bindings.crypto_secretstream_xchacha20poly1305_state,
        header: _EnvelopeHeader,
        plaintext: bytes,
        *,
        purpose: str,
        aad: bytes,
        index: int,
        final: bool,
    ) -> bytes:
        tag = (
            bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL
            if final
            else bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
        )
        try:
            ciphertext = bindings.crypto_secretstream_xchacha20poly1305_push(
                state,
                plaintext,
                _frame_aad(header, purpose=purpose, aad=aad, index=index),
                tag,
            )
        except (exceptions.CryptoError, ValueError) as exc:
            raise CryptoError("secretstream encryption failed") from exc
        return len(ciphertext).to_bytes(_FRAME_LENGTH_BYTES, "big") + ciphertext

    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        """Authenticate the complete envelope and return its plaintext."""

        _validate_context(purpose, aad)
        parsed = _parse_envelope(envelope)
        return self._decrypt_parsed(parsed, purpose=purpose, aad=aad)

    def rewrap(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        """Rewrap only the DEK; the payload is never decrypted or rewritten."""

        _validate_context(purpose, aad)
        parsed = _parse_envelope(envelope)
        _validate_payload_framing(parsed)
        try:
            wrapped = self._provider.rewrap_key(
                parsed.header.wrapped_key,
                purpose=purpose,
                aad=aad,
            )
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError("key provider failed while rewrapping a data key") from exc
        _require_wrapped_key(wrapped)
        if wrapped.generation != self._provider.active_generation:
            raise KeyProviderError("key provider did not use its active generation")
        new_header = _EnvelopeHeader(
            chunk_size=parsed.header.chunk_size,
            stream_header=parsed.header.stream_header,
            wrapped_key=wrapped,
        )
        encoded_header = _encode_header(new_header)
        if encoded_header == _encode_header(parsed.header):
            return envelope
        return b"".join(
            (
                ENVELOPE_MAGIC,
                len(encoded_header).to_bytes(_HEADER_LENGTH_BYTES, "big"),
                encoded_header,
                parsed.payload,
            )
        )

    def self_test(self) -> None:
        """Exercise provider, round trip, AAD binding, tamper, and truncation checks."""

        purpose = "synthetic-secretstream-self-test"
        aad = b"ledgerbridge.synthetic.self-test"
        plaintext = b"secretstream self-test" * 5
        try:
            self._provider.self_test()
            envelope = self.encrypt(plaintext, purpose=purpose, aad=aad)
            if self.decrypt(envelope, purpose=purpose, aad=aad) != plaintext:
                raise CryptoSelfTestError("secretstream self-test plaintext mismatch")
            try:
                self.decrypt(envelope, purpose=purpose, aad=aad + b"!")
            except (AuthenticationError, KeyProviderError):
                pass
            else:
                raise CryptoSelfTestError("secretstream accepted incorrect associated data")
            tampered = envelope[:-1] + bytes((envelope[-1] ^ 1,))
            try:
                self.decrypt(tampered, purpose=purpose, aad=aad)
            except AuthenticationError:
                pass
            else:
                raise CryptoSelfTestError("secretstream accepted tampered ciphertext")
            try:
                self.decrypt(envelope[:-1], purpose=purpose, aad=aad)
            except TruncatedCiphertextError:
                pass
            else:
                raise CryptoSelfTestError("secretstream accepted truncated ciphertext")
        except CryptoSelfTestError:
            raise
        except Exception as exc:
            raise CryptoSelfTestError("secretstream self-test failed") from exc

    def _decrypt_parsed(
        self,
        parsed: _ParsedEnvelope,
        *,
        purpose: str,
        aad: bytes,
    ) -> bytes:
        try:
            dek = self._provider.unwrap_key(
                parsed.header.wrapped_key,
                purpose=purpose,
                aad=aad,
            )
        except KeyProviderError:
            raise
        except Exception as exc:
            raise KeyProviderError("key provider failed while unwrapping a data key") from exc
        if type(dek) is not bytes or len(dek) != _DEK_BYTES:
            raise KeyProviderError("key provider returned an invalid data key")
        state = bindings.crypto_secretstream_xchacha20poly1305_state()
        try:
            bindings.crypto_secretstream_xchacha20poly1305_init_pull(
                state,
                parsed.header.stream_header,
                dek,
            )
        except (exceptions.CryptoError, ValueError) as exc:
            raise AuthenticationError("secretstream header authentication failed") from exc

        plaintext_parts: list[bytes] = []
        offset = 0
        index = 0
        final_seen = False
        while offset < len(parsed.payload):
            if len(parsed.payload) - offset < _FRAME_LENGTH_BYTES:
                raise TruncatedCiphertextError("ciphertext frame length is truncated")
            frame_size = int.from_bytes(
                parsed.payload[offset : offset + _FRAME_LENGTH_BYTES],
                "big",
            )
            offset += _FRAME_LENGTH_BYTES
            if not _OVERHEAD_BYTES <= frame_size <= parsed.header.chunk_size + _OVERHEAD_BYTES:
                raise EnvelopeFormatError("ciphertext frame size is invalid")
            if len(parsed.payload) - offset < frame_size:
                raise TruncatedCiphertextError("ciphertext frame is truncated")
            ciphertext = parsed.payload[offset : offset + frame_size]
            offset += frame_size
            try:
                plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(
                    state,
                    ciphertext,
                    _frame_aad(parsed.header, purpose=purpose, aad=aad, index=index),
                )
            except (exceptions.CryptoError, ValueError) as exc:
                raise AuthenticationError("ciphertext authentication failed") from exc
            if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
                if offset != len(parsed.payload):
                    raise EnvelopeFormatError("ciphertext follows the FINAL frame")
                final_seen = True
            elif tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE:
                raise EnvelopeFormatError("ciphertext frame tag is unsupported")
            if not final_seen and len(plaintext) != parsed.header.chunk_size:
                raise EnvelopeFormatError("non-final plaintext frame has an invalid size")
            plaintext_parts.append(plaintext)
            index += 1
        if not final_seen:
            raise TruncatedCiphertextError("ciphertext has no authenticated FINAL frame")
        return b"".join(plaintext_parts)


def _encode_header(header: _EnvelopeHeader) -> bytes:
    if not 1 <= header.chunk_size <= MAX_CHUNK_SIZE:
        raise EnvelopeFormatError("envelope chunk size is invalid")
    if len(header.stream_header) != _STREAM_HEADER_BYTES:
        raise EnvelopeFormatError("secretstream header length is invalid")
    key = header.wrapped_key
    value: dict[str, object] = {
        "algorithm": ENVELOPE_ALGORITHM,
        "chunk_size": header.chunk_size,
        "key": {
            "ciphertext": base64.b64encode(key.ciphertext).decode("ascii"),
            "generation": key.generation,
            "nonce": base64.b64encode(key.nonce).decode("ascii"),
        },
        "schema": ENVELOPE_SCHEMA,
        "stream_header": base64.b64encode(header.stream_header).decode("ascii"),
    }
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > MAX_HEADER_BYTES:
        raise EnvelopeFormatError("envelope header is too large")
    return encoded


def _parse_envelope(envelope: bytes) -> _ParsedEnvelope:
    if type(envelope) is not bytes:
        raise EnvelopeFormatError("encrypted envelope must be bytes")
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise EnvelopeFormatError("encrypted envelope exceeds the supported size")
    prefix_size = len(ENVELOPE_MAGIC) + _HEADER_LENGTH_BYTES
    if len(envelope) < prefix_size or not envelope.startswith(ENVELOPE_MAGIC):
        raise EnvelopeFormatError("envelope magic or version is unsupported")
    header_size = int.from_bytes(
        envelope[len(ENVELOPE_MAGIC) : prefix_size],
        "big",
    )
    if not 1 <= header_size <= MAX_HEADER_BYTES:
        raise EnvelopeFormatError("envelope header length is invalid")
    header_end = prefix_size + header_size
    if len(envelope) < header_end:
        raise TruncatedCiphertextError("envelope header is truncated")
    encoded_header = envelope[prefix_size:header_end]
    try:
        value = json.loads(encoded_header.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, EnvelopeFormatError) as exc:
        raise EnvelopeFormatError("envelope header JSON is invalid") from exc
    if not isinstance(value, dict) or set(value) != _HEADER_FIELDS:
        raise EnvelopeFormatError("envelope header fields are invalid")
    if value.get("schema") != ENVELOPE_SCHEMA or value.get("algorithm") != ENVELOPE_ALGORITHM:
        raise EnvelopeFormatError("envelope schema or algorithm is unsupported")
    chunk_size = value.get("chunk_size")
    stream_header_text = value.get("stream_header")
    key_value = value.get("key")
    if (
        type(chunk_size) is not int
        or not 1 <= chunk_size <= MAX_CHUNK_SIZE
        or not isinstance(stream_header_text, str)
        or not isinstance(key_value, dict)
        or set(key_value) != _KEY_FIELDS
    ):
        raise EnvelopeFormatError("envelope header types are invalid")
    generation = key_value.get("generation")
    nonce_text = key_value.get("nonce")
    ciphertext_text = key_value.get("ciphertext")
    if (
        type(generation) is not str
        or type(nonce_text) is not str
        or type(ciphertext_text) is not str
    ):
        raise EnvelopeFormatError("wrapped-key fields are invalid")
    stream_header = _decode_base64(stream_header_text, "secretstream header")
    nonce = _decode_base64(nonce_text, "wrapped-key nonce")
    ciphertext = _decode_base64(ciphertext_text, "wrapped-key ciphertext")
    try:
        wrapped = WrappedKey(generation=generation, nonce=nonce, ciphertext=ciphertext)
    except KeyProviderError as exc:
        raise EnvelopeFormatError("wrapped-key metadata is invalid") from exc
    header = _EnvelopeHeader(
        chunk_size=chunk_size,
        stream_header=stream_header,
        wrapped_key=wrapped,
    )
    if len(stream_header) != _STREAM_HEADER_BYTES or _encode_header(header) != encoded_header:
        raise EnvelopeFormatError("envelope header is not canonical")
    payload = envelope[header_end:]
    if not payload:
        raise TruncatedCiphertextError("envelope has no ciphertext frames")
    return _ParsedEnvelope(header=header, payload=payload)


def _frame_aad(header: _EnvelopeHeader, *, purpose: str, aad: bytes, index: int) -> bytes:
    purpose_bytes = purpose.encode("utf-8")
    immutable_header = json.dumps(
        {
            "algorithm": ENVELOPE_ALGORITHM,
            "chunk_size": header.chunk_size,
            "schema": ENVELOPE_SCHEMA,
            "stream_header": base64.b64encode(header.stream_header).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return b"".join(
        (
            _STREAM_DOMAIN,
            len(immutable_header).to_bytes(2, "big"),
            immutable_header,
            len(purpose_bytes).to_bytes(2, "big"),
            purpose_bytes,
            len(aad).to_bytes(4, "big"),
            aad,
            index.to_bytes(8, "big"),
        )
    )


def _validate_payload_framing(parsed: _ParsedEnvelope) -> None:
    """Validate length framing without accessing plaintext or secretstream tags."""

    offset = 0
    frames = 0
    while offset < len(parsed.payload):
        if len(parsed.payload) - offset < _FRAME_LENGTH_BYTES:
            raise TruncatedCiphertextError("ciphertext frame length is truncated")
        frame_size = int.from_bytes(
            parsed.payload[offset : offset + _FRAME_LENGTH_BYTES],
            "big",
        )
        offset += _FRAME_LENGTH_BYTES
        if not _OVERHEAD_BYTES <= frame_size <= parsed.header.chunk_size + _OVERHEAD_BYTES:
            raise EnvelopeFormatError("ciphertext frame size is invalid")
        if len(parsed.payload) - offset < frame_size:
            raise TruncatedCiphertextError("ciphertext frame is truncated")
        offset += frame_size
        frames += 1
    if frames == 0:
        raise TruncatedCiphertextError("envelope has no ciphertext frames")


def _require_wrapped_key(value: object) -> None:
    if not isinstance(value, WrappedKey):
        raise KeyProviderError("key provider returned an invalid wrapped key")


def _decode_base64(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise EnvelopeFormatError(f"{label} encoding is invalid") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise EnvelopeFormatError("envelope header contains duplicate fields")
        value[key] = item
    return value


def _validate_inputs(plaintext: bytes, purpose: str, aad: bytes) -> None:
    if type(plaintext) is not bytes:
        raise CryptoError("plaintext must be bytes")
    if len(plaintext) > MAX_ENVELOPE_BYTES // 2:
        raise CryptoError("plaintext exceeds the supported size")
    _validate_context(purpose, aad)


def _validate_context(purpose: str, aad: bytes) -> None:
    if type(purpose) is not str or not purpose:
        raise CryptoError("encryption purpose is required")
    try:
        purpose_bytes = purpose.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CryptoError("encryption purpose is invalid") from exc
    if len(purpose_bytes) > 256:
        raise CryptoError("encryption purpose is invalid")
    if type(aad) is not bytes or len(aad) > 1_048_576:
        raise CryptoError("associated data is invalid")
