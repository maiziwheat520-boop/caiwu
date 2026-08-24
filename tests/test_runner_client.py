from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from ledgerbridge.connectors import ArtifactMetadata
from ledgerbridge.runner_client import ConnectorRunnerClient, RunnerClientError
from ledgerbridge.runner_protocol import (
    FrameKind,
    RunnerOperation,
    RunnerRequest,
    encode_frame,
)


class _ClosedAfterSend:
    """A cross-platform socket-shaped peer that closes before responding."""

    def __enter__(self) -> _ClosedAfterSend:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def send(self, payload: memoryview[bytes]) -> int:
        return len(payload)

    def recv(self, _size: int) -> bytes:
        return b""


class _ResponsePeer(_ClosedAfterSend):
    def __init__(self, response: bytes) -> None:
        self._response = response

    def recv(self, size: int) -> bytes:
        chunk, self._response = self._response[:size], self._response[size:]
        return chunk


def _request(content: bytes = b"ok") -> RunnerRequest:
    digest = hashlib.sha256(content).hexdigest()
    metadata = ArtifactMetadata(
        source="manual_upload",
        original_filename="fixture.txt",
        media_type="text/plain",
        byte_size=len(content),
        sha256_hex=digest,
    )
    return RunnerRequest(
        request_id="00000000-0000-4000-8000-000000000001",
        operation=RunnerOperation.PARSE,
        connector_name="synthetic.csv",
        connector_version="1",
        source_system="synthetic",
        metadata=metadata,
        declared_artifact_size=len(content),
        verified_sha256_hex=digest,
    )


def test_runner_client_maps_truncated_capacity_close_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConnectorRunnerClient("unused", timeout_seconds=1)
    monkeypatch.setattr(client, "_connect", lambda: _ClosedAfterSend())

    with pytest.raises(RunnerClientError) as error:
        client.parse(_request(), BytesIO(b"ok"))

    assert error.value.error_code == "RUNNER_UNAVAILABLE"


def test_runner_client_keeps_nontruncated_protocol_failure_nonretryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConnectorRunnerClient("unused", timeout_seconds=1)
    malformed_response = encode_frame(FrameKind.CONTROL, b"not a terminal response")
    monkeypatch.setattr(client, "_connect", lambda: _ResponsePeer(malformed_response))

    with pytest.raises(RunnerClientError) as error:
        client.parse(_request(), BytesIO(b"ok"))

    assert error.value.error_code == "RUNNER_PROTOCOL"
