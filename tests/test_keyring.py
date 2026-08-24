from __future__ import annotations

import pytest

from ledgerbridge.keyring import (
    KeyProviderError,
    KeyUnwrapError,
    SyntheticKeyProvider,
    UnknownKeyGenerationError,
    WrappedKey,
)

OLD_KEY = b"\x11" * 32
ACTIVE_KEY = b"\x22" * 32
DEK = b"\x33" * 32


def test_synthetic_provider_wraps_unwraps_and_binds_context() -> None:
    provider = SyntheticKeyProvider({"gen-1": OLD_KEY}, active_generation="gen-1")

    wrapped = provider.wrap_key(DEK, purpose="evidence", aad=b"object-123")

    assert wrapped.generation == "gen-1"
    assert wrapped.ciphertext != DEK
    assert provider.unwrap_key(wrapped, purpose="evidence", aad=b"object-123") == DEK
    with pytest.raises(KeyUnwrapError, match="authentication"):
        provider.unwrap_key(wrapped, purpose="candidate", aad=b"object-123")
    with pytest.raises(KeyUnwrapError, match="authentication"):
        provider.unwrap_key(wrapped, purpose="evidence", aad=b"object-124")


def test_synthetic_provider_reads_old_generation_and_rewraps_to_active() -> None:
    old_provider = SyntheticKeyProvider({"gen-1": OLD_KEY}, active_generation="gen-1")
    old_wrapped = old_provider.wrap_key(DEK, purpose="state", aad=b"row-7")
    rotated_provider = SyntheticKeyProvider(
        {"gen-1": OLD_KEY, "gen-2": ACTIVE_KEY},
        active_generation="gen-2",
    )

    new_wrapped = rotated_provider.rewrap_key(old_wrapped, purpose="state", aad=b"row-7")

    assert new_wrapped.generation == "gen-2"
    assert new_wrapped != old_wrapped
    assert rotated_provider.unwrap_key(new_wrapped, purpose="state", aad=b"row-7") == DEK


def test_synthetic_provider_rewrap_is_idempotent_for_active_generation() -> None:
    provider = SyntheticKeyProvider({"gen-1": OLD_KEY}, active_generation="gen-1")
    wrapped = provider.wrap_key(DEK, purpose="state", aad=b"row-7")

    assert provider.rewrap_key(wrapped, purpose="state", aad=b"row-7") is wrapped


def test_synthetic_provider_rejects_unknown_generation_and_tamper() -> None:
    provider = SyntheticKeyProvider({"gen-1": OLD_KEY}, active_generation="gen-1")
    wrapped = provider.wrap_key(DEK, purpose="state", aad=b"row-7")
    unknown = WrappedKey(generation="gen-9", nonce=wrapped.nonce, ciphertext=wrapped.ciphertext)
    tampered = WrappedKey(
        generation=wrapped.generation,
        nonce=wrapped.nonce,
        ciphertext=wrapped.ciphertext[:-1] + bytes((wrapped.ciphertext[-1] ^ 1,)),
    )

    with pytest.raises(UnknownKeyGenerationError, match="unavailable"):
        provider.unwrap_key(unknown, purpose="state", aad=b"row-7")
    with pytest.raises(KeyUnwrapError, match="authentication"):
        provider.unwrap_key(tampered, purpose="state", aad=b"row-7")


def test_synthetic_provider_authenticates_generation_even_if_keys_match() -> None:
    provider = SyntheticKeyProvider(
        {"gen-1": OLD_KEY, "gen-2": OLD_KEY},
        active_generation="gen-1",
    )
    wrapped = provider.wrap_key(DEK, purpose="state", aad=b"row-7")
    renamed = WrappedKey(
        generation="gen-2",
        nonce=wrapped.nonce,
        ciphertext=wrapped.ciphertext,
    )

    with pytest.raises(KeyUnwrapError, match="authentication"):
        provider.unwrap_key(renamed, purpose="state", aad=b"row-7")


@pytest.mark.parametrize(
    ("generations", "active_generation"),
    [
        ({}, "gen-1"),
        ({"gen-1": b"short"}, "gen-1"),
        ({"bad generation": OLD_KEY}, "bad generation"),
        ({"gen-1": OLD_KEY}, "gen-2"),
    ],
)
def test_synthetic_provider_rejects_invalid_configuration(
    generations: dict[str, bytes], active_generation: str
) -> None:
    with pytest.raises(KeyProviderError):
        SyntheticKeyProvider(generations, active_generation=active_generation)


def test_wrapped_key_rejects_malformed_framing() -> None:
    with pytest.raises(KeyProviderError, match="nonce"):
        WrappedKey(generation="gen-1", nonce=b"short", ciphertext=b"x" * 48)
    with pytest.raises(KeyProviderError, match="ciphertext"):
        WrappedKey(generation="gen-1", nonce=b"x" * 24, ciphertext=b"short")


def test_synthetic_provider_self_test_passes_for_active_and_old_generations() -> None:
    provider = SyntheticKeyProvider(
        {"gen-1": OLD_KEY, "gen-2": ACTIVE_KEY},
        active_generation="gen-2",
    )

    provider.self_test()
