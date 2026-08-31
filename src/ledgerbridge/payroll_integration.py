"""Read-only PayrollVerification publication adapter for accounting review.

The module deliberately stops at a narrow seam.  It can retrieve and validate
one deterministic, non-payable publication, but it cannot import formal data,
write LedgerBridge state, submit payments, or infer cross-system identities.
Deployment-owned configuration must inject the provider URL, timeout, and an
explicit bijection between PayrollVerification ``company_id`` values and
LedgerBridge ``entity_ref`` UUIDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Never, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from ledgerbridge.internal_payroll_assertion import (
    PROVIDER_USER_ASSERTION_HEADER,
    PROVIDER_WORKLOAD_ASSERTION_HEADER,
)

PUBLICATION_SCHEMA = "payroll-ledgerbridge-publication/v1"
STATUS_SCHEMA = "1.0"
LIVE_PROJECTION_SCHEMA = "payroll-ledgerbridge-live-projection/v1"
LIVE_PROJECTION_CONTRACT_VERSION = "1.0.0"
LIVE_PROJECTION_PATH = "/api/v1/ledgerbridge-projections/current"
TEST_WORKSPACES_PATH = "/api/v1/test-workspaces"
TEST_WORKSPACE_SCHEMA = "payroll-ledgerbridge-test-projection/v1"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SAFE_INTEGER = (2**53) - 1
_PUBLICATION_ID = re.compile(r"^publication_[a-f0-9]{24}$")
_STABLE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{2,127}$")
_ALLOWED_CHANNELS = frozenset({"mybank", "bank_of_china", "wechat", "cash"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "publication_id",
        "published_at",
        "scope",
        "safety",
        "payroll_batch",
        "verification_results",
        "material_summaries",
        "audit_events",
        "audit_chain_proof",
        "payload_sha256",
    }
)
_SCOPE_FIELDS = frozenset({"company_id", "batch_id", "pay_period", "locked_version"})
_SAFETY_FIELDS = frozenset(
    {
        "purpose",
        "payable",
        "payment_submission_supported",
        "payment_execution_supported",
    }
)
_BATCH_FIELDS = frozenset(
    {
        "schema_version",
        "company_id",
        "batch_id",
        "pay_period",
        "version",
        "locked_version",
        "status",
        "lines",
        "exceptions",
    }
)
_LINE_FIELDS = frozenset(
    {
        "company_id",
        "employee_id",
        "account_id",
        "gross_pay_minor",
        "net_pay_minor",
        "disbursement_channel",
    }
)
_PROOF_FIELDS = frozenset(
    {"schema_version", "algorithm", "event_count", "head_hash", "tail_hash", "events_sha256"}
)
_BATCH_EXCEPTION_FIELDS = frozenset({"exception_id", "code", "status"})
_VERIFICATION_FIELDS = frozenset(
    {
        "schema_version",
        "verification_id",
        "company_id",
        "batch_id",
        "pay_period",
        "version",
        "source_artifact_id",
        "overall_status",
        "unknown_receipt_count",
        "results",
    }
)
_VERIFICATION_RESULT_FIELDS = frozenset(
    {
        "company_id",
        "employee_id",
        "account_id",
        "expected_amount_minor",
        "match_status",
        "exception_codes",
    }
)
_MATERIAL_FIELDS = frozenset(
    {"schema_version", "artifact_id", "company_id", "period", "kind", "source", "sha256", "status"}
)
_AUDIT_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "occurred_at",
        "action",
        "actor_id",
        "batch_id",
        "company_id",
        "version",
        "reason",
        "data",
        "previous_hash",
        "hash",
    }
)
_AUDIT_ACTION_DATA_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "payroll.source_adopted": frozenset({"source_artifact_id", "explicitly_confirmed"}),
        "payroll.review_submitted": frozenset({"explicitly_confirmed"}),
        "payroll.review_completed": frozenset({"explicitly_confirmed"}),
        "payroll.version_approved_locked": frozenset(
            {
                "explicitly_approved",
                "locked_version",
                "active_exception_count",
                "locked_batch_sha256",
            }
        ),
        "payroll.bank_draft_exported": frozenset(
            {
                "draft_id",
                "draft_type",
                "explicitly_requested",
                "payable",
                "submission_supported",
                "idempotency_key_hash",
            }
        ),
        "payroll.receipts_verified": frozenset(
            {
                "verification_id",
                "source_artifact_id",
                "overall_status",
                "matched_count",
                "attention_count",
                "unknown_receipt_count",
                "idempotency_key_hash",
            }
        ),
    }
)
_AUDIT_REQUIRED_TRUE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "payroll.source_adopted": ("explicitly_confirmed",),
        "payroll.review_submitted": ("explicitly_confirmed",),
        "payroll.review_completed": ("explicitly_confirmed",),
        "payroll.version_approved_locked": ("explicitly_approved",),
        "payroll.bank_draft_exported": ("explicitly_requested",),
    }
)
_ALLOWED_DRAFT_TYPES = frozenset(
    {
        "normal_bank_payroll",
        "cash_disbursement",
        "supplemental_bank_payroll",
        "payroll_summary",
    }
)
_CONTROLLED_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ACCOUNT_LIKE_NUMBER = re.compile(r"\d(?:[\s._:-]*\d){11,}")
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\]|file://|/(?:Users|home|mnt|private)/)", re.I)
_SENSITIVE_FIELDS = frozenset(
    {
        "accountnumber",
        "bankaccount",
        "bankaccountnumber",
        "bytes",
        "cardnumber",
        "employeename",
        "filename",
        "filepath",
        "fullname",
        "originalfilebase64",
        "originalfilename",
        "rawaccount",
        "rawbytes",
        "sourcepath",
        "absolutepath",
        "localpath",
        "storagekey",
        "filecontent",
    }
)
_SENSITIVE_VALUE_SKIP_FIELDS = frozenset(
    {
        "hash",
        "previoushash",
        "sha256",
        "payloadsha256",
        "eventssha256",
        "idempotencykeyhash",
        "publicationid",
    }
)
_LIVE_TOP_LEVEL_FIELDS = frozenset(
    {
        "contract_version",
        "schema_version",
        "company_id",
        "projection_revision",
        "etag",
        "generated_at",
        "live_data_ready",
        "payable",
        "payment_submission_supported",
        "submission_supported",
        "server_capabilities",
        "unassigned_material_count",
        "materials",
        "batches",
        "available_evidence",
        "verifications",
        "resources",
    }
)
_LIVE_CAPABILITY_FIELDS = frozenset(
    {
        "projection_read",
        "material_review_command",
        "batch_command",
        "verification_command",
        "payment_submission",
    }
)
_LIVE_MATERIAL_FIELDS = frozenset(
    {
        "company_id",
        "material_id",
        "period",
        "material_type",
        "status",
        "review_revision",
        "payable",
        "submission_supported",
    }
)
_LIVE_BATCH_FIELDS = frozenset(
    {
        "company_id",
        "batch_id",
        "pay_period",
        "revision",
        "status",
        "payable",
        "submission_supported",
        "payment_submission_supported",
        "lines",
        "audit_closure",
    }
)
_LIVE_BATCH_REQUIRED_FIELDS = _LIVE_BATCH_FIELDS - {"audit_closure"}
_LIVE_LINE_FIELDS = frozenset(
    {
        "company_id",
        "employee_id",
        "employee_display",
        "account_id",
        "account_display",
        "net_pay_minor",
    }
)
_LIVE_VERIFICATION_FIELDS = frozenset(
    {
        "company_id",
        "verification_id",
        "batch_id",
        "status",
        "source_artifact_ids",
        "results",
        "payable",
        "submission_supported",
        "payment_submission_supported",
    }
)
_LIVE_VERIFICATION_RESULT_FIELDS = frozenset(
    {
        "company_id",
        "employee_id",
        "employee_display",
        "account_id",
        "account_display",
        "status",
    }
)
_LIVE_RESOURCE_FIELDS = frozenset(
    {
        "company_id",
        "employee_id",
        "employee_display",
        "account_id",
        "account_display",
    }
)
_LIVE_AUDIT_CLOSURE_FIELDS = frozenset({"audit_event_id", "audit_hash"})
_LIVE_EVIDENCE_FIELDS = frozenset(
    {
        "company_id",
        "artifact_id",
        "period",
        "evidence_type",
        "status",
        "display_label",
    }
)
_LIVE_EVIDENCE_TYPES = frozenset(
    {
        "MYBANK_STATEMENT",
        "BOC_RECEIPT",
        "WECHAT_RECEIPT",
        "CASH_SIGNOFF",
        "BANK_RECEIPT",
    }
)
# Kept only for the unreachable legacy-validator reference below; the live
# adapter calls the exact provider contract validator defined above it.
_LIVE_DASHBOARD_FIELDS: frozenset[str] = frozenset()
_LIVE_VERIFICATION_AUDIT_FIELDS: frozenset[str] = frozenset()


class PayrollIntegrationError(RuntimeError):
    """A bounded, machine-readable payroll publication failure."""

    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


class PayrollHttpTransport(Protocol):
    """Retrieve bounded JSON; deployment may supply service authentication here."""

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> Mapping[str, object]: ...


class PayrollLiveHttpTransport(Protocol):
    """Retrieve protected provider JSON with server-generated headers only."""

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str],
    ) -> Mapping[str, object]: ...

    def post_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Mapping[str, object]: ...


class PayrollPublicationSource(Protocol):
    """The source seam shared by the HTTP adapter and the in-memory test adapter."""

    def pull_publication(
        self,
        *,
        entity_ref: UUID,
        publication_id: str,
        idempotency_key: str,
    ) -> PayrollPublication: ...


@dataclass(frozen=True, slots=True)
class PayrollPublication:
    """Immutable, source-faithful projection; identifiers are never remapped."""

    publication_id: str
    company_id: str
    entity_ref: UUID
    batch_id: str
    pay_period: str
    employee_account_ids: tuple[tuple[str, str], ...]
    payload: Mapping[str, object]

    def payload_copy(self) -> dict[str, object]:
        """Return JSON-ready data without changing any source identifier."""

        return cast(dict[str, object], _deep_thaw(self.payload))


@dataclass(frozen=True, slots=True)
class PayrollLiveRead:
    """One company-scoped, immutable live-payroll read result."""

    entity_ref: UUID
    company_id: str
    payload: Mapping[str, object]

    def payload_copy(self) -> dict[str, object]:
        return cast(dict[str, object], _deep_thaw(self.payload))


@dataclass(frozen=True, slots=True)
class PayrollTestWorkspaceResult:
    """Company-scoped TEST_ONLY provider result; never a payable projection."""

    entity_ref: UUID
    company_id: str
    replayed: bool
    payload: Mapping[str, object]

    def payload_copy(self) -> dict[str, object]:
        return cast(dict[str, object], _deep_thaw(self.payload))


class HttpPayrollTestWorkspaceSource:
    """Narrow adapter for disposable payroll test workspaces."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        company_mapping: Mapping[str, UUID],
        enabled: bool,
        transport: PayrollLiveHttpTransport,
    ) -> None:
        self._enabled = enabled
        self._base_url = _require_base_url(base_url)
        self._timeout_seconds = float(timeout_seconds)
        self._companies = PayrollCompanyMap(company_mapping)
        self._transport = transport

    def read_workspace(
        self, *, entity_ref: UUID, test_batch_id: str, provider_headers: Mapping[str, str]
    ) -> PayrollTestWorkspaceResult:
        company_id = self._companies.company_for_entity(entity_ref)
        batch_id = _require_stable_identifier(test_batch_id, "test_batch_id")
        path = f"{TEST_WORKSPACES_PATH}/{quote(batch_id, safe='')}"
        raw = self._get(path, provider_headers)
        _validate_test_workspace_projection(raw, company_id, batch_id)
        return self._result(entity_ref, company_id, False, raw)

    def create_workspace(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult:
        company_id = self._companies.company_for_entity(entity_ref)
        raw = self._post(TEST_WORKSPACES_PATH, provider_headers, body)
        _require_exact_keys(
            raw,
            frozenset({"schema_version", "replayed", "projection"}),
            "test workspace create result",
        )
        if (
            raw.get("schema_version") != "payroll-test-workspace-create-result/v1"
            or type(raw.get("replayed")) is not bool
        ):
            _invalid_response("payroll test workspace create result is invalid")
        projection = _require_object(raw.get("projection"), "test workspace projection")
        _validate_test_workspace_projection(projection, company_id, test_batch_id)
        return self._result(entity_ref, company_id, bool(raw["replayed"]), projection)

    def clear_workspace(
        self,
        *,
        entity_ref: UUID,
        test_batch_id: str,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> PayrollTestWorkspaceResult:
        company_id = self._companies.company_for_entity(entity_ref)
        batch_id = _require_stable_identifier(test_batch_id, "test_batch_id")
        raw = self._post(
            f"{TEST_WORKSPACES_PATH}/{quote(batch_id, safe='')}/clear", provider_headers, body
        )
        _validate_test_workspace_clear_receipt(raw, company_id, batch_id)
        return self._result(entity_ref, company_id, bool(raw["replayed"]), raw)

    def _get(self, path: str, headers: Mapping[str, str]) -> Mapping[str, object]:
        if not self._enabled:
            raise PayrollIntegrationError(
                "PAYROLL_TEST_WORKSPACE_DISABLED", "payroll test workspaces are disabled"
            )
        return self._transport.get_json(
            self._base_url + path,
            timeout_seconds=self._timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
            headers=headers,
        )

    def _post(self, path: str, headers: Mapping[str, str], body: bytes) -> Mapping[str, object]:
        if not self._enabled:
            raise PayrollIntegrationError(
                "PAYROLL_TEST_WORKSPACE_DISABLED", "payroll test workspaces are disabled"
            )
        return self._transport.post_json(
            self._base_url + path,
            timeout_seconds=self._timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
            headers=headers,
            body=body,
        )

    @staticmethod
    def _result(
        entity_ref: UUID, company_id: str, replayed: bool, payload: Mapping[str, object]
    ) -> PayrollTestWorkspaceResult:
        return PayrollTestWorkspaceResult(
            entity_ref, company_id, replayed, cast(Mapping[str, object], _deep_freeze(payload))
        )


@dataclass(frozen=True, slots=True)
class _VerificationProof:
    verification_id: str
    source_artifact_id: str
    overall_status: str
    matched_count: int
    attention_count: int
    unknown_receipt_count: int


class PayrollCompanyMap:
    """Explicit one-to-one mapping supplied by trusted server configuration."""

    def __init__(self, values: Mapping[str, UUID]) -> None:
        by_company: dict[str, UUID] = {}
        by_entity: dict[UUID, str] = {}
        for company_id, entity_ref in values.items():
            _require_stable_identifier(company_id, "company_id")
            if not isinstance(entity_ref, UUID) or entity_ref.int == 0:
                raise PayrollIntegrationError(
                    "PAYROLL_COMPANY_MAPPING_INVALID",
                    "payroll company mapping contains an invalid entity reference",
                )
            if entity_ref in by_entity:
                raise PayrollIntegrationError(
                    "PAYROLL_COMPANY_MAPPING_CONFLICT",
                    "payroll company mapping is not one-to-one",
                )
            by_company[company_id] = entity_ref
            by_entity[entity_ref] = company_id
        self._by_company = MappingProxyType(by_company)
        self._by_entity = MappingProxyType(by_entity)

    def company_for_entity(self, entity_ref: UUID) -> str:
        if not isinstance(entity_ref, UUID):
            raise PayrollIntegrationError(
                "PAYROLL_COMPANY_MAPPING_INVALID",
                "payroll entity reference is invalid",
            )
        company_id = self._by_entity.get(entity_ref)
        if company_id is None:
            raise PayrollIntegrationError(
                "PAYROLL_COMPANY_MAPPING_MISSING",
                "payroll company mapping is unavailable for this entity",
            )
        return company_id

    def entity_for_company(self, company_id: str) -> UUID:
        entity_ref = self._by_company.get(company_id)
        if entity_ref is None:
            raise PayrollIntegrationError(
                "PAYROLL_COMPANY_MAPPING_MISSING",
                "payroll entity mapping is unavailable for this company",
            )
        return entity_ref


class HttpPayrollLiveSource:
    """Protected live projection adapter; old dashboard data is never consumed."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        company_mapping: Mapping[str, UUID],
        enabled: bool = False,
        transport: PayrollLiveHttpTransport,
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        self._base_url = _require_base_url(base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        self._timeout_seconds = float(timeout_seconds)
        self._companies = PayrollCompanyMap(company_mapping)
        self._transport = transport

    def read_status(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
        projection_headers: Mapping[str, str] | None = None,
        allowed_actions: tuple[str, ...] = (),
    ) -> PayrollLiveRead:
        self._require_enabled()
        company_id = self._companies.company_for_entity(entity_ref)
        if any(action != "VERIFY_RECEIPTS" for action in allowed_actions) or len(
            set(allowed_actions)
        ) != len(allowed_actions):
            raise PayrollIntegrationError(
                "PAYROLL_COMMAND_NOT_ALLOWED",
                "payroll command capability is invalid",
            )
        projection = self._request(
            LIVE_PROJECTION_PATH,
            provider_headers=projection_headers or provider_headers,
        )
        _validate_live_projection(projection, expected_company_id=company_id)
        capabilities = cast(Mapping[str, object], projection["server_capabilities"])
        commands_enabled = bool(
            projection["live_data_ready"] is True
            and capabilities["verification_command"] is True
            and allowed_actions
        )
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-status.v1",
            "live_data_ready": projection["live_data_ready"],
            "live_projection_schema": projection["schema_version"],
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "setup_summary": _setup_summary(projection),
            "capabilities": {
                "commands_enabled": commands_enabled,
                "allowed_actions": list(allowed_actions) if commands_enabled else [],
            },
            "payment_operations_exposed": False,
        }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def read_dashboard(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
    ) -> PayrollLiveRead:
        company_id, projection = self._read_projection(
            entity_ref=entity_ref,
            provider_headers=provider_headers,
            require_ready=False,
        )
        batches = cast(list[Mapping[str, object]], projection["batches"])
        materials = cast(list[Mapping[str, object]], projection["materials"])
        verifications = cast(list[Mapping[str, object]], projection["verifications"])
        net_pay_minor = sum(
            cast(int, line["net_pay_minor"])
            for batch in batches
            for line in cast(list[Mapping[str, object]], batch["lines"])
        )
        setup_summary = _setup_summary(projection)
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-dashboard.v1",
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "generated_at": projection["generated_at"],
            "live_data_ready": projection["live_data_ready"],
            "setup_summary": setup_summary,
        }
        if projection["live_data_ready"] is True:
            payload["dashboard"] = {
                "batch_count": len(batches),
                "material_count": len(materials),
                "materials_needing_review_count": sum(
                    item["status"] != "REVIEWED" for item in materials
                ),
                "verification_attention_count": sum(
                    item["status"] != "MATCHED" for item in verifications
                ),
                "unassigned_material_count": projection["unassigned_material_count"],
                "net_pay_minor": net_pay_minor,
            }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def list_materials(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
    ) -> PayrollLiveRead:
        company_id, projection = self._read_projection(
            entity_ref=entity_ref,
            provider_headers=provider_headers,
        )
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-material-list.v1",
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "generated_at": projection["generated_at"],
            "items": projection["materials"],
        }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def read_material(
        self,
        *,
        entity_ref: UUID,
        material_id: str,
        provider_headers: Mapping[str, str],
    ) -> PayrollLiveRead:
        _require_stable_identifier(material_id, "material_id")
        company_id, projection = self._read_projection(
            entity_ref=entity_ref,
            provider_headers=provider_headers,
        )
        materials = cast(list[Mapping[str, object]], projection["materials"])
        material = next(
            (item for item in materials if item.get("material_id") == material_id),
            None,
        )
        if material is None:
            raise PayrollIntegrationError(
                "PAYROLL_MATERIAL_NOT_FOUND",
                "payroll material is unavailable in the authenticated company projection",
            )
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-material-detail.v1",
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "generated_at": projection["generated_at"],
            "material": material,
        }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def list_batches(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
    ) -> PayrollLiveRead:
        company_id, projection = self._read_projection(
            entity_ref=entity_ref,
            provider_headers=provider_headers,
        )
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-batch-list.v1",
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "generated_at": projection["generated_at"],
            "items": projection["batches"],
        }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def list_verification_results(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
    ) -> PayrollLiveRead:
        company_id, projection = self._read_projection(
            entity_ref=entity_ref,
            provider_headers=provider_headers,
        )
        payload: dict[str, object] = {
            "schema_version": "ledgerbridge.payroll-verification-list.v1",
            "projection_revision": projection["projection_revision"],
            "etag": projection["etag"],
            "generated_at": projection["generated_at"],
            "items": projection["verifications"],
            "available_evidence": projection["available_evidence"],
        }
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(payload)),
        )

    def verify_receipts(
        self,
        *,
        entity_ref: UUID,
        batch_id: str,
        provider_body: bytes,
        provider_headers: Mapping[str, str],
        idempotency_key: str,
    ) -> PayrollLiveRead:
        self._require_enabled()
        company_id = self._companies.company_for_entity(entity_ref)
        _require_stable_identifier(batch_id, "batch_id")
        path = f"/api/v1/batches/{quote(batch_id, safe='')}/verify-receipts"
        receipt = self._request_post(
            path,
            provider_headers=provider_headers,
            body=provider_body,
        )
        _validate_command_receipt(
            receipt,
            company_id=company_id,
            resource_id=batch_id,
            action="payroll.receipts.verify",
            idempotency_key=idempotency_key,
        )
        return PayrollLiveRead(
            entity_ref=entity_ref,
            company_id=company_id,
            payload=cast(Mapping[str, object], _deep_freeze(receipt)),
        )

    def _read_projection(
        self,
        *,
        entity_ref: UUID,
        provider_headers: Mapping[str, str],
        require_ready: bool = True,
    ) -> tuple[str, Mapping[str, object]]:
        self._require_enabled()
        company_id = self._companies.company_for_entity(entity_ref)
        projection = self._request(
            LIVE_PROJECTION_PATH,
            provider_headers=provider_headers,
        )
        _validate_live_projection(projection, expected_company_id=company_id)
        if require_ready and projection["live_data_ready"] is not True:
            raise PayrollIntegrationError(
                "PAYROLL_LIVE_DATA_UNAVAILABLE",
                "payroll provider has not made a live projection available",
            )
        return company_id, projection

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise PayrollIntegrationError(
                "PAYROLL_INTEGRATION_DISABLED",
                "payroll live integration is disabled",
            )

    def _request(
        self,
        path: str,
        *,
        provider_headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        if (
            not isinstance(provider_headers, Mapping)
            or frozenset(provider_headers)
            != frozenset(
                {
                    PROVIDER_WORKLOAD_ASSERTION_HEADER,
                    PROVIDER_USER_ASSERTION_HEADER,
                }
            )
            or provider_headers.get(PROVIDER_WORKLOAD_ASSERTION_HEADER) in {None, ""}
            or provider_headers.get(PROVIDER_USER_ASSERTION_HEADER) in {None, ""}
        ):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_AUTH_UNAVAILABLE",
                "payroll provider assertion is unavailable",
            )
        try:
            value = self._transport.get_json(
                f"{self._base_url}{path}",
                timeout_seconds=self._timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
                headers=MappingProxyType(dict(provider_headers)),
            )
        except PayrollIntegrationError:
            raise
        except TimeoutError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_TIMEOUT",
                "payroll provider request timed out",
            ) from exc
        except Exception as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_UNAVAILABLE",
                "payroll provider is unavailable",
            ) from exc
        if not isinstance(value, Mapping):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            )
        return value

    def _request_post(
        self,
        path: str,
        *,
        provider_headers: Mapping[str, str],
        body: bytes,
    ) -> Mapping[str, object]:
        self._validate_provider_headers(provider_headers)
        try:
            value = self._transport.post_json(
                f"{self._base_url}{path}",
                timeout_seconds=self._timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
                headers=MappingProxyType(dict(provider_headers)),
                body=body,
            )
        except PayrollIntegrationError:
            raise
        except TimeoutError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_TIMEOUT",
                "payroll provider request timed out",
            ) from exc
        except Exception as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_UNAVAILABLE",
                "payroll provider is unavailable",
            ) from exc
        if not isinstance(value, Mapping):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            )
        return value

    @staticmethod
    def _validate_provider_headers(provider_headers: Mapping[str, str]) -> None:
        if (
            not isinstance(provider_headers, Mapping)
            or frozenset(provider_headers)
            != frozenset(
                {
                    PROVIDER_WORKLOAD_ASSERTION_HEADER,
                    PROVIDER_USER_ASSERTION_HEADER,
                }
            )
            or provider_headers.get(PROVIDER_WORKLOAD_ASSERTION_HEADER) in {None, ""}
            or provider_headers.get(PROVIDER_USER_ASSERTION_HEADER) in {None, ""}
        ):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_AUTH_UNAVAILABLE",
                "payroll provider assertion is unavailable",
            )


def _setup_summary(projection: Mapping[str, object]) -> dict[str, object]:
    materials = cast(list[Mapping[str, object]], projection["materials"])
    blocking_reason_codes: list[str] = []
    if cast(int, projection["unassigned_material_count"]) > 0:
        blocking_reason_codes.append("UNASSIGNED_MATERIALS")
    if any(item["status"] != "REVIEWED" for item in materials):
        blocking_reason_codes.append("MATERIAL_REVIEW_REQUIRED")
    if not cast(list[object], projection["batches"]):
        blocking_reason_codes.append("PAYROLL_BATCH_REQUIRED")
    if projection["live_data_ready"] is not True:
        blocking_reason_codes.append("LIVE_DATA_NOT_READY")
    return {
        "provider_connected": True,
        "runtime_mode": "live-provider",
        "unassigned_material_count": projection["unassigned_material_count"],
        "ready_material_count": sum(item["status"] == "REVIEWED" for item in materials),
        "company_mapped_material_count": len(materials),
        "blocking_reason_codes": blocking_reason_codes,
    }


class UrllibPayrollHttpTransport:
    """Small blocking JSON transport; authentication remains deployment-owned."""

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> Mapping[str, object]:
        request = Request(url, headers={"Accept": "application/json"})
        try:
            # The source constructor normalizes this to a credential-free HTTP(S) origin.
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                status = getattr(response, "status", None)
                if status != 200:
                    raise PayrollIntegrationError(
                        "PAYROLL_PROVIDER_REJECTED",
                        "payroll provider rejected the read-only request",
                    )
                content = response.read(max_bytes + 1)
        except PayrollIntegrationError:
            raise
        except TimeoutError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_TIMEOUT",
                "payroll provider request timed out",
            ) from exc
        except HTTPError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_REJECTED",
                "payroll provider rejected the read-only request",
            ) from exc
        except (URLError, OSError) as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_UNAVAILABLE",
                "payroll provider is unavailable",
            ) from exc
        if len(content) > max_bytes:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response exceeds the bounded limit",
            )
        try:
            value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            ) from exc
        if not isinstance(value, Mapping):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            )
        return value


class UrllibPayrollLiveHttpTransport:
    """Bounded protected transport dedicated to the live provider contract."""

    def get_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        return self._json_request(
            url,
            method="GET",
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            headers=headers,
            body=None,
        )

    def post_json(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Mapping[str, object]:
        return self._json_request(
            url,
            method="POST",
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            headers=headers,
            body=body,
        )

    def _json_request(
        self,
        url: str,
        *,
        method: str,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> Mapping[str, object]:
        request_headers = {"Accept": "application/json", **dict(headers)}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            # The source constructor normalizes this to a credential-free HTTP(S) origin.
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
                if getattr(response, "status", None) != 200:
                    raise PayrollIntegrationError(
                        "PAYROLL_PROVIDER_REJECTED",
                        "payroll provider rejected the protected request",
                    )
                content = response.read(max_bytes + 1)
        except PayrollIntegrationError:
            raise
        except TimeoutError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_TIMEOUT",
                "payroll provider request timed out",
            ) from exc
        except HTTPError as exc:
            code = "PAYROLL_VERSION_CONFLICT" if exc.code == 409 else "PAYROLL_PROVIDER_REJECTED"
            raise PayrollIntegrationError(code, "payroll provider rejected the request") from exc
        except (URLError, OSError) as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_UNAVAILABLE",
                "payroll provider is unavailable",
            ) from exc
        if len(content) > max_bytes:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response exceeds the bounded limit",
            )
        try:
            value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            ) from exc
        if not isinstance(value, Mapping):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            )
        return value


class _ValidatingPayrollPublicationSource:
    def __init__(
        self,
        *,
        enabled: bool,
        company_mapping: Mapping[str, UUID],
    ) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        self._companies = PayrollCompanyMap(company_mapping)
        self._idempotency: dict[
            str,
            tuple[tuple[UUID, str], PayrollPublication],
        ] = {}
        self._idempotency_lock = threading.Lock()

    def pull_publication(
        self,
        *,
        entity_ref: UUID,
        publication_id: str,
        idempotency_key: str,
    ) -> PayrollPublication:
        if not self._enabled:
            raise PayrollIntegrationError(
                "PAYROLL_INTEGRATION_DISABLED",
                "payroll publication integration is disabled",
            )
        company_id = self._companies.company_for_entity(entity_ref)
        _require_publication_id(publication_id)
        _require_stable_identifier(idempotency_key, "idempotency_key")
        fingerprint = (entity_ref, publication_id)
        replay = self._lookup_idempotency(idempotency_key, fingerprint)
        if replay is not None:
            return replay

        status = self._get_status()
        _validate_status(status)
        raw = self._get_publication(publication_id)
        publication = _validate_publication(raw, company_id=company_id, entity_ref=entity_ref)
        if publication.publication_id != publication_id:
            raise PayrollIntegrationError(
                "PAYROLL_PUBLICATION_ID_MISMATCH",
                "payroll publication does not match the requested identifier",
            )
        return self._record_idempotency(idempotency_key, fingerprint, publication)

    def _get_status(self) -> Mapping[str, object]:
        raise NotImplementedError

    def _get_publication(self, publication_id: str) -> Mapping[str, object]:
        raise NotImplementedError

    def _lookup_idempotency(
        self,
        key: str,
        fingerprint: tuple[UUID, str],
    ) -> PayrollPublication | None:
        with self._idempotency_lock:
            record = self._idempotency.get(key)
            if record is None:
                return None
            if record[0] != fingerprint:
                raise PayrollIntegrationError(
                    "PAYROLL_IDEMPOTENCY_CONFLICT",
                    "payroll idempotency key was used for a different read request",
                )
            return record[1]

    def _record_idempotency(
        self,
        key: str,
        fingerprint: tuple[UUID, str],
        publication: PayrollPublication,
    ) -> PayrollPublication:
        with self._idempotency_lock:
            record = self._idempotency.get(key)
            if record is None:
                self._idempotency[key] = (fingerprint, publication)
                return publication
            if record[0] != fingerprint:
                raise PayrollIntegrationError(
                    "PAYROLL_IDEMPOTENCY_CONFLICT",
                    "payroll idempotency key was used for a different read request",
                )
            return record[1]


class HttpPayrollPublicationSource(_ValidatingPayrollPublicationSource):
    """Read-only HTTP adapter; disabled unless the composition root enables it."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        company_mapping: Mapping[str, UUID],
        enabled: bool = False,
        transport: PayrollHttpTransport | None = None,
    ) -> None:
        super().__init__(enabled=enabled, company_mapping=company_mapping)
        self._base_url = _require_base_url(base_url)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport or UrllibPayrollHttpTransport()

    def _get_status(self) -> Mapping[str, object]:
        return self._request("/api/v1/status")

    def _get_publication(self, publication_id: str) -> Mapping[str, object]:
        encoded = quote(publication_id, safe="")
        return self._request(f"/api/v1/payroll-publications/{encoded}")

    def _request(self, path: str) -> Mapping[str, object]:
        try:
            value = self._transport.get_json(
                f"{self._base_url}{path}",
                timeout_seconds=self._timeout_seconds,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except PayrollIntegrationError:
            raise
        except TimeoutError as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_TIMEOUT",
                "payroll provider request timed out",
            ) from exc
        except Exception as exc:
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_UNAVAILABLE",
                "payroll provider is unavailable",
            ) from exc
        if not isinstance(value, Mapping):
            raise PayrollIntegrationError(
                "PAYROLL_PROVIDER_RESPONSE",
                "payroll provider response is invalid",
            )
        return value


