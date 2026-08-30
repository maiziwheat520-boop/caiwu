from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from typing import cast
from uuid import UUID

import pytest

from ledgerbridge.payroll_integration import (
    HttpPayrollPublicationSource,
    InMemoryPayrollPublicationSource,
    PayrollCompanyMap,
    PayrollIntegrationError,
    PayrollPublication,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
OTHER_ENTITY = UUID("10000000-0000-4000-8000-000000000002")
COMPANY = "company_demo_hotel"


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _publication(*, company_id: str = COMPANY) -> dict[str, object]:
    batch: dict[str, object] = {
        "schema_version": "payroll-batch-export/v1",
        "company_id": company_id,
        "batch_id": "batch_demo_2026_08",
        "pay_period": "2026-08",
        "version": 4,
        "locked_version": 4,
        "status": "approved",
        "lines": [
            {
                "company_id": company_id,
                "employee_id": "employee_demo_001",
                "account_id": "account_demo_001",
                "gross_pay_minor": 550000,
                "net_pay_minor": 524000,
                "disbursement_channel": "mybank",
            }
        ],
        "exceptions": [],
    }
    audit_events: list[object] = []
    payload = {
        "payroll_batch": batch,
        "verification_results": [],
        "material_summaries": [],
        "audit_events": audit_events,
    }
    digest = hashlib.sha256(_stable_json(payload).encode()).hexdigest()
    events_digest = hashlib.sha256(_stable_json(audit_events).encode()).hexdigest()
    return {
        "schema_version": "payroll-ledgerbridge-publication/v1",
        "publication_id": f"publication_{digest[:24]}",
        "published_at": "2026-08-30T06:00:00.000Z",
        "scope": {
            "company_id": company_id,
            "batch_id": "batch_demo_2026_08",
            "pay_period": "2026-08",
            "locked_version": 4,
        },
        "safety": {
            "purpose": "ACCOUNTING_AND_RECONCILIATION_ONLY",
            "payable": False,
            "payment_submission_supported": False,
            "payment_execution_supported": False,
        },
        **payload,
        "audit_chain_proof": {
            "schema_version": "payroll-audit-chain-proof/v1",
            "algorithm": "sha256",
            "event_count": 0,
            "head_hash": None,
            "tail_hash": None,
            "events_sha256": events_digest,
        },
        "payload_sha256": digest,
    }


def _status() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "ready",
        "demo_mode": True,
        "payment_submission_supported": False,
    }


class _Transport:
    def __init__(self, publication: dict[str, object]) -> None:
        self.publication = publication
        self.status = _status()
        self.calls: list[tuple[str, float, int]] = []
        self.error: BaseException | None = None

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> dict[str, object]:
        self.calls.append((url, timeout_seconds, max_bytes))
        if self.error is not None:
            raise self.error
        if url.endswith("/api/v1/status"):
            return self.status
        return self.publication


def _source(
    publication: dict[str, object] | None = None,
    *,
    enabled: bool = True,
) -> tuple[HttpPayrollPublicationSource, _Transport]:
    current = publication or _publication()
    transport = _Transport(current)
    return (
        HttpPayrollPublicationSource(
            base_url="http://127.0.0.1:4318",
            timeout_seconds=2.5,
            company_mapping={COMPANY: ENTITY},
            enabled=enabled,
            transport=transport,
        ),
        transport,
    )


def _pull(
    source: HttpPayrollPublicationSource | InMemoryPayrollPublicationSource,
    publication: dict[str, object],
    *,
    entity_ref: UUID = ENTITY,
    key: str = "payroll-read-001",
) -> PayrollPublication:
    return source.pull_publication(
        entity_ref=entity_ref,
        publication_id=cast(str, publication["publication_id"]),
        idempotency_key=key,
    )


