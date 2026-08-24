from __future__ import annotations

from collections.abc import Iterable

import pytest

from ledgerbridge.upload import (
    MultipartComplete,
    MultipartError,
    MultipartField,
    MultipartFileChunk,
    MultipartFileEnd,
    MultipartFileStart,
    _decode_field,
    _parse_boundary,
    _parse_part_headers,
    _unquote,
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


def _part(boundary: str, headers: str, body: bytes) -> bytes:
    return f"--{boundary}\r\n{headers}\r\n\r\n".encode() + body + b"\r\n"


def _channel(boundary: str, value: bytes = b"manual_upload") -> bytes:
    return _part(boundary, "Content-Disposition: form-data; name=ingest_channel", value)


def _file(boundary: str, body: bytes = b"data", headers: str | None = None) -> bytes:
    return _part(
        boundary,
        headers
        or 'Content-Disposition: form-data; name="file"; filename="x.txt"\r\n'
        "Content-Type: text/plain",
        body,
    )


def _finish(boundary: str) -> bytes:
    return f"--{boundary}--\r\n".encode()


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
    assert isinstance(events[-2], MultipartFileEnd)
    assert isinstance(events[-1], MultipartComplete)


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


def test_parser_rejects_invalid_limits_chunk_types_and_body_size() -> None:
    with pytest.raises(ValueError, match="limits"):
        list(parse_multipart([], "multipart/form-data; boundary=b", max_file_bytes=0))
    with pytest.raises(ValueError, match="limits"):
        list(
            parse_multipart(
                [], "multipart/form-data; boundary=b", max_file_bytes=8, max_body_bytes=7
            )
        )
    with pytest.raises(MultipartError, match="chunks"):
        list(parse_multipart([b"", "not-bytes"], "multipart/form-data; boundary=b"))  # type: ignore[list-item]
    with pytest.raises(MultipartError, match="exceeds"):
        list(
            parse_multipart(
                [b"--b\r\n" + b"x" * 20],
                "multipart/form-data; boundary=b",
                max_file_bytes=10,
                max_body_bytes=10,
            )
        )
    with pytest.raises(MultipartError, match="exceeds"):
        list(parse_multipart([], "multipart/form-data; boundary=b", declared_length=-1))


@pytest.mark.parametrize("body", [b"x", b"x" * 12])
def test_parser_rejects_invalid_opening_boundary(body: bytes) -> None:
    with pytest.raises(MultipartError, match="opening boundary"):
        list(parse_multipart([body], "multipart/form-data; boundary=b"))


def test_parser_rejects_oversized_headers_in_both_streaming_forms() -> None:
    initial = b"--b\r\n"
    with pytest.raises(MultipartError, match="headers"):
        list(
            parse_multipart(
                [initial + b"x" * (16 * 1024 + 1)],
                "multipart/form-data; boundary=b",
            )
        )
    with pytest.raises(MultipartError, match="headers"):
        list(
            parse_multipart(
                [initial + b"x" * (16 * 1024 + 1) + b"\r\n\r\n"],
                "multipart/form-data; boundary=b",
            )
        )


def test_parser_rejects_duplicate_channel_and_file_without_filename() -> None:
    boundary = "b"
    duplicate_channel = (
        _channel(boundary) + _channel(boundary) + _file(boundary) + _finish(boundary)
    )
    with pytest.raises(MultipartError, match="duplicated"):
        list(parse_multipart([duplicate_channel], f"multipart/form-data; boundary={boundary}"))

    missing_filename = (
        _channel(boundary)
        + _file(
            boundary,
            headers="Content-Disposition: form-data; name=file\r\nContent-Type: text/plain",
        )
        + _finish(boundary)
    )
    with pytest.raises(MultipartError, match="missing filename"):
        list(parse_multipart([missing_filename], f"multipart/form-data; boundary={boundary}"))


def test_parser_streams_data_before_delimiter_and_limits_field() -> None:
    boundary = "b"
    body = _channel(boundary, b"channel") + _file(boundary, b"abcdef") + _finish(boundary)
    file_start = body.rindex(b"abcdef")
    events = list(
        parse_multipart(
            [body[: file_start + 6], body[file_start + 6 :]],
            f"multipart/form-data; boundary={boundary}",
        )
    )
    assert any(isinstance(event, MultipartFileChunk) for event in events)

    field_prefix = _channel(boundary, b"x" * 520)
    with pytest.raises(MultipartError, match="field exceeds"):
        list(parse_multipart([field_prefix], f"multipart/form-data; boundary={boundary}"))


def test_parser_rejects_field_overflow_after_delimiter_and_truncated_body() -> None:
    boundary = "b"
    oversized = _channel(boundary, b"x" * 513) + _finish(boundary)
    with pytest.raises(MultipartError, match="field exceeds"):
        list(parse_multipart([oversized], f"multipart/form-data; boundary={boundary}"))

    truncated = (
        _channel(boundary) + b"--b\r\nContent-Disposition: form-data; name=file; filename=x\r\n"
    )
    with pytest.raises(MultipartError, match="truncated"):
        list(parse_multipart([truncated], f"multipart/form-data; boundary={boundary}"))


def test_parser_validates_boundary_suffix_and_trailing_bytes() -> None:
    boundary = "b"
    valid = _channel(boundary) + _file(boundary) + _finish(boundary)
    delimiter = b"\r\n--b"
    split_at = valid.index(delimiter) + len(delimiter) + 1
    events = list(
        parse_multipart(
            [valid[:split_at], valid[split_at:]], f"multipart/form-data; boundary={boundary}"
        )
    )
    assert isinstance(events[-2], MultipartFileEnd)
    assert isinstance(events[-1], MultipartComplete)

    invalid_suffix = _channel(boundary) + _file(boundary) + b"--bXY"
    with pytest.raises(MultipartError, match="terminator"):
        list(parse_multipart([invalid_suffix], f"multipart/form-data; boundary={boundary}"))
    with pytest.raises(MultipartError, match="trailing"):
        list(parse_multipart([valid + b"junk"], f"multipart/form-data; boundary={boundary}"))


@pytest.mark.parametrize(
    "content_type",
    [
        "multipart/form-data\n; boundary=b",
        "multipart/form-data; boundary",
        "multipart/form-data; charset=utf-8; boundary=b",
        "multipart/form-data; boundary=é",
        "multipart/form-data; boundary=" + "b" * 71,
        "multipart/form-data; boundary=bad,",
    ],
)
def test_parse_boundary_rejects_malformed_parameters(content_type: str) -> None:
    with pytest.raises(MultipartError):
        _parse_boundary(content_type)


def test_parse_boundary_accepts_quoted_value_and_empty_parameter() -> None:
    assert _parse_boundary('multipart/form-data; ; boundary="b"') == b"b"


@pytest.mark.parametrize(
    "headers",
    [
        b"Content-Disposition: form-data; name=file\xff",
        b"Content-Disposition: form-data; name=file\r\nBroken",
        b"Content-Disposition: form-data; name=file\r\nX: one\r\nX: two",
        b"Content-Disposition: form-data; name=file\r\nX: bad\x00value",
        b"X: value",
        b"Content-Disposition: attachment; name=file",
        b"Content-Disposition: form-data; name=file; filename",
        b"Content-Disposition: form-data; name=file; name=other",
        b"Content-Disposition: form-data; name=unknown",
        b"Content-Disposition: form-data; name=file; filename=../x",
        b"Content-Disposition: form-data; name=file; filename=" + b"x" * 513,
        b"Content-Disposition: form-data; name=file\r\nContent-Type: invalid type",
    ],
)
def test_parse_part_headers_rejects_malformed_headers(headers: bytes) -> None:
    with pytest.raises(MultipartError):
        _parse_part_headers(headers)


def test_parse_part_headers_accepts_default_media_type_and_quoted_filename() -> None:
    name, filename, media_type = _parse_part_headers(
        b'Content-Disposition: form-data; name="file"; filename="a\\"b.txt"'
    )
    assert (name, filename, media_type) == ("file", 'a"b.txt', "application/octet-stream")


@pytest.mark.parametrize("value", ["", "\x00", "bad\rvalue"])
def test_unquote_rejects_invalid_values(value: str) -> None:
    with pytest.raises(MultipartError):
        _unquote(value)


@pytest.mark.parametrize("value", [b"\xff", b"bad\x00value", b""])
def test_decode_field_rejects_invalid_text(value: bytes) -> None:
    with pytest.raises(MultipartError):
        _decode_field(value)


@pytest.mark.parametrize("value", [b"\xff", b"bad\x00value", b""])
def test_parser_rejects_invalid_channel_text(value: bytes) -> None:
    boundary = "b"
    body = _channel(boundary, value) + _file(boundary) + _finish(boundary)
    with pytest.raises(MultipartError):
        list(parse_multipart([body], f"multipart/form-data; boundary={boundary}"))
