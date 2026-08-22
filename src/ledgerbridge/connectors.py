"""Typed, side-effect-free connector boundary for synthetic Phase 2 imports."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

MAX_JSON_BYTES = 1_000_000
MAX_JSON_DEPTH = 64
MIN_MINOR_UNITS = -(2**63)
MAX_MINOR_UNITS = 2**63 - 1
CANONICAL_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ConnectorContractError(ValueError):
    """A connector returned a value outside the frozen SDK contract."""


class DetectionResult(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    source: str
    original_filename: str
    media_type: str
    byte_size: int
    sha256_hex: str


class ReadableBinary(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ParsedSourceRecord:
    record_locator: str
    source: str
    parser_version: str
    raw_fields: Mapping[str, object]
    normalized_fields: Mapping[str, object]
    external_transaction_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("record_locator", self.record_locator, 500)
        _require_canonical_source("source", self.source)
        _require_text("parser_version", self.parser_version, 100)
        if self.external_transaction_id is not None:
            _require_text("external_transaction_id", self.external_transaction_id, 300)
        _validate_json_object("raw_fields", self.raw_fields, reject_floats=False)
        _validate_json_object("normalized_fields", self.normalized_fields, reject_floats=True)
        _validate_money(self.normalized_fields)


class Connector(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def source_system(self) -> str: ...

    def detect(
        self,
        metadata: ArtifactMetadata,
        bounded_prefix: bytes,
    ) -> DetectionResult: ...

    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]: ...


def validate_connector(connector: Connector) -> tuple[str, str, str]:
    name = connector.name
    version = connector.version
    _require_text("connector.name", name, 100)
    _require_text("connector.version", version, 100)
    if name.startswith("ledgerbridge."):
        raise ConnectorContractError(
            "connector.name uses the reserved internal namespace ledgerbridge.*"
        )
    source_system = connector.source_system
    _require_canonical_source("connector.source_system", source_system)
    return name, version, source_system


def _require_text(field: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConnectorContractError(f"{field} must be non-blank and at most {maximum} characters")


def _require_canonical_source(field: str, value: str) -> None:
    if not isinstance(value, str) or CANONICAL_SOURCE_PATTERN.fullmatch(value) is None:
        raise ConnectorContractError(
            f"{field} must be a lowercase canonical identifier of at most 64 characters"
        )


def _validate_json_object(
    field: str,
    value: Mapping[str, object],
    *,
    reject_floats: bool,
) -> None:
    if not isinstance(value, Mapping):
        raise ConnectorContractError(f"{field} must be an object")
    _walk_json(value, field=field, reject_floats=reject_floats)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ConnectorContractError(f"{field} must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ConnectorContractError(f"{field} must serialize to at most {MAX_JSON_BYTES} bytes")


def _walk_json(value: object, *, field: str, reject_floats: bool) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ConnectorContractError(f"{field} must not exceed {MAX_JSON_DEPTH} nested levels")
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if reject_floats:
                raise ConnectorContractError(f"{field} cannot contain floating-point values")
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ConnectorContractError(f"{field} object keys must be strings")
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        raise ConnectorContractError(f"{field} must contain JSON values only")


def _validate_money(value: object) -> None:
    if isinstance(value, Mapping):
        amount_keys = [key for key in value if key == "amount_minor" or key.endswith("_minor")]
        for key in amount_keys:
            amount = value[key]
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise ConnectorContractError(f"{key} must be a signed integer minor-unit value")
            if amount < MIN_MINOR_UNITS or amount > MAX_MINOR_UNITS:
                raise ConnectorContractError(f"{key} must fit a signed 64-bit integer")
        if amount_keys and value.get("currency") != "CNY":
            raise ConnectorContractError("normalized money must use currency CNY")
        for child in value.values():
            _validate_money(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_money(child)