class InMemoryPayrollPublicationSource(_ValidatingPayrollPublicationSource):
    """Deterministic adapter for consumer and route tests; never for deployment."""

    def __init__(
        self,
        *,
        status: Mapping[str, object],
        publications: Mapping[str, Mapping[str, object]],
        company_mapping: Mapping[str, UUID],
        enabled: bool = False,
    ) -> None:
        super().__init__(enabled=enabled, company_mapping=company_mapping)
        self._status = status
        self._publications = publications

    def _get_status(self) -> Mapping[str, object]:
        return self._status

    def _get_publication(self, publication_id: str) -> Mapping[str, object]:
        value = self._publications.get(publication_id)
        if value is None:
            raise PayrollIntegrationError(
                "PAYROLL_PUBLICATION_NOT_FOUND",
                "payroll publication is unavailable",
            )
        return value


def _validate_status(value: Mapping[str, object]) -> None:
    if (
        value.get("schema_version") != STATUS_SCHEMA
        or value.get("status") != "ready"
        or value.get("demo_mode") is not True
        or value.get("payment_submission_supported") is not False
    ):
        raise PayrollIntegrationError(
            "PAYROLL_STATUS_UNSAFE",
            "payroll provider status is not safe for read-only demo integration",
        )


def _validate_live_status(value: Mapping[str, object]) -> None:
    if (
        value.get("schema_version") != STATUS_SCHEMA
        or value.get("status") != "ready"
        or type(value.get("demo_mode")) is not bool
        or value.get("payment_submission_supported") is not False
        or value.get("payment_execution_supported", False) is not False
        or value.get("payable", False) is not False
    ):
        raise PayrollIntegrationError(
            "PAYROLL_STATUS_UNSAFE",
            "payroll provider status is not safe for non-payable integration",
        )


