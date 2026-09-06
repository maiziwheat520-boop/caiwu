"""Every way an envelope, its header, or its key provider can be refused.

An envelope is the only thing standing between stored bytes and plaintext
evidence, so the cipher never repairs, re-encodes, or partially trusts one.
Each test below names one way an envelope could otherwise be bent - a header
that is not canonical, framing that does not add up, a provider that returns
the wrong thing, a libsodium call that fails - and fixes that the cipher
refuses instead of guessing.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from nacl import bindings

from ledgerbridge import crypto
from ledgerbridge.crypto import (
    ENVELOPE_ALGORITHM,
    ENVELOPE_MAGIC,
    ENVELOPE_SCHEMA,
    MAX_CHUNK_SIZE,
    MAX_HEADER_BYTES,
    AuthenticationError,
    CryptoError,
    CryptoSelfTestError,
    EnvelopeFormatError,
    SecretStreamCipher,
    TruncatedCiphertextError,
)
from ledgerbridge.keyring import KeyProviderError, SyntheticKeyProvider, WrappedKey
from tests.test_crypto import OLD_KEY, _cipher

PURPOSE = "evidence"
AAD = b"object-1"

_INIT_PUSH = "crypto_secretstream_xchacha20poly1305_init_push"
_INIT_PULL = "crypto_secretstream_xchacha20poly1305_init_pull"
_PUSH = "crypto_secretstream_xchacha20poly1305_push"
_PULL = "crypto_secretstream_xchacha20poly1305_pull"


# --- envelope surgery -------------------------------------------------------


def _envelope(cipher: SecretStreamCipher, plaintext: bytes = b"payload") -> bytes:
    return cipher.encrypt(plaintext, purpose=PURPOSE, aad=AAD)


def _split(envelope: bytes) -> tuple[bytes, bytes]:
    """Return the raw header bytes and the ciphertext frames after them."""

    prefix_end = len(ENVELOPE_MAGIC) + 4
    size = int.from_bytes(envelope[len(ENVELOPE_MAGIC) : prefix_end], "big")
    return envelope[prefix_end : prefix_end + size], envelope[prefix_end + size :]


def _frame(header: bytes, payload: bytes, *, declared: int | None = None) -> bytes:
    size = len(header) if declared is None else declared
    return ENVELOPE_MAGIC + size.to_bytes(4, "big") + header + payload


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _reheader(envelope: bytes, value: object) -> bytes:
    return _frame(_canonical(value), _split(envelope)[1])


def _mutated(envelope: bytes, **overrides: object) -> bytes:
    header, payload = _split(envelope)
    value: dict[str, Any] = json.loads(header)
    value.update(overrides)
    return _frame(_canonical(value), payload)


def _mutated_key(envelope: bytes, **overrides: object) -> bytes:
    header, payload = _split(envelope)
    value: dict[str, Any] = json.loads(header)
    value["key"].update(overrides)
    return _frame(_canonical(value), payload)


# --- key providers that misbehave -------------------------------------------


class _Delegating:
    """A well-behaved provider each refusal test bends in exactly one place."""

    def __init__(self, *, active_generation: str = "gen-1") -> None:
        self._inner = SyntheticKeyProvider({"gen-1": OLD_KEY}, active_generation=active_generation)

    @property
    def active_generation(self) -> str:
        return self._inner.active_generation

    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        return self._inner.wrap_key(dek, purpose=purpose, aad=aad)

    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes:
        return self._inner.unwrap_key(wrapped, purpose=purpose, aad=aad)

    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        return self._inner.rewrap_key(wrapped, purpose=purpose, aad=aad)

    def self_test(self) -> None:
        self._inner.self_test()


class _RefusesToWrap(_Delegating):
    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        raise KeyProviderError("the key authority declined")


class _BreaksWhileWrapping(_Delegating):
    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        raise RuntimeError("socket closed")


class _WrapsIntoSomethingElse(_Delegating):
    def wrap_key(self, dek: bytes, *, purpose: str, aad: bytes) -> WrappedKey:
        return "not-a-wrapped-key"  # type: ignore[return-value]


class _RefusesToRewrap(_Delegating):
    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        raise KeyProviderError("the key authority declined")


class _BreaksWhileRewrapping(_Delegating):
    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        raise RuntimeError("socket closed")


class _RewrapsUnderAStaleGeneration(_Delegating):
    def rewrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> WrappedKey:
        return dataclasses.replace(
            self._inner.rewrap_key(wrapped, purpose=purpose, aad=aad),
            generation="gen-retired",
        )


class _BreaksWhileUnwrapping(_Delegating):
    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes:
        raise RuntimeError("socket closed")


class _UnwrapsIntoTheWrongKey(_Delegating):
    def unwrap_key(self, wrapped: WrappedKey, *, purpose: str, aad: bytes) -> bytes:
        return b"too short"


class _FailsItsOwnSelfTest(_Delegating):
    def self_test(self) -> None:
        raise RuntimeError("the key authority is unreachable")


# --- the cipher's own configuration -----------------------------------------


@pytest.mark.parametrize("chunk_size", [0, -1, MAX_CHUNK_SIZE + 1, True, 1.0])
def test_a_cipher_must_be_given_a_usable_chunk_size(chunk_size: object) -> None:
    with pytest.raises(ValueError, match="chunk size is invalid"):
        SecretStreamCipher(_Delegating(), chunk_size=chunk_size)  # type: ignore[arg-type]


def test_a_chunk_size_that_changed_after_construction_is_still_refused() -> None:
    """The header encoder re-checks the bound the constructor already enforced."""

    cipher = _cipher()
    cipher._chunk_size = 0
    with pytest.raises(EnvelopeFormatError, match="envelope chunk size is invalid"):
        cipher.encrypt(b"payload", purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("purpose", ["", "x" * 257, b"evidence", "\ud800"])
def test_an_unusable_purpose_is_refused(purpose: object) -> None:
    cipher = _cipher()
    with pytest.raises(CryptoError, match=r"encryption purpose is (required|invalid)"):
        cipher.encrypt(b"payload", purpose=purpose, aad=AAD)  # type: ignore[arg-type]


def test_plaintext_beyond_the_supported_size_is_refused_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crypto, "MAX_ENVELOPE_BYTES", 8)
    with pytest.raises(CryptoError, match="plaintext exceeds the supported size"):
        _cipher().encrypt(b"abcdefghij", purpose=PURPOSE, aad=AAD)


def test_plaintext_beyond_the_supported_size_is_refused_chunk_by_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A streamed caller cannot smuggle past the bound one chunk at a time."""

    monkeypatch.setattr(crypto, "MAX_ENVELOPE_BYTES", 8)
    cipher = _cipher()
    with pytest.raises(CryptoError, match="plaintext exceeds the supported size"):
        list(cipher.encrypt_chunks((b"abc", b"defg"), purpose=PURPOSE, aad=AAD))


