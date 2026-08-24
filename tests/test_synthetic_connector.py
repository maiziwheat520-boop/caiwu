from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ledgerbridge.connector_registry import ConnectorRegistry
from ledgerbridge.connectors import ArtifactMetadata, ConnectorContractError, DetectionResult
from ledgerbridge.synthetic_connector import (
    SYNTHETIC_MEDIA_TYPE,
    SYNTHETIC_SCHEMA,
    SyntheticBankConnector,
    SyntheticBankFactory,
)

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_bank_statement.json"


def _metadata() -> ArtifactMetadata:
    content = FIXTURE.read_bytes()
    return ArtifactMetadata(
        source="synthetic_upload",
        original_filename="synthetic_bank_statement.json",
        media_type=SYNTHETIC_MEDIA_TYPE,
        byte_size=len(content),
        sha256_hex="0" * 64,
    )


def test_synthetic_fixture_detects_and_parses_stable_records() -> None:
    content = FIXTURE.read_bytes()
    connector = SyntheticBankConnector()

    assert connector.detect(_metadata(), content[:512]) is DetectionResult.MATCH
    records = list(connector.parse(_BytesReader(content)))

    assert [record.record_locator for record in records] == [
        "statement:2026-08:0001",
        "statement:2026-08:0002",
    ]
    assert [record.external_transaction_id for record in records] == [
        "demo-tx-0001",
        "demo-tx-0002",
    ]
    assert records[0].normalized_fields["amount_minor"] == -12345
    assert records[1].normalized_fields["currency"] == "CNY"


def test_synthetic_factory_is_explicit_and_registry_stays_empty_by_default() -> None:
    assert ConnectorRegistry().build_all() == ()
    registry = ConnectorRegistry([SyntheticBankFactory()])
    built = registry.build_all()
    assert len(built) == 1
    assert built[0].name == "synthetic.bank_statement"
    assert built[0].source_system == "synthetic_bank"


@pytest.mark.parametrize(
    "metadata",
    [
        replace(_metadata(), source="manual_upload"),
        replace(_metadata(), media_type="text/csv"),
    ],
)
def test_synthetic_connector_does_not_claim_other_sources(metadata: ArtifactMetadata) -> None:
    connector = SyntheticBankConnector()
    assert connector.detect(metadata, FIXTURE.read_bytes()) is DetectionResult.NO_MATCH


def test_synthetic_connector_rejects_ambiguous_or_invalid_records() -> None:
    connector = SyntheticBankConnector()
    assert connector.detect(_metadata(), b'{"records": []}') is DetectionResult.NO_MATCH
    assert connector.detect(_metadata(), b"not-json") is DetectionResult.AMBIGUOUS

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["schema"] = SYNTHETIC_SCHEMA
    payload["records"][0]["amount_minor"] = 1.25
    with pytest.raises(ConnectorContractError, match=r"floating-point|signed integer"):
        list(connector.parse(_BytesReader(json.dumps(payload).encode())))

    payload["records"][0]["amount_minor"] = 1
    payload["records"][0]["currency"] = "USD"
    with pytest.raises(ConnectorContractError, match="currency CNY"):
        list(connector.parse(_BytesReader(json.dumps(payload).encode())))


def test_synthetic_connector_bounds_stream_and_shape() -> None:
    connector = SyntheticBankConnector()
    with pytest.raises(ConnectorContractError, match="records must be an array"):
        list(connector.parse(_BytesReader(b'{"schema":"' + SYNTHETIC_SCHEMA.encode() + b'"}')))

    with pytest.raises(ConnectorContractError, match="limit"):
        list(connector.parse(_BytesReader(b"x" * 1_000_001)))


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        self._value = value
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._value) - self._offset
        chunk = self._value[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk
