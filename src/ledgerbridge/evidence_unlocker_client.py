"""Synchronous, deadline-bound adapter for the local evidence-unlocker seam."""

from __future__ import annotations

import os
import socket
import stat
import struct
import time
from pathlib import Path
from typing import Protocol

from ledgerbridge.evidence_unlocker_protocol import (
    MAX_UNLOCKER_RESPONSE_BYTES,
    EvidenceUnlockerProtocolError,
    UnlockerRequest,
    UnlockerResponse,
    decode_unlocker_response,
    encode_unlocker_request,
)


class EvidenceUnlockerClientError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__("evidence unlocker request failed")
        self.error_code = error_code


class _SocketPeer(Protocol):
    def settimeout(self, value: float) -> None: ...

    def send(self, data: memoryview) -> int: ...

    def recv(self, size: int) -> bytes: ...


class EvidenceUnlockerClient:
    """Expose one deep ``process`` interface over the owned Unix-socket adapter."""

    def __init__(self, socket_path: str | Path, *, timeout_seconds: float = 30.0) -> None:
        resolved = Path(socket_path)
        if not resolved.is_absolute() or "\x00" in str(resolved):
            raise ValueError("evidence unlocker socket path must be absolute")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("evidence unlocker timeout is invalid")
        self._socket_path = resolved
        self._timeout_seconds = timeout_seconds

    def process(self, request: UnlockerRequest) -> UnlockerResponse:
        deadline = time.monotonic() + self._timeout_seconds
        request_frame = bytearray(encode_unlocker_request(request))
        try:
            with self._connect() as peer:
                peer.settimeout(self._timeout_seconds)
                self._send_all(peer, request_frame, deadline)
                header = self._recv_exact(peer, 4, deadline)
                declared = struct.unpack("!I", header)[0]
                if declared > MAX_UNLOCKER_RESPONSE_BYTES:
                    raise EvidenceUnlockerProtocolError("unlocker response exceeds its byte limit")
                body = self._recv_exact(peer, declared, deadline)
                response = decode_unlocker_response(header + body)
        except EvidenceUnlockerProtocolError:
            raise EvidenceUnlockerClientError("UNLOCKER_PROTOCOL") from None
        except (OSError, TimeoutError, ValueError, struct.error):
            raise EvidenceUnlockerClientError("UNLOCKER_UNAVAILABLE") from None
        finally:
            for index in range(len(request_frame)):
                request_frame[index] = 0
        if (
            response.request_id != request.request_id
            or response.operation_id != request.operation_id
            or response.request_nonce != request.request_nonce
            or response.source_ref != request.source.source_ref
        ):
            raise EvidenceUnlockerClientError("UNLOCKER_PROTOCOL")
        return response

    def _connect(self) -> socket.socket:
        self._validate_socket_path()
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise OSError("Unix sockets are unavailable")
        peer = socket.socket(family, socket.SOCK_STREAM)
        try:
            peer.connect(str(self._socket_path))
        except BaseException:
            peer.close()
            raise
        return peer

    def _validate_socket_path(self) -> None:
        if os.name == "nt":
            return
        try:
            metadata = self._socket_path.lstat()
            parent = self._socket_path.parent.lstat()
        except OSError as exc:
            raise OSError("evidence unlocker socket is unavailable") from exc
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            raise OSError("Unix peer identity is unavailable")
        expected_uid = int(geteuid())
        if (
            not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != expected_uid
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise OSError("evidence unlocker socket identity is invalid")

    @staticmethod
    def _send_all(peer: _SocketPeer, frame: bytearray, deadline: float) -> None:
        view = memoryview(frame)
        sent = 0
        while sent < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("unlocker send timed out")
            peer.settimeout(remaining)
            count = peer.send(view[sent:])
            if not isinstance(count, int) or count <= 0:
                raise OSError("unlocker socket closed during send")
            sent += count

    @staticmethod
    def _recv_exact(peer: _SocketPeer, size: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        observed = 0
        while observed < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("unlocker receive timed out")
            peer.settimeout(remaining)
            chunk = peer.recv(size - observed)
            if not isinstance(chunk, bytes) or not chunk:
                raise OSError("unlocker socket closed during receive")
            chunks.append(chunk)
            observed += len(chunk)
        return b"".join(chunks)
