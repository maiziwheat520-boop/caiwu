from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgerbridge.connectors import (
    ConnectorContractError,
    ParsedSourceRecord,
    validate_connector,
)


def _record(normalized: dict[str, object]) -> ParsedSourceRecord:
    return ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={"amount": "12.34"},
        normalized_fields=normalized,
    )


@given(amount=st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_signed_integer_minor_units_are_accepted(amount: int) -> None:
    record = _record({"amount_minor": amount, "currency": "CNY"})
    assert record.normalized_fields["amount_minor"] == amount


@pytest.mark.parametrize("amount", [1.5, True, "100"])
def test_non_integer_minor_units_are_rejected(amount: object) -> None:
    with pytest.raises(ConnectorContractError, match=r"floating-point|signed integer"):
        _record({"amount_minor": amount, "currency": "CNY"})


def test_float_smuggling_and_non_cny_money_are_rejected() -> None:
    with pytest.raises(ConnectorContractError, match="floating-point"):
        _record({"nested": [{"value": 0.1}]})
    with pytest.raises(ConnectorContractError, match="currency CNY"):
        _record({"fee_minor": 10, "currency": "USD"})


@pytest.mark.parametrize("locator", ["", "   ", "x" * 501])
def test_record_locator_is_bounded_and_non_blank(locator: str) -> None:
    with pytest.raises(ConnectorContractError, match="record_locator"):
        ParsedSourceRecord(
            record_locator=locator,
            source="synthetic",
            parser_version="1",
            raw_fields={},
            normalized_fields={},
        )


class InvalidConnector:
    name = " "
    version = "1"


def test_connector_metadata_and_json_edge_cases_are_rejected() -> None:
    with pytest.raises(ConnectorContractError, match=r"connector.name"):
        validate_connector(InvalidConnector())  # type: ignore[arg-type]
    with pytest.raises(ConnectorContractError, match="external_transaction_id"):
        ParsedSourceRecord(
            record_locator="row:1",
            source="synthetic",
            parser_version="1",
            raw_fields={},
            normalized_fields={},
            external_transaction_id=" ",
        )
    with pytest.raises(ConnectorContractError, match="must be an object"):
        ParsedSourceRecord(
            record_locator="row:1",
            source="synthetic",
            parser_version="1",
            raw_fields=[],  # type: ignore[arg-type]
            normalized_fields={},
        )
    with pytest.raises(ConnectorContractError, match="keys must be strings"):
        ParsedSourceRecord(
            record_locator="row:1",
            source="synthetic",
            parser_version="1",
            raw_fields={1: "bad"},  # type: ignore[dict-item]
            normalized_fields={},
        )
    with pytest.raises(ConnectorContractError, match="JSON values"):
        ParsedSourceRecord(
            record_locator="row:1",
            source="synthetic",
            parser_version="1",
            raw_fields={"not_finite": float("nan")},
            normalized_fields={},
        )
    nested = _record({"items": [{"fee_minor": 1, "currency": "CNY"}]})
    assert nested.normalized_fields["items"] == [{"fee_minor": 1, "currency": "CNY"}]


def test_raw_fields_may_preserve_float_but_must_remain_json() -> None:
    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={"source_float": 1.25},
        normalized_fields={},
    )
    assert record.raw_fields["source_float"] == 1.25
    with pytest.raises(ConnectorContractError, match="JSON values"):
        ParsedSourceRecord(
            record_locator="row:2",
            source="synthetic",
            parser_version="1",
            raw_fields={"bad": object()},
            normalized_fields={},
        )
