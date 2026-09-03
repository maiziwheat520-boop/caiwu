from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
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


def _rehash_audit_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    previous_hash: str | None = None
    rehashed: list[dict[str, object]] = []
    for index, event in enumerate(events):
        unsigned = {key: value for key, value in event.items() if key != "hash"}
        unsigned["sequence"] = index + 1
        unsigned["previous_hash"] = previous_hash
        event_hash = hashlib.sha256(_stable_json(unsigned).encode()).hexdigest()
        rehashed.append({**unsigned, "hash": event_hash})
        previous_hash = event_hash
    return rehashed


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
    verification_results: list[object] = [
        {
            "schema_version": "payroll-receipt-verification/v1",
            "verification_id": "verification_demo_001",
            "company_id": company_id,
            "batch_id": "batch_demo_2026_08",
            "pay_period": "2026-08",
            "version": 4,
            "source_artifact_id": "artifact_statement_001",
            "overall_status": "matched",
            "unknown_receipt_count": 0,
            "results": [
                {
                    "company_id": company_id,
                    "employee_id": "employee_demo_001",
                    "account_id": "account_demo_001",
                    "expected_amount_minor": 524000,
                    "match_status": "matched",
                    "exception_codes": [],
                }
            ],
        }
    ]
    material_summaries: list[object] = [
        {
            "schema_version": "payroll-material-summary/v1",
            "artifact_id": "artifact_payroll_demo_001",
            "company_id": company_id,
            "period": "2026-08",
            "kind": "PAYROLL_SHEET",
            "source": "CONTROLLED_UPLOAD",
            "sha256": "1" * 64,
            "status": "READY_FOR_REVIEW",
        }
    ]
    common_event = {
        "schema_version": "payroll-audit-event/v1",
        "occurred_at": "2026-08-30T06:00:00.000Z",
        "batch_id": "batch_demo_2026_08",
        "company_id": company_id,
        "version": 4,
        "reason": None,
    }
    locked_batch_sha256 = hashlib.sha256(_stable_json(batch).encode()).hexdigest()
    audit_events: list[dict[str, object]] = _rehash_audit_events(
        [
            {
                **common_event,
                "action": "payroll.review_submitted",
                "actor_id": "maker_demo_001",
                "data": {"explicitly_confirmed": True},
            },
            {
                **common_event,
                "action": "payroll.review_completed",
                "actor_id": "checker_demo_001",
                "data": {"explicitly_confirmed": True},
            },
            {
                **common_event,
                "action": "payroll.version_approved_locked",
                "actor_id": "approver_demo_001",
                "data": {
                    "explicitly_approved": True,
                    "locked_version": 4,
                    "active_exception_count": 0,
                    "locked_batch_sha256": locked_batch_sha256,
                },
            },
            {
                **common_event,
                "action": "payroll.receipts_verified",
                "actor_id": "checker_demo_001",
                "data": {
                    "verification_id": "verification_demo_001",
                    "source_artifact_id": "artifact_statement_001",
                    "overall_status": "matched",
                    "matched_count": 1,
                    "attention_count": 0,
                    "unknown_receipt_count": 0,
                    "idempotency_key_hash": "2" * 64,
                },
            },
        ]
    )
    payload = {
        "payroll_batch": batch,
        "verification_results": verification_results,
        "material_summaries": material_summaries,
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
            "event_count": len(audit_events),
            "head_hash": audit_events[0]["hash"],
            "tail_hash": audit_events[-1]["hash"],
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


def _refresh_publication_integrity(publication: dict[str, object]) -> None:
    audit_events = cast(list[object], publication["audit_events"])
    payload = {
        "payroll_batch": publication["payroll_batch"],
        "verification_results": publication["verification_results"],
        "material_summaries": publication["material_summaries"],
        "audit_events": audit_events,
    }
    digest = hashlib.sha256(_stable_json(payload).encode()).hexdigest()
    event_digest = hashlib.sha256(_stable_json(audit_events).encode()).hexdigest()
    publication["publication_id"] = f"publication_{digest[:24]}"
    publication["payload_sha256"] = digest
    proof = cast(dict[str, object], publication["audit_chain_proof"])
    proof["event_count"] = len(audit_events)
    proof["head_hash"] = (
        cast(dict[str, object], audit_events[0]).get("hash") if audit_events else None
    )
    proof["tail_hash"] = (
        cast(dict[str, object], audit_events[-1]).get("hash") if audit_events else None
    )
    proof["events_sha256"] = event_digest


def _replace_publication_account_id(publication: dict[str, object], account_id: str) -> None:
    batch = cast(dict[str, object], publication["payroll_batch"])
    batch_line = cast(list[dict[str, object]], batch["lines"])[0]
    batch_line["account_id"] = account_id

    verification = cast(list[dict[str, object]], publication["verification_results"])[0]
    verification_result = cast(list[dict[str, object]], verification["results"])[0]
    verification_result["account_id"] = account_id

    audit_events = cast(list[dict[str, object]], publication["audit_events"])
    for event in audit_events:
        if event.get("action") == "payroll.version_approved_locked":
            data = cast(dict[str, object], event["data"])
            data["locked_batch_sha256"] = hashlib.sha256(_stable_json(batch).encode()).hexdigest()
    publication["audit_events"] = _rehash_audit_events(audit_events)
    _refresh_publication_integrity(publication)


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


def test_source_accepts_opaque_account_digest_with_scattered_digits() -> None:
    publication = _publication()
    _replace_publication_account_id(publication, "account_9f99f99999f99999f9f99f99")
    source, _ = _source(publication)

    result = _pull(source, publication)

    result_batch = cast(dict[str, object], result.payload["payroll_batch"])
    result_line = cast(list[dict[str, object]], result_batch["lines"])[0]
    assert result_line["account_id"] == "account_9f99f99999f99999f9f99f99"


def test_source_accepts_opaque_account_digest_with_contiguous_digits() -> None:
    publication = _publication()
    _replace_publication_account_id(publication, "account_123456789012abcdefabcdef")
    source, _ = _source(publication)

    result = _pull(source, publication)

    result_batch = cast(dict[str, object], result.payload["payroll_batch"])
    result_line = cast(list[dict[str, object]], result_batch["lines"])[0]
    assert result_line["account_id"] == "account_123456789012abcdefabcdef"


def test_source_rejects_payload_tampering_and_publication_id_mismatch() -> None:
    publication = _publication()
    verification = cast(list[dict[str, object]], publication["verification_results"])[0]
    verification["optional_note"] = "tampered"
    source, _ = _source(publication)

    _assert_error("PAYROLL_PAYLOAD_INTEGRITY_FAILED", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    "section",
    [
        "verification_results",
        "material_summaries",
        "audit_events",
    ],
)
def test_source_rejects_floats_in_nested_financial_sections(section: str) -> None:
    publication = _publication()
    nested = cast(list[dict[str, object]], publication[section])[0]
    nested["optional_ratio"] = 0.5
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_PROVIDER_RESPONSE", lambda: _pull(source, publication))


@pytest.mark.parametrize("value", [True, "524000", 2**53])
def test_source_requires_json_safe_integer_minor_values(value: object) -> None:
    publication = _publication()
    verification = cast(list[dict[str, object]], publication["verification_results"])[0]
    result = cast(list[dict[str, object]], verification["results"])[0]
    result["expected_amount_minor"] = value
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_PROVIDER_RESPONSE", lambda: _pull(source, publication))


def test_source_requires_integer_cents_in_safe_optional_fields() -> None:
    publication = _publication()
    material = cast(list[dict[str, object]], publication["material_summaries"])[0]
    material["legacy_amount_cents"] = "100"
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_PROVIDER_RESPONSE", lambda: _pull(source, publication))


def test_source_preserves_signed_integer_minor_optional_fields() -> None:
    publication = _publication()
    material = cast(list[dict[str, object]], publication["material_summaries"])[0]
    material["adjustment_minor"] = -125
    publication["optional_contract_metadata"] = {"revision": 1}
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    result = _pull(source, publication)

    frozen_material = cast(tuple[dict[str, object], ...], result.payload["material_summaries"])[0]
    assert frozen_material["adjustment_minor"] == -125
    assert result.payload["optional_contract_metadata"] == {"revision": 1}


def test_source_rejects_open_exception_even_when_resolved_flag_is_true() -> None:
    publication = _publication()
    batch = cast(dict[str, object], publication["payroll_batch"])
    batch["exceptions"] = [
        {
            "exception_id": "exception_demo_001",
            "code": "SYNTHETIC_EXCEPTION",
            "status": "OPEN",
            "resolved": True,
        }
    ]
    events = cast(list[dict[str, object]], publication["audit_events"])
    approval_data = cast(dict[str, object], events[2]["data"])
    approval_data["locked_batch_sha256"] = hashlib.sha256(_stable_json(batch).encode()).hexdigest()
    publication["audit_events"] = _rehash_audit_events(events)
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_BATCH_NOT_LOCKED", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("section", "expected_code"),
    [
        ("verification_results", "PAYROLL_SCHEMA_UNSUPPORTED"),
        ("material_summaries", "PAYROLL_SCHEMA_UNSUPPORTED"),
        ("audit_events", "PAYROLL_SCHEMA_UNSUPPORTED"),
    ],
)
def test_source_rejects_unsupported_nested_schema_versions(
    section: str,
    expected_code: str,
) -> None:
    publication = _publication()
    nested = cast(list[dict[str, object]], publication[section])[0]
    nested["schema_version"] = "unsupported/v2"
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error(expected_code, lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("account_id", "account_demo_002", "PAYROLL_IDENTITY_MAPPING_CONFLICT"),
        ("expected_amount_minor", 523999, "PAYROLL_VERIFICATION_AMOUNT_MISMATCH"),
    ],
)
def test_source_rejects_verification_identity_and_amount_drift(
    field: str,
    value: object,
    expected_code: str,
) -> None:
    publication = _publication()
    verification = cast(list[dict[str, object]], publication["verification_results"])[0]
    result = cast(list[dict[str, object]], verification["results"])[0]
    result[field] = value
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error(expected_code, lambda: _pull(source, publication))