_COMMAND_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "company_id",
        "resource_id",
        "action",
        "audit_event_id",
        "audit_hash",
        "occurred_at",
        "idempotency_key",
        "replayed",
        "audit_closure",
    }
)
_COMMAND_AUDIT_FIELDS = frozenset(
    {
        "company_id",
        "resource_id",
        "action",
        "actor_subject",
        "actor_id",
        "audit_event_id",
        "audit_hash",
        "occurred_at",
    }
)


def _validate_command_receipt(
    value: Mapping[str, object],
    *,
    company_id: str,
    resource_id: str,
    action: str,
    idempotency_key: str,
) -> None:
    _require_exact_keys(value, _COMMAND_RECEIPT_FIELDS, "command receipt")
    if (
        value.get("schema_version") != "payroll-ledgerbridge-command-receipt/v1"
        or value.get("company_id") != company_id
        or value.get("resource_id") != resource_id
        or value.get("action") != action
        or value.get("idempotency_key") != idempotency_key
        or type(value.get("replayed")) is not bool
    ):
        raise PayrollIntegrationError(
            "PAYROLL_COMMAND_RECEIPT_INVALID",
            "payroll command receipt does not close the authenticated operation",
        )
    audit_event_id = _require_stable_identifier(
        value.get("audit_event_id"),
        "audit_event_id",
    )
    audit_hash = _require_sha256(value.get("audit_hash"), "audit_hash")
    occurred_at = _require_live_timestamp(value.get("occurred_at"), "occurred_at")
    closure = _require_object(value.get("audit_closure"), "audit_closure")
    _require_exact_keys(closure, _COMMAND_AUDIT_FIELDS, "audit_closure")
    if (
        closure.get("company_id") != company_id
        or closure.get("resource_id") != resource_id
        or closure.get("action") != action
        or closure.get("audit_event_id") != audit_event_id
        or closure.get("audit_hash") != audit_hash
        or closure.get("occurred_at") != occurred_at
    ):
        raise PayrollIntegrationError(
            "PAYROLL_AUDIT_PROOF_INVALID",
            "payroll command audit closure is incomplete",
        )
    _require_stable_identifier(closure.get("actor_subject"), "actor_subject")
    _require_stable_identifier(closure.get("actor_id"), "actor_id")


