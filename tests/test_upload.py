from __future__ import annotations

from collections.abc import Iterable

import pytest

from ledgerbridge.upload import (
    MultipartError,
    MultipartField,
    MultipartFileChunk,
    MultipartFileEnd,
    MultipartFileStart,
    parse_multipart,
)


def _body(boundary: str, *, filename: str = "statement.csv", media_type: str = "text/csv") -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=ingest_channel\r\n\r\n"
            "manual_upload\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode()
        + b"row\r\n--not-the-boundary\r\n"
        + f"--{boundary}--\r\n".encode()
    )


def _chunks(value: bytes, sizes: Iterable[int]) -> list[bytes]:
    result: list[bytes] = []
    offset = 0
    for size in sizes:
        if offset >= len(value):
            break
        result.append(value[offset : offset + size])
        offset += size
    if offset < len(value):
        result.append(value[offset:])
    return result


def test_parser_streams_file_and_handles_fragmented_boundaries() -> None:
    boundary = "LedgerBridgeBoundary"
    body = _body(boundary)
    events = list(
        parse_multipart(
            _chunks(body, [1] * 11 + [2, 3, 5, 8, 13, 21]),
            f"multipart/form-data; boundary={boundary}",
        )
    )
    assert events[0] == MultipartField("ingest_channel", "manual_upload")
    assert events[1] == MultipartFileStart("statement.csv", "text/csv")
    file_chunks = [event.data for event in events if isinstance(event, MultipartFileChunk)]
    assert b"".join(file_chunks) == b"row\r\n--not-the-boundary"
    assert isinstance(events[-1], MultipartFileEnd)


def test_parser_rejects_duplicate_or_unknown_fields() -> None:
    boundary = "b"
    prefix = f"--{boundary}\r\nContent-Disposition: form-data; name=x\r\n\r\n".encode()
    with pytest.raises(MultipartError, match="unsupported"):
        list(
            parse_multipart(
                [prefix + f"--{boundary}--\r\n".encode()],
                f"multipart/form-data; boundary={boundary}",
            )
        )


def test_parser_requires_ingest_channel_before_file_bytes() -> None:
    boundary = "b"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "data\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    with pytest.raises(MultipartError, match="precede"):
        list(parse_multipart([body], f"multipart/form-data; boundary={boundary}"))


def test_parser_rejects_file_overflow_before_emitting_end() -> None:
    boundary = "b"
    body = _body(boundary).replace(b"row\r\n--not-the-boundary", b"x" * 40)
    with pytest.raises(MultipartError, match="file exceeds"):
        list(
            parse_multipart([body], f"multipart/form-data; boundary={boundary}", max_file_bytes=32)
        )


def test_parser_rejects_declared_body_overflow_before_reading() -> None:
    with pytest.raises(MultipartError, match="exceeds"):
        list(
            parse_multipart(
                [],
                "multipart/form-data; boundary=b",
                max_body_bytes=10,
                max_file_bytes=5,
                declared_length=11,
            )
        )


@pytest.mark.parametrize(
    "content_type",
    [
        "text/plain",
        "multipart/form-data",
        "multipart/form-data; boundary=bad space",
        "multipart/form-data; boundary=b; boundary=c",
    ],
)
def test_parser_rejects_invalid_content_type(content_type: str) -> None:
    with pytest.raises(MultipartError):
        list(parse_multipart([], content_type))
