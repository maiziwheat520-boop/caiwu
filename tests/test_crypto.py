from __future__ import annotations

import json

import pytest

from ledgerbridge.crypto import (
    ENVELOPE_MAGIC,
    AuthenticationError,
    CryptoError,
    EnvelopeFormatError,
    SecretStreamCipher,
    TruncatedCiphertextError,
)
from ledgerbridge.keyring import KeyUnwrapError, SyntheticKeyProvider, WrappedKey

OLD_KEY = b"\x44" * 32
ACTIVE_KEY = b"\x55" * 32


def _cipher(*, generation: str = "gen-1", chunk_size: int = 8) -> SecretStreamCipher:
    keys = {"gen-1": OLD_KEY}
    if generation == "gen-2":
        keys["gen-2"] = ACTIVE_KEY
    return SecretStreamCipher(
        SyntheticKeyProvider(keys, active_generation=generation),
        chunk_size=chunk_size,
    )


def _payload(envelope: bytes) -> bytes:
    prefix_end = len(ENVELOPE_MAGIC) + 4
    header_size = int.from_bytes(envelope[len(ENVELOPE_MAGIC) : prefix_end], "big")
    return envelope[prefix_end + header_size :]


@pytest.mark.parametrize("plaintext", [b"", b"short", b"exactly-8", b"many chunks of text"])
def test_secretstream_round_trip_and_per_object_randomness(plaintext: bytes) -> None:
    cipher = _cipher()

    first = cipher.encrypt(plaintext, purpose="evidence", aad=b"object-1")
    second = cipher.encrypt(plaintext, purpose="evidence", aad=b"object-1")

    assert first.startswith(ENVELOPE_MAGIC)
    assert first != second
    assert cipher.decrypt(first, purpose="evidence", aad=b"object-1") == plaintext
    assert cipher.decrypt(second, purpose="evidence", aad=b"object-1") == plaintext


def test_secretstream_chunk_iterator_never_requires_complete_plaintext() -> None:
    cipher = _cipher(chunk_size=4)
    supplied = (chunk for chunk in (b"ab", b"", b"cdefg", b"h"))

    envelope = b"".join(cipher.encrypt_chunks(supplied, purpose="evidence", aad=b"stream-object"))

    assert cipher.decrypt(envelope, purpose="evidence", aad=b"stream-object") == b"abcdefgh"


def test_secretstream_chunk_iterator_rejects_non_bytes() -> None:
    cipher = _cipher()
    with pytest.raises(CryptoError, match="chunks"):
        b"".join(
            cipher.encrypt_chunks(
                [b"valid", "invalid"],  # type: ignore[list-item]
                purpose="evidence",
            )
        )


def test_secretstream_binds_purpose_and_associated_data() -> None:
    cipher = _cipher()
    envelope = cipher.encrypt(b"private", purpose="evidence", aad=b"object-1")

    with pytest.raises(KeyUnwrapError, match="authentication"):
        cipher.decrypt(envelope, purpose="candidate", aad=b"object-1")
    with pytest.raises(KeyUnwrapError, match="authentication"):
        cipher.decrypt(envelope, purpose="evidence", aad=b"object-2")


def test_secretstream_verified_metadata_binds_authenticated_header() -> None:
    cipher = _cipher(chunk_size=8)
    envelope = cipher.encrypt(b"descriptor-bound", purpose="evidence", aad=b"object-1")
    # Parse through the module helper so the descriptor test uses the exact
    # immutable values that would have come from the database row.
    from ledgerbridge.crypto import _parse_envelope

    parsed = _parse_envelope(envelope)
    assert (
        cipher.decrypt_verified_metadata(
            envelope,
            purpose="evidence",
            aad=b"object-1",
            expected_chunk_size=parsed.header.chunk_size,
            expected_stream_header=parsed.header.stream_header,
            expected_wrapped_key=parsed.header.wrapped_key,
        )
        == b"descriptor-bound"
    )
    with pytest.raises(EnvelopeFormatError, match="metadata"):
        cipher.decrypt_verified_metadata(
            envelope,
            purpose="evidence",
            aad=b"object-1",
            expected_chunk_size=parsed.header.chunk_size + 1,
            expected_stream_header=parsed.header.stream_header,
            expected_wrapped_key=parsed.header.wrapped_key,
        )
    with pytest.raises(EnvelopeFormatError, match="metadata"):
        cipher.decrypt_verified_metadata(
            envelope,
            purpose="evidence",
            aad=b"object-1",
            expected_chunk_size=parsed.header.chunk_size,
            expected_stream_header=b"x" * 24,
            expected_wrapped_key=parsed.header.wrapped_key,
        )
    with pytest.raises(EnvelopeFormatError, match="metadata"):
        cipher.decrypt_verified_metadata(
            envelope,
            purpose="evidence",
            aad=b"object-1",
            expected_chunk_size=parsed.header.chunk_size,
            expected_stream_header=parsed.header.stream_header,
            expected_wrapped_key=WrappedKey(
                generation=parsed.header.wrapped_key.generation,
                nonce=b"x" * 24,
                ciphertext=parsed.header.wrapped_key.ciphertext,
            ),
        )


