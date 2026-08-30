"""Bounded in-memory protocol for the dedicated evidence-unlocker process."""

from __future__ import annotations

import json
import re
import struct
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_UNLOCKER_REQUEST_BYTES = 64 * 1024
MAX_UNLOCKER_RESPONSE_BYTES = 512 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UNLOCKED_OUTPUTS = 64
MAX_CIPHERTEXT_BYTES = 256 * 1024 * 1024
_HEX_64 = re.compile(r"^[a-f0-9]{64}$")
_HEX_48 = re.compile(r"^[a-f0-9]{48}$")
_HEX_96 = re.compile(r"^[a-f0-9]{96}$")
_STORAGE_KEY = re.compile(r"^sha256/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{64}$")


class EvidenceUnlockerProtocolError(ValueError):
    """The local peer sent a malformed, oversized, or stale unlock message."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UnlockerSourceDescriptor(_FrozenModel):
    source_ref: UUID
    evidence_ref: UUID
    object_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    plaintext_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plaintext_size: int = Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)
    ciphertext_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ciphertext_size: int = Field(strict=True, ge=1, le=MAX_CIPHERTEXT_BYTES)
    storage_key: str = Field(min_length=77, max_length=77)
    chunk_size: int = Field(strict=True, ge=1, le=1_048_576)
    stream_header: str = Field(pattern=r"^[a-f0-9]{48}$")
    wrapped_key_generation: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    wrapped_key_nonce: str = Field(pattern=r"^[a-f0-9]{48}$")
    wrapped_key_ciphertext: str = Field(pattern=r"^[a-f0-9]{96}$")

    @model_validator(mode="after")
    def descriptor_is_bound(self) -> UnlockerSourceDescriptor:
        if self.source_ref.int == 0 or self.evidence_ref.int == 0:
            raise ValueError("unlocker source identity is invalid")
        if _HEX_64.fullmatch(self.object_ref) is None:
            raise ValueError("unlocker object reference is invalid")
        if _HEX_64.fullmatch(self.plaintext_sha256) is None:
            raise ValueError("unlocker plaintext digest is invalid")
        if _HEX_64.fullmatch(self.ciphertext_sha256) is None:
            raise ValueError("unlocker ciphertext digest is invalid")
        if _STORAGE_KEY.fullmatch(self.storage_key) is None:
            raise ValueError("unlocker storage key is invalid")
        digest = self.ciphertext_sha256
        if self.storage_key != f"sha256/{digest[:2]}/{digest[2:4]}/{digest}":
            raise ValueError("unlocker storage key does not match ciphertext")
        if _HEX_48.fullmatch(self.stream_header) is None:
            raise ValueError("unlocker stream header is invalid")
        if _HEX_48.fullmatch(self.wrapped_key_nonce) is None:
            raise ValueError("unlocker wrapped key nonce is invalid")
        if _HEX_96.fullmatch(self.wrapped_key_ciphertext) is None:
            raise ValueError("unlocker wrapped key ciphertext is invalid")
        return self


class UnlockerRequest(_FrozenModel):
    contract_version: Literal["ledgerbridge.evidence-unlocker.v1"] = (
        "ledgerbridge.evidence-unlocker.v1"
    )
    request_id: UUID
    operation_id: UUID
    request_nonce: UUID
    source: UnlockerSourceDescriptor
    password: str = Field(min_length=1, max_length=1024, repr=False)

    @model_validator(mode="after")
    def request_is_canonical(self) -> UnlockerRequest:
        if self.request_id.int == 0 or self.operation_id.int == 0 or self.request_nonce.int == 0:
            raise ValueError("unlocker request identity is invalid")
        if "\x00" in self.password:
            raise ValueError("unlocker password is invalid")
        return self


class UnlockerStatus(StrEnum):
    UNLOCKED = "UNLOCKED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class UnlockerOutputDescriptor(_FrozenModel):
    evidence_ref: UUID
    media_type: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    object_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    plaintext_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plaintext_size: int = Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)
    ciphertext_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ciphertext_size: int = Field(strict=True, ge=1, le=MAX_CIPHERTEXT_BYTES)
    storage_key: str = Field(min_length=77, max_length=77)
    chunk_size: int = Field(strict=True, ge=1, le=1_048_576)
    stream_header: str = Field(pattern=r"^[a-f0-9]{48}$")
    wrapped_key_generation: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    wrapped_key_nonce: str = Field(pattern=r"^[a-f0-9]{48}$")
    wrapped_key_ciphertext: str = Field(pattern=r"^[a-f0-9]{96}$")

    @model_validator(mode="after")
    def output_is_bound(self) -> UnlockerOutputDescriptor:
        if self.evidence_ref.int == 0:
            raise ValueError("unlocker output identity is invalid")
        if (
            self.display_name in {".", ".."}
            or "/" in self.display_name
            or "\\" in self.display_name
            or any(ord(char) < 32 or ord(char) == 127 for char in self.display_name)
        ):
            raise ValueError("unlocker output name is invalid")
        if "\r" in self.media_type or "\n" in self.media_type or "\x00" in self.media_type:
            raise ValueError("unlocker output media type is invalid")
        digest = self.ciphertext_sha256
        if self.storage_key != f"sha256/{digest[:2]}/{digest[2:4]}/{digest}":
            raise ValueError("unlocker output storage key does not match ciphertext")
        return self


class UnlockerResponse(_FrozenModel):
    contract_version: Literal["ledgerbridge.evidence-unlocker-result.v1"] = (
        "ledgerbridge.evidence-unlocker-result.v1"
    )
    request_id: UUID
    operation_id: UUID
    request_nonce: UUID
    source_ref: UUID
    status: UnlockerStatus
    outputs: tuple[UnlockerOutputDescriptor, ...] = Field(default=(), max_length=64)
    error_code: Literal["UNLOCK_REJECTED", "UNLOCKER_UNAVAILABLE"] | None = None

    @model_validator(mode="after")
    def response_shape_matches_status(self) -> UnlockerResponse:
        if any(
            value.int == 0
            for value in (
                self.request_id,
                self.operation_id,
                self.request_nonce,
                self.source_ref,
            )
        ):
            raise ValueError("unlocker response identity is invalid")
        if self.status == UnlockerStatus.UNLOCKED:
            if not self.outputs or self.error_code is not None:
                raise ValueError("successful unlocker response is incomplete")
        elif self.outputs or self.error_code is None:
            raise ValueError("failed unlocker response is incomplete")
        return self


def encode_unlocker_request(request: UnlockerRequest) -> bytes:
    if type(request) is not UnlockerRequest:
        raise EvidenceUnlockerProtocolError("unlocker request type is invalid")
    try:
        body = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise EvidenceUnlockerProtocolError("unlocker request is not bounded JSON") from None
    if len(body) > MAX_UNLOCKER_REQUEST_BYTES:
        raise EvidenceUnlockerProtocolError("unlocker request exceeds its byte limit")
    return struct.pack("!I", len(body)) + body


def decode_unlocker_request(frame: bytes) -> UnlockerRequest:
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise EvidenceUnlockerProtocolError("unlocker request frame is truncated")
    declared = struct.unpack("!I", frame[:4])[0]
    if declared > MAX_UNLOCKER_REQUEST_BYTES:
        raise EvidenceUnlockerProtocolError("unlocker request exceeds its byte limit")
    if len(frame) != declared + 4:
        raise EvidenceUnlockerProtocolError("unlocker request frame length is invalid")
    try:
        value = json.loads(frame[4:].decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict):
            raise ValueError("request is not an object")
        request = UnlockerRequest.model_validate(value)
        _require_canonical_uuid(value, "request_id")
        _require_canonical_uuid(value, "operation_id")
        _require_canonical_uuid(value, "request_nonce")
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValueError("source is not an object")
        _require_canonical_uuid(source, "source_ref")
        _require_canonical_uuid(source, "evidence_ref")
        return request
    except (UnicodeDecodeError, ValueError, TypeError, ValidationError):
        raise EvidenceUnlockerProtocolError("unlocker request is invalid") from None


def encode_unlocker_response(response: UnlockerResponse) -> bytes:
    if type(response) is not UnlockerResponse:
        raise EvidenceUnlockerProtocolError("unlocker response type is invalid")
    try:
        body = json.dumps(
            response.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise EvidenceUnlockerProtocolError("unlocker response is not bounded JSON") from None
    if len(body) > MAX_UNLOCKER_RESPONSE_BYTES:
        raise EvidenceUnlockerProtocolError("unlocker response exceeds its byte limit")
    return struct.pack("!I", len(body)) + body


def decode_unlocker_response(frame: bytes) -> UnlockerResponse:
    if not isinstance(frame, bytes) or len(frame) < 4:
        raise EvidenceUnlockerProtocolError("unlocker response frame is truncated")
    declared = struct.unpack("!I", frame[:4])[0]
    if declared > MAX_UNLOCKER_RESPONSE_BYTES:
        raise EvidenceUnlockerProtocolError("unlocker response exceeds its byte limit")
    if len(frame) != declared + 4:
        raise EvidenceUnlockerProtocolError("unlocker response frame length is invalid")
    try:
        value = json.loads(frame[4:].decode("utf-8"), object_pairs_hook=_closed_object)
        if not isinstance(value, dict):
            raise ValueError("response is not an object")
        response = UnlockerResponse.model_validate(value)
        for field in ("request_id", "operation_id", "request_nonce", "source_ref"):
            _require_canonical_uuid(value, field)
        outputs = value.get("outputs")
        if not isinstance(outputs, list) or len(outputs) > MAX_UNLOCKED_OUTPUTS:
            raise ValueError("outputs are invalid")
        for output in outputs:
            if not isinstance(output, dict):
                raise ValueError("output is not an object")
            _require_canonical_uuid(output, "evidence_ref")
        return response
    except (UnicodeDecodeError, ValueError, TypeError, ValidationError):
        raise EvidenceUnlockerProtocolError("unlocker response is invalid") from None


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _require_canonical_uuid(value: dict[str, object], field: str) -> None:
    raw = value.get(field)
    if not isinstance(raw, str):
        raise ValueError("UUID field is not text")
    parsed = UUID(raw)
    if str(parsed) != raw:
        raise ValueError("UUID field is not canonical")
