"""Credential-free synthetic Connector used only by tests and isolated replay."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ledgerbridge.connectors import (
    MAX_JSON_BYTES,
    ArtifactMetadata,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
    ReadableBinary,
)

SYNTHETIC_SCHEMA = "ledgerbridge.synthetic.bank.v1"
SYNTHETIC_SOURCE = "synthetic_bank"
SYNTHETIC_MEDIA_TYPE = "application/json"
MAX_SYNTHETIC_BYTES = MAX_JSON_BYTES


class SyntheticBankConnector:
    """Parse a deterministic, local-only bank statement envelope."""

    name = "synthetic.bank_statement"
    version = "1"
    source_system = SYNTHETIC_SOURCE
    execution_mode = "in_process"

    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        if metadata.source != "synthetic_upload" or metadata.media_type != SYNTHETIC_MEDIA_TYPE:
            return DetectionResult.NO_MATCH
        try:
            envelope = _decode_object(bounded_prefix)
        except ConnectorContractError:
            if f'"schema": "{SYNTHETIC_SCHEMA}"'.encode() in bounded_prefix:
                return DetectionResult.MATCH
            return DetectionResult.AMBIGUOUS
        if envelope.get("schema") != SYNTHETIC_SCHEMA:
            return DetectionResult.NO_MATCH
        return DetectionResult.MATCH

    def parse(self, stream: ReadableBinary) -> Iterable[ParsedSourceRecord]:
        envelope = _decode_object(_read_bounded(stream))
        if envelope.get("schema") != SYNTHETIC_SCHEMA:
            raise ConnectorContractError("synthetic schema is not recognized")
        records = envelope.get("records")
        if not isinstance(records, list):
            raise ConnectorContractError("synthetic records must be an array")
        for index, value in enumerate(records):
            if not isinstance(value, dict):
                raise ConnectorContractError("synthetic record must be an object")
            yield _record(value, index)


class SyntheticBankFactory:
    """Explicit factory for isolated tests; never loaded by default composition."""

    factory_id = "ledgerbridge.synthetic.bank"

    def build(self) -> SyntheticBankConnector:
        return SyntheticBankConnector()


def _read_bounded(stream: ReadableBinary) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_SYNTHETIC_BYTES:
        chunk = stream.read(min(64 * 1024, MAX_SYNTHETIC_BYTES + 1 - total))
        if not isinstance(chunk, bytes):
            raise ConnectorContractError("synthetic stream returned non-bytes")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise ConnectorContractError("synthetic payload exceeds the configured limit")


def _decode_object(value: bytes) -> dict[str, Any]:
    if len(value) > MAX_SYNTHETIC_BYTES:
        raise ConnectorContractError("synthetic payload exceeds the configured limit")
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorContractError("synthetic payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ConnectorContractError("synthetic payload must be an object")
    return decoded


def _record(value: dict[str, Any], index: int) -> ParsedSourceRecord:
    locator = value.get("record_locator")
    external_id = value.get("external_transaction_id")
    occurred_on = value.get("occurred_on")
    amount_minor = value.get("amount_minor")
    currency = value.get("currency")
    counterparty = value.get("counterparty")
    description = value.get("description")
    balance_minor = value.get("balance_minor")
    if not isinstance(locator, str) or not locator:
        raise ConnectorContractError(f"synthetic record {index} has an invalid locator")
    if not isinstance(external_id, str) or not external_id:
        raise ConnectorContractError(f"synthetic record {index} has an invalid external ID")
    if not isinstance(occurred_on, str) or not occurred_on:
        raise ConnectorContractError(f"synthetic record {index} has an invalid date")
    if not isinstance(counterparty, str) or not counterparty:
        raise ConnectorContractError(f"synthetic record {index} has an invalid counterparty")
    if not isinstance(description, str) or not description:
        raise ConnectorContractError(f"synthetic record {index} has an invalid description")
    raw = dict(value)
    normalized = {
        "occurred_on": occurred_on,
        "amount_minor": amount_minor,
        "currency": currency,
        "counterparty": counterparty,
        "description": description,
        "balance_minor": balance_minor,
    }
    return ParsedSourceRecord(
        record_locator=locator,
        source=SYNTHETIC_SOURCE,
        parser_version="1",
        raw_fields=raw,
        normalized_fields=normalized,
        external_transaction_id=external_id,
    )