def _validate_live_projection(
    value: Mapping[str, object],
    *,
    expected_company_id: str,
) -> None:
    """Validate the provider's frozen, company-scoped integration projection."""

    _require_exact_keys(value, _LIVE_TOP_LEVEL_FIELDS, "live projection")
    if (
        value.get("contract_version") != LIVE_PROJECTION_CONTRACT_VERSION
        or value.get("schema_version") != LIVE_PROJECTION_SCHEMA
    ):
        raise PayrollIntegrationError(
            "PAYROLL_SCHEMA_UNSUPPORTED",
            "payroll live projection contract is unsupported",
        )
    company_id = _require_stable_identifier(value.get("company_id"), "company_id")
    if company_id != expected_company_id or company_id == "UNASSIGNED":
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll live projection company does not match the authenticated entity grant",
        )
    revision = _require_sha256(value.get("projection_revision"), "projection_revision")
    if value.get("etag") != f'"{revision}"':
        _invalid_response("payroll projection etag does not match its revision")
    _require_live_timestamp(value.get("generated_at"), "generated_at")
    if type(value.get("live_data_ready")) is not bool:
        _invalid_response("payroll live readiness is invalid")
    for field in ("payable", "submission_supported", "payment_submission_supported"):
        if value.get(field) is not False:
            raise PayrollIntegrationError(
                "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
                "payroll live projection exposed a payment or submission capability",
            )
    capabilities = _require_object(value.get("server_capabilities"), "server_capabilities")
    _require_exact_keys(capabilities, _LIVE_CAPABILITY_FIELDS, "server_capabilities")
    for field in _LIVE_CAPABILITY_FIELDS:
        if type(capabilities.get(field)) is not bool:
            _invalid_response("payroll provider capability is invalid")
    if capabilities["payment_submission"] is not False:
        raise PayrollIntegrationError(
            "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
            "payroll provider exposed payment submission",
        )
    _require_non_negative_integer(
        value.get("unassigned_material_count"),
        "unassigned_material_count",
    )

    materials = _require_list(value.get("materials"), "materials")
    for index, raw in enumerate(materials):
        item = _company_item(raw, company_id, _LIVE_MATERIAL_FIELDS, f"materials[{index}]")
        _require_stable_identifier(item.get("material_id"), "material_id")
        if item.get("material_type") is not None:
            _require_controlled_token(item.get("material_type"), "material.material_type")
        if item.get("period") is not None:
            _require_period(item.get("period"), "material.period")
        _require_controlled_token(item.get("status"), "material.status")
        _require_non_negative_integer(item.get("review_revision"), "material.review_revision")
        _require_disabled_flags(item, ("payable", "submission_supported"))

    batches = _require_list(value.get("batches"), "batches")
    for index, raw in enumerate(batches):
        item = _require_object(raw, f"batches[{index}]")
        if frozenset(item) not in {_LIVE_BATCH_REQUIRED_FIELDS, _LIVE_BATCH_FIELDS}:
            _invalid_response("payroll batch contains missing or unknown fields")
        _require_company(item, company_id)
        _require_stable_identifier(item.get("batch_id"), "batch_id")
        _require_period(item.get("pay_period"), "batch.pay_period")
        _require_positive_integer(item.get("revision"), "batch.revision")
        _require_controlled_token(item.get("status"), "batch.status")
        _require_disabled_flags(
            item,
            ("payable", "submission_supported", "payment_submission_supported"),
        )
        for line_index, line_raw in enumerate(_require_list(item.get("lines"), "batch.lines")):
            line = _company_item(
                line_raw,
                company_id,
                _LIVE_LINE_FIELDS,
                f"batches[{index}].lines[{line_index}]",
            )
            for field in ("employee_id", "account_id"):
                _require_stable_identifier(line.get(field), field)
            _require_safe_display(line.get("employee_display"), "employee_display")
            _require_safe_display(line.get("account_display"), "account_display")
            _require_minor_integer(line.get("net_pay_minor"), "net_pay_minor")
        if "audit_closure" in item:
            closure = _require_object(item["audit_closure"], "batch.audit_closure")
            _require_exact_keys(closure, _LIVE_AUDIT_CLOSURE_FIELDS, "batch.audit_closure")
            _require_stable_identifier(closure.get("audit_event_id"), "audit_event_id")
            _require_sha256(closure.get("audit_hash"), "audit_hash")

    verifications = _require_list(value.get("verifications"), "verifications")
    for index, raw in enumerate(verifications):
        item = _company_item(
            raw,
            company_id,
            _LIVE_VERIFICATION_FIELDS,
            f"verifications[{index}]",
        )
        for field in ("verification_id", "batch_id"):
            _require_stable_identifier(item.get(field), field)
        _require_controlled_token(item.get("status"), "verification.status")
        _require_disabled_flags(
            item,
            ("payable", "submission_supported", "payment_submission_supported"),
        )
        for artifact_id in _require_list(item.get("source_artifact_ids"), "source_artifact_ids"):
            _require_non_demo_identifier(artifact_id, "source_artifact_id")
        for result_index, result_raw in enumerate(
            _require_list(item.get("results"), "verification.results")
        ):
            result = _company_item(
                result_raw,
                company_id,
                _LIVE_VERIFICATION_RESULT_FIELDS,
                f"verifications[{index}].results[{result_index}]",
            )
            for field in ("employee_id", "account_id"):
                _require_stable_identifier(result.get(field), field)
            _require_safe_display(result.get("employee_display"), "employee_display")
            _require_safe_display(result.get("account_display"), "account_display")
            _require_controlled_token(result.get("status"), "verification.result.status")

    for index, raw in enumerate(_require_list(value.get("resources"), "resources")):
        item = _company_item(raw, company_id, _LIVE_RESOURCE_FIELDS, f"resources[{index}]")
        for field in ("employee_id", "account_id"):
            _require_stable_identifier(item.get(field), field)
        _require_safe_display(item.get("employee_display"), "employee_display")
        _require_safe_display(item.get("account_display"), "account_display")

    for index, raw in enumerate(
        _require_list(value.get("available_evidence"), "available_evidence")
    ):
        item = _company_item(
            raw,
            company_id,
            _LIVE_EVIDENCE_FIELDS,
            f"available_evidence[{index}]",
        )
        artifact_id = _require_non_demo_identifier(item.get("artifact_id"), "artifact_id")
        if artifact_id.startswith("receipt_demo_"):
            _invalid_response("demo verification evidence is not allowed")
        period = _require_period(item.get("period"), "evidence.period")
        evidence_type = item.get("evidence_type")
        if evidence_type not in _LIVE_EVIDENCE_TYPES or item.get("status") != "READY_FOR_MATCHING":
            _invalid_response("payroll available evidence is invalid")
        if item.get("display_label") != f"{evidence_type} · {period}":
            _invalid_response("payroll available evidence label is not provider-controlled")
    _validate_publication_tree(value, expected_company_id=company_id)


