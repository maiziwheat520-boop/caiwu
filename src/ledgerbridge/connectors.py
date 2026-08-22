"""Typed, side-effect-free connector boundary for synthetic Phase 2 imports."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


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
        _require_text("source", self.source, 200)
        _require_text("parser_version", self.parser_version, 100)
        if self.external_transaction_id is not None:
            _require_text("external_transaction_id", self.external_transaction_id, 300)
        _validate_json_object("raw_fields", self.raw_fields, reject_floats=False)
        _validate_json_object("normalized_fields", self.normalized_fields, reject_floats=True)
        _validate_money(self.normalized_fields)


class Connector(Protocol):
    name: str
    version: str

    def detect(
        self,
        metadata: ArtifactMetadata,
        bounded_prefix: bytes,
    ) -> DetectionResult: ...

    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]: ...


def validate_connector(connector: Connector) -> None:
    _require_text("connector.name", connector.name, 100)
    _require_text("connector.version", connector.version, 100)


def _require_text(field: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConnectorContractError(f"{field} must be non-blank and at most {maximum} characters")


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
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ConnectorContractError(f"{field} must contain JSON values only") from exc


def _walk_json(value: object, *, field: str, reject_floats: bool) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if reject_floats:
            raise ConnectorContractError(f"{field} cannot contain floating-point values")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ConnectorContractError(f"{field} object keys must be strings")
            _walk_json(child, field=field, reject_floats=reject_floats)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _walk_json(child, field=field, reject_floats=reject_floats)
        return
    raise ConnectorContractError(f"{field} must contain JSON values only")


def _validate_money(value: object) -> None:
    if isinstance(value, Mapping):
        amount_keys = [key for key in value if key == "amount_minor" or key.endswith("_minor")]
        for key in amount_keys:
            amount = value[key]
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise ConnectorContractError(f"{key} must be a signed integer minor-unit value")
        if amount_keys and value.get("currency") != "CNY":
            raise ConnectorContractError("normalized money must use currency CNY")
        for child in value.values():
            _validate_money(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_money(child)
