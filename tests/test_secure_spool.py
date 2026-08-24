from __future__ import annotations

import os

import pytest

from ledgerbridge.secure_spool import EncryptedSpool, SecureSpoolError


def test_spool_persists_ciphertext_only_and_round_trips() -> None:
    marker = b"S1-CANARY-never-persist-plaintext"
    with EncryptedSpool() as spool:
        assert spool.write(marker[:8]) == 8
        assert spool.write(marker[8:]) == len(marker) - 8
        assert marker not in spool.ciphertext_for_test()
        spool.seal()
        assert marker not in spool.ciphertext_for_test()
        assert spool.read(5) + spool.read() == marker
        assert spool.seek(0) == 0
        assert spool.read() == marker


def test_empty_spool_has_authenticated_final_frame() -> None:
    with EncryptedSpool() as spool:
        spool.seal()
        assert spool.plaintext_size == 0
        assert spool.read() == b""


def test_spool_rejects_tamper_truncation_and_trailing_bytes() -> None:
    for mutation in ("tamper", "truncate", "append"):
        spool = EncryptedSpool()
        spool.write(b"authenticated bytes")
        spool.seal()
        raw = bytearray(spool.ciphertext_for_test())
        if mutation == "tamper":
            raw[-1] ^= 1
        elif mutation == "truncate":
            raw.pop()
        else:
            raw.extend(b"x")
        spool._file.seek(0)
        spool._file.truncate(0)
        spool._file.write(raw)
        spool._file.flush()
        with pytest.raises(SecureSpoolError):
            spool.seek(0)
            spool.read()
        spool.close()


def test_seal_failure_poison_closes_spool_before_any_prefix_can_be_read() -> None:
    spool = EncryptedSpool()
    # Persist two complete frames, then corrupt the later one.  The first frame
    # authenticates successfully during seal, but no prefix may become readable.
    spool.write(b"A" * (2 * 1024 * 1024) + b"tail")
    raw = bytearray(spool.ciphertext_for_test())
    raw[-1] ^= 1
    spool._file.seek(0)
    spool._file.truncate(0)
    spool._file.write(raw)
    spool._file.seek(0, os.SEEK_END)

    with pytest.raises(SecureSpoolError, match="authentication"):
        spool.seal()
    assert spool.closed
    with pytest.raises(ValueError, match="closed"):
        spool.read(1)


def test_spool_coalesces_many_small_writes_into_bounded_frames() -> None:
    marker = b"S1-small-write-canary"
    with EncryptedSpool() as spool:
        for byte in marker * 10_000:
            spool.write(bytes((byte,)))
        spool.seal()
        ciphertext = spool.ciphertext_for_test()
        assert marker not in ciphertext
        assert len(ciphertext) < spool.plaintext_size + 256
        assert spool.read() == marker * 10_000


def test_spool_state_and_seek_are_fail_closed() -> None:
    spool = EncryptedSpool()
    with pytest.raises(SecureSpoolError, match="sealed"):
        spool.read()
    spool.write(b"x")
    spool.seal()
    with pytest.raises(SecureSpoolError, match="sealed"):
        spool.write(b"late")
    with pytest.raises(OSError, match=r"seek\(0\)"):
        spool.seek(1, os.SEEK_SET)
    spool.close()
    spool.close()
    with pytest.raises(ValueError, match="closed"):
        spool.read()