def _company_item(
    raw: object,
    company_id: str,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    item = _require_object(raw, label)
    _require_exact_keys(item, fields, label)
    _require_company(item, company_id)
    return item


def _require_company(value: Mapping[str, object], company_id: str) -> None:
    if value.get("company_id") != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll projection object crosses company scope",
        )


def _require_disabled_flags(value: Mapping[str, object], fields: tuple[str, ...]) -> None:
    if any(value.get(field) is not False for field in fields):
        raise PayrollIntegrationError(
            "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
            "payroll projection object exposed payment or submission capability",
        )


def _require_non_demo_identifier(value: object, field: str) -> str:
    identifier = _require_stable_identifier(value, field)
    if identifier.startswith(("artifact_demo_", "receipt_demo_")):
        raise PayrollIntegrationError(
            "PAYROLL_DEMO_DATA_NOT_ALLOWED",
            "demo payroll evidence is not allowed",
        )
    return identifier


def _require_safe_display(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        _invalid_response(f"payroll {field} is invalid")
    if _LOCAL_PATH.search(value) or _ACCOUNT_LIKE_NUMBER.search(value):
        _invalid_response(f"payroll {field} is unsafe")
    return value


def _validate_live_projection_legacy(
    value: Mapping[str, object],
    *,
    expected_company_id: str,
) -> None:
    _require_exact_keys(value, _LIVE_TOP_LEVEL_FIELDS, "live projection")
    if value.get("schema_version") != LIVE_PROJECTION_SCHEMA:
        raise PayrollIntegrationError(
            "PAYROLL_SCHEMA_UNSUPPORTED",
            "payroll live projection schema is unsupported",
        )
    company_id = _require_stable_identifier(value.get("company_id"), "company_id")
    if company_id != expected_company_id or company_id == "UNASSIGNED":
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll live projection company does not match the authenticated entity grant",
        )
    _require_positive_integer(value.get("projection_revision"), "projection_revision")
    _require_sha256(value.get("etag"), "etag")
    _require_live_timestamp(value.get("generated_at"), "generated_at")
    if value.get("live_data_ready") is not True:
        raise PayrollIntegrationError(
            "PAYROLL_LIVE_DATA_UNAVAILABLE",
            "payroll provider has not made a live projection available",
        )
    for field in ("payable", "payment_submission_supported", "payment_execution_supported"):
        if value.get(field) is not False:
            raise PayrollIntegrationError(
                "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
                "payroll live projection exposed a payment or submission capability",
            )

    dashboard = _require_object(value.get("dashboard"), "dashboard")
    _require_exact_keys(dashboard, _LIVE_DASHBOARD_FIELDS, "dashboard")
    if (
        dashboard.get("schema_version") != "payroll-live-dashboard/v1"
        or dashboard.get("company_id") != company_id
    ):
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll dashboard does not match the authenticated company",
        )
    for field in (
        "batch_count",
        "material_count",
        "materials_needing_review_count",
        "verification_attention_count",
        "unassigned_material_count",
    ):
        _require_non_negative_integer(dashboard.get(field), f"dashboard.{field}")
    for field in ("gross_pay_minor", "net_pay_minor"):
        _require_minor_integer(dashboard.get(field), f"dashboard.{field}")

    materials = _require_list(value.get("materials"), "materials")
    for index, item in enumerate(materials):
        material = _require_object(item, f"materials[{index}]")
        _require_exact_keys(material, _LIVE_MATERIAL_FIELDS, f"materials[{index}]")
        if (
            material.get("schema_version") != "payroll-live-material/v1"
            or material.get("company_id") != company_id
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll material does not match the authenticated company",
            )
        _require_stable_identifier(material.get("material_id"), "material_id")
        _require_sha256(material.get("sha256"), "material.sha256")
        _require_non_negative_integer(material.get("size_bytes"), "material.size_bytes")
        _require_period(material.get("period"), "material.period")
        _require_controlled_token(material.get("material_type"), "material.material_type")
        _require_controlled_token(material.get("status"), "material.status")
        _require_non_negative_integer(material.get("review_revision"), "material.review_revision")
        last_reviewed_at = material.get("last_reviewed_at")
        if last_reviewed_at is not None:
            _require_live_timestamp(last_reviewed_at, "material.last_reviewed_at")
        if (
            material.get("adoption_eligible") is not False
            or material.get("payment_submission_supported") is not False
        ):
            raise PayrollIntegrationError(
                "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
                "payroll material exposed adoption or payment capability",
            )

    batches = _require_list(value.get("batches"), "batches")
    for index, item in enumerate(batches):
        batch = _require_object(item, f"batches[{index}]")
        _require_exact_keys(batch, _LIVE_BATCH_FIELDS, f"batches[{index}]")
        if (
            batch.get("schema_version") != "payroll-live-batch/v1"
            or batch.get("company_id") != company_id
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll batch does not match the authenticated company",
            )
        _require_stable_identifier(batch.get("batch_id"), "batch_id")
        _require_period(batch.get("pay_period"), "batch.pay_period")
        version = _require_positive_integer(batch.get("version"), "batch.version")
        locked_version = batch.get("locked_version")
        if locked_version is not None and (
            _require_positive_integer(locked_version, "batch.locked_version") > version
        ):
            _invalid_response("payroll locked batch version exceeds the current version")
        if batch.get("status") not in {
            "draft",
            "submitted_for_review",
            "reviewed",
            "approved",
        }:
            _invalid_response("payroll batch status is invalid")
        _require_non_negative_integer(batch.get("employee_count"), "batch.employee_count")
        _require_minor_integer(batch.get("gross_pay_minor"), "batch.gross_pay_minor")
        _require_minor_integer(batch.get("net_pay_minor"), "batch.net_pay_minor")
        _require_non_negative_integer(
            batch.get("active_exception_count"),
            "batch.active_exception_count",
        )
        actors = []
        for field in ("maker_actor_id", "checker_actor_id", "approver_actor_id"):
            actor = batch.get(field)
            if actor is not None:
                actors.append(_require_stable_identifier(actor, field))
        if len(actors) != len(set(actors)):
            raise PayrollIntegrationError(
                "PAYROLL_DUTY_SEPARATION_INVALID",
                "payroll batch assigns more than one workflow duty to the same actor",
            )

    available_evidence = _require_list(value.get("available_evidence"), "available_evidence")
    available_artifact_ids: set[str] = set()
    for index, item in enumerate(available_evidence):
        evidence = _require_object(item, f"available_evidence[{index}]")
        _require_exact_keys(evidence, _LIVE_EVIDENCE_FIELDS, f"available_evidence[{index}]")
        if evidence.get("company_id") != company_id:
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll verification evidence crosses company scope",
            )
        artifact_id = _require_stable_identifier(evidence.get("artifact_id"), "artifact_id")
        if artifact_id.startswith("artifact_demo_") or artifact_id in available_artifact_ids:
            _invalid_response("payroll verification evidence identifier is invalid")
        available_artifact_ids.add(artifact_id)
        _require_period(evidence.get("period"), "evidence.period")
        if evidence.get("evidence_type") not in _LIVE_EVIDENCE_TYPES:
            _invalid_response("payroll verification evidence type is unsupported")
        if evidence.get("status") != "READY_FOR_MATCHING":
            _invalid_response("payroll verification evidence is not ready for matching")
        _require_safe_display_label(evidence.get("display_label"))

    verifications = _require_list(value.get("verification_results"), "verification_results")
    batch_by_id = {
        cast(str, cast(Mapping[str, object], item)["batch_id"]): cast(Mapping[str, object], item)
        for item in batches
    }
    attention_verification_count = 0
    for index, item in enumerate(verifications):
        verification = _require_object(item, f"verification_results[{index}]")
        _require_exact_keys(
            verification,
            _LIVE_VERIFICATION_FIELDS,
            f"verification_results[{index}]",
        )
        if (
            verification.get("schema_version") != "payroll-receipt-verification/v1"
            or verification.get("company_id") != company_id
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll verification does not match the authenticated company",
            )
        verification_id = _require_stable_identifier(
            verification.get("verification_id"),
            "verification_id",
        )
        batch_id = _require_stable_identifier(verification.get("batch_id"), "batch_id")
        batch = batch_by_id.get(batch_id)  # type: ignore[assignment]
        if (
            batch is None
            or verification.get("pay_period") != batch.get("pay_period")
            or verification.get("version") != batch.get("version")
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll verification does not match a projected batch version",
            )
        source_artifact_ids = _require_list(
            verification.get("source_artifact_ids"),
            "verification.source_artifact_ids",
        )
        if not source_artifact_ids:
            _invalid_response("payroll verification has no source evidence")
        normalized_artifact_ids = [
            _require_stable_identifier(item, "source_artifact_id") for item in source_artifact_ids
        ]
        if (
            len(normalized_artifact_ids) != len(set(normalized_artifact_ids))
            or any(item.startswith("artifact_demo_") for item in normalized_artifact_ids)
            or any(item not in available_artifact_ids for item in normalized_artifact_ids)
        ):
            _invalid_response("payroll verification source evidence is invalid")
        overall_status = verification.get("overall_status")
        if overall_status not in {"matched", "attention_required"}:
            _invalid_response("payroll verification status is invalid")
        unknown_count = _require_non_negative_integer(
            verification.get("unknown_receipt_count"),
            "verification.unknown_receipt_count",
        )
        results = _require_list(verification.get("results"), "verification.results")
        if not results:
            _invalid_response("payroll verification has no employee results")
        seen_employees: set[str] = set()
        attention_result_count = 0
        for result_index, result_item in enumerate(results):
            result = _require_object(
                result_item,
                f"verification.results[{result_index}]",
            )
            _require_exact_keys(
                result,
                _LIVE_VERIFICATION_RESULT_FIELDS,
                f"verification.results[{result_index}]",
            )
            if result.get("company_id") != company_id:
                raise PayrollIntegrationError(
                    "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                    "payroll verification result crosses company scope",
                )
            employee_id = _require_stable_identifier(
                result.get("employee_id"),
                "employee_id",
            )
            if employee_id in seen_employees:
                _invalid_response("payroll verification repeats an employee")
            seen_employees.add(employee_id)
            _require_stable_identifier(result.get("account_id"), "account_id", account=True)
            _require_minor_integer(
                result.get("expected_amount_minor"),
                "verification.expected_amount_minor",
            )
            match_status = result.get("match_status")
            exception_codes = _require_list(
                result.get("exception_codes"),
                "verification.exception_codes",
            )
            for code in exception_codes:
                _require_controlled_token(code, "verification.exception_code")
            if match_status == "matched":
                if exception_codes:
                    _invalid_response("matched payroll verification contains exceptions")
            elif match_status == "attention_required":
                attention_result_count += 1
                if not exception_codes:
                    _invalid_response("attention payroll verification has no exception code")
            else:
                _invalid_response("payroll verification result status is invalid")
        expected_overall = (
            "attention_required" if attention_result_count or unknown_count else "matched"
        )
        if overall_status != expected_overall:
            _invalid_response("payroll verification aggregate status is inconsistent")
        if overall_status == "attention_required":
            attention_verification_count += 1

        audit = _require_object(verification.get("audit_receipt"), "verification.audit_receipt")
        _require_exact_keys(audit, _LIVE_VERIFICATION_AUDIT_FIELDS, "verification.audit_receipt")
        if (
            audit.get("schema_version") != "payroll-verification-audit-receipt/v1"
            or audit.get("company_id") != company_id
            or audit.get("batch_id") != batch_id
            or audit.get("verification_id") != verification_id
            or audit.get("action") != "payroll.receipts_verified"
        ):
            raise PayrollIntegrationError(
                "PAYROLL_AUDIT_PROOF_INVALID",
                "payroll verification audit receipt does not close the projected result",
            )
        _require_stable_identifier(audit.get("actor_id"), "audit.actor_id")
        _require_live_timestamp(audit.get("occurred_at"), "audit.occurred_at")
        _require_sha256(audit.get("event_hash"), "audit.event_hash", audit=True)

    if dashboard.get("verification_attention_count") != attention_verification_count:
        _invalid_response("payroll dashboard verification count is inconsistent")