def _assert_error(code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(PayrollIntegrationError) as captured:
        operation()
    assert captured.value.error_code == code


def test_http_source_is_disabled_by_default_and_performs_no_io() -> None:
    publication = _publication()
    source, transport = _source(publication, enabled=False)

    _assert_error("PAYROLL_INTEGRATION_DISABLED", lambda: _pull(source, publication))

    assert transport.calls == []


def test_http_source_preserves_opaque_ids_and_uses_injected_configuration() -> None:
    publication = _publication()
    source, transport = _source(publication)

    result = _pull(source, publication)

    assert result.company_id == COMPANY
    assert result.entity_ref == ENTITY
    assert result.employee_account_ids == (("employee_demo_001", "account_demo_001"),)
    assert result.payload["publication_id"] == publication["publication_id"]
    assert [call[:2] for call in transport.calls] == [
        ("http://127.0.0.1:4318/api/v1/status", 2.5),
        (
            f"http://127.0.0.1:4318/api/v1/payroll-publications/{publication['publication_id']}",
            2.5,
        ),
    ]
    assert not hasattr(source, "submit_payment")
    assert not hasattr(source, "import_formal_data")


@pytest.mark.parametrize(
    ("field", "value"),
    [("demo_mode", False), ("payment_submission_supported", True), ("status", "degraded")],
)
def test_source_rejects_unsafe_provider_status(field: str, value: object) -> None:
    publication = _publication()
    source, transport = _source(publication)
    transport.status[field] = value

    _assert_error("PAYROLL_STATUS_UNSAFE", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("error", "code"),
    [(TimeoutError(), "PAYROLL_PROVIDER_TIMEOUT"), (OSError(), "PAYROLL_PROVIDER_UNAVAILABLE")],
)
def test_source_translates_timeout_and_unreachable_provider(
    error: BaseException,
    code: str,
) -> None:
    publication = _publication()
    source, transport = _source(publication)
    transport.error = error

    _assert_error(code, lambda: _pull(source, publication))


def test_source_rejects_unknown_schema_before_using_payload() -> None:
    publication = _publication()
    publication["schema_version"] = "payroll-ledgerbridge-publication/v2"
    source, _ = _source(publication)

    _assert_error("PAYROLL_SCHEMA_UNSUPPORTED", lambda: _pull(source, publication))


def test_company_mapping_is_explicit_bijective_and_scope_checked() -> None:
    publication = _publication()
    source, transport = _source(publication)

    _assert_error(
        "PAYROLL_COMPANY_MAPPING_MISSING",
        lambda: _pull(source, publication, entity_ref=OTHER_ENTITY),
    )
    assert transport.calls == []
    _assert_error(
        "PAYROLL_COMPANY_MAPPING_CONFLICT",
        lambda: PayrollCompanyMap({COMPANY: ENTITY, "company_other": ENTITY}),
    )

    other = _publication(company_id="company_other")
    other["publication_id"] = publication["publication_id"]
    source, _ = _source(other)
    _assert_error("PAYROLL_IDENTITY_SCOPE_MISMATCH", lambda: _pull(source, other))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payable", True),
        ("payment_submission_supported", True),
        ("payment_execution_supported", True),
    ],
)
def test_source_rejects_every_payable_or_submission_mode(field: str, value: object) -> None:
    publication = _publication()
    safety = cast(dict[str, object], publication["safety"])
    safety[field] = value
    source, _ = _source(publication)

    _assert_error("PAYROLL_PAYMENT_MODE_NOT_ALLOWED", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("field", "value"),
    [("employee_id", " employee_demo_001"), ("account_id", "card123456789012")],
)
def test_source_never_guesses_or_reencodes_employee_and_account_ids(
    field: str,
    value: object,
) -> None:
    publication = _publication()
    batch = cast(dict[str, object], publication["payroll_batch"])
    line = cast(list[dict[str, object]], batch["lines"])[0]
    line[field] = value
    source, _ = _source(publication)

    _assert_error("PAYROLL_IDENTITY_INVALID", lambda: _pull(source, publication))


def test_source_rejects_payload_tampering_and_publication_id_mismatch() -> None:
    publication = _publication()
    batch = cast(dict[str, object], publication["payroll_batch"])
    line = cast(list[dict[str, object]], batch["lines"])[0]
    line["net_pay_minor"] = 1
    source, _ = _source(publication)

    _assert_error("PAYROLL_PAYLOAD_INTEGRITY_FAILED", lambda: _pull(source, publication))


def test_idempotency_replays_exact_request_and_rejects_key_reuse() -> None:
    publication = _publication()
    source, transport = _source(publication)

    first = _pull(source, publication)
    replay = _pull(source, publication)
    assert replay is first
    assert len(transport.calls) == 2

    other = deepcopy(publication)
    other["publication_id"] = "publication_000000000000000000000000"
    _assert_error(
        "PAYROLL_IDEMPOTENCY_CONFLICT",
        lambda: _pull(source, other),
    )
    assert len(transport.calls) == 2


def test_in_memory_adapter_uses_the_same_interface_and_validation() -> None:
    publication = _publication()
    publication_id = cast(str, publication["publication_id"])
    source = InMemoryPayrollPublicationSource(
        status=_status(),
        publications={publication_id: publication},
        company_mapping={COMPANY: ENTITY},
        enabled=True,
    )

    result = _pull(source, publication)

    assert result.publication_id == publication_id
    with pytest.raises(TypeError):
        cast(dict[str, object], result.payload)["company_id"] = "changed"