# --- key providers, while wrapping ------------------------------------------


def test_a_provider_that_declines_to_wrap_is_not_reinterpreted() -> None:
    cipher = SecretStreamCipher(_RefusesToWrap(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="the key authority declined"):
        cipher.encrypt(b"payload", purpose=PURPOSE, aad=AAD)


def test_a_provider_that_breaks_while_wrapping_is_reported_as_a_provider_failure() -> None:
    cipher = SecretStreamCipher(_BreaksWhileWrapping(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="failed while wrapping a data key"):
        cipher.encrypt(b"payload", purpose=PURPOSE, aad=AAD)


def test_a_provider_that_returns_something_other_than_a_wrapped_key_is_refused() -> None:
    cipher = SecretStreamCipher(_WrapsIntoSomethingElse(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="returned an invalid wrapped key"):
        cipher.encrypt(b"payload", purpose=PURPOSE, aad=AAD)


# --- libsodium, while encrypting --------------------------------------------


def test_a_secretstream_that_cannot_be_started_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> bytes:
        raise ValueError("no state")

    monkeypatch.setattr(bindings, _INIT_PUSH, refuse)
    with pytest.raises(CryptoError, match="secretstream initialization failed"):
        _cipher().encrypt(b"payload", purpose=PURPOSE, aad=AAD)


def test_a_stream_header_of_the_wrong_length_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bindings, _INIT_PUSH, lambda state, dek: b"short")
    with pytest.raises(EnvelopeFormatError, match="secretstream header length is invalid"):
        _cipher().encrypt(b"payload", purpose=PURPOSE, aad=AAD)


def test_a_header_larger_than_the_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "MAX_HEADER_BYTES", 16)
    with pytest.raises(EnvelopeFormatError, match="envelope header is too large"):
        _cipher().encrypt(b"payload", purpose=PURPOSE, aad=AAD)


def test_a_secretstream_that_cannot_encrypt_a_frame_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> bytes:
        raise ValueError("no frame")

    monkeypatch.setattr(bindings, _PUSH, refuse)
    with pytest.raises(CryptoError, match="secretstream encryption failed"):
        _cipher().encrypt(b"payload", purpose=PURPOSE, aad=AAD)


# --- reading an envelope back -----------------------------------------------


def test_an_envelope_that_is_not_bytes_is_refused() -> None:
    cipher = _cipher()
    envelope = _envelope(cipher)
    with pytest.raises(EnvelopeFormatError, match="envelope must be bytes"):
        cipher.decrypt(bytearray(envelope), purpose=PURPOSE, aad=AAD)  # type: ignore[arg-type]


def test_an_envelope_beyond_the_supported_size_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cipher = _cipher()
    envelope = _envelope(cipher)
    monkeypatch.setattr(crypto, "MAX_ENVELOPE_BYTES", 4)
    with pytest.raises(EnvelopeFormatError, match="envelope exceeds the supported size"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("declared", [0, MAX_HEADER_BYTES + 1])
def test_a_header_length_outside_the_bounds_is_refused(declared: int) -> None:
    cipher = _cipher()
    header, payload = _split(_envelope(cipher))
    with pytest.raises(EnvelopeFormatError, match="header length is invalid"):
        cipher.decrypt(_frame(header, payload, declared=declared), purpose=PURPOSE, aad=AAD)


def test_a_header_that_is_cut_short_is_refused() -> None:
    cipher = _cipher()
    header, payload = _split(_envelope(cipher))
    truncated = _frame(header, payload, declared=len(header) + len(payload) + 1)
    with pytest.raises(TruncatedCiphertextError, match="envelope header is truncated"):
        cipher.decrypt(truncated, purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("header", [b"not json", b"\xff\xfe{}", b"["])
def test_a_header_that_is_not_ascii_json_is_refused(header: bytes) -> None:
    cipher = _cipher()
    payload = _split(_envelope(cipher))[1]
    with pytest.raises(EnvelopeFormatError, match="header JSON is invalid"):
        cipher.decrypt(_frame(header, payload), purpose=PURPOSE, aad=AAD)


def test_a_header_that_repeats_a_field_is_refused() -> None:
    """Duplicate keys would let one field be signed and another one read."""

    cipher = _cipher()
    header, payload = _split(_envelope(cipher))
    doubled = b'{"schema":"' + ENVELOPE_SCHEMA.encode("ascii") + b'",' + header[1:]
    with pytest.raises(EnvelopeFormatError, match="header JSON is invalid"):
        cipher.decrypt(_frame(doubled, payload), purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("value", [[], {"schema": ENVELOPE_SCHEMA}])
def test_a_header_without_exactly_the_expected_fields_is_refused(value: object) -> None:
    cipher = _cipher()
    with pytest.raises(EnvelopeFormatError, match="header fields are invalid"):
        cipher.decrypt(_reheader(_envelope(cipher), value), purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize(
    "overrides",
    [{"schema": "ledgerbridge.secretstream.v2"}, {"algorithm": "aes-gcm"}],
)
def test_a_header_from_another_schema_or_algorithm_is_refused(
    overrides: dict[str, object],
) -> None:
    cipher = _cipher()
    with pytest.raises(EnvelopeFormatError, match="schema or algorithm is unsupported"):
        cipher.decrypt(_mutated(_envelope(cipher), **overrides), purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize(
    "overrides",
    [
        {"chunk_size": "8"},
        {"chunk_size": 0},
        {"chunk_size": MAX_CHUNK_SIZE + 1},
        {"stream_header": 1},
        {"key": []},
        {"key": {"generation": "gen-1"}},
    ],
)
def test_a_header_whose_types_are_wrong_is_refused(overrides: dict[str, object]) -> None:
    cipher = _cipher()
    with pytest.raises(EnvelopeFormatError, match="header types are invalid"):
        cipher.decrypt(_mutated(_envelope(cipher), **overrides), purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize(
    "overrides",
    [{"generation": 1}, {"nonce": None}, {"ciphertext": []}],
)
def test_a_wrapped_key_whose_fields_are_not_strings_is_refused(
    overrides: dict[str, object],
) -> None:
    cipher = _cipher()
    with pytest.raises(EnvelopeFormatError, match="wrapped-key fields are invalid"):
        cipher.decrypt(_mutated_key(_envelope(cipher), **overrides), purpose=PURPOSE, aad=AAD)


def test_a_stream_header_that_is_not_base64_is_refused() -> None:
    cipher = _cipher()
    broken = _mutated(_envelope(cipher), stream_header="not base64!")
    with pytest.raises(EnvelopeFormatError, match="secretstream header encoding is invalid"):
        cipher.decrypt(broken, purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_a_wrapped_key_part_that_is_not_base64_is_refused(field: str) -> None:
    cipher = _cipher()
    broken = _mutated_key(_envelope(cipher), **{field: "not base64!"})
    with pytest.raises(EnvelopeFormatError, match="encoding is invalid"):
        cipher.decrypt(broken, purpose=PURPOSE, aad=AAD)


@pytest.mark.parametrize("field", ["nonce", "ciphertext"])
def test_a_wrapped_key_part_of_the_wrong_length_is_refused(field: str) -> None:
    cipher = _cipher()
    broken = _mutated_key(_envelope(cipher), **{field: "AAAA"})
    with pytest.raises(EnvelopeFormatError, match="wrapped-key metadata is invalid"):
        cipher.decrypt(broken, purpose=PURPOSE, aad=AAD)


def test_an_envelope_without_ciphertext_frames_is_refused() -> None:
    cipher = _cipher()
    header = _split(_envelope(cipher))[0]
    with pytest.raises(TruncatedCiphertextError, match="no ciphertext frames"):
        cipher.decrypt(_frame(header, b""), purpose=PURPOSE, aad=AAD)


# --- key providers and libsodium, while decrypting --------------------------


def test_a_provider_that_breaks_while_unwrapping_is_reported_as_a_provider_failure() -> None:
    envelope = _envelope(_cipher())
    cipher = SecretStreamCipher(_BreaksWhileUnwrapping(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="failed while unwrapping a data key"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


def test_a_provider_that_returns_an_unusable_data_key_is_refused() -> None:
    envelope = _envelope(_cipher())
    cipher = SecretStreamCipher(_UnwrapsIntoTheWrongKey(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="returned an invalid data key"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


def test_a_stream_header_that_does_not_authenticate_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cipher = _cipher()
    envelope = _envelope(cipher)

    def refuse(*args: object, **kwargs: object) -> None:
        raise ValueError("bad header")

    monkeypatch.setattr(bindings, _INIT_PULL, refuse)
    with pytest.raises(AuthenticationError, match="header authentication failed"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


def test_a_frame_length_that_is_cut_short_is_refused() -> None:
    cipher = _cipher()
    header = _split(_envelope(cipher))[0]
    with pytest.raises(TruncatedCiphertextError, match="frame length is truncated"):
        cipher.decrypt(_frame(header, b"\x00\x00"), purpose=PURPOSE, aad=AAD)


def test_a_frame_size_outside_the_bounds_is_refused() -> None:
    cipher = _cipher()
    header = _split(_envelope(cipher))[0]
    with pytest.raises(EnvelopeFormatError, match="frame size is invalid"):
        cipher.decrypt(_frame(header, b"\x00\x00\x00\x00"), purpose=PURPOSE, aad=AAD)


def test_a_frame_tag_that_is_neither_message_nor_final_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REKEY and PUSH carry stream semantics this format does not define."""

    cipher = _cipher()
    envelope = _envelope(cipher)
    rekey = bindings.crypto_secretstream_xchacha20poly1305_TAG_REKEY
    monkeypatch.setattr(bindings, _PULL, lambda state, ciphertext, aad: (b"x" * 8, rekey))
    with pytest.raises(EnvelopeFormatError, match="frame tag is unsupported"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


def test_a_non_final_frame_that_is_not_a_full_chunk_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short interior frames would let a reader be handed a shortened stream."""

    cipher = _cipher()
    envelope = _envelope(cipher)
    message = bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
    monkeypatch.setattr(bindings, _PULL, lambda state, ciphertext, aad: (b"x", message))
    with pytest.raises(EnvelopeFormatError, match="non-final plaintext frame has an invalid size"):
        cipher.decrypt(envelope, purpose=PURPOSE, aad=AAD)


# --- rewrapping -------------------------------------------------------------


def test_a_provider_that_declines_to_rewrap_is_not_reinterpreted() -> None:
    envelope = _envelope(_cipher())
    cipher = SecretStreamCipher(_RefusesToRewrap(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="the key authority declined"):
        cipher.rewrap(envelope, purpose=PURPOSE, aad=AAD)


def test_a_provider_that_breaks_while_rewrapping_is_reported_as_a_provider_failure() -> None:
    envelope = _envelope(_cipher())
    cipher = SecretStreamCipher(_BreaksWhileRewrapping(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="failed while rewrapping a data key"):
        cipher.rewrap(envelope, purpose=PURPOSE, aad=AAD)


def test_a_rewrap_that_did_not_use_the_active_generation_is_refused() -> None:
    """A rewrap exists to move an envelope forward; a stale pin defeats it."""

    envelope = _envelope(_cipher())
    cipher = SecretStreamCipher(_RewrapsUnderAStaleGeneration(), chunk_size=8)
    with pytest.raises(KeyProviderError, match="did not use its active generation"):
        cipher.rewrap(envelope, purpose=PURPOSE, aad=AAD)


def test_rewrap_refuses_a_frame_length_that_is_cut_short() -> None:
    cipher = _cipher()
    header = _split(_envelope(cipher))[0]
    with pytest.raises(TruncatedCiphertextError, match="frame length is truncated"):
        cipher.rewrap(_frame(header, b"\x00\x00"), purpose=PURPOSE, aad=AAD)


def test_rewrap_refuses_a_frame_size_outside_the_bounds() -> None:
    cipher = _cipher()
    header = _split(_envelope(cipher))[0]
    with pytest.raises(EnvelopeFormatError, match="frame size is invalid"):
        cipher.rewrap(_frame(header, b"\x00\x00\x00\x00"), purpose=PURPOSE, aad=AAD)


# --- the startup self-test --------------------------------------------------


SELF_TEST_PLAINTEXT = b"secretstream self-test" * 5


class _ReturnsTheWrongPlaintext(SecretStreamCipher):
    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        return b"something else"


class _AcceptsEverything(SecretStreamCipher):
    """A cipher that authenticates nothing at all."""

    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        return SELF_TEST_PLAINTEXT


class _AcceptsTamperedCiphertext(SecretStreamCipher):
    """A cipher whose frame authentication has been defeated."""

    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        try:
            return super().decrypt(envelope, purpose=purpose, aad=aad)
        except TruncatedCiphertextError:
            raise
        except AuthenticationError:
            return SELF_TEST_PLAINTEXT


class _AcceptsTruncatedCiphertext(SecretStreamCipher):
    """A cipher that treats a missing FINAL frame as the end of the stream."""

    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        try:
            return super().decrypt(envelope, purpose=purpose, aad=aad)
        except TruncatedCiphertextError:
            return SELF_TEST_PLAINTEXT


@pytest.mark.parametrize(
    ("cipher_type", "message"),
    [
        (_ReturnsTheWrongPlaintext, "self-test plaintext mismatch"),
        (_AcceptsEverything, "accepted incorrect associated data"),
        (_AcceptsTamperedCiphertext, "accepted tampered ciphertext"),
        (_AcceptsTruncatedCiphertext, "accepted truncated ciphertext"),
    ],
)
def test_a_self_test_reports_the_first_guarantee_the_cipher_failed_to_keep(
    cipher_type: type[SecretStreamCipher], message: str
) -> None:
    cipher = cipher_type(_Delegating(), chunk_size=8)
    with pytest.raises(CryptoSelfTestError, match=message):
        cipher.self_test()


def test_a_self_test_reports_a_provider_that_fails_its_own() -> None:
    cipher = SecretStreamCipher(_FailsItsOwnSelfTest(), chunk_size=8)
    with pytest.raises(CryptoSelfTestError, match="secretstream self-test failed"):
        cipher.self_test()


def test_a_healthy_cipher_passes_its_self_test() -> None:
    SecretStreamCipher(_Delegating(), chunk_size=8).self_test()


def test_the_envelope_algorithm_and_schema_are_the_ones_the_header_carries() -> None:
    header = json.loads(_split(_envelope(_cipher()))[0])

    assert header["schema"] == ENVELOPE_SCHEMA
    assert header["algorithm"] == ENVELOPE_ALGORITHM