def _require_exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    field: str,
) -> None:
    if frozenset(value) != expected:
        _invalid_response(f"payroll {field} fields do not match the v1 allowlist")


def _require_period(value: object, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value) is None:
        _invalid_response(f"payroll {field} is invalid")
    return value


def _require_live_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid_response(f"payroll {field} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid_response(f"payroll {field} is invalid")
    if parsed.utcoffset() is None:
        _invalid_response(f"payroll {field} is not a UTC timestamp")
    return value


def _require_safe_display_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 80
        or "/" in value
        or "\\" in value
        or re.search(r"\.(?:csv|xlsx?|pdf|png|jpe?g|zip)$", value, re.I) is not None
    ):
        _invalid_response("payroll evidence display label is not server-controlled text")
    _validate_publishable_string(value, parent_key="display_label")
    return value


def _validate_publication(
    raw: Mapping[str, object],
    *,
    company_id: str,
    entity_ref: UUID,
) -> PayrollPublication:
    _require_required_keys(raw, _TOP_LEVEL_FIELDS, "publication")
    if raw.get("schema_version") != PUBLICATION_SCHEMA:
        raise PayrollIntegrationError(
            "PAYROLL_SCHEMA_UNSUPPORTED",
            "payroll publication schema is unsupported",
        )
    publication_id = _require_publication_id(raw.get("publication_id"))
    if not isinstance(raw.get("published_at"), str) or not raw["published_at"]:
        _invalid_response("payroll publication timestamp is invalid")

    scope = _require_object(raw.get("scope"), "scope")
    safety = _require_object(raw.get("safety"), "safety")
    batch = _require_object(raw.get("payroll_batch"), "payroll_batch")
    proof = _require_object(raw.get("audit_chain_proof"), "audit_chain_proof")
    _require_required_keys(scope, _SCOPE_FIELDS, "scope")
    _require_required_keys(safety, _SAFETY_FIELDS, "safety")
    _require_required_keys(batch, _BATCH_FIELDS, "payroll_batch")
    _require_required_keys(proof, _PROOF_FIELDS, "audit_chain_proof")
    _validate_publication_tree(raw, expected_company_id=company_id)

    if scope.get("company_id") != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll publication company does not match the configured entity mapping",
        )
    if (
        safety.get("purpose") != "ACCOUNTING_AND_RECONCILIATION_ONLY"
        or safety.get("payable") is not False
        or safety.get("payment_submission_supported") is not False
        or safety.get("payment_execution_supported") is not False
    ):
        raise PayrollIntegrationError(
            "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
            "payroll publication is not explicitly read-only and non-payable",
        )

    if batch.get("schema_version") != "payroll-batch-export/v1":
        raise PayrollIntegrationError(
            "PAYROLL_SCHEMA_UNSUPPORTED",
            "payroll batch schema is unsupported",
        )
    if batch.get("company_id") != company_id:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll batch company does not match the configured entity mapping",
        )
    batch_id = _require_stable_identifier(batch.get("batch_id"), "batch_id")
    pay_period = batch.get("pay_period")
    if (
        not isinstance(pay_period, str)
        or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", pay_period) is None
    ):
        _invalid_response("payroll period is invalid")
    version = _require_positive_integer(batch.get("version"), "version")
    locked_version = _require_positive_integer(batch.get("locked_version"), "locked_version")
    if batch.get("status") != "approved" or version != locked_version:
        raise PayrollIntegrationError(
            "PAYROLL_BATCH_NOT_LOCKED",
            "payroll publication does not contain an approved locked batch",
        )
    if (
        scope.get("batch_id") != batch_id
        or scope.get("pay_period") != pay_period
        or scope.get("locked_version") != locked_version
    ):
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_SCOPE_MISMATCH",
            "payroll publication scope does not match its locked batch",
        )

    lines = _require_list(batch.get("lines"), "payroll_batch.lines")
    exceptions = _require_list(batch.get("exceptions"), "payroll_batch.exceptions")
    verification_results = _require_list(raw.get("verification_results"), "verification_results")
    material_summaries = _require_list(raw.get("material_summaries"), "material_summaries")
    audit_events = _require_list(raw.get("audit_events"), "audit_events")

    identities: list[tuple[str, str]] = []
    identity_index: dict[str, tuple[str, int]] = {}
    for index, item in enumerate(lines):
        line = _require_object(item, f"payroll_batch.lines[{index}]")
        _require_required_keys(line, _LINE_FIELDS, f"payroll_batch.lines[{index}]")
        if line.get("company_id") != company_id:
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll line company does not match the configured entity mapping",
            )
        employee_id = _require_stable_identifier(line.get("employee_id"), "employee_id")
        account_id = _require_stable_identifier(line.get("account_id"), "account_id", account=True)
        if employee_id in identity_index:
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_MAPPING_CONFLICT",
                "payroll employee mapping is duplicated",
            )
        _require_non_negative_integer(line.get("gross_pay_minor"), "gross_pay_minor")
        net_pay_minor = _require_non_negative_integer(
            line.get("net_pay_minor"),
            "net_pay_minor",
        )
        identity_index[employee_id] = (account_id, net_pay_minor)
        identities.append((employee_id, account_id))
        if line.get("disbursement_channel") not in _ALLOWED_CHANNELS:
            _invalid_response("payroll disbursement channel is invalid")

    _validate_batch_exceptions(exceptions)
    verification_proofs = _validate_verifications(
        verification_results,
        batch=batch,
        identities=identity_index,
    )
    _validate_material_summaries(material_summaries, batch=batch)
    _validate_audit_events(
        audit_events,
        batch=batch,
        verification_proofs=verification_proofs,
    )
    _verify_payload_integrity(
        raw,
        publication_id=publication_id,
        verification_results=verification_results,
        material_summaries=material_summaries,
        audit_events=audit_events,
    )
    _verify_audit_proof(proof, audit_events)
    return PayrollPublication(
        publication_id=publication_id,
        company_id=company_id,
        entity_ref=entity_ref,
        batch_id=batch_id,
        pay_period=pay_period,
        employee_account_ids=tuple(identities),
        payload=cast(Mapping[str, object], _deep_freeze(raw)),
    )


def _validate_batch_exceptions(exceptions: list[object]) -> None:
    for index, item in enumerate(exceptions):
        field = f"payroll_batch.exceptions[{index}]"
        exception = _require_object(item, field)
        _require_required_keys(exception, _BATCH_EXCEPTION_FIELDS, field)
        _require_stable_identifier(exception.get("exception_id"), "exception_id")
        _require_controlled_token(exception.get("code"), f"{field}.code")
        status = _require_controlled_token(exception.get("status"), f"{field}.status")
        resolved = exception.get("resolved")
        if resolved is not None and not isinstance(resolved, bool):
            _invalid_response("payroll exception resolved flag is invalid")
        if status != "RESOLVED" or resolved is False:
            raise PayrollIntegrationError(
                "PAYROLL_BATCH_NOT_LOCKED",
                "payroll publication contains an unresolved exception",
            )