def test_secretstream_detects_tamper_truncation_and_trailing_data() -> None:
    cipher = _cipher(chunk_size=4)
    envelope = cipher.encrypt(b"twelve-bytes", purpose="state", aad=b"row-1")
    tampered = envelope[:-1] + bytes((envelope[-1] ^ 1,))

    with pytest.raises(AuthenticationError, match="authentication"):
        cipher.decrypt(tampered, purpose="state", aad=b"row-1")
    with pytest.raises(TruncatedCiphertextError, match="truncated"):
        cipher.decrypt(envelope[:-1], purpose="state", aad=b"row-1")
    with pytest.raises(EnvelopeFormatError, match="FINAL"):
        cipher.decrypt(envelope + b"extra", purpose="state", aad=b"row-1")


def test_secretstream_requires_final_frame_when_whole_final_record_is_removed() -> None:
    cipher = _cipher(chunk_size=4)
    envelope = cipher.encrypt(b"eight888", purpose="state", aad=b"row-1")
    header_end = (
        len(ENVELOPE_MAGIC)
        + 4
        + int.from_bytes(envelope[len(ENVELOPE_MAGIC) : len(ENVELOPE_MAGIC) + 4], "big")
    )
    frames: list[bytes] = []
    offset = header_end
    while offset < len(envelope):
        size = int.from_bytes(envelope[offset : offset + 4], "big")
        frames.append(envelope[offset : offset + 4 + size])
        offset += 4 + size

    with pytest.raises(TruncatedCiphertextError, match="FINAL"):
        cipher.decrypt(
            envelope[:header_end] + b"".join(frames[:-1]),
            purpose="state",
            aad=b"row-1",
        )


def test_secretstream_rejects_noncanonical_and_unsupported_headers() -> None:
    cipher = _cipher()
    envelope = cipher.encrypt(b"private", purpose="state", aad=b"row-1")
    prefix_end = len(ENVELOPE_MAGIC) + 4
    header_size = int.from_bytes(envelope[len(ENVELOPE_MAGIC) : prefix_end], "big")
    header_end = prefix_end + header_size
    header = json.loads(envelope[prefix_end:header_end])
    noncanonical = json.dumps(header, indent=2).encode("ascii")
    rebuilt = (
        ENVELOPE_MAGIC + len(noncanonical).to_bytes(4, "big") + noncanonical + envelope[header_end:]
    )
    unsupported = b"XXXX\x01" + envelope[len(ENVELOPE_MAGIC) :]

    with pytest.raises(EnvelopeFormatError, match="canonical"):
        cipher.decrypt(rebuilt, purpose="state", aad=b"row-1")
    with pytest.raises(EnvelopeFormatError, match="unsupported"):
        cipher.decrypt(unsupported, purpose="state", aad=b"row-1")


def test_secretstream_rewraps_old_generation_without_changing_plaintext() -> None:
    old_cipher = _cipher(generation="gen-1")
    plaintext = b"encrypted state"
    old_envelope = old_cipher.encrypt(plaintext, purpose="state", aad=b"row-7")
    rotated = _cipher(generation="gen-2")

    new_envelope = rotated.rewrap(old_envelope, purpose="state", aad=b"row-7")

    assert new_envelope != old_envelope
    assert _payload(new_envelope) == _payload(old_envelope)
    assert rotated.decrypt(new_envelope, purpose="state", aad=b"row-7") == plaintext
    assert rotated.rewrap(new_envelope, purpose="state", aad=b"row-7") == new_envelope


def test_secretstream_rewrap_refuses_malformed_framing_without_decrypting_payload() -> None:
    cipher = _cipher()
    envelope = cipher.encrypt(b"encrypted state", purpose="state", aad=b"row-7")

    with pytest.raises(TruncatedCiphertextError):
        cipher.rewrap(envelope[:-1], purpose="state", aad=b"row-7")


@pytest.mark.parametrize(
    ("plaintext", "purpose", "aad"),
    [
        ("not bytes", "state", b"row"),
        (b"state", "", b"row"),
        (b"state", "state", "not bytes"),
    ],
)
def test_secretstream_rejects_invalid_inputs(plaintext: object, purpose: str, aad: object) -> None:
    cipher = _cipher()
    with pytest.raises(CryptoError):
        cipher.encrypt(plaintext, purpose=purpose, aad=aad)  # type: ignore[arg-type]


def test_secretstream_self_test_passes() -> None:
    _cipher(chunk_size=7).self_test()