def test_source_rejects_raw_bytes_in_material_projection() -> None:
    publication = _publication()
    material = cast(list[dict[str, object]], publication["material_summaries"])[0]
    material["attachment"] = b"raw payroll bytes"
    source, _ = _source(publication)

    _assert_error("PAYROLL_PROVIDER_RESPONSE", lambda: _pull(source, publication))


def test_source_rejects_prohibited_material_bytes_field() -> None:
    publication = _publication()
    material = cast(list[dict[str, object]], publication["material_summaries"])[0]
    material["bytes"] = "not publishable"
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_SENSITIVE_FIELD_NOT_ALLOWED", lambda: _pull(source, publication))


def test_source_accepts_same_company_material_from_another_valid_period() -> None:
    publication = _publication()
    material = cast(list[dict[str, object]], publication["material_summaries"])[0]
    material["period"] = "2026-07"
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    result = _pull(source, publication)

    summaries = cast(tuple[object, ...], result.payload["material_summaries"])
    assert cast(Mapping[str, object], summaries[0])["period"] == "2026-07"


def test_source_rejects_empty_audit_chain_even_with_matching_summary() -> None:
    publication = _publication()
    publication["audit_events"] = []
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


def test_source_rejects_non_whitelisted_audit_action_data() -> None:
    publication = _publication()
    events = cast(list[dict[str, object]], publication["audit_events"])
    approval_data = cast(dict[str, object], events[2]["data"])
    approval_data["private_note"] = "must remain in the controlled audit store"
    publication["audit_events"] = _rehash_audit_events(events)
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 9),
        ("actor_id", "checker_demo_002"),
        ("previous_hash", "0" * 64),
    ],
)
def test_source_rejects_broken_audit_sequence_or_event_hash(
    field: str,
    value: object,
) -> None:
    publication = _publication()
    events = cast(list[dict[str, object]], publication["audit_events"])
    events[1][field] = value
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


def test_source_rejects_audit_chain_without_three_distinct_roles() -> None:
    publication = _publication()
    events = cast(list[dict[str, object]], publication["audit_events"])
    events[2]["actor_id"] = events[1]["actor_id"]
    publication["audit_events"] = _rehash_audit_events(events)
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("locked_batch_sha256", "0" * 64),
        ("explicitly_approved", False),
        ("active_exception_count", 1),
    ],
)
def test_source_rejects_invalid_locked_approval(field: str, value: object) -> None:
    publication = _publication()
    events = cast(list[dict[str, object]], publication["audit_events"])
    approval_data = cast(dict[str, object], events[2]["data"])
    approval_data[field] = value
    publication["audit_events"] = _rehash_audit_events(events)
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


def test_source_rejects_verification_audit_fact_mismatch() -> None:
    publication = _publication()
    events = cast(list[dict[str, object]], publication["audit_events"])
    receipt_data = cast(dict[str, object], events[3]["data"])
    receipt_data["matched_count"] = 0
    publication["audit_events"] = _rehash_audit_events(events)
    _refresh_publication_integrity(publication)
    source, _ = _source(publication)

    _assert_error("PAYROLL_AUDIT_PROOF_INVALID", lambda: _pull(source, publication))


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