def _validate_verifications(
    verifications: list[object],
    *,
    batch: Mapping[str, object],
    identities: Mapping[str, tuple[str, int]],
) -> tuple[_VerificationProof, ...]:
    batch_company_id = cast(str, batch["company_id"])
    batch_id = cast(str, batch["batch_id"])
    pay_period = cast(str, batch["pay_period"])
    locked_version = cast(int, batch["locked_version"])
    proofs: list[_VerificationProof] = []
    verification_ids: set[str] = set()
    verified_employees: set[str] = set()

    for verification_index, item in enumerate(verifications):
        field = f"verification_results[{verification_index}]"
        verification = _require_object(item, field)
        _require_required_keys(verification, _VERIFICATION_FIELDS, field)
        if verification.get("schema_version") != "payroll-receipt-verification/v1":
            raise PayrollIntegrationError(
                "PAYROLL_SCHEMA_UNSUPPORTED",
                "payroll verification schema is unsupported",
            )
        if (
            verification.get("company_id") != batch_company_id
            or verification.get("batch_id") != batch_id
            or verification.get("pay_period") != pay_period
            or verification.get("version") != locked_version
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll verification does not match the locked batch scope",
            )
        verification_id = _require_stable_identifier(
            verification.get("verification_id"),
            "verification_id",
        )
        if verification_id in verification_ids:
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_MAPPING_CONFLICT",
                "payroll verification identifier is duplicated",
            )
        verification_ids.add(verification_id)
        source_artifact_id = _require_stable_identifier(
            verification.get("source_artifact_id"),
            "source_artifact_id",
        )
        overall_status = verification.get("overall_status")
        if overall_status not in {"matched", "attention_required"}:
            _invalid_response("payroll verification status is invalid")
        unknown_receipt_count = _require_non_negative_integer(
            verification.get("unknown_receipt_count"),
            "unknown_receipt_count",
        )
        results = _require_list(verification.get("results"), f"{field}.results")
        if not results:
            _invalid_response("payroll verification results cannot be empty")

        matched_count = 0
        attention_count = 0
        for result_index, result_item in enumerate(results):
            result_field = f"{field}.results[{result_index}]"
            result = _require_object(result_item, result_field)
            _require_required_keys(result, _VERIFICATION_RESULT_FIELDS, result_field)
            if result.get("company_id") != batch_company_id:
                raise PayrollIntegrationError(
                    "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                    "payroll verification result crosses company scope",
                )
            employee_id = _require_stable_identifier(result.get("employee_id"), "employee_id")
            account_id = _require_stable_identifier(
                result.get("account_id"),
                "account_id",
                account=True,
            )
            locked_identity = identities.get(employee_id)
            if locked_identity is None or locked_identity[0] != account_id:
                raise PayrollIntegrationError(
                    "PAYROLL_IDENTITY_MAPPING_CONFLICT",
                    "payroll verification identity does not match the locked batch",
                )
            if employee_id in verified_employees:
                raise PayrollIntegrationError(
                    "PAYROLL_IDENTITY_MAPPING_CONFLICT",
                    "payroll employee verification is duplicated",
                )
            verified_employees.add(employee_id)
            expected_amount_minor = _require_non_negative_integer(
                result.get("expected_amount_minor"),
                "expected_amount_minor",
            )
            if expected_amount_minor != locked_identity[1]:
                raise PayrollIntegrationError(
                    "PAYROLL_VERIFICATION_AMOUNT_MISMATCH",
                    "payroll verification amount does not match locked net pay",
                )
            match_status = result.get("match_status")
            if match_status not in {"matched", "attention_required"}:
                _invalid_response("payroll verification match status is invalid")
            exception_codes = _require_list(
                result.get("exception_codes"),
                f"{result_field}.exception_codes",
            )
            for code in exception_codes:
                _require_controlled_token(code, "exception_code")
            expects_attention = bool(exception_codes)
            if (match_status == "matched" and expects_attention) or (
                match_status == "attention_required" and not expects_attention
            ):
                _invalid_response("payroll verification status contradicts its exceptions")
            if match_status == "matched":
                matched_count += 1
            else:
                attention_count += 1

        derived_status = (
            "attention_required" if attention_count > 0 or unknown_receipt_count > 0 else "matched"
        )
        if overall_status != derived_status:
            _invalid_response("payroll verification overall status is inconsistent")
        proofs.append(
            _VerificationProof(
                verification_id=verification_id,
                source_artifact_id=source_artifact_id,
                overall_status=overall_status,
                matched_count=matched_count,
                attention_count=attention_count,
                unknown_receipt_count=unknown_receipt_count,
            )
        )

    if verifications and verified_employees != set(identities):
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_MAPPING_CONFLICT",
            "payroll verifications do not cover each locked employee exactly once",
        )
    return tuple(proofs)


def _validate_material_summaries(
    materials: list[object],
    *,
    batch: Mapping[str, object],
) -> None:
    batch_company_id = cast(str, batch["company_id"])
    for index, item in enumerate(materials):
        field = f"material_summaries[{index}]"
        material = _require_object(item, field)
        _require_required_keys(material, _MATERIAL_FIELDS, field)
        if material.get("schema_version") != "payroll-material-summary/v1":
            raise PayrollIntegrationError(
                "PAYROLL_SCHEMA_UNSUPPORTED",
                "payroll material summary schema is unsupported",
            )
        if material.get("company_id") != batch_company_id:
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll material summary does not match the locked batch company",
            )
        period = material.get("period")
        if not isinstance(period, str) or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period) is None:
            _invalid_response("payroll material summary period is invalid")
        _require_stable_identifier(material.get("artifact_id"), "artifact_id")
        for token_field in ("kind", "source", "status"):
            _require_controlled_token(material.get(token_field), f"{field}.{token_field}")
        _require_sha256(material.get("sha256"), f"{field}.sha256")


def _validate_audit_events(
    events: list[object],
    *,
    batch: Mapping[str, object],
    verification_proofs: tuple[_VerificationProof, ...],
) -> None:
    if not events:
        _invalid_audit("payroll audit chain cannot be empty")

    batch_company_id = cast(str, batch["company_id"])
    batch_id = cast(str, batch["batch_id"])
    locked_version = cast(int, batch["locked_version"])
    previous_hash: str | None = None
    validated_events: list[Mapping[str, object]] = []

    for index, item in enumerate(events):
        field = f"audit_events[{index}]"
        event = _require_object(item, field)
        _require_required_keys(event, _AUDIT_EVENT_FIELDS, field)
        if event.get("schema_version") != "payroll-audit-event/v1":
            raise PayrollIntegrationError(
                "PAYROLL_SCHEMA_UNSUPPORTED",
                "payroll audit event schema is unsupported",
            )
        if (
            event.get("company_id") != batch_company_id
            or event.get("batch_id") != batch_id
            or event.get("version") != locked_version
        ):
            raise PayrollIntegrationError(
                "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                "payroll audit event does not match the locked batch scope",
            )
        if event.get("sequence") != index + 1 or event.get("previous_hash") != previous_hash:
            _invalid_audit("payroll audit sequence or previous hash is invalid")
        _require_audit_timestamp(event.get("occurred_at"), f"{field}.occurred_at")
        _require_stable_identifier(event.get("actor_id"), "actor_id")
        if event.get("reason") is not None:
            _invalid_audit("payroll audit free text cannot be published")
        action = event.get("action")
        if not isinstance(action, str) or action not in _AUDIT_ACTION_DATA_FIELDS:
            _invalid_audit("payroll audit action is unsupported")
        data = _require_object(event.get("data"), f"{field}.data")
        expected_data_fields = _AUDIT_ACTION_DATA_FIELDS[action]
        if set(data) != expected_data_fields:
            _invalid_audit("payroll audit action data does not match its strict v1 whitelist")
        for key, value in data.items():
            if key.endswith("_hash"):
                _require_sha256(value, f"{field}.data.{key}", audit=True)
            if key.endswith("_id"):
                _require_stable_identifier(value, key)
            if key.endswith("_count"):
                _require_non_negative_integer(value, key)
        if any(data.get(key) is not True for key in _AUDIT_REQUIRED_TRUE_FIELDS.get(action, ())):
            _invalid_audit("payroll audit confirmation facts must be strict true booleans")
        if action == "payroll.version_approved_locked" and (
            data.get("locked_version") != locked_version or data.get("active_exception_count") != 0
        ):
            _invalid_audit("payroll approval does not bind the locked exception-free version")
        if action == "payroll.bank_draft_exported":
            if data.get("payable") is not False or data.get("submission_supported") is not False:
                raise PayrollIntegrationError(
                    "PAYROLL_PAYMENT_MODE_NOT_ALLOWED",
                    "payroll audit draft facts are not explicitly non-payable",
                )
            if data.get("draft_type") not in _ALLOWED_DRAFT_TYPES:
                _invalid_audit("payroll audit draft type is unsupported")
        if action == "payroll.receipts_verified" and data.get("overall_status") not in {
            "matched",
            "attention_required",
        }:
            _invalid_audit("payroll audit receipt status is unsupported")

        current_hash = _require_sha256(event.get("hash"), f"{field}.hash", audit=True)
        unsigned = dict(event)
        del unsigned["hash"]
        expected_hash = hashlib.sha256(_stable_json(unsigned).encode("utf-8")).hexdigest()
        if current_hash != expected_hash:
            _invalid_audit("payroll audit event hash is invalid")
        previous_hash = current_hash
        validated_events.append(event)

    approval_indexes = [
        index
        for index, event in enumerate(validated_events)
        if event.get("action") == "payroll.version_approved_locked"
    ]
    if len(approval_indexes) != 1:
        _invalid_audit("payroll audit chain must contain exactly one locked approval")

    submitted_index = _find_audit_action(validated_events, "payroll.review_submitted")
    reviewed_index = _find_audit_action(
        validated_events,
        "payroll.review_completed",
        after=submitted_index,
    )
    approved_index = _find_audit_action(
        validated_events,
        "payroll.version_approved_locked",
        after=reviewed_index,
    )
    if submitted_index < 0 or reviewed_index < 0 or approved_index < 0:
        _invalid_audit("payroll audit chain does not prove ordered review and approval")
    actors = {
        cast(str, validated_events[submitted_index]["actor_id"]),
        cast(str, validated_events[reviewed_index]["actor_id"]),
        cast(str, validated_events[approved_index]["actor_id"]),
    }
    if len(actors) != 3:
        _invalid_audit("payroll audit chain does not prove three-role separation")
    approval_data = cast(Mapping[str, object], validated_events[approved_index]["data"])
    locked_batch_sha256 = hashlib.sha256(_stable_json(batch).encode("utf-8")).hexdigest()
    if approval_data.get("locked_batch_sha256") != locked_batch_sha256:
        _invalid_audit("payroll approval does not bind the published locked batch")

    receipt_events = [
        event for event in validated_events if event.get("action") == "payroll.receipts_verified"
    ]
    if len(receipt_events) != len(verification_proofs):
        _invalid_audit("payroll verification and receipt audit counts do not match")
    matched_receipts: set[int] = set()
    for proof in verification_proofs:
        matches = [
            (index, event)
            for index, event in enumerate(receipt_events)
            if cast(Mapping[str, object], event["data"]).get("verification_id")
            == proof.verification_id
        ]
        if len(matches) != 1:
            _invalid_audit("payroll verification does not have exactly one receipt audit event")
        receipt_index, receipt_event = matches[0]
        receipt_data = cast(Mapping[str, object], receipt_event["data"])
        expected = {
            "source_artifact_id": proof.source_artifact_id,
            "overall_status": proof.overall_status,
            "matched_count": proof.matched_count,
            "attention_count": proof.attention_count,
            "unknown_receipt_count": proof.unknown_receipt_count,
        }
        if any(receipt_data.get(key) != value for key, value in expected.items()):
            _invalid_audit("payroll receipt audit facts do not match verification results")
        matched_receipts.add(receipt_index)
    if len(matched_receipts) != len(receipt_events):
        _invalid_audit("payroll receipt audit events are duplicated or unreferenced")


def _find_audit_action(
    events: list[Mapping[str, object]],
    action: str,
    *,
    after: int = -1,
) -> int:
    return next(
        (
            index
            for index, event in enumerate(events)
            if index > after and event.get("action") == action
        ),
        -1,
    )


def _verify_payload_integrity(
    raw: Mapping[str, object],
    *,
    publication_id: str,
    verification_results: list[object],
    material_summaries: list[object],
    audit_events: list[object],
) -> None:
    payload = {
        "payroll_batch": raw["payroll_batch"],
        "verification_results": verification_results,
        "material_summaries": material_summaries,
        "audit_events": audit_events,
    }
    digest = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    if raw.get("payload_sha256") != digest or publication_id != f"publication_{digest[:24]}":
        raise PayrollIntegrationError(
            "PAYROLL_PAYLOAD_INTEGRITY_FAILED",
            "payroll publication payload integrity check failed",
        )


