"""Encrypted transient spool with an in-memory, per-instance key.

The ciphertext file is authenticated before any caller can read plaintext.  The
ephemeral key is intentionally not recoverable after process loss, so crash-left
spools are ciphertext-only garbage rather than retry state.
"""

from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterator
from typing import BinaryIO, cast

from nacl import bindings
from nacl.exceptions import CryptoError

_MAGIC = b"LBSPv1\x00\x00"
_AAD = b"ledgerbridge.secure-spool.v1"
_FRAME_LENGTH = struct.Struct(">I")
_MAX_FRAME_PLAINTEXT = 1024 * 1024
_MAX_FRAME_CIPHERTEXT = _MAX_FRAME_PLAINTEXT + bindings.crypto_secretstream_xchacha20poly1305_ABYTES


class SecureSpoolError(RuntimeError):
    """Transient ciphertext is malformed, unauthenticated, or misused."""


class EncryptedSpool:
    """Write-only-then-read-only secretstream spool.

    Plaintext exists only in bounded caller chunks and the reader's memory
    buffer.  The backing temporary file contains only an authenticated header,
    frame lengths, and ciphertext.
    """

    def __init__(self) -> None:
        self._file = cast(
            BinaryIO,
            tempfile.TemporaryFile(  # noqa: SIM115 - owned across call boundaries
                mode="w+b"
            ),
        )
        self._key = bytearray(bindings.crypto_secretstream_xchacha20poly1305_keygen())
        self._push_state = bindings.crypto_secretstream_xchacha20poly1305_state()
        header = bindings.crypto_secretstream_xchacha20poly1305_init_push(
            self._push_state, bytes(self._key)
        )
        self._file.write(_MAGIC)
        self._file.write(header)
        self._sealed = False
        self._closed = False
        self._reader: Iterator[bytes] | None = None
        self._read_buffer = bytearray()
        self._read_offset = 0
        self._write_buffer = bytearray()
        self._plaintext_size = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def plaintext_size(self) -> int:
        return self._plaintext_size

    def write(self, data: bytes) -> int:
        self._require_open()
        if self._sealed:
            raise SecureSpoolError("encrypted spool is sealed")
        if not isinstance(data, bytes):
            raise TypeError("encrypted spool accepts bytes only")
        if not data:
            return 0
        offset = 0
        while offset < len(data):
            take = min(_MAX_FRAME_PLAINTEXT - len(self._write_buffer), len(data) - offset)
            self._write_buffer.extend(data[offset : offset + take])
            offset += take
            if len(self._write_buffer) == _MAX_FRAME_PLAINTEXT:
                self._write_frame(
                    bytes(self._write_buffer),
                    bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE,
                )
                self._write_buffer.clear()
        self._plaintext_size += len(data)
        return len(data)

    def seal(self) -> None:
        self._require_open()
        if self._sealed:
            return
        try:
            if self._write_buffer:
                self._write_frame(
                    bytes(self._write_buffer),
                    bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE,
                )
            self._write_frame(
                b"",
                bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL,
            )
            self._write_buffer.clear()
            self._file.flush()
            os.fsync(self._file.fileno())
            verified_size = sum(len(chunk) for chunk in self._iter_decrypted())
            if verified_size != self._plaintext_size:
                raise SecureSpoolError("encrypted spool plaintext length mismatch")
        except BaseException:
            # A failed verification permanently poisons the spool.  In
            # particular, callers must never be able to read an authenticated
            # prefix after a later frame fails authentication.
            self.close()
            raise
        self._sealed = True
        self.seek(0)

    def read(self, size: int = -1) -> bytes:
        self._require_open()
        if not self._sealed:
            raise SecureSpoolError("encrypted spool must be sealed before reading")
        if size == 0:
            return b""
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        if size == -1:
            result = bytearray(self._remaining_buffer())
            for chunk in self._require_reader():
                result.extend(chunk)
            self._read_buffer.clear()
            self._read_offset = 0
            return bytes(result)

        while len(self._read_buffer) - self._read_offset < size:
            try:
                self._read_buffer.extend(next(self._require_reader()))
            except StopIteration:
                break
        available = min(size, len(self._read_buffer) - self._read_offset)
        start = self._read_offset
        self._read_offset += available
        result_bytes = bytes(self._read_buffer[start : start + available])
        if self._read_offset == len(self._read_buffer):
            self._read_buffer.clear()
            self._read_offset = 0
        return result_bytes

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._require_open()
        if not self._sealed:
            raise SecureSpoolError("encrypted spool must be sealed before seeking")
        if offset != 0 or whence != os.SEEK_SET:
            raise OSError("encrypted spool supports only seek(0)")
        self._reader = self._iter_decrypted()
        self._read_buffer.clear()
        self._read_offset = 0
        return 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.close()
        finally:
            for index in range(len(self._key)):
                self._key[index] = 0
            self._write_buffer.clear()
            self._read_buffer.clear()
            self._reader = None

    def ciphertext_for_test(self) -> bytes:
        """Return ciphertext for leakage/tamper tests; never a production export API."""

        self._require_open()
        position = self._file.tell()
        self._file.seek(0)
        try:
            return self._file.read()
        finally:
            self._file.seek(position)

    def __enter__(self) -> EncryptedSpool:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def _iter_decrypted(self) -> Iterator[bytes]:
        self._file.seek(0)
        magic = self._read_exact(len(_MAGIC))
        if magic != _MAGIC:
            raise SecureSpoolError("encrypted spool format is invalid")
        header = self._read_exact(bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES)
        state = bindings.crypto_secretstream_xchacha20poly1305_state()
        try:
            bindings.crypto_secretstream_xchacha20poly1305_init_pull(
                state, header, bytes(self._key)
            )
        except (CryptoError, ValueError) as exc:
            raise SecureSpoolError("encrypted spool header authentication failed") from exc

        final_seen = False
        while not final_seen:
            encoded_length = self._file.read(_FRAME_LENGTH.size)
            if not encoded_length:
                raise SecureSpoolError("encrypted spool is truncated before FINAL")
            if len(encoded_length) != _FRAME_LENGTH.size:
                raise SecureSpoolError("encrypted spool frame length is truncated")
            (ciphertext_length,) = _FRAME_LENGTH.unpack(encoded_length)
            if not (
                bindings.crypto_secretstream_xchacha20poly1305_ABYTES
                <= ciphertext_length
                <= _MAX_FRAME_CIPHERTEXT
            ):
                raise SecureSpoolError("encrypted spool frame length is invalid")
            ciphertext = self._read_exact(ciphertext_length)
            try:
                plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(
                    state, ciphertext, ad=_AAD
                )
            except CryptoError as exc:
                raise SecureSpoolError("encrypted spool frame authentication failed") from exc
            if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
                if plaintext:
                    raise SecureSpoolError("encrypted spool FINAL frame must be empty")
                final_seen = True
            elif tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE:
                raise SecureSpoolError("encrypted spool frame tag is invalid")
            else:
                yield plaintext
        if self._file.read(1):
            raise SecureSpoolError("encrypted spool has trailing ciphertext")

    def _read_exact(self, size: int) -> bytes:
        value = self._file.read(size)
        if len(value) != size:
            raise SecureSpoolError("encrypted spool ciphertext is truncated")
        return value

    def _write_frame(self, plaintext: bytes, tag: int) -> None:
        ciphertext = bindings.crypto_secretstream_xchacha20poly1305_push(
            self._push_state,
            plaintext,
            ad=_AAD,
            tag=tag,
        )
        self._file.write(_FRAME_LENGTH.pack(len(ciphertext)))
        self._file.write(ciphertext)

    def _remaining_buffer(self) -> bytes:
        value = bytes(self._read_buffer[self._read_offset :])
        self._read_buffer.clear()
        self._read_offset = 0
        return value

    def _require_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed encrypted spool")

    def _require_reader(self) -> Iterator[bytes]:
        if self._reader is None:
            raise SecureSpoolError("encrypted spool reader is unavailable")
        return self._reader
