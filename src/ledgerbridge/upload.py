"""Pure, bounded multipart/form-data parsing for the future upload boundary.

This module deliberately knows nothing about FastAPI, SQLAlchemy, or the
artifact store.  It turns an iterable of request-body chunks into validated
metadata and file-byte events; a later route can feed those events directly to
the existing publication authority without creating a second storage path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

MAX_MULTIPART_BOUNDARY_BYTES = 70
MAX_MULTIPART_HEADER_BYTES = 16 * 1024
MAX_MULTIPART_FIELD_BYTES = 512
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_BODY_BYTES = (
    DEFAULT_MAX_FILE_BYTES + (2 * MAX_MULTIPART_HEADER_BYTES) + MAX_MULTIPART_FIELD_BYTES + 1024
)
_MEDIA_TYPE_PATTERN = re.compile(r"[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+\Z")
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+\Z")


class MultipartError(ValueError):
    """A malformed, duplicated, oversized, or unsupported multipart body."""


@dataclass(frozen=True, slots=True)
class MultipartField:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class MultipartFileStart:
    filename: str
    media_type: str


@dataclass(frozen=True, slots=True)
class MultipartFileChunk:
    data: bytes


@dataclass(frozen=True, slots=True)
class MultipartFileEnd:
    pass


MultipartEvent = MultipartField | MultipartFileStart | MultipartFileChunk | MultipartFileEnd


def parse_multipart(
    chunks: Iterable[bytes],
    content_type: str,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    declared_length: int | None = None,
) -> Iterator[MultipartEvent]:
    """Yield validated fields and file bytes from one bounded multipart body.

    Only one ``ingest_channel`` field and one ``file`` part are accepted.  The
    parser retains at most a boundary-sized suffix while streaming file data;
    it never buffers the file itself.
    """

    if max_file_bytes <= 0 or max_body_bytes <= 0 or max_body_bytes < max_file_bytes:
        raise ValueError("multipart limits must be positive and ordered")
    if declared_length is not None and (declared_length < 0 or declared_length > max_body_bytes):
        raise MultipartError("multipart body exceeds its configured limit")
    boundary = _parse_boundary(content_type)
    initial = b"--" + boundary + b"\r\n"
    delimiter = b"\r\n--" + boundary
    buffer = bytearray()
    total_bytes = 0
    file_bytes = 0
    field_seen = False
    file_seen = False
    state = "initial"
    current_name = ""
    field_buffer = bytearray()
    file_started = False

    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise MultipartError("multipart chunks must be bytes")
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > max_body_bytes:
            raise MultipartError("multipart body exceeds its configured limit")
        buffer.extend(chunk)

        while True:
            if state == "initial":
                if len(buffer) < len(initial):
                    if not initial.startswith(buffer):
                        raise MultipartError("multipart opening boundary is invalid")
                    break
                if bytes(buffer[: len(initial)]) != initial:
                    raise MultipartError("multipart opening boundary is invalid")
                del buffer[: len(initial)]
                state = "headers"
                continue

            if state == "headers":
                marker = buffer.find(b"\r\n\r\n")
                if marker < 0:
                    if len(buffer) > MAX_MULTIPART_HEADER_BYTES:
                        raise MultipartError("multipart headers exceed their limit")
                    break
                if marker > MAX_MULTIPART_HEADER_BYTES:
                    raise MultipartError("multipart headers exceed their limit")
                header_bytes = bytes(buffer[:marker])
                del buffer[: marker + 4]
                name, filename, media_type = _parse_part_headers(header_bytes)
                if name == "ingest_channel":
                    if filename is not None or field_seen:
                        raise MultipartError("multipart ingest_channel field is duplicated")
                    field_seen = True
                    current_name = name
                    field_buffer.clear()
                    state = "field"
                elif name == "file":
                    if not field_seen:
                        raise MultipartError("ingest_channel must precede file")
                    if filename is None or file_seen:
                        raise MultipartError(
                            "multipart file part is duplicated or missing filename"
                        )
                    file_seen = True
                    file_started = True
                    state = "file"
                    yield MultipartFileStart(filename=filename, media_type=media_type)
                else:
                    raise MultipartError("multipart contains an unsupported field")
                continue

            if state in {"file", "field"}:
                marker = buffer.find(delimiter)
                if marker < 0:
                    safe_length = max(0, len(buffer) - len(delimiter) + 1)
                    if state == "file":
                        data = bytes(buffer[:safe_length])
                        del buffer[:safe_length]
                        file_bytes = _emit_file_chunk(data, file_bytes, max_file_bytes)
                        if data:
                            yield MultipartFileChunk(data)
                    else:
                        data = bytes(buffer[:safe_length])
                        del buffer[:safe_length]
                        field_buffer.extend(data)
                        if len(field_buffer) > MAX_MULTIPART_FIELD_BYTES:
                            raise MultipartError("multipart field exceeds its limit")
                    break

                data = bytes(buffer[:marker])
                del buffer[: marker + len(delimiter)]
                if state == "file":
                    file_bytes = _emit_file_chunk(data, file_bytes, max_file_bytes)
                    if data:
                        yield MultipartFileChunk(data)
                    yield MultipartFileEnd()
                else:
                    field_buffer.extend(data)
                    if len(field_buffer) > MAX_MULTIPART_FIELD_BYTES:
                        raise MultipartError("multipart field exceeds its limit")
                    yield MultipartField(current_name, _decode_field(bytes(field_buffer)))
                    field_buffer.clear()
                state = "boundary"
                continue

            if state == "boundary":
                if len(buffer) < 2:
                    break
                if bytes(buffer[:2]) == b"--":
                    del buffer[:2]
                    state = "final"
                elif bytes(buffer[:2]) == b"\r\n":
                    del buffer[:2]
                    state = "headers"
                else:
                    raise MultipartError("multipart boundary terminator is invalid")
                continue

            if state == "final":
                if buffer.startswith(b"\r\n"):
                    del buffer[:2]
                if buffer:
                    raise MultipartError("multipart has trailing bytes")
                break

            raise MultipartError("multipart parser entered an invalid state")

    if state == "final" and not buffer and field_seen and file_seen and file_started:
        return
    raise MultipartError("multipart body is truncated")


def _emit_file_chunk(data: bytes, current: int, maximum: int) -> int:
    next_size = current + len(data)
    if next_size > maximum:
        raise MultipartError("multipart file exceeds its configured limit")
    return next_size


def _parse_boundary(content_type: str) -> bytes:
    if not isinstance(content_type, str) or "\r" in content_type or "\n" in content_type:
        raise MultipartError("multipart content type is invalid")
    pieces = [piece.strip() for piece in content_type.split(";")]
    if not pieces or pieces[0].lower() != "multipart/form-data":
        raise MultipartError("multipart content type is required")
    boundary_value: str | None = None
    for piece in pieces[1:]:
        if not piece:
            continue
        if "=" not in piece:
            raise MultipartError("multipart content type parameter is invalid")
        name, value = (part.strip() for part in piece.split("=", 1))
        if name.lower() != "boundary" or boundary_value is not None:
            if name.lower() == "boundary":
                raise MultipartError("multipart boundary is duplicated")
            raise MultipartError("multipart content type parameter is unsupported")
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        if not value or len(value.encode("ascii", errors="ignore")) != len(value):
            raise MultipartError("multipart boundary is invalid")
        boundary_value = value
    if boundary_value is None or len(boundary_value) > MAX_MULTIPART_BOUNDARY_BYTES:
        raise MultipartError("multipart boundary is missing or too long")
    if _TOKEN_PATTERN.fullmatch(boundary_value) is None:
        raise MultipartError("multipart boundary is invalid")
    return boundary_value.encode("ascii")


def _parse_part_headers(header_bytes: bytes) -> tuple[str, str | None, str]:
    try:
        text = header_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultipartError("multipart headers are not valid UTF-8") from exc
    headers: dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" not in line:
            raise MultipartError("multipart header is invalid")
        name, value = (part.strip() for part in line.split(":", 1))
        normalized = name.lower()
        if not normalized or normalized in headers:
            raise MultipartError("multipart header is duplicated")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise MultipartError("multipart header contains control text")
        headers[normalized] = value
    disposition = headers.get("content-disposition")
    if disposition is None:
        raise MultipartError("multipart content disposition is required")
    disposition_parts = [part.strip() for part in disposition.split(";")]
    if not disposition_parts or disposition_parts[0].lower() != "form-data":
        raise MultipartError("multipart content disposition is invalid")
    params: dict[str, str] = {}
    for part in disposition_parts[1:]:
        if "=" not in part:
            raise MultipartError("multipart content disposition parameter is invalid")
        key, value = (piece.strip() for piece in part.split("=", 1))
        key = key.lower()
        if key in params or key not in {"name", "filename"}:
            raise MultipartError("multipart content disposition parameter is invalid")
        params[key] = _unquote(value)
    part_name = params.get("name")
    if part_name not in {"file", "ingest_channel"}:
        raise MultipartError("multipart field name is unsupported")
    filename = params.get("filename")
    if filename is not None:
        if not filename or "/" in filename or "\\" in filename:
            raise MultipartError("multipart filename is invalid")
        if len(filename) > 512:
            raise MultipartError("multipart filename exceeds its limit")
    raw_media_type = headers.get("content-type", "application/octet-stream")
    media_type = raw_media_type.split(";", 1)[0].strip()
    if part_name == "file" and _MEDIA_TYPE_PATTERN.fullmatch(media_type) is None:
        raise MultipartError("multipart file media type is invalid")
    return part_name, filename, media_type


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
        value = re.sub(r"\\(.)", r"\1", value)
    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise MultipartError("multipart header parameter is invalid")
    return value


def _decode_field(value: bytes) -> str:
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MultipartError("multipart field is not valid UTF-8") from exc
    if not decoded or "\r" in decoded or "\n" in decoded or "\x00" in decoded:
        raise MultipartError("multipart field contains invalid text")
    return decoded