def _verify_audit_proof(proof: Mapping[str, object], audit_events: list[object]) -> None:
    event_digest = hashlib.sha256(_stable_json(audit_events).encode("utf-8")).hexdigest()
    head = (
        audit_events[0].get("hash")
        if audit_events and isinstance(audit_events[0], Mapping)
        else None
    )
    tail = (
        audit_events[-1].get("hash")
        if audit_events and isinstance(audit_events[-1], Mapping)
        else None
    )
    if (
        proof.get("schema_version") != "payroll-audit-chain-proof/v1"
        or proof.get("algorithm") != "sha256"
        or proof.get("event_count") != len(audit_events)
        or proof.get("head_hash") != head
        or proof.get("tail_hash") != tail
        or proof.get("events_sha256") != event_digest
    ):
        raise PayrollIntegrationError(
            "PAYROLL_AUDIT_PROOF_INVALID",
            "payroll publication audit proof is invalid",
        )


def _validate_publication_tree(
    value: object,
    *,
    expected_company_id: str,
    parent_key: str = "",
    seen: set[int] | None = None,
) -> None:
    active = set() if seen is None else seen
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            _invalid_response("payroll publication must be acyclic JSON data")
        active.add(marker)
        try:
            for key, nested in value.items():
                if not isinstance(key, str):
                    _invalid_response("payroll publication field is invalid")
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _SENSITIVE_FIELDS:
                    raise PayrollIntegrationError(
                        "PAYROLL_SENSITIVE_FIELD_NOT_ALLOWED",
                        "payroll publication contains a prohibited raw or sensitive field",
                    )
                if key == "company_id" and nested != expected_company_id:
                    raise PayrollIntegrationError(
                        "PAYROLL_IDENTITY_SCOPE_MISMATCH",
                        "payroll publication contains a cross-company identity",
                    )
                if key == "employee_id":
                    _require_stable_identifier(nested, "employee_id")
                if key == "account_id":
                    _require_stable_identifier(nested, "account_id", account=True)
                if key.endswith(("_minor", "_cents")):
                    _require_minor_integer(nested, key)
                _validate_publication_tree(
                    nested,
                    expected_company_id=expected_company_id,
                    parent_key=key,
                    seen=active,
                )
        finally:
            active.remove(marker)
    elif isinstance(value, list):
        marker = id(value)
        if marker in active:
            _invalid_response("payroll publication must be acyclic JSON data")
        active.add(marker)
        try:
            for nested in value:
                _validate_publication_tree(
                    nested,
                    expected_company_id=expected_company_id,
                    parent_key=parent_key,
                    seen=active,
                )
        finally:
            active.remove(marker)
    elif isinstance(value, str):
        _validate_publishable_string(value, parent_key=parent_key)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            _invalid_response("payroll publication integer exceeds the JSON-safe range")
    else:
        _invalid_response("payroll publication contains a non-JSON value")


def _require_base_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("base_url must be a non-empty absolute HTTP URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be a credential-free absolute HTTP URL")
    return value.rstrip("/")


def _require_publication_id(value: object) -> str:
    if not isinstance(value, str) or _PUBLICATION_ID.fullmatch(value) is None:
        raise PayrollIntegrationError(
            "PAYROLL_PUBLICATION_ID_INVALID",
            "payroll publication identifier is invalid",
        )
    return value


def _require_stable_identifier(value: object, field: str, *, account: bool = False) -> str:
    if not isinstance(value, str) or _STABLE_IDENTIFIER.fullmatch(value) is None:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_INVALID",
            f"payroll {field} is not a stable opaque identifier",
        )
    if account and sum(character.isdigit() for character in value) >= 12:
        raise PayrollIntegrationError(
            "PAYROLL_IDENTITY_INVALID",
            "payroll account_id resembles a raw account number",
        )
    return value


def _require_positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SAFE_INTEGER:
        _invalid_response(f"payroll {field} is invalid")
    return value


def _require_non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        _invalid_response(f"payroll {field} must be a non-negative JSON-safe integer")
    return value


def _require_minor_integer(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    ):
        _invalid_response(f"payroll {field} must use signed JSON-safe integer minor units")
    return value


def _require_controlled_token(value: object, field: str) -> str:
    if not isinstance(value, str) or _CONTROLLED_TOKEN.fullmatch(value) is None:
        _invalid_response(f"payroll {field} must use a controlled uppercase token")
    return value


def _require_sha256(value: object, field: str, *, audit: bool = False) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        if audit:
            _invalid_audit(f"payroll {field} is not a lowercase SHA-256 digest")
        _invalid_response(f"payroll {field} is not a lowercase SHA-256 digest")
    return value


def _require_audit_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid_audit(f"payroll {field} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid_audit(f"payroll {field} is not a valid timestamp")
    if parsed.utcoffset() is None:
        _invalid_audit(f"payroll {field} is not a UTC timestamp")
    return value


def _require_object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _invalid_response(f"payroll {field} is invalid")
    return value


def _validate_test_workspace_projection(
    value: Mapping[str, object], expected_company_id: str, expected_batch_id: str
) -> None:
    _require_exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "contract_version",
                "data_scope",
                "test_batch_id",
                "company_id",
                "cutoff_date",
                "workspace_revision",
                "projection_revision",
                "etag",
                "generated_at",
                "payment_submission_supported",
                "payable",
                "submission_supported",
                "routing_counts",
                "materials",
            }
        ),
        "test workspace projection",
    )
    if (
        value.get("schema_version") != TEST_WORKSPACE_SCHEMA
        or value.get("contract_version") != "1.0.0"
        or value.get("data_scope") != "TEST_ONLY"
        or value.get("company_id") != expected_company_id
        or value.get("test_batch_id") != expected_batch_id
        or value.get("cutoff_date") != "2026-08-31"
    ):
        _invalid_response("payroll test workspace scope is invalid")
    _require_non_negative_integer(value.get("workspace_revision"), "workspace_revision")
    revision = _require_sha256(value.get("projection_revision"), "projection_revision")
    if value.get("etag") != f'"{revision}"':
        _invalid_response("payroll test workspace etag is invalid")
    _require_live_timestamp(value.get("generated_at"), "generated_at")
    _require_disabled_flags(
        value, ("payment_submission_supported", "payable", "submission_supported")
    )
    counts = _require_object(value.get("routing_counts"), "routing_counts")
    _require_exact_keys(
        counts, frozenset({"auto_test", "review_required", "date_unknown"}), "routing counts"
    )
    actual = {"auto_test": 0, "review_required": 0, "date_unknown": 0}
    materials = _require_list(value.get("materials"), "materials")
    seen: set[str] = set()
    for item_value in materials:
        item = _require_object(item_value, "material")
        _require_exact_keys(
            item,
            frozenset(
                {
                    "company_id",
                    "material_id",
                    "routing_status",
                    "period",
                    "material_type",
                    "payable",
                    "submission_supported",
                }
            ),
            "test workspace material",
        )
        material_id = _require_stable_identifier(item.get("material_id"), "material_id")
        if material_id in seen or item.get("company_id") != expected_company_id:
            _invalid_response("payroll test workspace material scope is invalid")
        seen.add(material_id)
        period = item.get("period")
        if period is None:
            expected_status = "DATE_UNKNOWN"
            key = "date_unknown"
        else:
            parsed = _require_period(period, "period")
            expected_status = "AUTO_TEST" if parsed <= "2026-08" else "REVIEW_REQUIRED"
            key = "auto_test" if expected_status == "AUTO_TEST" else "review_required"
        if item.get("routing_status") != expected_status:
            _invalid_response("payroll test workspace date routing is invalid")
        material_type = item.get("material_type")
        if material_type is not None:
            _require_stable_identifier(material_type, "material_type")
        _require_disabled_flags(item, ("payable", "submission_supported"))
        actual[key] += 1
    for key, expected in actual.items():
        if _require_non_negative_integer(counts.get(key), f"routing_counts.{key}") != expected:
            _invalid_response("payroll test workspace routing counts are inconsistent")


def _validate_test_workspace_clear_receipt(
    value: Mapping[str, object], expected_company_id: str, expected_batch_id: str
) -> None:
    _require_exact_keys(
        value,
        frozenset(
            {
                "schema_version",
                "data_scope",
                "test_batch_id",
                "company_id",
                "cleared_workspace_revision",
                "cleared_material_count",
                "cleared_at",
                "payment_submission_supported",
                "payable",
                "submission_supported",
                "replayed",
            }
        ),
        "test workspace clear receipt",
    )
    if (
        value.get("schema_version") != "payroll-test-workspace-clear-receipt/v1"
        or value.get("data_scope") != "TEST_ONLY"
        or value.get("test_batch_id") != expected_batch_id
        or value.get("company_id") != expected_company_id
        or type(value.get("replayed")) is not bool
    ):
        _invalid_response("payroll test workspace clear receipt scope is invalid")
    _require_non_negative_integer(
        value.get("cleared_workspace_revision"), "cleared_workspace_revision"
    )
    _require_non_negative_integer(value.get("cleared_material_count"), "cleared_material_count")
    _require_live_timestamp(value.get("cleared_at"), "cleared_at")
    _require_disabled_flags(
        value, ("payment_submission_supported", "payable", "submission_supported")
    )


def _require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        _invalid_response(f"payroll {field} is invalid")
    return value


def _require_required_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    field: str,
) -> None:
    missing = expected.difference(value)
    if missing:
        _invalid_response(f"payroll {field} is missing required v1 fields")


def _validate_publishable_string(value: str, *, parent_key: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _invalid_response("payroll publication contains invalid Unicode text")
    normalized = re.sub(r"[^a-z0-9]", "", parent_key.lower())
    if (
        normalized not in _SENSITIVE_VALUE_SKIP_FIELDS
        and not normalized.endswith("hash")
        and not normalized.endswith("sha256")
        and (
            _ACCOUNT_LIKE_NUMBER.search(value) is not None or _LOCAL_PATH.search(value) is not None
        )
    ):
        raise PayrollIntegrationError(
            "PAYROLL_SENSITIVE_FIELD_NOT_ALLOWED",
            "payroll publication contains an account-like number or local path",
        )


def _invalid_audit(summary: str) -> Never:
    raise PayrollIntegrationError("PAYROLL_AUDIT_PROOF_INVALID", summary)


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_RESPONSE",
            "payroll publication is not canonical JSON",
        ) from exc


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(nested) for nested in value)
    return value


def _deep_thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(nested) for nested in value]
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_response(summary: str) -> Never:
    raise PayrollIntegrationError("PAYROLL_PROVIDER_RESPONSE", summary)
