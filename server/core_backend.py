from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import ssl
import time
import uuid
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_RESPONSE_BYTES = 128 * 1024 * 1024
JSON_SAFE_INTEGER = 9_007_199_254_740_991
SAFE_HEADER_VALUE = re.compile(r"^[\x21-\x7e]+$")
EVIDENCE_UNLOCK_CORE_PATH = "/internal/v1/evidence/unlocks"
EVIDENCE_UNLOCK_STATUSES = {"NOT_REQUIRED", "PASSWORD_REQUIRED", "UNLOCKED"}
PAYROLL_STATUS_CORE_PATH = "/internal/v1/payroll/status"
PAYROLL_TEST_WORKSPACES_CORE_PATH = "/internal/v1/payroll/test-workspaces"
PAYROLL_USER_ASSERTION_VERSION = "ledgerbridge.payroll-bff-user-assertion.v1"
PAYROLL_RESOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PAYROLL_PROJECTION_REVISION = re.compile(r"^[0-9a-f]{64}$")
PAYROLL_PERIOD = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
PAYROLL_CANONICAL_ACCOUNT_ID = re.compile(r"^account_[0-9a-f]{24}$")
PAYROLL_LEGACY_REVIEW_RULE_TYPES = frozenset(
    {
        "PAYMENT_CHANNEL_REQUIRED",
        "SUPPORTING_MATERIAL_REQUIRED",
        "HISTORY_CHANGE_REVIEW",
    }
)
PAYROLL_LEGACY_REVIEW_RULE_SEVERITIES = frozenset({"BLOCKING", "REVIEW"})
PAYROLL_TEST_MATERIAL_TYPES = frozenset(
    {
        "PAYROLL_SHEET",
        "ATTENDANCE_SHEET",
        "AUNT_ATTENDANCE_SHEET",
        "REVIEW_STATISTICS",
        "ADJUSTMENT_SOURCE",
        "PAYROLL_SUMMARY",
    }
)
PAYROLL_TEST_PROJECTION_FACT_KEYS = (
    "data_scope",
    "test_batch_id",
    "company_id",
    "cutoff_date",
    "workspace_revision",
    "auto_test_ready",
    "payment_submission_supported",
    "payable",
    "submission_supported",
    "routing_counts",
    "materials",
)
PAYROLL_BLOCKING_REASON_ORDER = (
    "UNASSIGNED_MATERIALS",
    "MATERIAL_REVIEW_REQUIRED",
    "PAYROLL_BATCH_REQUIRED",
    "LIVE_DATA_NOT_READY",
)
PAYROLL_SAFETY_FLAGS = frozenset(
    {
        "payable",
        "payment_execution_allowed",
        "payment_execution_supported",
        "payment_submission_allowed",
        "payment_submission_supported",
        "payment_operations_exposed",
        "submission_supported",
    }
)
PAYROLL_COMMAND_ROLES = {
    "verify-receipts": "checker",
}
ACCOUNTING_DIMENSIONS_CORE_PATH = "/internal/v1/accounting-dimensions"
CLASSIFICATION_GROUPS_CORE_PATH = "/internal/v1/candidate-classification-groups"
COMPANY_TRANSACTION_CLASSIFICATIONS_CORE_PATH = (
    "/internal/v1/company-transaction-classifications"
)
COMPANY_TRANSACTION_CLASSIFICATION_SUMMARY_CORE_PATH = (
    "/internal/v1/company-transaction-classification-summary"
)
PERSONAL_FINANCE_CORE_PATH = "/internal/v1/personal-finance"
COMPANY_BANK_REVIEW_WORKLOAD_PRINCIPAL = "workload:ledgerbridge-company-bank-review"
CLASSIFICATION_GROUP_REF = re.compile(r"^cg_[0-9a-f]{32}$")
CLASSIFICATION_RISK_CODES = frozenset(
    {
        "FUNDING_STATEMENT_REQUIRED",
        "HOTEL_PAYOUT_STATEMENT_REQUIRED",
        "RELATED_ACCOUNT_STATEMENT_REQUIRED",
        "REVERSAL_MATCH_REQUIRED",
        "TRANSFER_REVIEW_REQUIRED",
        "UNSETTLED_TRANSACTION",
    }
)


class CoreBackendError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, object]) -> None:
        super().__init__(str(payload.get("code", "CORE_REQUEST_FAILED")))
        self.status = status
        self.payload = payload


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class CoreHttpClient:
    """Bounded loopback mTLS client for the closed LedgerBridge Core surface."""

    def __init__(
        self,
        *,
        base_url: str,
        ca_file: str | Path,
        certificate_file: str | Path,
        private_key_file: str | Path,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("CORE_BASE_URL must be an origin-only HTTPS URL")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("Core timeout must be between 0 and 30 seconds")
        context = ssl.create_default_context(cafile=str(ca_file))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(str(certificate_file), str(private_key_file))
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        content, response_headers = self._request(
            method,
            path,
            body=body,
            headers=headers,
            max_bytes=MAX_JSON_RESPONSE_BYTES,
        )
        content_type = response_headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID")) from error
        if not isinstance(payload, dict):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return payload

    def evidence(self, path: str) -> dict[str, object]:
        content, headers = self._request(
            "GET",
            path,
            max_bytes=MAX_EVIDENCE_RESPONSE_BYTES,
        )
        digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        if headers.get("Content-Digest") != f"sha-256=:{digest}:":
            raise CoreBackendError(503, _problem(503, "CORE_EVIDENCE_INTEGRITY_FAILED"))
        disposition = headers.get("Content-Disposition", "")
        filename_match = re.fullmatch(
            r'attachment; filename="([A-Za-z0-9._-]{1,200})"',
            disposition,
        )
        if headers.get("Content-Type") != "application/octet-stream" or filename_match is None:
            raise CoreBackendError(503, _problem(503, "CORE_EVIDENCE_CONTRACT_INVALID"))
        return {
            "content": content,
            "content_type": "application/octet-stream",
            "disposition": "attachment",
            "filename": filename_match.group(1),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int,
    ) -> tuple[bytes, Any]:
        if not path.startswith("/internal/v1/") or "#" in path:
            raise ValueError("Core path is outside the internal v1 surface")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if any(
            not SAFE_HEADER_VALUE.fullmatch(name)
            or not SAFE_HEADER_VALUE.fullmatch(value)
            for name, value in request_headers.items()
        ):
            raise ValueError("Core request header is invalid")
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > max_bytes:
                    raise CoreBackendError(503, _problem(503, "CORE_RESPONSE_TOO_LARGE"))
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise CoreBackendError(503, _problem(503, "CORE_RESPONSE_TOO_LARGE"))
                return content, response.headers
        except HTTPError as error:
            content = error.read(MAX_JSON_RESPONSE_BYTES + 1)
            try:
                payload = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = _problem(error.code, "CORE_REQUEST_FAILED")
            if not isinstance(payload, dict):
                payload = _problem(error.code, "CORE_REQUEST_FAILED")
            raise CoreBackendError(error.code, payload) from error
        except (OSError, URLError, ValueError) as error:
            raise CoreBackendError(503, _problem(503, "CORE_UNAVAILABLE")) from error


class CoreBackedState:
    """Business adapter: Core owns facts; this object keeps no candidate state."""

    opaque_cursors = True

    def __init__(
        self,
        client: CoreHttpClient,
        *,
        company_report_client: CoreHttpClient | None = None,
        company_bank_review_client: CoreHttpClient | None = None,
        company_bank_statement_mappings: tuple[tuple[str, str, str], ...] = (),
        assertion_key: bytes,
        assertion_issuer: str,
        assertion_audience: str,
        workload_principal: str,
        policy_generation: int,
        user_subject: str,
        authentication_generation: int,
        entity_ref: str,
        business_unit_ref: str,
        candidate_business_unit_refs: tuple[str, ...] | None = None,
        personal_finance_entity_ref: str | None = None,
        personal_finance_statement_ref: str | None = None,
        personal_finance_statement_refs: tuple[str, ...] | None = None,
        evidence_unlock_path: str | None = None,
        payroll_commands_enabled: bool = False,
        payroll_role_bindings: dict[str, frozenset[str]] | None = None,
        payroll_test_workspace_enabled: bool = False,
        payroll_test_batch_id: str | None = None,
        payroll_test_workspace_autocreate: bool = False,
        payroll_test_workspace_expected_store_revision: int = 0,
    ) -> None:
        if not 32 <= len(assertion_key) <= 256:
            raise ValueError("CORE_USER_ASSERTION_KEY must contain 32 to 256 bytes")
        if policy_generation < 1 or authentication_generation < 1:
            raise ValueError("Core policy and authentication generations must be positive")
        self.client = client
        self.company_report_client = company_report_client
        self.company_bank_review_client = company_bank_review_client
        self.assertion_key = assertion_key
        self.assertion_issuer = _bounded(assertion_issuer)
        self.assertion_audience = _bounded(assertion_audience)
        self.workload_principal = _bounded(workload_principal)
        self.policy_generation = policy_generation
        self.user_subject = _bounded(user_subject)
        self.authentication_generation = authentication_generation
        self.entity_ref = str(uuid.UUID(entity_ref))
        self.business_unit_ref = _bounded(business_unit_ref, maximum=100)
        supplied_candidate_units = candidate_business_unit_refs or (self.business_unit_ref,)
        if not supplied_candidate_units or len(supplied_candidate_units) > 8:
            raise ValueError("between one and eight candidate business units are required")
        self.candidate_business_unit_refs = tuple(
            _bounded(value, maximum=100) for value in supplied_candidate_units
        )
        if (
            self.business_unit_ref not in self.candidate_business_unit_refs
            or len(set(self.candidate_business_unit_refs)) != len(self.candidate_business_unit_refs)
        ):
            raise ValueError("candidate business units must be unique and include CORE_BUSINESS_UNIT_REF")
        supplied_statement_refs = personal_finance_statement_refs or ()
        if personal_finance_statement_ref and supplied_statement_refs:
            raise ValueError(
                "CORE_PERSONAL_STATEMENT_REF and CORE_PERSONAL_STATEMENT_REFS cannot both be configured"
            )
        if personal_finance_statement_ref:
            supplied_statement_refs = (personal_finance_statement_ref,)
        if bool(personal_finance_entity_ref) != bool(supplied_statement_refs):
            raise ValueError(
                "CORE_PERSONAL_ENTITY_REF and personal statement refs must be configured together"
            )
        if len(supplied_statement_refs) > 32:
            raise ValueError("at most 32 personal statement refs may be configured")
        self.personal_finance_entity_ref = (
            str(uuid.UUID(personal_finance_entity_ref))
            if personal_finance_entity_ref
            else None
        )
        self.personal_finance_statement_refs = tuple(
            str(uuid.UUID(statement_ref)) for statement_ref in supplied_statement_refs
        )
        if len(set(self.personal_finance_statement_refs)) != len(
            self.personal_finance_statement_refs
        ):
            raise ValueError("personal statement refs must be unique")
        if company_bank_statement_mappings and len(company_bank_statement_mappings) != 6:
            raise ValueError("exactly six company bank statements must be configured")
        normalized_company_statements: list[tuple[str, str, str]] = []
        for statement_ref, company_ref, company_name in company_bank_statement_mappings:
            canonical_name = _bounded(company_name, maximum=200)
            normalized_company_statements.append(
                (str(uuid.UUID(statement_ref)), str(uuid.UUID(company_ref)), canonical_name)
            )
        if len({item[0] for item in normalized_company_statements}) != len(
            normalized_company_statements
        ):
            raise ValueError("company statement refs must be unique")
        self.company_bank_statement_mappings = tuple(normalized_company_statements)
        self.payroll_commands_enabled = payroll_commands_enabled
        self.payroll_test_workspace_enabled = payroll_test_workspace_enabled
        if payroll_test_workspace_enabled and (
            not isinstance(payroll_test_batch_id, str)
            or PAYROLL_RESOURCE_REF.fullmatch(payroll_test_batch_id) is None
        ):
            raise ValueError("PAYROLL_TEST_BATCH_ID is required when test workspaces are enabled")
        self.payroll_test_batch_id = payroll_test_batch_id if payroll_test_workspace_enabled else None
        if (
            payroll_test_workspace_autocreate
            and not payroll_test_workspace_enabled
        ) or (
            type(payroll_test_workspace_expected_store_revision) is not int
            or payroll_test_workspace_expected_store_revision < 0
        ):
            raise ValueError("invalid payroll test workspace bootstrap configuration")
        self.payroll_test_workspace_autocreate = payroll_test_workspace_autocreate
        self.payroll_test_workspace_expected_store_revision = (
            payroll_test_workspace_expected_store_revision
        )
        supplied_bindings = payroll_role_bindings or {}
        self.payroll_role_bindings: dict[str, frozenset[str]] = {}
        for subject, roles in supplied_bindings.items():
            canonical_subject = _bounded(subject)
            canonical_roles = frozenset(roles)
            if not canonical_roles.issubset({"maker", "checker", "approver"}):
                raise ValueError("payroll role binding contains an unsupported role")
            self.payroll_role_bindings[canonical_subject] = canonical_roles
        if evidence_unlock_path not in {None, "", EVIDENCE_UNLOCK_CORE_PATH}:
            raise ValueError("Core evidence unlock path is unsupported")
        self.evidence_unlock_path = EVIDENCE_UNLOCK_CORE_PATH if evidence_unlock_path else None
        # These fields are unreachable when core-backed mode has its required
        # AuthManager, but keep the server Interface closed and explicit.
        self.cookie_secure = True
        self.cookie_name = "__Host-ledgerbridge_session"
        self.session_id = ""
        self.csrf_token = ""

    def session_active(self) -> bool:
        return False

    def session_payload(self) -> dict[str, str]:
        raise CoreBackendError(503, _problem(503, "AUTH_BACKEND_REQUIRED"))

    def list_candidates(
        self,
        *,
        status: str | None,
        month: str | None,
        cursor: str | None,
    ) -> dict[str, object]:
        unit_index, core_cursor = self._candidate_cursor(cursor)
        query = {
            key: value
            for key, value in {
                "status": status,
                "month": month,
                "business_unit": self.candidate_business_unit_refs[unit_index],
                "cursor": core_cursor,
            }.items()
            if value is not None
        }
        payload = self.client.json("GET", f"/internal/v1/candidates?{urlencode(query)}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        next_core_cursor = payload.get("next_cursor")
        if next_core_cursor is not None and not isinstance(next_core_cursor, str):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        next_cursor = None
        if next_core_cursor is not None:
            next_cursor = (
                next_core_cursor
                if len(self.candidate_business_unit_refs) == 1
                else self._encode_candidate_cursor(unit_index, next_core_cursor)
            )
        elif unit_index + 1 < len(self.candidate_business_unit_refs):
            next_cursor = self._encode_candidate_cursor(unit_index + 1, None)
        return {
            "items": [_candidate_from_core(item) for item in items],
            "next_cursor": next_cursor,
        }

    def _candidate_cursor(self, cursor: str | None) -> tuple[int, str | None]:
        if cursor is None:
            return 0, None
        if len(self.candidate_business_unit_refs) == 1:
            return 0, cursor
        try:
            padding = "=" * (-len(cursor) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoreBackendError(400, _problem(400, "INVALID_CURSOR")) from error
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"v", "unit", "cursor"}
            or decoded["v"] != 1
            or type(decoded["unit"]) is not int
            or not 0 <= decoded["unit"] < len(self.candidate_business_unit_refs)
            or decoded["cursor"] is not None and not isinstance(decoded["cursor"], str)
        ):
            raise CoreBackendError(400, _problem(400, "INVALID_CURSOR"))
        return decoded["unit"], decoded["cursor"]

    @staticmethod
    def _encode_candidate_cursor(unit_index: int, core_cursor: str | None) -> str:
        raw = json.dumps(
            {"v": 1, "unit": unit_index, "cursor": core_cursor},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def candidate_detail(self, candidate_id: str) -> dict[str, object] | None:
        candidate_ref = str(uuid.UUID(candidate_id))
        try:
            payload = self.client.json("GET", f"/internal/v1/candidates/{candidate_ref}")
        except CoreBackendError as error:
            if error.status == 404:
                return None
            raise
        events = self.client.json(
            "GET",
            f"/internal/v1/candidate-events?{urlencode({'candidate_ref': candidate_ref})}",
        )
        detail = _candidate_from_core(payload)
        values = events.get("items")
        if not isinstance(values, list):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        detail["review_events"] = [_event_from_core(item) for item in values]
        return detail

    def accounting_dimensions(self) -> dict[str, object]:
        query = urlencode({"entity_ref": self.entity_ref})
        try:
            payload = self.client.json("GET", f"{ACCOUNTING_DIMENSIONS_CORE_PATH}?{query}")
        except CoreBackendError as error:
            if error.status == 403:
                raise CoreBackendError(
                    403,
                    _problem(403, "ACCOUNTING_DIMENSIONS_FORBIDDEN"),
                ) from error
            if error.status == 404:
                raise CoreBackendError(
                    404,
                    _problem(404, "ACCOUNTING_DIMENSIONS_NOT_FOUND"),
                ) from error
            raise CoreBackendError(
                503,
                _problem(503, "ACCOUNTING_DIMENSIONS_UNAVAILABLE"),
            ) from error

        def invalid_contract() -> CoreBackendError:
            return CoreBackendError(
                503,
                _problem(503, "ACCOUNTING_DIMENSIONS_INVALID"),
            )

        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"contract_version", "entity_ref", "business_units", "categories"}
            or payload.get("contract_version") != "ledgerbridge.accounting-dimensions.v1"
            or payload.get("entity_ref") != self.entity_ref
        ):
            raise invalid_contract()

        mapped: dict[str, object] = {
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
        }
        for collection, reference_field in (
            ("business_units", "ref"),
            ("categories", "code"),
        ):
            values = payload.get(collection)
            if not isinstance(values, list) or len(values) > 1_000:
                raise invalid_contract()
            items: list[dict[str, str]] = []
            references: set[str] = set()
            labels: set[str] = set()
            for value in values:
                if not isinstance(value, dict) or set(value) != {reference_field, "label"}:
                    raise invalid_contract()
                reference = value.get(reference_field)
                label = value.get("label")
                if (
                    not isinstance(reference, str)
                    or not 1 <= len(reference) <= 100
                    or not isinstance(label, str)
                    or not 1 <= len(label) <= 200
                    or reference in references
                    or label in labels
                ):
                    raise invalid_contract()
                references.add(reference)
                labels.add(label)
                items.append({reference_field: reference, "label": label})
            if [item[reference_field] for item in items] != sorted(references):
                raise invalid_contract()
            mapped[collection] = items
        return mapped

    def candidate_classification_groups(self) -> dict[str, object]:
        payload = self.client.json("GET", CLASSIFICATION_GROUPS_CORE_PATH)
        return _classification_groups_from_core(payload, entity_ref=self.entity_ref)

    def apply_candidate_classification_batch(
        self,
        group_ref: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        if CLASSIFICATION_GROUP_REF.fullmatch(group_ref) is None:
            return 422, _problem(422, "INVALID_CLASSIFICATION_GROUP")
        try:
            operation_id = str(uuid.UUID(idempotency_key))
            source_candidate_ref = str(uuid.UUID(str(request.get("source_candidate_ref"))))
        except (TypeError, ValueError):
            return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
        members = request.get("members")
        if not isinstance(members, list) or not 2 <= len(members) <= 100:
            return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
        member_refs: list[str] = []
        for member in members:
            if not isinstance(member, dict) or set(member) != {
                "candidate_ref",
                "expected_revision",
            }:
                return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
            try:
                candidate_ref = str(uuid.UUID(str(member.get("candidate_ref"))))
            except (TypeError, ValueError):
                return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
            revision = member.get("expected_revision")
            if (
                member.get("candidate_ref") != candidate_ref
                or type(revision) is not int
                or int(revision) < 1
            ):
                return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
            member_refs.append(candidate_ref)
        if len(member_refs) != len(set(member_refs)) or source_candidate_ref not in member_refs:
            return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
        source_members = [
            member
            for member in members
            if isinstance(member, dict)
            and member.get("candidate_ref") == source_candidate_ref
        ]
        if len(source_members) != 1 or type(source_members[0].get("expected_revision")) is not int:
            return 422, _problem(422, "INVALID_CLASSIFICATION_BATCH")
        expected_revision = int(source_members[0]["expected_revision"])
        path = f"{CLASSIFICATION_GROUPS_CORE_PATH}/{group_ref}/decisions"
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._user_assertion(
            path=path,
            body=body,
            candidate_ref=source_candidate_ref,
            expected_revision=expected_revision,
            operation_id=operation_id,
        )
        try:
            payload = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        try:
            mapped = _classification_batch_receipt_from_core(
                payload,
                operation_id=operation_id,
                group_ref=group_ref,
                source_candidate_ref=source_candidate_ref,
                accounting_month=request.get("accounting_month"),
                target=request.get("target"),
                acknowledged_risk_codes=request.get("acknowledged_risk_codes"),
                member_refs=member_refs,
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, mapped

    def list_review_events(self, *, cursor: str | None) -> dict[str, object]:
        if cursor is not None:
            raise CoreBackendError(400, _problem(400, "INVALID_CURSOR"))
        payload = self.client.json("GET", "/internal/v1/candidate-events")
        values = payload.get("items")
        if not isinstance(values, list):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return {
            "items": [_event_from_core(item) for item in values],
            "next_cursor": None,
        }

    def company_reports(self, from_month: str, to_month: str) -> dict[str, object]:
        if self.company_report_client is None:
            raise CoreBackendError(
                503,
                _problem(503, "COMPANY_REPORTS_UNAVAILABLE"),
            )
        client = self.company_report_client
        layers: list[dict[str, object]] = []
        compositions: list[dict[str, object]] = []
        posted_ledger_status = "AVAILABLE"
        for basis in _COMPANY_REPORT_BASES:
            query = urlencode(
                {
                    "from_month": from_month,
                    "to_month": to_month,
                    "basis": basis,
                }
            )
            try:
                payload = client.json(
                    "GET",
                    f"/internal/v1/company-reports?{query}",
                )
            except CoreBackendError as error:
                if basis == "POSTED_LEDGER" and error.status in {404, 503}:
                    posted_ledger_status = "UNAVAILABLE"
                    continue
                raise
            layers.append(
                _company_report_layer_from_core(
                    payload,
                    basis,
                    from_month,
                    to_month,
                )
            )
        _validate_company_report_layer_identities(layers)
        layer_by_basis = {str(layer["basis"]): layer for layer in layers}
        for basis in _COMPANY_REPORT_COMPOSITION_BASES:
            if basis == "POSTED_LEDGER" and posted_ledger_status == "UNAVAILABLE":
                continue
            query = urlencode(
                {
                    "from_month": from_month,
                    "to_month": to_month,
                    "basis": basis,
                }
            )
            payload = client.json(
                "GET",
                f"/internal/v1/company-report-composition?{query}",
            )
            composition = _company_report_composition_from_core(
                payload,
                basis,
                from_month,
                to_month,
            )
            _validate_company_report_composition_against_layer(
                composition,
                layer_by_basis[basis],
            )
            compositions.append(composition)
        summary_query = urlencode(
            {
                "from_date": f"{from_month}-01",
                "to_date_exclusive": _month_after(to_month),
            }
        )
        classification_summary = _company_transaction_classification_summary_from_core(
            client.json(
                "GET",
                f"{COMPANY_TRANSACTION_CLASSIFICATION_SUMMARY_CORE_PATH}?{summary_query}",
            ),
            expected_from_date=f"{from_month}-01",
            expected_to_date_exclusive=_month_after(to_month),
        )
        report_companies = {
            str(item["company_ref"]): str(item["company_name"])
            for item in layers[0]["items"]
            if isinstance(item, dict)
        }
        if {str(item["entity_ref"]) for item in classification_summary["items"]} != set(
            report_companies
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        for item in classification_summary["items"]:
            item["company_name"] = report_companies[str(item["entity_ref"])]
        return {
            "contract_version": "ledgerbridge.company-reports-bff.v3",
            "from_month": from_month,
            "to_month": to_month,
            "posted_ledger_status": posted_ledger_status,
            "layers": layers,
            "compositions": compositions,
            "transaction_classifications": classification_summary,
        }

    def personal_bank_transactions(self) -> dict[str, object]:
        if (
            self.personal_finance_entity_ref is None
            or not self.personal_finance_statement_refs
        ):
            raise CoreBackendError(
                503,
                _problem(503, "PERSONAL_BANK_FACTS_UNAVAILABLE"),
            )
        pages: list[dict[str, object]] = []
        for statement_ref in self.personal_finance_statement_refs:
            query = urlencode(
                {
                    "statement_ref": statement_ref,
                    "entity_ref": self.personal_finance_entity_ref,
                }
            )
            try:
                payload = self.client.json(
                    "GET",
                    f"{PERSONAL_FINANCE_CORE_PATH}?{query}",
                )
            except CoreBackendError as error:
                raise CoreBackendError(
                    503,
                    _problem(503, "PERSONAL_BANK_FACTS_UNAVAILABLE"),
                ) from error
            page = _personal_bank_transactions_from_core(payload)
            statement = page["statement"]
            if (
                not isinstance(statement, dict)
                or statement.get("statement_ref") != statement_ref
            ):
                raise CoreBackendError(
                    503,
                    _problem(503, "CORE_CONTRACT_INVALID"),
                )
            pages.append(page)
        return _merge_personal_bank_transactions(pages)

    def review_bank_statement(
        self,
        statement_ref: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        try:
            canonical_ref = str(uuid.UUID(statement_ref))
            operation_id = str(uuid.UUID(idempotency_key))
        except ValueError:
            return 422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW")
        if self.personal_finance_entity_ref is None:
            return 503, _problem(503, "PERSONAL_BANK_FACTS_UNAVAILABLE")
        revision = request.get("expected_revision")
        if type(revision) is not int:
            return 422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW")
        path = f"/internal/v1/bank-statements/{canonical_ref}/reviews"
        forwarded = {**request, "entity_ref": self.personal_finance_entity_ref}
        body = json.dumps(forwarded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._resource_user_assertion(
            path=path,
            body=body,
            resource_ref=canonical_ref,
            operation_id=operation_id,
            expected_revision=revision,
        )
        try:
            payload = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, payload

    def company_bank_statements(self) -> dict[str, object]:
        if self.company_bank_review_client is None or not self.company_bank_statement_mappings:
            raise CoreBackendError(503, _problem(503, "COMPANY_BANK_REVIEW_UNAVAILABLE"))
        statements: list[dict[str, object]] = []
        for statement_ref, company_ref, company_name in self.company_bank_statement_mappings:
            query = urlencode({"entity_ref": company_ref})
            try:
                payload = self.company_bank_review_client.json(
                    "GET",
                    f"/internal/v1/company-bank-statements/{statement_ref}?{query}",
                )
                page = _personal_bank_transactions_from_core(payload)
            except CoreBackendError as error:
                raise CoreBackendError(
                    503, _problem(503, "COMPANY_BANK_REVIEW_UNAVAILABLE")
                ) from error
            statement = page.get("statement")
            if not isinstance(statement, dict) or statement.get("statement_ref") != statement_ref:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            statements.append({**statement, "company_name": company_name})
        return {
            "contract_version": "ledgerbridge.company-bank-statements-bff.v1",
            "statements": statements,
        }

    def company_transaction_classifications(self) -> dict[str, object]:
        if self.company_bank_review_client is None:
            raise CoreBackendError(
                503, _problem(503, "COMPANY_CLASSIFICATION_REVIEW_UNAVAILABLE")
            )
        payload = self.company_bank_review_client.json(
            "GET", f"{COMPANY_TRANSACTION_CLASSIFICATIONS_CORE_PATH}?status=PENDING"
        )
        page = _company_transaction_classifications_from_core(payload)
        company_names: dict[str, str] = {}
        for _, company_ref, company_name in self.company_bank_statement_mappings:
            existing = company_names.setdefault(company_ref, company_name)
            if existing != company_name:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        for item in page["items"]:
            company_name = company_names.get(str(item["entity_ref"]))
            if company_name is None:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            item["company_name"] = company_name
        return page

    def review_company_transaction_classification(
        self,
        transaction_ref: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        try:
            canonical_ref = str(uuid.UUID(transaction_ref))
            operation_id = str(uuid.UUID(idempotency_key))
            entity_ref = str(uuid.UUID(str(request.get("entity_ref"))))
        except (TypeError, ValueError):
            return 422, _problem(422, "INVALID_COMPANY_CLASSIFICATION_REVIEW")
        company_refs = {item[1] for item in self.company_bank_statement_mappings}
        if self.company_bank_review_client is None or entity_ref not in company_refs:
            return 404, _problem(404, "COMPANY_TRANSACTION_NOT_FOUND")
        revision = request.get("expected_revision")
        if type(revision) is not int:
            return 422, _problem(422, "INVALID_COMPANY_CLASSIFICATION_REVIEW")
        path = f"{COMPANY_TRANSACTION_CLASSIFICATIONS_CORE_PATH}/{canonical_ref}/reviews"
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._resource_user_assertion(
            path=path,
            body=body,
            resource_ref=canonical_ref,
            operation_id=operation_id,
            expected_revision=revision,
            workload_principal=COMPANY_BANK_REVIEW_WORKLOAD_PRINCIPAL,
        )
        try:
            payload = self.company_bank_review_client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, _company_transaction_classification_receipt_from_core(
            payload,
            transaction_ref=canonical_ref,
        )

    def review_company_bank_statement(
        self,
        statement_ref: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        try:
            canonical_ref = str(uuid.UUID(statement_ref))
            operation_id = str(uuid.UUID(idempotency_key))
        except ValueError:
            return 422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW")
        mapping = next(
            (item for item in self.company_bank_statement_mappings if item[0] == canonical_ref),
            None,
        )
        if self.company_bank_review_client is None or mapping is None:
            return 404, _problem(404, "COMPANY_BANK_STATEMENT_NOT_FOUND")
        revision = request.get("expected_revision")
        if type(revision) is not int:
            return 422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW")
        path = f"/internal/v1/bank-statements/{canonical_ref}/reviews"
        body = json.dumps(
            {**request, "entity_ref": mapping[1]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assertion = self._resource_user_assertion(
            path=path,
            body=body,
            resource_ref=canonical_ref,
            operation_id=operation_id,
            expected_revision=revision,
            workload_principal=COMPANY_BANK_REVIEW_WORKLOAD_PRINCIPAL,
        )
        try:
            payload = self.company_bank_review_client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, payload

    def append_decision(
        self,
        candidate_id: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        candidate_ref = str(uuid.UUID(candidate_id))
        operation_id = str(uuid.UUID(idempotency_key))
        path = f"/internal/v1/candidates/{candidate_ref}/decisions"
        revision = request.get("expected_revision")
        if type(revision) is not int:
            return 422, _problem(422, "INVALID_REVISION")
        forwarded_request = deepcopy(request)
        corrections = forwarded_request.get("corrections")
        explicit_fields = {
            "business_unit_ref": "business_unit",
            "category_code": "category",
        }
        if isinstance(corrections, dict) and {"business_unit", "category"} & corrections.keys():
            return 422, _problem(422, "INVALID_CORRECTIONS")
        if isinstance(corrections, dict):
            for explicit, legacy in explicit_fields.items():
                if explicit not in corrections:
                    continue
                reference = corrections.pop(explicit)
                if not isinstance(reference, str) or not 1 <= len(reference) <= 100:
                    return 422, _problem(422, "INVALID_CORRECTION_REFERENCE")
                corrections[legacy] = reference
        body = json.dumps(forwarded_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._user_assertion(
            path=path,
            body=body,
            candidate_ref=candidate_ref,
            expected_revision=revision,
            operation_id=operation_id,
        )
        try:
            payload = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        candidate = payload.get("candidate")
        events = payload.get("events")
        if not isinstance(candidate, dict) or not isinstance(events, list) or not events:
            return 503, _problem(503, "CORE_CONTRACT_INVALID")
        return 200, {
            "candidate": _candidate_from_core(candidate),
            "event": _event_from_core(events[-1]),
        }

    def evidence(self, evidence_id: str) -> dict[str, object]:
        evidence_ref = str(uuid.UUID(evidence_id))
        return self.client.evidence(f"/internal/v1/evidence/{evidence_ref}/content")

    def unlock_evidence_source(
        self,
        source_ref: str,
        password: str,
        idempotency_key: str,
    ) -> tuple[int, dict[str, object]]:
        try:
            canonical_source_ref = _opaque_source_ref(source_ref)
            operation_id = str(uuid.UUID(idempotency_key))
        except (TypeError, ValueError):
            return 422, _problem(422, "INVALID_EVIDENCE_UNLOCK_REQUEST")
        if self.evidence_unlock_path is None:
            return 503, _problem(503, "EVIDENCE_UNLOCK_UNAVAILABLE")

        request_payload: dict[str, object] = {
            "contract_version": "ledgerbridge.evidence-unlock.v1",
            "source_ref": canonical_source_ref,
            "password": password,
        }
        body = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._resource_user_assertion(
            path=self.evidence_unlock_path,
            body=body,
            resource_ref=canonical_source_ref,
            operation_id=operation_id,
        )
        try:
            payload = self.client.json(
                "POST",
                self.evidence_unlock_path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            status = 503 if error.status >= 500 else 422
            code = "EVIDENCE_UNLOCK_UNAVAILABLE" if status == 503 else "EVIDENCE_UNLOCK_FAILED"
            return status, _problem(status, code)
        finally:
            request_payload["password"] = ""
            body = b""

        if (
            payload.get("contract_version") != "ledgerbridge.evidence-unlock-result.v1"
            or payload.get("source_ref") != canonical_source_ref
            or payload.get("unlock_status") != "UNLOCKED"
        ):
            return 503, _problem(503, "EVIDENCE_UNLOCK_UNAVAILABLE")
        return 200, {"unlocked": True}

    def reconciliation(self, month: str) -> dict[str, object]:
        query = urlencode(
            {
                "entity_ref": self.entity_ref,
                "business_unit": self.business_unit_ref,
            }
        )
        try:
            payload = self.client.json(
                "GET",
                f"/internal/v1/reconciliations/{month}?{query}",
            )
        except CoreBackendError as error:
            if error.status != 404:
                raise
            return {
                "accounting_month": month,
                "revision": 0,
                "ready": False,
                "blockers": [
                    {
                        "code": "RECONCILIATION_SNAPSHOT_MISSING",
                        "message": "正式候选已导入，月度对账快照尚未生成",
                    }
                ],
                "business_units": [],
            }
        blockers = payload.get("blockers")
        if not isinstance(blockers, list):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return {
            "accounting_month": payload.get("month"),
            "revision": payload.get("snapshot_revision"),
            "ready": not blockers,
            "blockers": deepcopy(blockers),
            "business_units": [
                {
                    "name": self.business_unit_ref,
                    "amounts_minor": {"已入账": payload.get("posted_amount_minor", 0)},
                }
            ],
        }

    def original_reconciliation(
        self,
        month: str,
        *,
        entity_ref: str | None,
        business_unit_ref: str | None,
    ) -> dict[str, object]:
        if entity_ref is not None and entity_ref != self.entity_ref:
            raise CoreBackendError(403, _problem(403, "SCOPE_NOT_AUTHORIZED"))
        if business_unit_ref is not None and business_unit_ref != self.business_unit_ref:
            raise CoreBackendError(403, _problem(403, "SCOPE_NOT_AUTHORIZED"))
        query = urlencode(
            {
                "entity_ref": self.entity_ref,
                "business_unit": self.business_unit_ref,
            }
        )
        payload = self.client.json(
            "GET",
            f"/internal/v1/original-reconciliations/{month}?{query}",
        )
        return _original_reconciliation_from_core(
            payload,
            month=month,
            entity_ref=self.entity_ref,
            business_unit_ref=self.business_unit_ref,
        )

    def cash_reconciliation(self, month: str) -> dict[str, object]:
        payload = self.client.json(
            "GET",
            f"/internal/v1/cash-reconciliations/{month}",
        )
        if payload.get("contract_version") != "ledgerbridge.cash-reconciliation.v2":
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return payload

    def connections(self) -> list[dict[str, str]]:
        checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return [
            {
                "id": "ledgerbridge_core",
                "state": "CONNECTED",
                "checked_at": checked_at,
                "detail": "Core-backed review mode",
            }
        ]

    def create_draft(
        self,
        month: str,
        idempotency_key: str,
        expected_revision: int,
    ) -> tuple[int, dict[str, object], None]:
        return 503, _problem(503, "WORKBOOK_COMMAND_UNAVAILABLE"), None

    def get_draft(self, draft_id: str) -> None:
        return None

    def payroll_status(self, session_token: str, session_subject: str) -> dict[str, object]:
        payload = self._payroll_read(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.status.read",
            path=PAYROLL_STATUS_CORE_PATH,
            resource_ref="payroll-status",
        )
        result = deepcopy(payload)
        data = result["data"]
        if not isinstance(data, dict):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        _validate_payroll_status_data(data)
        core_capabilities = data.get("capabilities")
        core_allows_verification = bool(
            isinstance(core_capabilities, dict)
            and core_capabilities.get("commands_enabled") is True
            and core_capabilities.get("allowed_actions") == ["VERIFY_RECEIPTS"]
        )
        locally_allows_verification = bool(
            self.payroll_commands_enabled
            and data.get("live_data_ready") is True
            and "checker" in self.payroll_role_bindings.get(session_subject, frozenset())
        )
        allowed = core_allows_verification and locally_allows_verification
        data["capabilities"] = {
            "commands_enabled": allowed,
            "allowed_actions": ["VERIFY_RECEIPTS"] if allowed else [],
        }
        return result

    def payroll_dashboard(self, session_token: str, session_subject: str) -> dict[str, object]:
        return self._payroll_read(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.dashboard.read",
            path="/internal/v1/payroll/dashboard",
            resource_ref="payroll-dashboard",
        )

    def payroll_test_workspace(
        self,
        session_token: str,
        session_subject: str,
    ) -> dict[str, object]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            raise CoreBackendError(404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED"))
        try:
            return self._read_payroll_test_workspace(session_token, session_subject)
        except CoreBackendError as error:
            if (
                error.status != 404
                or error.payload.get("code") != "PAYROLL_TEST_WORKSPACE_NOT_FOUND"
                or not self.payroll_test_workspace_autocreate
            ):
                raise
        return self._create_payroll_test_workspace(session_token, session_subject)

    def payroll_test_material_preview(
        self,
        session_token: str,
        session_subject: str,
        material_id: str,
    ) -> dict[str, object]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            raise CoreBackendError(404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED"))
        if not _payroll_identifier(material_id):
            raise CoreBackendError(400, _problem(400, "INVALID_PAYROLL_MATERIAL_ID"))
        path = (
            f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}/materials/"
            f"{material_id}/preview"
        )
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.read",
            method="GET",
            path=path,
            body=b"",
            resource_ref=material_id,
        )
        payload = self.client.json(
            "GET",
            path,
            headers={"X-LedgerBridge-User-Assertion": assertion},
        )
        _validate_payroll_test_material_preview_payload(
            payload,
            expected_entity_ref=self.entity_ref,
            expected_batch_id=self.payroll_test_batch_id,
            expected_material_id=material_id,
        )
        return payload

    def payroll_legacy_workspace(
        self,
        session_token: str,
        session_subject: str,
    ) -> dict[str, object]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            raise CoreBackendError(404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED"))
        path = (
            f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}/legacy-features"
        )
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.legacy.read",
            method="GET",
            path=path,
            body=b"",
            resource_ref=self.payroll_test_batch_id,
        )
        payload = self.client.json(
            "GET",
            path,
            headers={"X-LedgerBridge-User-Assertion": assertion},
        )
        _validate_payroll_legacy_workspace_payload(
            payload,
            expected_entity_ref=self.entity_ref,
            expected_batch_id=self.payroll_test_batch_id,
        )
        return payload

    def payroll_legacy_workspace_command(
        self,
        *,
        session_token: str,
        session_subject: str,
        action: str,
        expected_revision: int,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> tuple[int, dict[str, object]]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            return 404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED")
        path = (
            f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}"
            "/legacy-features/commands"
        )
        request = {
            "schema_version": "payroll-legacy-feature-command-request/v1",
            "action": action,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "explicitly_confirmed": True,
            "payload": payload,
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.legacy.command",
            method="POST",
            path=path,
            body=body,
            resource_ref=self.payroll_test_batch_id,
            expected_revision=expected_revision,
            operation_id=idempotency_key,
        )
        try:
            result = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
            _validate_payroll_legacy_command_payload(
                result,
                expected_entity_ref=self.entity_ref,
                expected_batch_id=self.payroll_test_batch_id,
                expected_action=action,
                expected_revision=expected_revision,
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, result

    def _read_payroll_test_workspace(
        self,
        session_token: str,
        session_subject: str,
    ) -> dict[str, object]:
        assert self.payroll_test_batch_id is not None
        path = f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}"
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.read",
            method="GET",
            path=path,
            body=b"",
            resource_ref=self.payroll_test_batch_id,
        )
        payload = self.client.json(
            "GET",
            path,
            headers={"X-LedgerBridge-User-Assertion": assertion},
        )
        _validate_payroll_test_workspace_payload(
            payload,
            expected_entity_ref=self.entity_ref,
            expected_batch_id=self.payroll_test_batch_id,
        )
        return payload

    def _create_payroll_test_workspace(
        self,
        session_token: str,
        session_subject: str,
    ) -> dict[str, object]:
        assert self.payroll_test_batch_id is not None
        operation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ledgerbridge-test-workspace:{self.entity_ref}:{self.payroll_test_batch_id}",
            )
        )
        request = {
            "schema_version": "payroll-test-workspace-create-request/v1",
            "test_batch_id": self.payroll_test_batch_id,
            "expected_store_revision": self.payroll_test_workspace_expected_store_revision,
            "cutoff_date": "2026-08-31",
            "idempotency_key": operation_id,
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.create",
            method="POST",
            path=PAYROLL_TEST_WORKSPACES_CORE_PATH,
            body=body,
            resource_ref=self.payroll_test_batch_id,
            expected_revision=self.payroll_test_workspace_expected_store_revision,
            operation_id=operation_id,
        )
        payload = self.client.json(
            "POST",
            PAYROLL_TEST_WORKSPACES_CORE_PATH,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": operation_id,
                "X-LedgerBridge-User-Assertion": assertion,
            },
        )
        return _payroll_test_workspace_read_from_create(
            payload,
            expected_entity_ref=self.entity_ref,
            expected_batch_id=self.payroll_test_batch_id,
        )

    def payroll_test_workspace_organize(
        self,
        *,
        session_token: str,
        session_subject: str,
        material_id: str,
        expected_workspace_revision: int,
        period: str,
        material_type: str,
        idempotency_key: str,
    ) -> tuple[int, dict[str, object]]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            return 404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED")
        path = (
            f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}/materials/"
            f"{material_id}/organize"
        )
        request = {
            "schema_version": "payroll-test-material-organize-request/v1",
            "expected_workspace_revision": expected_workspace_revision,
            "period": period,
            "material_type": material_type,
            "idempotency_key": idempotency_key,
            "explicitly_confirmed": True,
        }
        return self._payroll_test_workspace_command(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.organize",
            path=path,
            resource_ref=material_id,
            expected_revision=expected_workspace_revision,
            operation_id=idempotency_key,
            request=request,
        )

    def payroll_test_workspace_validate(
        self,
        *,
        session_token: str,
        session_subject: str,
        expected_workspace_revision: int,
        idempotency_key: str,
    ) -> tuple[int, dict[str, object]]:
        if not self.payroll_test_workspace_enabled or self.payroll_test_batch_id is None:
            return 404, _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED")
        path = f"{PAYROLL_TEST_WORKSPACES_CORE_PATH}/{self.payroll_test_batch_id}/validate"
        request = {
            "schema_version": "payroll-test-batch-validation-request/v1",
            "expected_workspace_revision": expected_workspace_revision,
            "idempotency_key": idempotency_key,
            "explicitly_confirmed": True,
        }
        return self._payroll_test_workspace_command(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.test_workspace.validate",
            path=path,
            resource_ref=self.payroll_test_batch_id,
            expected_revision=expected_workspace_revision,
            operation_id=idempotency_key,
            request=request,
        )

    def _payroll_test_workspace_command(
        self,
        *,
        session_token: str,
        session_subject: str,
        action: str,
        path: str,
        resource_ref: str,
        expected_revision: int,
        operation_id: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action=action,
            method="POST",
            path=path,
            body=body,
            resource_ref=resource_ref,
            expected_revision=expected_revision,
            operation_id=operation_id,
        )
        try:
            payload = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
            _validate_payroll_test_workspace_command_payload(
                payload,
                expected_entity_ref=self.entity_ref,
                expected_batch_id=str(self.payroll_test_batch_id),
                expected_action=action,
                expected_resource_ref=resource_ref,
            )
        except CoreBackendError as error:
            return error.status, error.payload
        return 200, payload

    def payroll_materials(self, session_token: str, session_subject: str) -> dict[str, object]:
        return self._payroll_read(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.materials.list",
            path="/internal/v1/payroll/materials",
            resource_ref="payroll-materials",
        )

    def payroll_batches(self, session_token: str, session_subject: str) -> dict[str, object]:
        return self._payroll_read(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.batches.list",
            path="/internal/v1/payroll/batches",
            resource_ref="payroll-batches",
        )

    def payroll_verification(self, session_token: str, session_subject: str) -> dict[str, object]:
        payload = self._payroll_read(
            session_token=session_token,
            session_subject=session_subject,
            action="payroll.verification.list",
            path="/internal/v1/payroll/verification",
            resource_ref="payroll-verification",
        )
        _payroll_available_evidence_ids(payload)
        return payload

    def payroll_batch_command(
        self,
        *,
        session_token: str,
        session_subject: str,
        batch_ref: str,
        command: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        if not self.payroll_commands_enabled:
            return 503, _problem(503, "PAYROLL_COMMAND_UNAVAILABLE")
        required_role = PAYROLL_COMMAND_ROLES.get(command)
        if required_role is None:
            return 404, _problem(404, "PAYROLL_ACTION_NOT_FOUND")
        if not hmac.compare_digest(
            session_subject.encode("utf-8"),
            self.user_subject.encode("utf-8"),
        ):
            return 403, _problem(403, "PAYROLL_SESSION_SCOPE_MISMATCH")
        if required_role not in self.payroll_role_bindings.get(session_subject, frozenset()):
            return 403, _problem(403, "PAYROLL_ROLE_NOT_AUTHORIZED")
        if command == "verify-receipts":
            try:
                status_payload = self.payroll_status(session_token, session_subject)
            except CoreBackendError as error:
                return error.status, error.payload
            status_data = status_payload.get("data")
            capabilities = (
                status_data.get("capabilities") if isinstance(status_data, dict) else None
            )
            if not (
                isinstance(capabilities, dict)
                and capabilities.get("commands_enabled") is True
                and capabilities.get("allowed_actions") == ["VERIFY_RECEIPTS"]
            ):
                return 403, _problem(403, "PAYROLL_ACTION_NOT_AUTHORIZED")
            requested = request.get("source_artifact_ids")
            if not isinstance(requested, list) or not requested:
                return 422, _problem(422, "VERIFICATION_EVIDENCE_REQUIRED")
            try:
                verification = self.payroll_verification(session_token, session_subject)
            except CoreBackendError as error:
                return error.status, error.payload
            available = _payroll_available_evidence_by_id(verification)
            if (
                any(not isinstance(value, str) for value in requested)
                or len({str(value) for value in requested}) != len(requested)
                or any(str(value) not in available for value in requested)
            ):
                return 422, _problem(422, "PAYROLL_VERIFICATION_EVIDENCE_NOT_AVAILABLE")
            selected = [available[str(value)] for value in requested]
            required_counts = {
                "MYBANK_STATEMENT": 5,
                "BOC_RECEIPT": 1,
                "WECHAT_RECEIPT": 1,
            }
            selected_counts = {
                evidence_type: sum(
                    item.get("evidence_type") == evidence_type for item in selected
                )
                for evidence_type in required_counts
            }
            if len(selected) != 7 or selected_counts != required_counts:
                return 422, _problem(422, "VERIFICATION_EVIDENCE_SET_INCOMPLETE")
        resource_ref = _payroll_resource_ref(batch_ref)
        operation_id = str(uuid.UUID(idempotency_key))
        expected_revision = request.get("expected_revision")
        if type(expected_revision) is not int or expected_revision < 1:
            return 422, _problem(422, "INVALID_PAYROLL_VERSION")
        action = f"payroll.batch.{command}"
        path = f"/internal/v1/payroll/batches/{resource_ref}/{command}"
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action=action,
            method="POST",
            path=path,
            body=body,
            resource_ref=resource_ref,
            expected_revision=expected_revision,
            operation_id=operation_id,
        )
        try:
            payload = self.client.json(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": operation_id,
                    "X-LedgerBridge-User-Assertion": assertion,
                },
            )
        except CoreBackendError as error:
            return error.status, error.payload
        _validate_payroll_payload(
            payload,
            expected_contract_version="ledgerbridge.payroll-command-result.v1",
            expected_entity_ref=self.entity_ref,
        )
        _validate_payroll_command_receipt_data(
            payload["data"],
            company_id=str(payload["company_id"]),
            resource_ref=resource_ref,
            idempotency_key=operation_id,
        )
        if (
            set(payload)
            != {
                "contract_version",
                "entity_ref",
                "company_id",
                "action",
                "resource_ref",
                "replayed",
                "data",
            }
            or payload.get("action") != action
            or payload.get("resource_ref") != resource_ref
            or type(payload.get("replayed")) is not bool
        ):
            return 503, _problem(503, "CORE_CONTRACT_INVALID")
        return 200, payload

    def _payroll_read(
        self,
        *,
        session_token: str,
        session_subject: str,
        action: str,
        path: str,
        resource_ref: str,
    ) -> dict[str, object]:
        assertion = self._payroll_user_assertion(
            session_token=session_token,
            session_subject=session_subject,
            action=action,
            method="GET",
            path=path,
            body=b"",
            resource_ref=resource_ref,
        )
        payload = self.client.json(
            "GET",
            path,
            headers={"X-LedgerBridge-User-Assertion": assertion},
        )
        _validate_payroll_payload(
            payload,
            expected_contract_version="ledgerbridge.payroll-read.v1",
            expected_entity_ref=self.entity_ref,
        )
        if path != PAYROLL_STATUS_CORE_PATH:
            _validate_payroll_view_data(
                payload["data"],
                path=path,
                company_id=str(payload["company_id"]),
            )
        return payload

    def _payroll_user_assertion(
        self,
        *,
        session_token: str,
        session_subject: str,
        action: str,
        method: str,
        path: str,
        body: bytes,
        operation_id: str | None = None,
        expected_revision: int | None = None,
        resource_ref: str,
    ) -> str:
        issued_at = int(time.time())
        session_ref = hmac.new(
            self.assertion_key,
            b"ledgerbridge.payroll-session.v1\x00" + session_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        claims: dict[str, object] = {
            "version": PAYROLL_USER_ASSERTION_VERSION,
            "issuer": self.assertion_issuer,
            "audience": self.assertion_audience,
            "subject": session_subject,
            "authentication_generation": self.authentication_generation,
            "session_ref": session_ref,
            "entity_ref": self.entity_ref,
            "action": action,
            "method": method,
            "canonical_path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "resource_ref": resource_ref,
            "expected_revision": expected_revision,
            "operation_id": operation_id,
            "workload_principal": self.workload_principal,
            "policy_generation": self.policy_generation,
            "issued_at": issued_at,
            "expires_at": issued_at + 45,
            "jti": str(uuid.uuid4()),
        }
        encoded = _b64url(
            json.dumps(
                claims,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signed = f"v1.{encoded}".encode("ascii")
        signature = hmac.new(self.assertion_key, signed, hashlib.sha256).digest()
        return f"v1.{encoded}.{_b64url(signature)}"

    def _user_assertion(
        self,
        *,
        path: str,
        body: bytes,
        candidate_ref: str,
        expected_revision: int,
        operation_id: str,
    ) -> str:
        issued_at = int(time.time())
        claims = {
            "version": "ledgerbridge.bff-user-assertion.v1",
            "issuer": self.assertion_issuer,
            "audience": self.assertion_audience,
            "subject": self.user_subject,
            "authentication_generation": self.authentication_generation,
            "method": "POST",
            "canonical_path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "resource_ref": candidate_ref,
            "expected_revision": expected_revision,
            "operation_id": operation_id,
            "workload_principal": self.workload_principal,
            "policy_generation": self.policy_generation,
            "issued_at": issued_at,
            "expires_at": issued_at + 45,
            "jti": str(uuid.uuid4()),
        }
        encoded = _b64url(
            json.dumps(
                claims,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signed = f"v1.{encoded}".encode("ascii")
        signature = hmac.new(self.assertion_key, signed, hashlib.sha256).digest()
        return f"v1.{encoded}.{_b64url(signature)}"

    def _resource_user_assertion(
        self,
        *,
        path: str,
        body: bytes,
        resource_ref: str,
        operation_id: str,
        expected_revision: int | None = None,
        workload_principal: str | None = None,
    ) -> str:
        issued_at = int(time.time())
        claims = {
            "version": "ledgerbridge.bff-user-assertion.v1",
            "issuer": self.assertion_issuer,
            "audience": self.assertion_audience,
            "subject": self.user_subject,
            "authentication_generation": self.authentication_generation,
            "method": "POST",
            "canonical_path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "resource_ref": resource_ref,
            "operation_id": operation_id,
            "workload_principal": workload_principal or self.workload_principal,
            "policy_generation": self.policy_generation,
            "issued_at": issued_at,
            "expires_at": issued_at + 45,
            "jti": str(uuid.uuid4()),
        }
        if expected_revision is not None:
            claims["expected_revision"] = expected_revision
        encoded = _b64url(
            json.dumps(
                claims,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signed = f"v1.{encoded}".encode("ascii")
        signature = hmac.new(self.assertion_key, signed, hashlib.sha256).digest()
        return f"v1.{encoded}.{_b64url(signature)}"


def _classification_groups_from_core(
    payload: dict[str, object],
    *,
    entity_ref: str,
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if (
        set(payload) != {"contract_version", "items", "next_cursor"}
        or payload.get("contract_version") != "ledgerbridge.classification-groups.v1"
        or payload.get("next_cursor") is not None
        or not isinstance(payload.get("items"), list)
        or len(payload["items"]) > 10_000  # type: ignore[arg-type]
    ):
        raise invalid
    condition_fields = {
        "key_version",
        "entity_ref",
        "source_system",
        "source_kind",
        "platform",
        "direction",
        "transaction_type",
        "counterparty_key",
        "counterparty_label",
        "counterparty_basis",
        "funding_instrument",
        "transaction_status",
        "currency",
        "risk_signature",
    }
    member_fields = {
        "candidate_ref",
        "short_id",
        "revision",
        "status",
        "amount_minor",
        "accounting_month",
        "confidence_basis_points",
        "review_risk_codes",
        "amount_outlier",
        "batch_eligible",
        "one_click_eligible",
        "exclusion_codes",
    }
    group_fields = {
        "contract_version",
        "group_ref",
        "accounting_month",
        "conditions",
        "members",
        "batch_member_count",
        "one_click_member_count",
        "terminal_statuses",
        "terminal_classifications",
        "rule_learning_eligible",
        "rule_learning_blocks",
        "active_rule",
    }
    statuses = {"INCOMPLETE", "CONFLICTED", "PENDING", "CONFIRMED", "IGNORED", "SUPERSEDED"}
    exclusion_codes = {
        "NOT_PENDING",
        "LOW_CONFIDENCE",
        "BLOCKED",
        "STRUCTURAL_RISK",
        "AMOUNT_OUTLIER",
    }
    rule_blocks = {
        "PROVISIONAL_BASIS",
        "TERMINAL_DECISION_CONFLICT",
        "REVIEW_RISK_PRESENT",
        "AMOUNT_OUTLIER",
        "NO_CONFIRMED_SOURCE",
    }
    groups: list[dict[str, object]] = []
    group_keys: list[tuple[str, str]] = []
    all_member_refs: set[str] = set()
    for raw_group in payload["items"]:  # type: ignore[index]
        if not isinstance(raw_group, dict) or set(raw_group) != group_fields:
            raise invalid
        group_ref = raw_group.get("group_ref")
        month = raw_group.get("accounting_month")
        conditions = raw_group.get("conditions")
        members = raw_group.get("members")
        if (
            raw_group.get("contract_version") != "ledgerbridge.classification-group.v1"
            or not isinstance(group_ref, str)
            or CLASSIFICATION_GROUP_REF.fullmatch(group_ref) is None
            or not isinstance(month, str)
            or PAYROLL_PERIOD.fullmatch(month) is None
            or not isinstance(conditions, dict)
            or set(conditions) != condition_fields
            or conditions.get("key_version") != "ledgerbridge.classification-key.v1"
            or conditions.get("entity_ref") != entity_ref
            or conditions.get("direction") not in {"INFLOW", "OUTFLOW", "NEUTRAL"}
            or conditions.get("counterparty_basis")
            not in {"REGISTRY_COUNTERPARTY", "EXACT_PLATFORM_SUMMARY_V1"}
            or conditions.get("currency") != "CNY"
            or not isinstance(members, list)
            or not members
        ):
            raise invalid
        text_fields = condition_fields - {
            "key_version",
            "entity_ref",
            "direction",
            "counterparty_basis",
            "currency",
            "risk_signature",
        }
        if any(
            not isinstance(conditions.get(field), str)
            or not str(conditions[field]).strip()
            for field in text_fields
        ):
            raise invalid
        risk_signature = conditions.get("risk_signature")
        if not _ordered_unique_codes(risk_signature):
            raise invalid
        mapped_members: list[dict[str, object]] = []
        member_refs: set[str] = set()
        for raw_member in members:
            if not isinstance(raw_member, dict) or set(raw_member) != member_fields:
                raise invalid
            candidate_ref = raw_member.get("candidate_ref")
            try:
                canonical_candidate_ref = str(uuid.UUID(str(candidate_ref)))
            except (TypeError, ValueError):
                raise invalid from None
            review_risks = raw_member.get("review_risk_codes")
            exclusions = raw_member.get("exclusion_codes")
            if (
                candidate_ref != canonical_candidate_ref
                or canonical_candidate_ref in member_refs
                or canonical_candidate_ref in all_member_refs
                or not isinstance(raw_member.get("short_id"), str)
                or type(raw_member.get("revision")) is not int
                or int(raw_member["revision"]) < 1
                or raw_member.get("status") not in statuses
                or type(raw_member.get("amount_minor")) is not int
                or abs(int(raw_member["amount_minor"])) > 9_007_199_254_740_991
                or raw_member.get("accounting_month") != month
                or type(raw_member.get("confidence_basis_points")) is not int
                or not 0 <= int(raw_member["confidence_basis_points"]) <= 10_000
                or not _ordered_unique_codes(review_risks)
                or review_risks != risk_signature
                or type(raw_member.get("amount_outlier")) is not bool
                or type(raw_member.get("batch_eligible")) is not bool
                or type(raw_member.get("one_click_eligible")) is not bool
                or not isinstance(exclusions, list)
                or any(code not in exclusion_codes for code in exclusions)
                or len(exclusions) != len(set(exclusions))
                or raw_member.get("one_click_eligible") is True
                and raw_member.get("batch_eligible") is not True
            ):
                raise invalid
            member_refs.add(canonical_candidate_ref)
            mapped_members.append(deepcopy(raw_member))
        all_member_refs.update(member_refs)
        batch_count = sum(member.get("batch_eligible") is True for member in mapped_members)
        one_click_count = sum(
            member.get("one_click_eligible") is True for member in mapped_members
        )

        terminal_statuses = raw_group.get("terminal_statuses")
        terminal_classifications = raw_group.get("terminal_classifications")
        blocks = raw_group.get("rule_learning_blocks")
        active_rule = raw_group.get("active_rule")
        if (
            raw_group.get("batch_member_count") != batch_count
            or raw_group.get("one_click_member_count") != one_click_count
            or not isinstance(terminal_statuses, list)
            or any(status not in statuses for status in terminal_statuses)
            or len(terminal_statuses) != len(set(terminal_statuses))
            or not isinstance(terminal_classifications, list)
            or any(not isinstance(value, str) for value in terminal_classifications)
            or terminal_classifications != sorted(set(terminal_classifications))
            or type(raw_group.get("rule_learning_eligible")) is not bool
            or not isinstance(blocks, list)
            or any(block not in rule_blocks for block in blocks)
            or len(blocks) != len(set(blocks))
            or (raw_group.get("rule_learning_eligible") is True) != (not blocks)
            or active_rule is not None
        ):
            raise invalid
        groups.append(deepcopy(raw_group))
        group_keys.append((month, group_ref))
    if group_keys != sorted(group_keys) or len(group_keys) != len(set(group_keys)):
        raise invalid
    return {
        "contract_version": "ledgerbridge.classification-groups.v1",
        "items": groups,
        "next_cursor": None,
    }


def _classification_batch_receipt_from_core(
    payload: dict[str, object],
    *,
    operation_id: str,
    group_ref: str,
    source_candidate_ref: str,
    accounting_month: object,
    target: object,
    acknowledged_risk_codes: object,
    member_refs: list[str],
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    expected = {
        "contract_version",
        "operation_id",
        "replayed",
        "group_ref",
        "accounting_month",
        "source_candidate_ref",
        "target",
        "acknowledged_risk_codes",
        "results",
    }
    results = payload.get("results")
    try:
        uuid.UUID(str(payload.get("operation_id")))
    except (TypeError, ValueError):
        raise invalid from None
    if (
        set(payload) != expected
        or payload.get("contract_version") != "ledgerbridge.classification-batch.v1"
        or payload.get("operation_id") != operation_id
        or type(payload.get("replayed")) is not bool
        or payload.get("group_ref") != group_ref
        or payload.get("accounting_month") != accounting_month
        or payload.get("source_candidate_ref") != source_candidate_ref
        or payload.get("target") != target
        or payload.get("acknowledged_risk_codes") != acknowledged_risk_codes
        or not _ordered_unique_codes(payload.get("acknowledged_risk_codes"), maximum=6)
        or not isinstance(results, list)
        or not 2 <= len(results) <= 100
    ):
        raise invalid
    mapped_results: list[dict[str, object]] = []
    candidate_refs: set[str] = set()
    operations: set[str] = set()
    event_operations: set[str] = set()
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "candidate_ref",
            "operation_id",
            "status",
            "candidate",
            "events",
        }:
            raise invalid
        try:
            candidate_ref = str(uuid.UUID(str(result.get("candidate_ref"))))
            operation_id = str(uuid.UUID(str(result.get("operation_id"))))
        except (TypeError, ValueError):
            raise invalid from None
        candidate = result.get("candidate")
        events = result.get("events")
        raw_event_operations: list[str] = []
        if isinstance(events, list):
            try:
                raw_event_operations = [
                    str(uuid.UUID(str(event.get("operation_id"))))
                    for event in events
                    if isinstance(event, dict)
                ]
            except (TypeError, ValueError):
                raise invalid from None
        target_business_unit = target.get("business_unit_ref") if isinstance(target, dict) else None
        target_category = target.get("category_code") if isinstance(target, dict) else None
        if (
            result.get("candidate_ref") != candidate_ref
            or result.get("operation_id") != operation_id
            or candidate_ref in candidate_refs
            or operation_id in operations
            or result.get("status") not in {"APPLIED", "REPLAYED"}
            or not isinstance(candidate, dict)
            or candidate.get("candidate_ref") != candidate_ref
            or candidate.get("business_unit_ref") != target_business_unit
            or candidate.get("category_code") != target_category
            or not isinstance(events, list)
            or not 1 <= len(events) <= 2
            or any(not isinstance(event, dict) for event in events)
            or len(raw_event_operations) != len(events)
            or len(raw_event_operations) != len(set(raw_event_operations))
            or any(operation in event_operations for operation in raw_event_operations)
            or any(
                event.get("candidate_ref") != candidate_ref
                or event.get("operation_id") != raw_event_operations[index]
                for index, event in enumerate(events)
                if isinstance(event, dict)
            )
            or result.get("status")
            != ("REPLAYED" if payload.get("replayed") is True else "APPLIED")
        ):
            raise invalid
        candidate_revision = candidate.get("revision")
        if (
            type(candidate_revision) is not int
            or any(
                type(event.get("from_revision")) is not int
                or type(event.get("to_revision")) is not int
                or event.get("to_revision") != event.get("from_revision") + 1
                for event in events
                if isinstance(event, dict)
            )
            or any(
                events[index].get("from_revision")
                != events[index - 1].get("to_revision")
                for index in range(1, len(events))
            )
            or events[-1].get("to_revision") != candidate_revision
        ):
            raise invalid
        candidate_refs.add(candidate_ref)
        operations.add(operation_id)
        event_operations.update(raw_event_operations)
        mapped_results.append(
            {
                "candidate_ref": candidate_ref,
                "operation_id": operation_id,
                "status": result["status"],
                "candidate": _candidate_from_core(candidate),
                "events": [_event_from_core(event) for event in events],
            }
        )
    if candidate_refs != set(member_refs):
        raise invalid
    return {
        "contract_version": "ledgerbridge.classification-batch.v1",
        "operation_id": payload["operation_id"],
        "replayed": payload["replayed"],
        "group_ref": group_ref,
        "accounting_month": accounting_month,
        "source_candidate_ref": source_candidate_ref,
        "target": deepcopy(target),
        "acknowledged_risk_codes": list(payload["acknowledged_risk_codes"]),
        "results": mapped_results,
    }


def _ordered_unique_codes(value: object, *, maximum: int = 6) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(
            isinstance(code, str) and code in CLASSIFICATION_RISK_CODES
            for code in value
        )
        and value == sorted(set(value))
    )


def sqlite_contains_business_facts(path: str | Path) -> bool:
    """Refuse a Core-backed start over a Web database containing preview facts."""

    database = Path(path)
    if not database.is_file():
        return False
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in ("candidates", "review_events", "workbook_drafts"):
            if table in tables and connection.execute(
                f'SELECT EXISTS(SELECT 1 FROM "{table}" LIMIT 1)'
            ).fetchone()[0]:
                return True
    return False


_COMPANY_TRANSACTION_CATEGORIES = frozenset(
    {
        "PLATFORM_ROOM_REVENUE",
        "RELATED_PARTY_CURRENT",
        "PAYROLL",
        "FINANCING",
        "BOTTLED_WATER",
        "INTERNAL_TRANSFER",
        "RENT",
        "RENTAL_INCOME",
        "BANK_INTEREST",
        "LINEN_LAUNDRY",
        "OPERATING_FEE",
    }
)
_COMPANY_TRANSACTION_CATEGORY_ROLES = {
    "PLATFORM_ROOM_REVENUE": "OPERATING_INCOME",
    "BANK_INTEREST": "OPERATING_INCOME",
    "RENTAL_INCOME": "OPERATING_INCOME",
    "PAYROLL": "OPERATING_EXPENSE",
    "BOTTLED_WATER": "OPERATING_EXPENSE",
    "LINEN_LAUNDRY": "OPERATING_EXPENSE",
    "RENT": "OPERATING_EXPENSE",
    "OPERATING_FEE": "OPERATING_EXPENSE",
    "RELATED_PARTY_CURRENT": "NON_OPERATING",
    "FINANCING": "NON_OPERATING",
    "INTERNAL_TRANSFER": "NON_OPERATING",
}


def _month_after(month: str) -> str:
    year, value = (int(part) for part in month.split("-"))
    if value == 12:
        return f"{year + 1:04d}-01-01"
    return f"{year:04d}-{value + 1:02d}-01"


def _company_transaction_classifications_from_core(
    payload: dict[str, object],
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if (
        set(payload) != {"contract_version", "items"}
        or payload.get("contract_version")
        != "ledgerbridge.company-transaction-classification.v1"
        or not isinstance(payload.get("items"), list)
        or len(payload["items"]) > 200  # type: ignore[arg-type]
    ):
        raise invalid
    items = [
        _company_transaction_classification_item_from_core(item, pending_only=True)
        for item in payload["items"]  # type: ignore[union-attr]
    ]
    refs = [str(item["transaction_ref"]) for item in items]
    if len(refs) != len(set(refs)):
        raise invalid
    return {
        "contract_version": "ledgerbridge.company-transaction-classifications-bff.v1",
        "items": items,
    }


def _company_transaction_classification_item_from_core(
    value: object,
    *,
    pending_only: bool,
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    required = {
        "transaction_ref",
        "entity_ref",
        "occurred_at",
        "amount_minor",
        "currency",
        "counterparty_name",
        "transaction_name",
        "status",
        "category_code",
        "cashflow_role",
        "revision",
        "source",
        "rule_version",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise invalid
    status = value.get("status")
    category = value.get("category_code")
    role = value.get("cashflow_role")
    if (
        status not in {"PENDING", "CONFIRMED"}
        or pending_only
        and status != "PENDING"
        or (status == "PENDING" and (category is not None or role is not None))
        or (
            status == "CONFIRMED"
            and (
                category not in _COMPANY_TRANSACTION_CATEGORIES
                or role != _COMPANY_TRANSACTION_CATEGORY_ROLES.get(str(category))
            )
        )
    ):
        raise invalid
    occurred_at = value.get("occurred_at")
    try:
        parsed_time = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    except ValueError as error:
        raise invalid from error
    counterparty = value.get("counterparty_name")
    if (
        parsed_time.tzinfo is None
        or not isinstance(occurred_at, str)
        or value.get("currency") != "CNY"
        or type(value.get("amount_minor")) is not int
        or abs(int(value["amount_minor"])) > JSON_SAFE_INTEGER
        or counterparty is not None
        and (not isinstance(counterparty, str) or len(counterparty) > 300)
        or not isinstance(value.get("transaction_name"), str)
        or not 1 <= len(str(value["transaction_name"])) <= 300
        or type(value.get("revision")) is not int
        or not 1 <= int(value["revision"]) <= JSON_SAFE_INTEGER
        or value.get("source") not in {"AUTO_RULE", "HUMAN_REVIEW"}
        or not isinstance(value.get("rule_version"), str)
        or not 1 <= len(str(value["rule_version"])) <= 100
    ):
        raise invalid
    return {
        **value,
        "transaction_ref": _company_report_uuid(value["transaction_ref"]),
        "entity_ref": _company_report_uuid(value["entity_ref"]),
    }


def _company_transaction_classification_summary_from_core(
    payload: dict[str, object],
    *,
    expected_from_date: str,
    expected_to_date_exclusive: str,
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if (
        set(payload) != {"contract_version", "items"}
        or payload.get("contract_version")
        != "ledgerbridge.company-transaction-classification-summary.v1"
        or not isinstance(payload.get("items"), list)
        or not 1 <= len(payload["items"]) <= 50  # type: ignore[arg-type]
    ):
        raise invalid
    items: list[dict[str, object]] = []
    for raw in payload["items"]:  # type: ignore[union-attr]
        if not isinstance(raw, dict) or set(raw) != {
            "entity_ref",
            "from_date",
            "to_date_exclusive",
            "confirmed_count",
            "pending_count",
            "confirmed_gross_minor",
            "categories",
        }:
            raise invalid
        categories = raw.get("categories")
        if not isinstance(categories, list) or len(categories) > len(
            _COMPANY_TRANSACTION_CATEGORIES
        ):
            raise invalid
        safe_categories: list[dict[str, object]] = []
        for category in categories:
            if not isinstance(category, dict) or set(category) != {
                "category_code",
                "cashflow_role",
                "transaction_count",
                "inflow_minor",
                "outflow_minor",
                "net_minor",
                "gross_minor",
                "transaction_share_ppm",
                "gross_share_ppm",
            }:
                raise invalid
            code = category.get("category_code")
            numeric = {
                key: _company_report_integer(category.get(key), nonnegative=key != "net_minor")
                for key in (
                    "transaction_count",
                    "inflow_minor",
                    "outflow_minor",
                    "net_minor",
                    "gross_minor",
                    "transaction_share_ppm",
                    "gross_share_ppm",
                )
            }
            if (
                code not in _COMPANY_TRANSACTION_CATEGORIES
                or category.get("cashflow_role")
                != _COMPANY_TRANSACTION_CATEGORY_ROLES.get(str(code))
                or numeric["net_minor"]
                != numeric["inflow_minor"] - numeric["outflow_minor"]
                or numeric["gross_minor"]
                != numeric["inflow_minor"] + numeric["outflow_minor"]
                or numeric["transaction_share_ppm"] > 1_000_000
                or numeric["gross_share_ppm"] > 1_000_000
            ):
                raise invalid
            safe_categories.append({**category, **numeric})
        codes = [str(item["category_code"]) for item in safe_categories]
        confirmed_count = _company_report_integer(
            raw.get("confirmed_count"), nonnegative=True
        )
        confirmed_gross = _company_report_integer(
            raw.get("confirmed_gross_minor"), nonnegative=True
        )
        from_date = _company_report_text(raw.get("from_date"), maximum=10)
        to_date_exclusive = _company_report_text(
            raw.get("to_date_exclusive"), maximum=10
        )
        if (
            codes != sorted(codes)
            or len(codes) != len(set(codes))
            or from_date != expected_from_date
            or to_date_exclusive != expected_to_date_exclusive
            or confirmed_count
            != sum(int(item["transaction_count"]) for item in safe_categories)
            or confirmed_gross != sum(int(item["gross_minor"]) for item in safe_categories)
        ):
            raise invalid
        items.append(
            {
                "entity_ref": _company_report_uuid(raw["entity_ref"]),
                "from_date": from_date,
                "to_date_exclusive": to_date_exclusive,
                "confirmed_count": confirmed_count,
                "pending_count": _company_report_integer(
                    raw.get("pending_count"), nonnegative=True
                ),
                "confirmed_gross_minor": confirmed_gross,
                "categories": safe_categories,
            }
        )
    refs = [str(item["entity_ref"]) for item in items]
    if refs != sorted(refs) or len(refs) != len(set(refs)):
        raise invalid
    return {
        "contract_version": payload["contract_version"],
        "items": items,
    }


def _company_transaction_classification_receipt_from_core(
    payload: dict[str, object],
    *,
    transaction_ref: str,
) -> dict[str, object]:
    invalid = CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if set(payload) != {
        "contract_version",
        "transaction_ref",
        "status",
        "category_code",
        "revision",
        "created",
    }:
        raise invalid
    category = payload.get("category_code")
    if (
        payload.get("contract_version")
        != "ledgerbridge.company-transaction-classification-review.v1"
        or payload.get("transaction_ref") != transaction_ref
        or payload.get("status") != "CONFIRMED"
        or category not in _COMPANY_TRANSACTION_CATEGORIES
        or type(payload.get("revision")) is not int
        or int(payload["revision"]) < 2
        or type(payload.get("created")) is not bool
    ):
        raise invalid
    return payload


_COMPANY_REPORT_BASES = (
    "CONFIRMED_CANDIDATE",
    "ACCOUNT_STATEMENT",
    "POSTED_LEDGER",
)
_COMPANY_REPORT_COMPOSITION_BASES = (
    "CONFIRMED_CANDIDATE",
    "POSTED_LEDGER",
)
_COMPANY_REPORT_ROLLUP_FIELDS = {
    "metrics",
    "pending_review_count",
    "attribution_pending_count",
    "missing_material_count",
    "taxonomy_version",
    "balance",
}
_COMPANY_REPORT_METRIC_FIELDS = {
    "CONFIRMED_CANDIDATE": {
        "basis",
        "confirmed_positive_minor",
        "confirmed_negative_minor",
        "confirmed_net_minor",
        "confirmed_count",
        "source_count",
    },
    "ACCOUNT_STATEMENT": {
        "basis",
        "cash_inflow_minor",
        "cash_outflow_minor",
        "net_cash_flow_minor",
        "confirmed_transaction_count",
        "statement_count",
    },
    "POSTED_LEDGER": {
        "basis",
        "revenue_minor",
        "expense_minor",
        "profit_minor",
        "posted_entry_count",
        "source_count",
    },
}
_MAX_SAFE_JSON_INTEGER = 2**53 - 1
_COMPANY_REPORT_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_COMPANY_REPORT_CURRENCY = re.compile(r"^[A-Z]{3}$")
_PERSONAL_BANK_SNAPSHOT = re.compile(r"^[0-9a-f]{64}$")
_PERSONAL_BANK_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PERSONAL_BANK_INSTITUTION = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_PERSONAL_BANK_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")


def _personal_bank_transactions_from_core(
    value: dict[str, object],
) -> dict[str, object]:
    _company_report_require_exact_keys(
        value,
        {
            "contract_version",
            "snapshot_revision",
            "owner_kind",
            "statement",
            "summary",
            "items",
        },
    )
    statement = value.get("statement")
    summary = value.get("summary")
    items = value.get("items")
    if (
        value.get("contract_version") != "ledgerbridge.personal-finance.v1"
        or value.get("owner_kind") != "PERSON"
        or not isinstance(statement, dict)
        or not isinstance(summary, dict)
        or not isinstance(items, list)
        or not 1 <= len(items) <= 10_000
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    snapshot_revision = _company_report_text(
        value.get("snapshot_revision"),
        maximum=64,
        pattern=_PERSONAL_BANK_SNAPSHOT,
    )
    mapped_statement = _personal_bank_statement_from_core(statement)
    mapped_items = [_personal_bank_transaction_from_core(item) for item in items]
    source_rows = [int(item["source_row_number"]) for item in mapped_items]
    if source_rows != sorted(set(source_rows)):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    mapped_summary = _personal_bank_summary_from_core(
        summary,
        transaction_count=len(mapped_items),
    )
    inflow = sum(max(int(item["amount_minor"]), 0) for item in mapped_items)
    outflow = sum(max(-int(item["amount_minor"]), 0) for item in mapped_items)
    if (
        mapped_statement["transaction_count"] != len(mapped_items)
        or mapped_summary["transaction_count"] != len(mapped_items)
        or mapped_summary["cash_inflow_minor"] != inflow
        or mapped_summary["cash_outflow_minor"] != outflow
        or mapped_summary["net_cash_flow_minor"] != inflow - outflow
        or any(item["currency"] != mapped_summary["currency"] for item in mapped_items)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "contract_version": "ledgerbridge.personal-bank-transactions-bff.v1",
        "snapshot_revision": snapshot_revision,
        "owner_kind": "PERSON",
        "statement": mapped_statement,
        "summary": mapped_summary,
        "items": mapped_items,
    }


def _personal_bank_statement_from_core(value: dict[str, object]) -> dict[str, object]:
    _company_report_require_exact_keys(
        value,
        {
            "statement_ref",
            "managed_account_ref",
            "institution_code",
            "account_suffix",
            "period_start",
            "period_end",
            "transaction_count",
            "review_status",
            "review_revision",
        },
    )
    period_start = _company_report_text(
        value.get("period_start"),
        maximum=10,
        pattern=_PERSONAL_BANK_DATE,
    )
    period_end = _company_report_text(
        value.get("period_end"),
        maximum=10,
        pattern=_PERSONAL_BANK_DATE,
    )
    try:
        parsed_period_start = datetime.strptime(period_start, "%Y-%m-%d").date()
        parsed_period_end = datetime.strptime(period_end, "%Y-%m-%d").date()
    except ValueError as error:
        raise CoreBackendError(
            503,
            _problem(503, "CORE_CONTRACT_INVALID"),
        ) from error
    transaction_count = _company_report_integer(
        value.get("transaction_count"),
        nonnegative=True,
    )
    review_revision = _company_report_integer(
        value.get("review_revision"),
        nonnegative=True,
    )
    review_status = value.get("review_status")
    if (
        parsed_period_start > parsed_period_end
        or not 1 <= transaction_count <= 10_000
        or review_status not in {"PENDING", "CONFIRMED", "REJECTED"}
        or review_revision < 1
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "statement_ref": _company_report_uuid(value.get("statement_ref")),
        "managed_account_ref": _company_report_uuid(value.get("managed_account_ref")),
        "institution_code": _company_report_text(
            value.get("institution_code"),
            maximum=32,
            pattern=_PERSONAL_BANK_INSTITUTION,
        ),
        "account_suffix": _company_report_text(
            value.get("account_suffix"),
            maximum=8,
            pattern=_PERSONAL_BANK_ACCOUNT_SUFFIX,
        ),
        "period_start": period_start,
        "period_end": period_end,
        "transaction_count": transaction_count,
        "review_status": review_status,
        "review_revision": review_revision,
    }


def _personal_bank_summary_from_core(
    value: dict[str, object],
    *,
    transaction_count: int,
) -> dict[str, object]:
    _company_report_require_exact_keys(
        value,
        {
            "currency",
            "cash_inflow_minor",
            "cash_outflow_minor",
            "net_cash_flow_minor",
        },
    )
    return {
        "currency": _company_report_text(
            value.get("currency"),
            maximum=3,
            pattern=re.compile(r"^CNY$"),
        ),
        "transaction_count": transaction_count,
        "cash_inflow_minor": _company_report_integer(
            value.get("cash_inflow_minor"),
            nonnegative=True,
        ),
        "cash_outflow_minor": _company_report_integer(
            value.get("cash_outflow_minor"),
            nonnegative=True,
        ),
        "net_cash_flow_minor": _company_report_integer(
            value.get("net_cash_flow_minor"),
        ),
    }


def _personal_bank_transaction_from_core(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        value,
        {
            "source_row_number",
            "occurred_at",
            "amount_minor",
            "balance_minor",
            "currency",
            "counterparty_name",
            "counterparty_account_masked",
            "counterparty_institution",
            "transaction_name",
        },
    )
    source_row_number = _company_report_integer(
        value.get("source_row_number"),
        nonnegative=True,
    )
    if source_row_number < 1:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "source_row_number": source_row_number,
        "occurred_at": _personal_bank_datetime(value.get("occurred_at")),
        "amount_minor": _company_report_integer(value.get("amount_minor")),
        "balance_minor": _company_report_integer(value.get("balance_minor")),
        "currency": _company_report_text(
            value.get("currency"),
            maximum=3,
            pattern=re.compile(r"^CNY$"),
        ),
        "counterparty_name": _personal_bank_optional_text(value.get("counterparty_name")),
        "counterparty_account_masked": _personal_bank_masked_account(
            value.get("counterparty_account_masked")
        ),
        "counterparty_institution": _personal_bank_optional_text(
            value.get("counterparty_institution")
        ),
        "transaction_name": _personal_bank_text(value.get("transaction_name")),
    }


def _personal_bank_datetime(value: object) -> str:
    text = _personal_bank_text(value, maximum=40)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID")) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return text


def _personal_bank_optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _personal_bank_text(value)


def _personal_bank_text(value: object, *, maximum: int = 300) -> str:
    text = _company_report_text(value, maximum=maximum)
    if not text.strip() or not text.isprintable():
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return text


def _personal_bank_masked_account(value: object) -> str | None:
    text = _personal_bank_optional_text(value)
    if text is None:
        return None
    if (len(text) <= 4 and set(text) != {"*"}) or (
        len(text) > 4 and set(text[:-4]) != {"*"}
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return text


def _validate_company_report_layer_identities(
    layers: list[dict[str, object]],
) -> None:
    identities: dict[str, tuple[str, str]] = {}
    expected_company_refs: set[str] | None = None
    for layer in layers:
        seen: set[str] = set()
        for company in layer["items"]:  # type: ignore[union-attr]
            company_ref = str(company["company_ref"])
            identity = (str(company["company_name"]), str(company["currency"]))
            if company_ref in seen or (
                company_ref in identities and identities[company_ref] != identity
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            seen.add(company_ref)
            identities[company_ref] = identity
        if expected_company_refs is None:
            expected_company_refs = seen
        elif seen != expected_company_refs:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _company_report_composition_from_core(
    value: dict[str, object],
    basis: str,
    from_month: str,
    to_month: str,
) -> dict[str, object]:
    _company_report_require_exact_keys(
        value,
        {"contract_version", "basis", "from_month", "to_month", "items"},
    )
    items = value.get("items")
    if (
        value.get("contract_version")
        != "ledgerbridge.company-report-composition.v1"
        or basis not in _COMPANY_REPORT_COMPOSITION_BASES
        or value.get("basis") != basis
        or value.get("from_month") != from_month
        or value.get("to_month") != to_month
        or not isinstance(items, list)
        or len(items) > 50
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    mapped_items = [
        _company_report_composition_item_from_core(item, basis)
        for item in items
    ]
    _company_report_require_stable_unique(mapped_items, "company_ref")
    return {
        "contract_version": "ledgerbridge.company-report-composition.v1",
        "basis": basis,
        "from_month": from_month,
        "to_month": to_month,
        "items": mapped_items,
    }


def _merge_personal_bank_transactions(
    pages: list[dict[str, object]],
) -> dict[str, object]:
    statements: list[dict[str, object]] = []
    items: list[dict[str, object]] = []
    snapshots: list[tuple[str, str]] = []
    cash_inflow_minor = 0
    cash_outflow_minor = 0
    net_cash_flow_minor = 0
    for page in pages:
        statement = page.get("statement")
        summary = page.get("summary")
        page_items = page.get("items")
        snapshot_revision = page.get("snapshot_revision")
        if (
            not isinstance(statement, dict)
            or not isinstance(summary, dict)
            or not isinstance(page_items, list)
            or not isinstance(snapshot_revision, str)
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        statement_ref = str(statement["statement_ref"])
        statements.append(statement)
        snapshots.append((statement_ref, snapshot_revision))
        cash_inflow_minor += int(summary["cash_inflow_minor"])
        cash_outflow_minor += int(summary["cash_outflow_minor"])
        net_cash_flow_minor += int(summary["net_cash_flow_minor"])
        items.extend({"statement_ref": statement_ref, **item} for item in page_items)
    if (
        len({str(statement["statement_ref"]) for statement in statements})
        != len(statements)
        or len(items) > 10_000
        or sum(int(statement["transaction_count"]) for statement in statements)
        != len(items)
        or cash_inflow_minor - cash_outflow_minor != net_cash_flow_minor
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    items.sort(
        key=lambda item: (
            datetime.fromisoformat(str(item["occurred_at"]).replace("Z", "+00:00")),
            str(item["statement_ref"]),
            int(item["source_row_number"]),
        ),
        reverse=True,
    )
    combined_snapshot = hashlib.sha256(
        json.dumps(snapshots, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return {
        "contract_version": "ledgerbridge.personal-bank-transactions-bff.v2",
        "snapshot_revision": combined_snapshot,
        "owner_kind": "PERSON",
        "statements": statements,
        "summary": {
            "currency": "CNY",
            "statement_count": len(statements),
            "transaction_count": len(items),
            "cash_inflow_minor": cash_inflow_minor,
            "cash_outflow_minor": cash_outflow_minor,
            "net_cash_flow_minor": net_cash_flow_minor,
        },
        "items": items,
    }


def _company_report_composition_item_from_core(
    value: object,
    basis: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    composition_fields = (
        ("positive", "negative")
        if basis == "CONFIRMED_CANDIDATE"
        else ("revenue", "expense")
    )
    _company_report_require_exact_keys(
        value,
        {
            "company_ref",
            "company_name",
            "currency",
            "basis",
            *composition_fields,
        },
    )
    if value.get("basis") != basis:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "company_ref": _company_report_uuid(value.get("company_ref")),
        "company_name": _company_report_text(value.get("company_name"), maximum=200),
        "currency": _company_report_text(
            value.get("currency"),
            maximum=3,
            pattern=_COMPANY_REPORT_CURRENCY,
        ),
        "basis": basis,
        **{
            field: _company_report_category_composition_from_core(value.get(field))
            for field in composition_fields
        },
    }


def _company_report_category_composition_from_core(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(value, {"total_minor", "fact_count", "items"})
    items = value.get("items")
    if not isinstance(items, list) or len(items) > 100:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    total_minor = _company_report_integer(value.get("total_minor"), nonnegative=True)
    fact_count = _company_report_integer(value.get("fact_count"), nonnegative=True)
    mapped_items = [_company_report_category_slice_from_core(item) for item in items]
    identities = [
        (item["category_code"], item["category_label"])
        for item in mapped_items
    ]
    ordered = sorted(
        mapped_items,
        key=lambda item: (
            -int(item["amount_minor"]),
            item["category_code"] is None,
            str(item["category_code"] or ""),
            str(item["category_label"] or ""),
        ),
    )
    if (
        len(identities) != len(set(identities))
        or mapped_items != ordered
        or sum(int(item["amount_minor"]) for item in mapped_items) != total_minor
        or sum(int(item["fact_count"]) for item in mapped_items) != fact_count
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "total_minor": total_minor,
        "fact_count": fact_count,
        "items": mapped_items,
    }


def _company_report_category_slice_from_core(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        value,
        {"category_code", "category_label", "amount_minor", "fact_count"},
    )
    category_code = value.get("category_code")
    category_label = value.get("category_label")
    if (category_code is None) != (category_label is None):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if category_code is not None:
        category_code = _company_report_text(category_code, maximum=100)
        category_label = _company_report_text(category_label, maximum=200)
    amount_minor = _company_report_integer(value.get("amount_minor"), nonnegative=True)
    fact_count = _company_report_integer(value.get("fact_count"), nonnegative=True)
    if amount_minor == 0 or fact_count == 0:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "category_code": category_code,
        "category_label": category_label,
        "amount_minor": amount_minor,
        "fact_count": fact_count,
    }


def _validate_company_report_composition_against_layer(
    composition: dict[str, object],
    layer: dict[str, object],
) -> None:
    composition_items = composition["items"]
    layer_items = layer["items"]
    if not isinstance(composition_items, list) or not isinstance(layer_items, list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    composition_by_ref = {
        str(item["company_ref"]): item for item in composition_items
    }
    layer_by_ref = {str(item["company_ref"]): item for item in layer_items}
    if set(composition_by_ref) != set(layer_by_ref):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    basis = str(composition["basis"])
    for company_ref, item in composition_by_ref.items():
        report = layer_by_ref[company_ref]
        if (
            item["company_name"] != report["company_name"]
            or item["currency"] != report["currency"]
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        metrics = report["metrics"]
        if not isinstance(metrics, dict):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        expected_totals = (
            (
                ("positive", metrics["confirmed_positive_minor"]),
                ("negative", -int(metrics["confirmed_negative_minor"])),
            )
            if basis == "CONFIRMED_CANDIDATE"
            else (
                ("revenue", metrics["revenue_minor"]),
                ("expense", metrics["expense_minor"]),
            )
        )
        for field, expected_total in expected_totals:
            category = item[field]
            if not isinstance(category, dict) or category["total_minor"] != expected_total:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _company_report_layer_from_core(
    value: dict[str, object],
    basis: str,
    from_month: str,
    to_month: str,
) -> dict[str, object]:
    _company_report_require_exact_keys(
        value,
        {"contract_version", "basis", "from_month", "to_month", "items"},
    )
    items = value.get("items")
    if (
        value.get("contract_version") != "ledgerbridge.company-report.v1"
        or value.get("basis") != basis
        or value.get("from_month") != from_month
        or value.get("to_month") != to_month
        or not isinstance(items, list)
        or len(items) > 50
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    mapped_items = [
        _company_report_item_from_core(item, basis, from_month, to_month)
        for item in items
    ]
    _company_report_require_stable_unique(mapped_items, "company_ref")
    return {
        "contract_version": "ledgerbridge.company-report.v1",
        "basis": basis,
        "from_month": from_month,
        "to_month": to_month,
        "items": mapped_items,
    }


def _company_report_item_from_core(
    value: object,
    basis: str,
    from_month: str,
    to_month: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("months"), list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        value,
        {
            "company_ref",
            "company_name",
            "currency",
            "business_unit_breakdown_status",
            "months",
        }
        | _COMPANY_REPORT_ROLLUP_FIELDS,
    )
    if len(value["months"]) > 24:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    months = [
        _company_report_month_from_core(month, basis, from_month, to_month)
        for month in value["months"]
    ]
    _company_report_require_stable_unique(months, "month")
    breakdown_status = _company_report_item_breakdown_status(basis, months)
    if value.get("business_unit_breakdown_status") != breakdown_status:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "company_ref": _company_report_uuid(value.get("company_ref")),
        "company_name": _company_report_text(value.get("company_name"), maximum=200),
        "currency": _company_report_text(
            value.get("currency"),
            maximum=3,
            pattern=_COMPANY_REPORT_CURRENCY,
        ),
        "business_unit_breakdown_status": breakdown_status,
        **_company_report_rollup_from_core(value, basis),
        "months": months,
    }


def _company_report_month_from_core(
    value: object,
    basis: str,
    from_month: str,
    to_month: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        value,
        {"month", "business_unit_breakdown_status", "business_units"}
        | _COMPANY_REPORT_ROLLUP_FIELDS,
    )
    breakdown_status = value.get("business_unit_breakdown_status")
    business_units = value.get("business_units")
    available = (
        breakdown_status == "AVAILABLE"
        and isinstance(business_units, list)
        and bool(business_units)
    )
    empty = breakdown_status == "EMPTY" and business_units == []
    statement_unavailable = (
        basis == "ACCOUNT_STATEMENT"
        and breakdown_status == "UNAVAILABLE_ATTRIBUTION_PENDING"
        and business_units is None
    )
    missing_snapshot_unavailable = (
        basis in {"ACCOUNT_STATEMENT", "POSTED_LEDGER"}
        and breakdown_status == "UNAVAILABLE_MISSING_SNAPSHOT"
        and business_units is None
    )
    if not (
        available
        or empty
        or statement_unavailable
        or missing_snapshot_unavailable
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    month = _company_report_text(
        value.get("month"),
        maximum=7,
        pattern=_COMPANY_REPORT_MONTH,
    )
    if not from_month <= month <= to_month:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    mapped_business_units = None
    if isinstance(business_units, list):
        if len(business_units) > 50:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        mapped_business_units = [
            _company_report_business_unit_from_core(business_unit, basis)
            for business_unit in business_units
        ]
        _company_report_require_stable_unique(
            mapped_business_units,
            "business_unit_ref",
        )
    return {
        "month": month,
        **_company_report_rollup_from_core(value, basis),
        "business_unit_breakdown_status": breakdown_status,
        "business_units": mapped_business_units,
    }


def _company_report_item_breakdown_status(
    basis: str,
    months: list[dict[str, object]],
) -> str:
    if not months:
        return "EMPTY"
    statuses = {str(month["business_unit_breakdown_status"]) for month in months}
    if basis == "ACCOUNT_STATEMENT" and "UNAVAILABLE_ATTRIBUTION_PENDING" in statuses:
        return "UNAVAILABLE_ATTRIBUTION_PENDING"
    if basis in {"ACCOUNT_STATEMENT", "POSTED_LEDGER"} and "UNAVAILABLE_MISSING_SNAPSHOT" in statuses:
        return "UNAVAILABLE_MISSING_SNAPSHOT"
    if "AVAILABLE" in statuses:
        return "AVAILABLE"
    return "EMPTY"


def _company_report_business_unit_from_core(
    value: object,
    basis: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        value,
        {"business_unit_ref", "business_unit_label"}
        | _COMPANY_REPORT_ROLLUP_FIELDS,
    )
    return {
        "business_unit_ref": _company_report_text(
            value.get("business_unit_ref"),
            maximum=200,
        ),
        "business_unit_label": _company_report_text(
            value.get("business_unit_label"),
            maximum=200,
        ),
        **_company_report_rollup_from_core(value, basis),
    }


def _company_report_rollup_from_core(
    value: dict[str, object],
    basis: str,
) -> dict[str, object]:
    metrics = value.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("basis") != basis:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(metrics, _COMPANY_REPORT_METRIC_FIELDS[basis])
    pending_review_count = _company_report_integer(
        value.get("pending_review_count"),
        nonnegative=True,
    )
    attribution_pending_count = _company_report_integer(
        value.get("attribution_pending_count"),
        nonnegative=True,
    )
    missing_material = value.get("missing_material_count")
    if missing_material is not None:
        missing_material = _company_report_integer(
            missing_material,
            nonnegative=True,
        )
    taxonomy_version = value.get("taxonomy_version")
    if taxonomy_version is not None:
        taxonomy_version = _company_report_text(taxonomy_version, maximum=200)
    if (missing_material is None) != (taxonomy_version is None):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    balance = value.get("balance")
    if (
        not isinstance(balance, dict)
        or balance.get("balance_basis") != "UNAVAILABLE"
        or balance.get("opening_balance_minor") is not None
        or balance.get("closing_balance_minor") is not None
        or balance.get("gap") != "AUTHORITATIVE_BALANCE_UNAVAILABLE"
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _company_report_require_exact_keys(
        balance,
        {
            "balance_basis",
            "opening_balance_minor",
            "closing_balance_minor",
            "gap",
        },
    )
    return {
        "metrics": _company_report_metrics_from_core(metrics, basis),
        "pending_review_count": pending_review_count,
        "attribution_pending_count": attribution_pending_count,
        "missing_material_count": missing_material,
        "taxonomy_version": taxonomy_version,
        "balance": {
            "balance_basis": "UNAVAILABLE",
            "opening_balance_minor": None,
            "closing_balance_minor": None,
            "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
        },
    }


def _company_report_metrics_from_core(
    value: dict[str, object],
    basis: str,
) -> dict[str, object]:
    if basis == "CONFIRMED_CANDIDATE":
        positive = _company_report_integer(
            value.get("confirmed_positive_minor"),
            nonnegative=True,
        )
        negative = _company_report_integer(
            value.get("confirmed_negative_minor"),
            nonpositive=True,
        )
        net = _company_report_integer(value.get("confirmed_net_minor"))
        if net != positive + negative:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        confirmed_count = _company_report_integer(
            value.get("confirmed_count"),
            nonnegative=True,
        )
        source_count = _company_report_integer(
            value.get("source_count"),
            nonnegative=True,
        )
        if source_count > confirmed_count:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return {
            "basis": basis,
            "confirmed_positive_minor": positive,
            "confirmed_negative_minor": negative,
            "confirmed_net_minor": net,
            "confirmed_count": confirmed_count,
            "source_count": source_count,
        }
    if basis == "ACCOUNT_STATEMENT":
        inflow = _company_report_integer(
            value.get("cash_inflow_minor"),
            nonnegative=True,
        )
        outflow = _company_report_integer(
            value.get("cash_outflow_minor"),
            nonnegative=True,
        )
        net = _company_report_integer(value.get("net_cash_flow_minor"))
        if net != inflow - outflow:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        confirmed_transaction_count = _company_report_integer(
            value.get("confirmed_transaction_count"),
            nonnegative=True,
        )
        statement_count = _company_report_integer(
            value.get("statement_count"),
            nonnegative=True,
        )
        if statement_count > confirmed_transaction_count:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return {
            "basis": basis,
            "cash_inflow_minor": inflow,
            "cash_outflow_minor": outflow,
            "net_cash_flow_minor": net,
            "confirmed_transaction_count": confirmed_transaction_count,
            "statement_count": statement_count,
        }
    if basis != "POSTED_LEDGER":
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    revenue = _company_report_integer(value.get("revenue_minor"), nonnegative=True)
    expense = _company_report_integer(value.get("expense_minor"), nonnegative=True)
    profit = _company_report_integer(value.get("profit_minor"))
    if profit != revenue - expense:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    posted_entry_count = _company_report_integer(
        value.get("posted_entry_count"),
        nonnegative=True,
    )
    source_count = _company_report_integer(
        value.get("source_count"),
        nonnegative=True,
    )
    if source_count > posted_entry_count:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return {
        "basis": basis,
        "revenue_minor": revenue,
        "expense_minor": expense,
        "profit_minor": profit,
        "posted_entry_count": posted_entry_count,
        "source_count": source_count,
    }


def _company_report_integer(
    value: object,
    *,
    nonnegative: bool = False,
    nonpositive: bool = False,
) -> int:
    if (
        type(value) is not int
        or abs(value) > _MAX_SAFE_JSON_INTEGER
        or (nonnegative and value < 0)
        or (nonpositive and value > 0)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return value


def _company_report_text(
    value: object,
    *,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return value


def _company_report_uuid(value: object) -> str:
    text = _company_report_text(value, maximum=36)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise CoreBackendError(
            503,
            _problem(503, "CORE_CONTRACT_INVALID"),
        ) from error
    if str(parsed) != text:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    return text


def _company_report_require_stable_unique(
    items: list[dict[str, object]],
    key: str,
) -> None:
    values = [str(item[key]) for item in items]
    if values != sorted(values) or len(values) != len(set(values)):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _company_report_require_exact_keys(
    value: dict[str, object],
    required: set[str],
) -> None:
    if set(value) != required:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _candidate_from_core(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    source = value.get("source")
    evidence = value.get("evidence")
    blockers = value.get("blockers")
    review_risks = value.get("review_risks")
    if not isinstance(source, dict) or not isinstance(evidence, list) or not isinstance(blockers, list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if review_risks is None:
        review_risks = [
            {
                "code": "RISK_ASSESSMENT_UNAVAILABLE",
                "message": "风险评估尚未就绪，暂不允许批量审批",
            }
        ]
    if not isinstance(review_risks, list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    source_system = source.get("source_system")
    if not isinstance(source_system, str) or re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,63}",
        source_system,
    ) is None:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    dimension_values: dict[str, str | None] = {}
    for reference_field, label_field in (
        ("business_unit_ref", "business_unit_label"),
        ("category_code", "category_label"),
    ):
        reference = value.get(reference_field)
        label = value.get(label_field)
        if (reference is None) != (label is None) or (
            reference is not None
            and (
                not isinstance(reference, str)
                or not 1 <= len(reference) <= 100
                or not isinstance(label, str)
                or not 1 <= len(label) <= 200
            )
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        dimension_values[reference_field] = reference
        dimension_values[label_field] = label
    channel = str(source.get("ingest_channel", "")).lower()
    return {
        "id": value.get("candidate_ref"),
        "short_id": value.get("short_id"),
        "revision": value.get("revision"),
        "status": value.get("status"),
        "source_channel": channel,
        "source_system": source_system,
        "source_message_id": source.get("source_event_ref"),
        "received_at": value.get("created_at"),
        "business_unit": dimension_values["business_unit_label"] or "",
        "business_unit_ref": dimension_values["business_unit_ref"],
        "category": dimension_values["category_label"] or "",
        "category_code": dimension_values["category_code"],
        "amount_minor": value.get("amount_minor") if value.get("amount_minor") is not None else 0,
        "currency": value.get("currency"),
        "accounting_month": value.get("accounting_month"),
        "summary": value.get("summary"),
        "confidence_basis_points": value.get("confidence_basis_points"),
        "evidence": [_evidence_from_core(item) for item in evidence],
        "blockers": [
            {"code": item.get("code"), "message": item.get("message")}
            for item in blockers
            if isinstance(item, dict)
        ],
        "review_risks": [
            {"code": item.get("code"), "message": item.get("message")}
            for item in review_risks
            if isinstance(item, dict)
        ],
    }


def _evidence_from_core(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    kind = str(value.get("kind"))
    mapped: dict[str, object] = {
        "id": value.get("evidence_ref"),
        "kind": "attachment" if kind == "ATTACHMENT" else "message",
        "media_type": value.get("media_type"),
        "sha256": None,
        "original_filename": value.get("display_name"),
    }
    unlock_status = value.get("unlock_status")
    source_ref = value.get("source_ref")
    if unlock_status is not None:
        if unlock_status not in EVIDENCE_UNLOCK_STATUSES:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if source_ref is not None:
            try:
                source_ref = _opaque_source_ref(source_ref)
            except (TypeError, ValueError) as error:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID")) from error
        if unlock_status == "PASSWORD_REQUIRED" and source_ref is None:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        mapped["unlock_status"] = unlock_status
        mapped["source_ref"] = source_ref
    return mapped


def _event_from_core(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    changes = value.get("changes")
    if not isinstance(changes, list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    direct_field_map = {
        "amount_minor": "amount_minor",
        "accounting_month": "accounting_month",
        "status": "status",
    }
    dimension_field_map = {
        "business_unit_ref": ("business_unit", "identity"),
        "business_unit_label": ("business_unit", "label"),
        "category_code": ("category", "identity"),
        "category_label": ("category", "label"),
    }
    dimension_label_fields = {
        "business_unit": "business_unit_label",
        "category": "category_label",
    }
    ordered_changes: list[tuple[str, object]] = []
    dimension_changes: dict[str, dict[str, object]] = {}
    for change in changes:
        if not isinstance(change, dict):
            continue
        field = change.get("field")
        if field in dimension_field_map:
            public_field, value_kind = dimension_field_map[str(field)]
            if public_field not in dimension_changes:
                dimension_changes[public_field] = {}
                ordered_changes.append(("dimension", public_field))
            dimension_changes[public_field][f"previous_{value_kind}"] = change.get("previous_value")
            dimension_changes[public_field][f"new_{value_kind}"] = change.get("new_value")
        elif field in direct_field_map:
            ordered_changes.append(
                (
                    "direct",
                    {
                        "field": direct_field_map[str(field)],
                        "previous_value": change.get("previous_value"),
                        "new_value": change.get("new_value"),
                        "identity_changed": False,
                    },
                )
            )

    prior_projection = value.get("prior_projection")
    result_projection = value.get("result_projection")
    prior = prior_projection if isinstance(prior_projection, dict) else {}
    result = result_projection if isinstance(result_projection, dict) else {}
    mapped_changes: list[dict[str, object]] = []
    missing = object()
    for change_kind, mapped in ordered_changes:
        if change_kind == "direct":
            mapped_changes.append(mapped)  # type: ignore[arg-type]
            continue
        public_field = str(mapped)
        dimension = dimension_changes[public_field]
        label_field = dimension_label_fields[public_field]
        previous_label = dimension.get("previous_label", prior.get(label_field, missing))
        new_label = dimension.get("new_label", result.get(label_field, missing))
        identity_changed = (
            "previous_identity" in dimension
            and dimension.get("previous_identity") != dimension.get("new_identity")
        )
        if (
            previous_label is missing
            or new_label is missing
            or not isinstance(previous_label, (str, type(None)))
            or not isinstance(new_label, (str, type(None)))
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        mapped_changes.append(
            {
                "field": public_field,
                "previous_value": previous_label,
                "new_value": new_label,
                "identity_changed": identity_changed,
            }
        )
    action = str(value.get("action"))
    decision = "CORRECT_AND_CONFIRM" if action == "COMPLETE_FIELDS" else action
    resolutions = value.get("resolved_conflicts")
    resolution = None
    if isinstance(resolutions, list):
        values = [str(item.get("resolution")) for item in resolutions if isinstance(item, dict)]
        resolution = "; ".join(values) or None
    return {
        "id": value.get("operation_id"),
        "candidate_id": value.get("candidate_ref"),
        "sequence": int(value.get("to_revision", 1)) - 1,
        "from_revision": value.get("from_revision"),
        "to_revision": value.get("to_revision"),
        "decision": decision,
        "actor": value.get("actor_ref"),
        "reason": value.get("reason"),
        "changes": mapped_changes,
        "conflict_resolution": resolution,
        "created_at": value.get("created_at"),
    }


def _original_reconciliation_from_core(
    value: object,
    *,
    month: str,
    entity_ref: str,
    business_unit_ref: str,
) -> dict[str, object]:
    invalid = lambda: CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if not isinstance(value, dict):
        raise invalid()
    if set(value) != {
        "contract_version",
        "taxonomy_version",
        "layout_version",
        "mapping_version",
        "is_complete",
        "posted_ledger_complete",
        "projection_gaps",
        "month",
        "scope",
        "columns",
        "rows",
        "totals",
        "pending_review_count",
        "confirmed_pending_posting_count",
        "missing_material_count",
        "unmapped_confirmed_count",
        "sources",
    }:
        raise invalid()
    scope = value.get("scope")
    columns = value.get("columns")
    rows = value.get("rows")
    totals = value.get("totals")
    sources = value.get("sources")
    projection_gaps = value.get("projection_gaps")
    if (
        value.get("contract_version") != "ledgerbridge.original-reconciliation.v1"
        or value.get("taxonomy_version") != "ledgerbridge.financial-foundation-blocker-taxonomy.v1"
        or not isinstance(value.get("layout_version"), str)
        or not 1 <= len(value["layout_version"]) <= 100
        or not isinstance(value.get("mapping_version"), str)
        or not 1 <= len(value["mapping_version"]) <= 100
        or type(value.get("is_complete")) is not bool
        or type(value.get("posted_ledger_complete")) is not bool
        or value.get("month") != month
        or not isinstance(scope, dict)
        or set(scope) != {"entity_ref", "business_unit_ref"}
        or scope.get("entity_ref") != entity_ref
        or scope.get("business_unit_ref") != business_unit_ref
        or not isinstance(columns, list)
        or not isinstance(rows, list)
        or not isinstance(totals, dict)
        or not isinstance(sources, list)
        or not isinstance(projection_gaps, list)
        or any(
            not isinstance(gap, str)
            or gap
            not in {"MISSING_TIME_GRANULARITY", "MISSING_BUSINESS_UNIT_ATTRIBUTION"}
            for gap in projection_gaps
        )
        or len(projection_gaps) != len(set(projection_gaps))
    ):
        raise invalid()

    expected_columns = [chr(ord("A") + offset) for offset in range(13)]
    safe_columns: list[dict[str, object]] = []
    if len(columns) != len(expected_columns):
        raise invalid()
    for ordinal, (column, expected_column) in enumerate(zip(columns, expected_columns), start=1):
        if not isinstance(column, dict):
            raise invalid()
        role = column.get("role")
        expected_role = "MAIN" if ordinal <= 5 else "SPACER" if ordinal <= 7 else "DETAIL"
        if (
            set(column) != {"column", "ordinal", "role"}
            or
            column.get("column") != expected_column
            or column.get("ordinal") != ordinal
            or role != expected_role
        ):
            raise invalid()
        safe_columns.append({"column": expected_column, "ordinal": ordinal, "role": role})

    safe_rows: list[dict[str, object]] = []
    if len(rows) != 40:
        raise invalid()
    for expected_row_number, row in enumerate(rows, start=1):
        if (
            not isinstance(row, dict)
            or set(row) != {"row_number", "cells"}
            or row.get("row_number") != expected_row_number
        ):
            raise invalid()
        cells = row.get("cells")
        if not isinstance(cells, list) or len(cells) != len(expected_columns):
            raise invalid()
        safe_cells: list[dict[str, object]] = []
        for cell, expected_column in zip(cells, expected_columns):
            if not isinstance(cell, dict):
                raise invalid()
            kind = cell.get("kind")
            label = cell.get("label")
            amount_minor = cell.get("amount_minor")
            currency = cell.get("currency")
            gap_code = cell.get("gap_code")
            source_fact_refs = cell.get("source_fact_refs")
            if (
                set(cell) != {
                    "coordinate",
                    "column",
                    "row_number",
                    "kind",
                    "label",
                    "amount_minor",
                    "currency",
                    "gap_code",
                    "source_fact_refs",
                }
                or cell.get("coordinate") != f"{expected_column}{expected_row_number}"
                or cell.get("column") != expected_column
                or cell.get("row_number") != expected_row_number
                or kind not in {"BLANK", "LABEL", "AMOUNT", "GAP"}
                or not isinstance(source_fact_refs, list)
                or any(not isinstance(item, str) or not 1 <= len(item) <= 200 for item in source_fact_refs)
                or len(set(source_fact_refs)) != len(source_fact_refs)
                or (label is not None and (not isinstance(label, str) or not 1 <= len(label) <= 200))
                or expected_column in {"F", "G"} and kind != "BLANK"
            ):
                raise invalid()
            if kind == "BLANK":
                valid_kind = label is None and amount_minor is None and currency is None and gap_code is None and not source_fact_refs
            elif kind == "LABEL":
                valid_kind = isinstance(label, str) and amount_minor is None and currency is None and gap_code is None
            elif kind == "AMOUNT":
                valid_kind = (
                    type(amount_minor) is int
                    and abs(amount_minor) <= 9_007_199_254_740_991
                    and currency == "CNY"
                    and gap_code is None
                )
            else:
                valid_kind = (
                    label is None
                    and amount_minor is None
                    and currency is None
                    and gap_code in {
                        "MISSING_LEGACY_SLOT_MAPPING",
                        "MISSING_BALANCE_MAPPING",
                        "MISSING_ECONOMIC_EFFECT",
                        "POSTED_LEDGER_UNAVAILABLE",
                    }
                )
            if not valid_kind:
                raise invalid()
            safe_cells.append(
                {
                    "coordinate": f"{expected_column}{expected_row_number}",
                    "column": expected_column,
                    "row_number": expected_row_number,
                    "kind": kind,
                    "label": label,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "gap_code": gap_code,
                    "source_fact_refs": list(source_fact_refs),
                }
            )
        safe_rows.append({"row_number": expected_row_number, "cells": safe_cells})

    posted_money_fields = (
        "posted_income_minor",
        "posted_expense_minor",
        "posted_profit_minor",
        "posted_amount_minor",
    )
    if set(totals) != {
        "posted_income_minor",
        "posted_expense_minor",
        "posted_profit_minor",
        "opening_balance_minor",
        "closing_balance_minor",
        "mapped_cell_count",
        "confirmed_candidate_amount_minor",
        "posted_amount_minor",
        "currency",
    }:
        raise invalid()
    if (
        type(totals.get("confirmed_candidate_amount_minor")) is not int
        or abs(totals["confirmed_candidate_amount_minor"]) > 9_007_199_254_740_991
    ):
        raise invalid()
    posted_ledger_complete = value["posted_ledger_complete"]
    if posted_ledger_complete:
        if any(type(totals.get(field)) is not int or abs(totals[field]) > 9_007_199_254_740_991 for field in posted_money_fields):
            raise invalid()
        if totals.get("posted_profit_minor") != totals.get("posted_income_minor") - totals.get("posted_expense_minor"):
            raise invalid()
    elif any(totals.get(field) is not None for field in posted_money_fields):
        raise invalid()
    opening_balance = totals.get("opening_balance_minor")
    closing_balance = totals.get("closing_balance_minor")
    if (
        totals.get("currency") != "CNY"
        or type(totals.get("mapped_cell_count")) is not int
        or not 0 <= totals.get("mapped_cell_count") <= 520
        or opening_balance is not None and (type(opening_balance) is not int or abs(opening_balance) > 9_007_199_254_740_991)
        or closing_balance is not None and (type(closing_balance) is not int or abs(closing_balance) > 9_007_199_254_740_991)
    ):
        raise invalid()
    safe_totals = {
        field: totals[field]
        for field in (
            *posted_money_fields,
            "confirmed_candidate_amount_minor",
            "opening_balance_minor",
            "closing_balance_minor",
            "mapped_cell_count",
            "currency",
        )
    }

    count_fields = (
        "pending_review_count",
        "confirmed_pending_posting_count",
        "missing_material_count",
        "unmapped_confirmed_count",
    )
    if any(type(value.get(field)) is not int or value[field] < 0 for field in count_fields):
        raise invalid()
    safe_sources: list[dict[str, object]] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "source_kind",
            "source_system",
            "source_label",
            "fact_count",
            "mapped_fact_count",
            "amount_minor",
        }:
            raise invalid()
        source_kind = source.get("source_kind")
        source_system = source.get("source_system")
        source_label = source.get("source_label")
        fact_count = source.get("fact_count")
        mapped_fact_count = source.get("mapped_fact_count")
        amount_minor = source.get("amount_minor")
        if (
            source_kind not in {"POSTED_LEDGER", "CONFIRMED_CANDIDATE", "ACCOUNT_STATEMENT"}
            or not isinstance(source_system, str)
            or not 1 <= len(source_system) <= 100
            or source_label is not None and (not isinstance(source_label, str) or not 1 <= len(source_label) <= 200)
            or type(fact_count) is not int
            or fact_count < 1
            or type(mapped_fact_count) is not int
            or not 0 <= mapped_fact_count <= fact_count
            or type(amount_minor) is not int
            or abs(amount_minor) > 9_007_199_254_740_991
        ):
            raise invalid()
        safe_sources.append(
            {
                "source_kind": source_kind,
                "source_system": source_system,
                "source_label": source_label,
                "fact_count": fact_count,
                "mapped_fact_count": mapped_fact_count,
                "amount_minor": amount_minor,
            }
        )

    confirmed_source_count = sum(
        source["fact_count"]
        for source in safe_sources
        if source["source_kind"] == "CONFIRMED_CANDIDATE"
    )
    posted_sources_fully_mapped = all(
        source["mapped_fact_count"] == source["fact_count"]
        for source in safe_sources
        if source["source_kind"] == "POSTED_LEDGER"
    )
    if (
        value["confirmed_pending_posting_count"] != confirmed_source_count
        or value["unmapped_confirmed_count"] > confirmed_source_count
    ):
        raise invalid()

    has_gap = any(
        cell["kind"] == "GAP"
        for row in safe_rows
        for cell in row["cells"]  # type: ignore[index]
    )
    if value["is_complete"] and (
        has_gap
        or projection_gaps
        or any(value[field] > 0 for field in count_fields)
        or opening_balance is None
        or closing_balance is None
        or not posted_ledger_complete
        or not posted_sources_fully_mapped
    ):
        raise invalid()

    return {
        "contract_version": "ledgerbridge.original-reconciliation.v1",
        "taxonomy_version": value["taxonomy_version"],
        "layout_version": value["layout_version"],
        "mapping_version": value["mapping_version"],
        "is_complete": value["is_complete"],
        "posted_ledger_complete": posted_ledger_complete,
        "projection_gaps": list(projection_gaps),
        "month": month,
        "scope": {"entity_ref": entity_ref, "business_unit_ref": business_unit_ref},
        "columns": safe_columns,
        "rows": safe_rows,
        "totals": safe_totals,
        **{field: value[field] for field in count_fields},
        "sources": safe_sources,
    }


def _bounded(value: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError("Core adapter configuration is missing or too long")
    return value


def _opaque_source_ref(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("source_ref must be a string")
    canonical = str(uuid.UUID(value))
    if value != canonical:
        raise ValueError("source_ref must be a canonical opaque UUID")
    return canonical


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_payroll_payload(
    payload: dict[str, object],
    *,
    expected_contract_version: str,
    expected_entity_ref: str,
) -> None:
    if (
        payload.get("contract_version") != expected_contract_version
        or payload.get("entity_ref") != expected_entity_ref
        or not isinstance(payload.get("data"), dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    company_id = payload.get("company_id")
    if not isinstance(company_id, str) or PAYROLL_RESOURCE_REF.fullmatch(company_id) is None:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))

    pending: list[object] = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in PAYROLL_SAFETY_FLAGS and nested is not False:
                    raise CoreBackendError(503, _problem(503, "PAYROLL_PAYMENT_MODE_NOT_ALLOWED"))
                pending.append(nested)
        elif isinstance(value, list):
            pending.extend(value)


def _validate_payroll_test_workspace_payload(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
) -> None:
    envelope_fields = {"contract_version", "entity_ref", "company_id", "data"}
    if (
        set(payload) != envelope_fields
        or payload.get("contract_version")
        != "ledgerbridge.payroll-test-workspace-read.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or not isinstance(payload.get("data"), dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    company_id = str(payload["company_id"])
    data = payload["data"]
    assert isinstance(data, dict)
    fields = {
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
        "auto_test_ready",
        "payment_submission_supported",
        "payable",
        "submission_supported",
        "routing_counts",
        "materials",
    }
    revision = data.get("projection_revision")
    generated_at = data.get("generated_at")
    try:
        parsed_generated_at = datetime.fromisoformat(
            str(generated_at)[:-1] + "+00:00"
        ) if isinstance(generated_at, str) and generated_at.endswith("Z") else None
    except ValueError:
        parsed_generated_at = None
    if (
        set(data) != fields
        or data.get("schema_version") != "payroll-ledgerbridge-test-projection/v1"
        or data.get("contract_version") != "1.0.0"
        or data.get("data_scope") != "TEST_ONLY"
        or data.get("test_batch_id") != expected_batch_id
        or data.get("company_id") != company_id
        or data.get("cutoff_date") != "2026-08-31"
        or type(data.get("workspace_revision")) is not int
        or int(data["workspace_revision"]) < 1
        or int(data["workspace_revision"]) > 9_007_199_254_740_991
        or not isinstance(revision, str)
        or PAYROLL_PROJECTION_REVISION.fullmatch(revision) is None
        or data.get("etag") != f'"{revision}"'
        or parsed_generated_at is None
        or type(data.get("auto_test_ready")) is not bool
        or data.get("payment_submission_supported") is not False
        or data.get("payable") is not False
        or data.get("submission_supported") is not False
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    counts = data.get("routing_counts")
    materials = data.get("materials")
    if (
        not isinstance(counts, dict)
        or set(counts) != {"auto_test", "review_required", "date_unknown"}
        or any(type(value) is not int or value < 0 for value in counts.values())
        or not isinstance(materials, list)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    actual = {"auto_test": 0, "review_required": 0, "date_unknown": 0}
    material_fields = {
        "company_id",
        "material_id",
        "routing_status",
        "period",
        "material_type",
        "payable",
        "submission_supported",
    }
    seen: set[str] = set()
    for material in materials:
        if not isinstance(material, dict) or set(material) != material_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        material_id = material.get("material_id")
        period = material.get("period")
        material_type = material.get("material_type")
        if (
            material.get("company_id") != company_id
            or not _payroll_identifier(material_id)
            or material_id in seen
            or material_type not in PAYROLL_TEST_MATERIAL_TYPES
            or material.get("payable") is not False
            or material.get("submission_supported") is not False
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        seen.add(str(material_id))
        if material_type == "PAYROLL_SUMMARY" and period is not None and (
            not isinstance(period, str) or PAYROLL_PERIOD.fullmatch(period) is None
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        routing_status = material.get("routing_status")
        if (
            routing_status != "AUTO_TEST"
            or material_type != "PAYROLL_SUMMARY" and period not in {"2026-07", "2026-08"}
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        count_key = {
            "AUTO_TEST": "auto_test",
            "REVIEW_REQUIRED": "review_required",
            "DATE_UNKNOWN": "date_unknown",
        }[str(routing_status)]
        actual[count_key] += 1
    if counts != actual or data.get("auto_test_ready") is not (actual["auto_test"] > 0):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _reject_unsafe_payroll_values(data)
    try:
        canonical_facts = json.dumps(
            {key: data[key] for key in PAYROLL_TEST_PROJECTION_FACT_KEYS},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError) as exc:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID")) from exc
    expected_revision = hashlib.sha256(canonical_facts).hexdigest()
    if revision != expected_revision:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_test_material_preview_payload(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
    expected_material_id: str,
) -> None:
    envelope_fields = {
        "contract_version", "entity_ref", "company_id", "material_id", "data",
    }
    data = payload.get("data")
    if (
        set(payload) != envelope_fields
        or payload.get("contract_version")
        != "ledgerbridge.payroll-test-material-preview-read.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or payload.get("material_id") != expected_material_id
        or not isinstance(data, dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    company_id = str(payload["company_id"])
    if data.get("schema_version") == "payroll-input-material-preview/v1":
        _validate_payroll_input_preview_data(
            data,
            expected_company_id=company_id,
            expected_batch_id=expected_batch_id,
            expected_material_id=expected_material_id,
        )
        return
    if data.get("schema_version") == "payroll-summary-authoritative-preview/v1":
        _validate_payroll_summary_preview_data(
            data,
            expected_company_id=company_id,
            expected_batch_id=expected_batch_id,
            expected_material_id=expected_material_id,
        )
        return
    fields = {
        "schema_version", "data_scope", "test_batch_id", "company_id", "material_id",
        "period", "routing_status", "auto_batch_eligible", "status", "line_count",
        "total_net_pay_cents", "lines", "exceptions",
        "payment_submission_supported", "payable", "submission_supported",
    }
    lines = data.get("lines")
    exceptions = data.get("exceptions")
    if (
        set(data) != fields
        or data.get("schema_version") != "payroll-test-material-preview/v1"
        or data.get("data_scope") != "TEST_ONLY"
        or data.get("test_batch_id") != expected_batch_id
        or data.get("company_id") != company_id
        or data.get("material_id") != expected_material_id
        or not isinstance(data.get("period"), str)
        or PAYROLL_PERIOD.fullmatch(str(data["period"])) is None
        or data.get("routing_status") not in {"AUTO_TEST", "REVIEW_REQUIRED"}
        or type(data.get("auto_batch_eligible")) is not bool
        or data.get("auto_batch_eligible") is not (
            data.get("routing_status") == "AUTO_TEST"
            and data.get("status") == "READY_FOR_REVIEW"
        )
        or data.get("status") not in {"READY_FOR_REVIEW", "NEEDS_HUMAN_REVIEW"}
        or type(data.get("line_count")) is not int
        or int(data["line_count"]) < 0
        or type(data.get("total_net_pay_cents")) is not int
        or not 0 <= int(data["total_net_pay_cents"]) <= 9_007_199_254_740_991
        or not isinstance(lines, list)
        or not isinstance(exceptions, list)
        or data.get("payment_submission_supported") is not False
        or data.get("payable") is not False
        or data.get("submission_supported") is not False
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    line_fields = {
        "source_row", "company_id", "employee_id", "employee_name", "account_id", "account_masked",
        "payment_channel", "base_salary_cents", "allowance_cents", "bonus_cents",
        "deduction_cents", "social_insurance_cents", "housing_fund_cents",
        "individual_income_tax_cents", "gross_pay_cents", "net_pay_cents", "notes",
    }
    money_fields = {
        "base_salary_cents", "allowance_cents", "bonus_cents", "deduction_cents",
        "social_insurance_cents", "housing_fund_cents", "individual_income_tax_cents",
        "gross_pay_cents", "net_pay_cents",
    }
    total = 0
    for line in lines:
        if not isinstance(line, dict) or set(line) != line_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if (
            line.get("company_id") != company_id
            or type(line.get("source_row")) is not int
            or int(line["source_row"]) < 1
            or not _payroll_identifier(line.get("employee_id"))
            or not _payroll_identifier(line.get("account_id"))
            or not isinstance(line.get("account_masked"), str)
            or re.fullmatch(r"\*{4}(?:\d{4}|\?{4})", str(line["account_masked"])) is None
            or not isinstance(line.get("employee_name"), str)
            or not 1 <= len(str(line["employee_name"])) <= 120
            or not isinstance(line.get("payment_channel"), str)
            or not 1 <= len(str(line["payment_channel"])) <= 40
            or not isinstance(line.get("notes"), str)
            or len(str(line["notes"])) > 500
            or any(
                type(line.get(field)) is not int
                or not 0 <= int(line[field]) <= 9_007_199_254_740_991
                for field in money_fields
            )
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        total += int(line["net_pay_cents"])
        if total > 9_007_199_254_740_991:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if len(lines) != data["line_count"] or total != data["total_net_pay_cents"]:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    allowed_exception_fields = {
        "code", "severity", "row", "field", "calculated_cents", "stated_cents",
    }
    for exception in exceptions:
        if (
            not isinstance(exception, dict)
            or not {"code", "severity", "row"}.issubset(exception)
            or not set(exception).issubset(allowed_exception_fields)
            or not isinstance(exception.get("code"), str)
            or not 1 <= len(str(exception["code"])) <= 80
            or not isinstance(exception.get("severity"), str)
            or not 1 <= len(str(exception["severity"])) <= 40
            or type(exception.get("row")) is not int
            or int(exception["row"]) < 1
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if "field" in exception and (
            not isinstance(exception["field"], str) or len(str(exception["field"])) > 80
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        for field in ("calculated_cents", "stated_cents"):
            if field in exception and (
                type(exception[field]) is not int
                or not 0 <= int(exception[field]) <= 9_007_199_254_740_991
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _contains_sensitive_payroll_text(value: str) -> bool:
    return bool(
        re.search(r"[\x00-\x1f\x7f]", value)
        or re.search(r"(?<!\d)\d(?:[\s-]?\d){11,18}(?!\d)", value)
        or re.search(r"(?:[A-Za-z]:\\|\\\\|/(?:home|Users?)/)", value)
    )


def _validate_payroll_input_preview_data(
    data: dict[str, object],
    *,
    expected_company_id: str,
    expected_batch_id: str,
    expected_material_id: str,
) -> None:
    fields = {
        "schema_version", "data_scope", "test_batch_id", "company_id", "material_id",
        "period", "material_type", "detected_material_type", "canonical_name",
        "selected_sheet", "sheet_names", "columns", "record_count", "preview_rows",
        "status", "payment_submission_supported", "payable", "submission_supported",
    }
    projected_types = {
        "ATTENDANCE_SHEET", "AUNT_ATTENDANCE_SHEET", "REVIEW_STATISTICS",
        "ADJUSTMENT_SOURCE",
    }
    detected_types = {
        "ATTENDANCE_SHEET", "AUNT_ATTENDANCE_SHEET", "REVIEW_STATISTICS",
        "UNRECOGNIZED",
    }
    if (
        set(data) != fields
        or data.get("schema_version") != "payroll-input-material-preview/v1"
        or data.get("data_scope") != "TEST_ONLY"
        or data.get("test_batch_id") != expected_batch_id
        or data.get("company_id") != expected_company_id
        or data.get("material_id") != expected_material_id
        or data.get("period") not in {"2026-07", "2026-08"}
        or data.get("material_type") not in projected_types
        or data.get("detected_material_type") not in detected_types
        or data.get("status") not in {"READY_FOR_REVIEW", "NEEDS_HUMAN_REVIEW"}
        or data.get("payment_submission_supported") is not False
        or data.get("payable") is not False
        or data.get("submission_supported") is not False
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    detected_type = data.get("detected_material_type")
    naming_type = data.get("material_type") if detected_type == "UNRECOGNIZED" else detected_type
    label = {
        "ATTENDANCE_SHEET": "考勤表",
        "AUNT_ATTENDANCE_SHEET": "阿姨考勤表",
        "REVIEW_STATISTICS": "好评统计",
        "ADJUSTMENT_SOURCE": "好评统计",
    }.get(naming_type, "工资表素材")
    period = str(data["period"])
    if data.get("canonical_name") != f"{period[:4]}.{int(period[5:])}_{label}":
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))

    def checked_strings(field: str, maximum_count: int, maximum_length: int) -> list[str]:
        values = data.get(field)
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= maximum_count
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > maximum_length
                or _contains_sensitive_payroll_text(value)
                for value in values
            )
            or len(set(values)) != len(values)
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return values

    sheet_names = checked_strings("sheet_names", 20, 60)
    columns = checked_strings("columns", 16, 80)
    if data.get("selected_sheet") not in sheet_names:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    rows = data.get("preview_rows")
    record_count = data.get("record_count")
    if (
        type(record_count) is not int
        or int(record_count) < 0
        or not isinstance(rows, list)
        or len(rows) > 8
        or int(record_count) < len(rows)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"source_row", "values"}
            or type(row.get("source_row")) is not int
            or int(row["source_row"]) < 1
            or not isinstance(row.get("values"), list)
            or len(row["values"]) != len(columns)
            or any(
                not isinstance(cell, str)
                or len(cell) > 120
                or _contains_sensitive_payroll_text(cell)
                for cell in row["values"]
            )
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_summary_preview_data(
    data: dict[str, object],
    *,
    expected_company_id: str,
    expected_batch_id: str,
    expected_material_id: str,
) -> None:
    fields = {
        "schema_version", "data_scope", "test_batch_id", "company_id", "material_id",
        "routing_status", "source_of_truth", "authoritative", "period_count",
        "latest_period", "periods", "payment_submission_supported", "payable",
        "submission_supported",
    }
    periods = data.get("periods")
    if (
        set(data) != fields
        or data.get("schema_version") != "payroll-summary-authoritative-preview/v1"
        or data.get("data_scope") != "TEST_ONLY"
        or data.get("test_batch_id") != expected_batch_id
        or data.get("company_id") != expected_company_id
        or data.get("material_id") != expected_material_id
        or data.get("routing_status") not in {"AUTO_TEST", "REVIEW_REQUIRED", "DATE_UNKNOWN"}
        or data.get("source_of_truth") != "PAYROLL_SUMMARY"
        or data.get("authoritative") is not True
        or type(data.get("period_count")) is not int
        or not isinstance(periods, list)
        or not periods
        or int(data["period_count"]) != len(periods)
        or not isinstance(data.get("latest_period"), str)
        or PAYROLL_PERIOD.fullmatch(str(data["latest_period"])) is None
        or data.get("payment_submission_supported") is not False
        or data.get("payable") is not False
        or data.get("submission_supported") is not False
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    period_fields = {
        "period", "store_count", "stores", "total_net_pay_cents", "total_source",
        "total_matches_stores",
    }
    store_fields = {"store_name", "net_pay_cents"}
    previous_period: str | None = None
    seen_periods: set[str] = set()
    for index, item in enumerate(periods):
        if not isinstance(item, dict) or set(item) != period_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        period = item.get("period")
        stores = item.get("stores")
        if (
            not isinstance(period, str)
            or PAYROLL_PERIOD.fullmatch(period) is None
            or period in seen_periods
            or previous_period is not None and period >= previous_period
            or index == 0 and period != data.get("latest_period")
            or type(item.get("store_count")) is not int
            or not isinstance(stores, list)
            or not stores
            or int(item["store_count"]) != len(stores)
            or type(item.get("total_net_pay_cents")) is not int
            or not 0 <= int(item["total_net_pay_cents"]) <= JSON_SAFE_INTEGER
            or item.get("total_source")
            not in {"SUMMARY_TOTAL_ROW", "SUM_OF_SUMMARY_STORE_ROWS"}
            or type(item.get("total_matches_stores")) is not bool
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        seen_periods.add(period)
        previous_period = period
        seen_stores: set[str] = set()
        calculated_total = 0
        for store in stores:
            if not isinstance(store, dict) or set(store) != store_fields:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            name = store.get("store_name")
            amount = store.get("net_pay_cents")
            if (
                not isinstance(name, str)
                or not name
                or name != name.strip()
                or len(name) > 40
                or name in seen_stores
                or re.search(r"[\x00-\x1f\x7f]", name)
                or re.search(r"(?<!\d)\d(?:[\s-]?\d){11,18}(?!\d)", name)
                or re.search(r"(?:[A-Za-z]:\\|\\\\|/(?:home|Users?)/)", name)
                or type(amount) is not int
                or not 0 <= int(amount) <= JSON_SAFE_INTEGER
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            seen_stores.add(name)
            calculated_total += int(amount)
            if calculated_total > JSON_SAFE_INTEGER:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        total = int(item["total_net_pay_cents"])
        if item["total_matches_stores"] is not (total == calculated_total) or (
            item["total_source"] == "SUM_OF_SUMMARY_STORE_ROWS"
            and total != calculated_total
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _reject_unsafe_payroll_values(data)


def _validate_payroll_legacy_workspace_payload(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
) -> None:
    if (
        set(payload) != {"contract_version", "entity_ref", "company_id", "data"}
        or payload.get("contract_version") != "ledgerbridge.payroll-legacy-feature-read.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or not isinstance(payload.get("data"), dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _validate_payroll_legacy_workspace_data(
        payload["data"],
        expected_company_id=str(payload["company_id"]),
        expected_batch_id=expected_batch_id,
    )


def _validate_payroll_legacy_workspace_data(
    data: object,
    *,
    expected_company_id: str,
    expected_batch_id: str,
) -> None:
    fields = {
        "schema_version",
        "data_scope",
        "company_id",
        "test_batch_id",
        "revision",
        "active_period",
        "rules",
        "batches",
        "audit_events",
        "payment_submission_supported",
        "payable",
        "submission_supported",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    revision = data.get("revision")
    active_period = data.get("active_period")
    rules = data.get("rules")
    batches = data.get("batches")
    events = data.get("audit_events")
    if any(data.get(field) is not False for field in (
        "payment_submission_supported", "payable", "submission_supported"
    )):
        raise CoreBackendError(503, _problem(503, "PAYROLL_PAYMENT_MODE_NOT_ALLOWED"))
    if (
        data.get("schema_version") != "payroll-legacy-feature-workspace/v1"
        or data.get("data_scope") != "TEST_ONLY"
        or data.get("company_id") != expected_company_id
        or data.get("test_batch_id") != expected_batch_id
        or type(revision) is not int
        or not 1 <= int(revision) <= JSON_SAFE_INTEGER
        or not isinstance(active_period, str)
        or PAYROLL_PERIOD.fullmatch(active_period) is None
        or not isinstance(rules, dict)
        or not {"revision", "employees"}.issubset(rules)
        or set(rules) - {"revision", "employees", "review_rules"}
        or type(rules.get("revision")) is not int
        or not 0 <= int(rules["revision"]) <= JSON_SAFE_INTEGER
        or not isinstance(rules.get("employees"), list)
        or not isinstance(batches, list)
        or not isinstance(events, list)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    review_rules = rules.get("review_rules", [])
    if (
        not isinstance(review_rules, list)
        or len(review_rules) > len(PAYROLL_LEGACY_REVIEW_RULE_TYPES)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    review_rule_ids: set[str] = set()
    review_rule_types: set[str] = set()
    review_rule_fields = {
        "rule_id", "name", "rule_type", "enabled", "severity", "threshold_cents"
    }
    for rule in review_rules:
        if not isinstance(rule, dict) or set(rule) != review_rule_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        rule_id = rule.get("rule_id")
        name = rule.get("name")
        rule_type = rule.get("rule_type")
        threshold = rule.get("threshold_cents")
        if (
            not _payroll_identifier(rule_id)
            or rule_id in review_rule_ids
            or not isinstance(name, str)
            or not name.strip()
            or name != name.strip()
            or len(name) > 120
            or rule_type not in PAYROLL_LEGACY_REVIEW_RULE_TYPES
            or rule_type in review_rule_types
            or type(rule.get("enabled")) is not bool
            or rule.get("severity") not in PAYROLL_LEGACY_REVIEW_RULE_SEVERITIES
            or type(threshold) is not int
            or not 0 <= int(threshold) <= JSON_SAFE_INTEGER
            or (rule_type != "HISTORY_CHANGE_REVIEW" and threshold != 0)
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        review_rule_ids.add(str(rule_id))
        review_rule_types.add(str(rule_type))
    batch_fields = {
        "batch_id",
        "period",
        "revision",
        "main_material_id",
        "supporting_material_ids",
        "lines",
        "adjustments",
        "source_exceptions",
        "drafts",
        "summary",
        "verification",
        "pending_items",
        "checks",
    }
    periods: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict) or set(batch) != batch_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        period = batch.get("period")
        lines = batch.get("lines")
        supporting = batch.get("supporting_material_ids")
        if (
            not _payroll_identifier(batch.get("batch_id"))
            or not isinstance(period, str)
            or PAYROLL_PERIOD.fullmatch(period) is None
            or period in periods
            or type(batch.get("revision")) is not int
            or not 1 <= int(batch["revision"]) <= JSON_SAFE_INTEGER
            or not _payroll_identifier(batch.get("main_material_id"))
            or not isinstance(supporting, dict)
            or any(not _payroll_identifier(item) for item in supporting.values())
            or not isinstance(lines, list)
            or not lines
            or any(not isinstance(batch.get(field), list) for field in (
                "adjustments", "source_exceptions", "drafts", "pending_items"
            ))
            or any(
                batch.get(field) is not None and not isinstance(batch.get(field), dict)
                for field in ("summary", "verification", "checks")
            )
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        periods.add(period)
        for line in lines:
            if (
                not isinstance(line, dict)
                or line.get("company_id") != expected_company_id
                or not _payroll_identifier(line.get("employee_id"))
                or not _payroll_account_identifier(line.get("account_id"))
                or not isinstance(line.get("account_masked"), str)
                or re.fullmatch(r"\*{4}(?:\d{4}|\?{4})", str(line["account_masked"])) is None
                or type(line.get("source_row")) is not int
                or int(line["source_row"]) < 1
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if periods and active_period not in periods:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("sequence") != sequence:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _reject_unsafe_legacy_payroll_values(data, expected_company_id, expected_batch_id)


def _reject_unsafe_legacy_payroll_values(
    value: object,
    expected_company_id: str,
    expected_batch_id: str,
) -> None:
    pending = [value]
    forbidden_keys = {
        "filename", "file_name", "file_path", "filepath", "source_path", "local_path",
        "account_number", "bank_account", "raw_account", "bytes", "raw_bytes",
    }
    while pending:
        item = pending.pop()
        if type(item) is float:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if type(item) is int and not -JSON_SAFE_INTEGER <= item <= JSON_SAFE_INTEGER:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if isinstance(item, dict):
            if any(str(key).lower() in forbidden_keys for key in item):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            for key, nested in item.items():
                if key in PAYROLL_SAFETY_FLAGS and nested is not False:
                    raise CoreBackendError(
                        503, _problem(503, "PAYROLL_PAYMENT_MODE_NOT_ALLOWED")
                    )
                if key == "company_id" and nested != expected_company_id:
                    raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
                if key == "test_batch_id" and nested != expected_batch_id:
                    raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
                if key.endswith(("_cents", "_minor")) and type(nested) is not int:
                    raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
                pending.append(nested)
        elif isinstance(item, list):
            pending.extend(item)


def _validate_payroll_legacy_command_payload(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
    expected_action: str,
    expected_revision: int,
) -> None:
    fields = {
        "contract_version",
        "entity_ref",
        "company_id",
        "action",
        "resource_ref",
        "replayed",
        "data",
    }
    data = payload.get("data")
    if (
        set(payload) != fields
        or payload.get("contract_version")
        != "ledgerbridge.payroll-legacy-feature-command-result.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or payload.get("action") != "payroll.test_workspace.legacy.command"
        or payload.get("resource_ref") != expected_batch_id
        or type(payload.get("replayed")) is not bool
        or not isinstance(data, dict)
        or set(data) != {"action", "replayed", "workspace"}
        or data.get("action") != expected_action
        or data.get("replayed") != payload.get("replayed")
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    workspace = data.get("workspace")
    _validate_payroll_legacy_workspace_data(
        workspace,
        expected_company_id=str(payload["company_id"]),
        expected_batch_id=expected_batch_id,
    )
    assert isinstance(workspace, dict)
    if workspace.get("revision") != expected_revision + 1:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _payroll_test_workspace_read_from_create(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
) -> dict[str, object]:
    fields = {
        "contract_version",
        "entity_ref",
        "company_id",
        "action",
        "resource_ref",
        "replayed",
        "data",
    }
    data = payload.get("data")
    if (
        set(payload) != fields
        or payload.get("contract_version")
        != "ledgerbridge.payroll-test-workspace-command-result.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or payload.get("action") != "payroll.test_workspace.create"
        or payload.get("resource_ref") != expected_batch_id
        or type(payload.get("replayed")) is not bool
        or not isinstance(data, dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    result = {
        "contract_version": "ledgerbridge.payroll-test-workspace-read.v1",
        "entity_ref": expected_entity_ref,
        "company_id": payload["company_id"],
        "data": data,
    }
    _validate_payroll_test_workspace_payload(
        result,
        expected_entity_ref=expected_entity_ref,
        expected_batch_id=expected_batch_id,
    )
    return result


def _validate_payroll_test_workspace_command_payload(
    payload: dict[str, object],
    *,
    expected_entity_ref: str,
    expected_batch_id: str,
    expected_action: str,
    expected_resource_ref: str,
) -> None:
    envelope_fields = {
        "contract_version",
        "entity_ref",
        "company_id",
        "action",
        "resource_ref",
        "replayed",
        "data",
    }
    data = payload.get("data")
    if (
        set(payload) != envelope_fields
        or payload.get("contract_version")
        != "ledgerbridge.payroll-test-workspace-command-result.v1"
        or payload.get("entity_ref") != expected_entity_ref
        or not _payroll_identifier(payload.get("company_id"))
        or payload.get("action") != expected_action
        or payload.get("resource_ref") != expected_resource_ref
        or type(payload.get("replayed")) is not bool
        or not isinstance(data, dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    company_id = str(payload["company_id"])
    common_fields = {
        "schema_version",
        "data_scope",
        "test_batch_id",
        "company_id",
        "workspace_revision",
        "payment_submission_supported",
        "payable",
        "submission_supported",
        "replayed",
    }
    if (
        data.get("data_scope") != "TEST_ONLY"
        or data.get("test_batch_id") != expected_batch_id
        or data.get("company_id") != company_id
        or type(data.get("workspace_revision")) is not int
        or not 1 <= int(data["workspace_revision"]) <= 9_007_199_254_740_991
        or data.get("payment_submission_supported") is not False
        or data.get("payable") is not False
        or data.get("submission_supported") is not False
        or type(data.get("replayed")) is not bool
        or data.get("replayed") != payload.get("replayed")
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    if expected_action == "payroll.test_workspace.organize":
        if (
            set(data) != common_fields | {"projection_revision", "material"}
            or data.get("schema_version") != "payroll-test-material-organize-result/v1"
            or not isinstance(data.get("projection_revision"), str)
            or PAYROLL_PROJECTION_REVISION.fullmatch(str(data["projection_revision"])) is None
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        material = data.get("material")
        material_fields = {
            "company_id",
            "material_id",
            "routing_status",
            "period",
            "material_type",
            "payable",
            "submission_supported",
        }
        if not isinstance(material, dict) or set(material) != material_fields:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        period = material.get("period")
        routing_status = material.get("routing_status")
        if (
            material.get("company_id") != company_id
            or material.get("material_id") != expected_resource_ref
            or not isinstance(period, str)
            or PAYROLL_PERIOD.fullmatch(period) is None
            or material.get("material_type") not in PAYROLL_TEST_MATERIAL_TYPES
            or routing_status != "AUTO_TEST"
            or period not in {"2026-07", "2026-08"}
            or material.get("payable") is not False
            or material.get("submission_supported") is not False
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    elif expected_action == "payroll.test_workspace.validate":
        if (
            set(data)
            != common_fields
            | {"ready_batch_count", "blocked_material_count", "batches"}
            or data.get("schema_version") != "payroll-test-batch-validation-result/v1"
            or type(data.get("ready_batch_count")) is not int
            or int(data["ready_batch_count"]) < 0
            or type(data.get("blocked_material_count")) is not int
            or int(data["blocked_material_count"]) < 0
            or not isinstance(data.get("batches"), list)
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        ready_count = 0
        batch_ids: set[str] = set()
        for batch in data["batches"]:
            if not isinstance(batch, dict) or set(batch) != {
                "batch_id",
                "period",
                "material_count",
                "payroll_sheet_count",
                "supporting_material_count",
                "status",
            }:
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            batch_id = batch.get("batch_id")
            period = batch.get("period")
            material_count = batch.get("material_count")
            payroll_sheet_count = batch.get("payroll_sheet_count")
            supporting_count = batch.get("supporting_material_count")
            status_value = batch.get("status")
            if (
                not _payroll_identifier(batch_id)
                or batch_id in batch_ids
                or not isinstance(period, str)
                or PAYROLL_PERIOD.fullmatch(period) is None
                or type(material_count) is not int
                or material_count < 1
                or type(payroll_sheet_count) is not int
                or payroll_sheet_count < 0
                or type(supporting_count) is not int
                or supporting_count < 0
                or material_count != payroll_sheet_count + supporting_count
                or status_value not in {"READY_FOR_TEST_REVIEW", "BLOCKED"}
                or (status_value == "READY_FOR_TEST_REVIEW" and payroll_sheet_count < 1)
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            batch_ids.add(str(batch_id))
            ready_count += int(status_value == "READY_FOR_TEST_REVIEW")
        if ready_count != data.get("ready_batch_count"):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    else:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _reject_unsafe_payroll_values(data)


def _validate_payroll_status_data(data: dict[str, object]) -> None:
    expected_fields = {
        "schema_version",
        "live_data_ready",
        "live_projection_schema",
        "payment_operations_exposed",
        "projection_revision",
        "etag",
        "setup_summary",
        "capabilities",
    }
    setup = data.get("setup_summary")
    capabilities = data.get("capabilities")
    if (
        set(data) != expected_fields
        or data.get("schema_version") != "ledgerbridge.payroll-status.v1"
        or type(data.get("live_data_ready")) is not bool
        or data.get("live_projection_schema")
        != "payroll-ledgerbridge-live-projection/v1"
        or data.get("payment_operations_exposed") is not False
        or not isinstance(data.get("projection_revision"), str)
        or PAYROLL_PROJECTION_REVISION.fullmatch(str(data.get("projection_revision"))) is None
        or data.get("etag") != f'"{data.get("projection_revision")}"'
        or not isinstance(setup, dict)
        or not isinstance(capabilities, dict)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))

    setup_fields = {
        "provider_connected",
        "runtime_mode",
        "unassigned_material_count",
        "ready_material_count",
        "company_mapped_material_count",
        "blocking_reason_codes",
    }
    counts = (
        setup.get("unassigned_material_count"),
        setup.get("ready_material_count"),
        setup.get("company_mapped_material_count"),
    )
    reason_codes = setup.get("blocking_reason_codes")
    if (
        set(setup) != setup_fields
        or setup.get("provider_connected") is not True
        or setup.get("runtime_mode") != "live-provider"
        or any(type(value) is not int or value < 0 for value in counts)
        or not isinstance(reason_codes, list)
        or any(
            not isinstance(code, str) or code not in PAYROLL_BLOCKING_REASON_ORDER
            for code in reason_codes
        )
        or len(set(reason_codes)) != len(reason_codes)
        or reason_codes
        != sorted(reason_codes, key=PAYROLL_BLOCKING_REASON_ORDER.index)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))

    allowed_actions = capabilities.get("allowed_actions")
    commands_enabled = capabilities.get("commands_enabled")
    if (
        set(capabilities) != {"commands_enabled", "allowed_actions"}
        or type(commands_enabled) is not bool
        or allowed_actions
        != (["VERIFY_RECEIPTS"] if commands_enabled else [])
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))

    _reject_unsafe_payroll_values(data)


def _validate_payroll_view_data(
    value: object,
    *,
    path: str,
    company_id: str,
) -> None:
    if not isinstance(value, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    expected_schema = {
        "/internal/v1/payroll/dashboard": "ledgerbridge.payroll-dashboard.v1",
        "/internal/v1/payroll/materials": "ledgerbridge.payroll-material-list.v1",
        "/internal/v1/payroll/batches": "ledgerbridge.payroll-batch-list.v1",
        "/internal/v1/payroll/verification": "ledgerbridge.payroll-verification-list.v1",
    }.get(path)
    if expected_schema is None or value.get("schema_version") != expected_schema:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _validate_payroll_snapshot(value)
    if path == "/internal/v1/payroll/dashboard":
        _validate_payroll_dashboard(value)
    elif path == "/internal/v1/payroll/materials":
        _validate_payroll_materials(value, company_id=company_id)
    elif path == "/internal/v1/payroll/batches":
        _validate_payroll_batches(value, company_id=company_id)
    else:
        _validate_payroll_verification(value, company_id=company_id)
    _reject_unsafe_payroll_values(value)


def _validate_payroll_snapshot(value: dict[str, object]) -> None:
    revision = value.get("projection_revision")
    generated_at = value.get("generated_at")
    if (
        not isinstance(revision, str)
        or PAYROLL_PROJECTION_REVISION.fullmatch(revision) is None
        or value.get("etag") != f'"{revision}"'
        or not isinstance(generated_at, str)
        or not generated_at.endswith("Z")
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_dashboard(value: dict[str, object]) -> None:
    ready = value.get("live_data_ready")
    expected = {
        "schema_version",
        "projection_revision",
        "etag",
        "generated_at",
        "live_data_ready",
        "setup_summary",
    }
    if ready is True:
        expected.add("dashboard")
    if type(ready) is not bool or set(value) != expected:
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    setup = value.get("setup_summary")
    if not isinstance(setup, dict):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _validate_setup_summary(setup)
    if ready is False:
        return
    dashboard = value.get("dashboard")
    fields = {
        "batch_count",
        "material_count",
        "materials_needing_review_count",
        "verification_attention_count",
        "unassigned_material_count",
        "net_pay_minor",
    }
    if not isinstance(dashboard, dict) or set(dashboard) != fields or any(
        type(item) is not int or item < 0 for item in dashboard.values()
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_setup_summary(setup: dict[str, object]) -> None:
    fields = {
        "provider_connected",
        "runtime_mode",
        "unassigned_material_count",
        "ready_material_count",
        "company_mapped_material_count",
        "blocking_reason_codes",
    }
    counts = (
        setup.get("unassigned_material_count"),
        setup.get("ready_material_count"),
        setup.get("company_mapped_material_count"),
    )
    reasons = setup.get("blocking_reason_codes")
    if (
        set(setup) != fields
        or setup.get("provider_connected") is not True
        or setup.get("runtime_mode") != "live-provider"
        or any(type(item) is not int or item < 0 for item in counts)
        or not isinstance(reasons, list)
        or any(reason not in PAYROLL_BLOCKING_REASON_ORDER for reason in reasons)
        or len(reasons) != len(set(reasons))
        or reasons != sorted(reasons, key=PAYROLL_BLOCKING_REASON_ORDER.index)
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_materials(value: dict[str, object], *, company_id: str) -> None:
    if set(value) != {
        "schema_version", "projection_revision", "etag", "generated_at", "items"
    } or not isinstance(value.get("items"), list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    fields = {
        "company_id", "material_id", "material_type", "period", "status",
        "review_revision", "payable", "submission_supported",
    }
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or item.get("company_id") != company_id
            or not _payroll_identifier(item.get("material_id"))
            or item.get("material_type") is not None
            and not isinstance(item.get("material_type"), str)
            or item.get("period") is not None
            and (
                not isinstance(item.get("period"), str)
                or PAYROLL_PERIOD.fullmatch(str(item.get("period"))) is None
            )
            or not isinstance(item.get("status"), str)
            or type(item.get("review_revision")) is not int
            or int(item["review_revision"]) < 0
            or item.get("payable") is not False
            or item.get("submission_supported") is not False
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_batches(value: dict[str, object], *, company_id: str) -> None:
    if set(value) != {
        "schema_version", "projection_revision", "etag", "generated_at", "items"
    } or not isinstance(value.get("items"), list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    required = {
        "company_id", "batch_id", "pay_period", "revision", "status", "payable",
        "submission_supported", "payment_submission_supported", "lines",
    }
    line_fields = {
        "company_id", "employee_id", "employee_display", "account_id",
        "account_display", "net_pay_minor",
    }
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or set(item) not in (required, required | {"audit_closure"})
            or item.get("company_id") != company_id
            or not _payroll_identifier(item.get("batch_id"))
            or not isinstance(item.get("pay_period"), str)
            or PAYROLL_PERIOD.fullmatch(str(item.get("pay_period"))) is None
            or type(item.get("revision")) is not int
            or int(item["revision"]) < 1
            or not isinstance(item.get("status"), str)
            or item.get("payable") is not False
            or item.get("submission_supported") is not False
            or item.get("payment_submission_supported") is not False
            or not isinstance(item.get("lines"), list)
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        for line in item["lines"]:
            if (
                not isinstance(line, dict)
                or set(line) != line_fields
                or line.get("company_id") != company_id
                or not _payroll_identifier(line.get("employee_id"))
                or not _payroll_identifier(line.get("account_id"))
                or not isinstance(line.get("employee_display"), str)
                or not isinstance(line.get("account_display"), str)
                or type(line.get("net_pay_minor")) is not int
                or int(line["net_pay_minor"]) < 0
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_verification(value: dict[str, object], *, company_id: str) -> None:
    if set(value) != {
        "schema_version", "projection_revision", "etag", "generated_at", "items",
        "available_evidence",
    } or not isinstance(value.get("items"), list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    verification_fields = {
        "company_id", "verification_id", "batch_id", "status", "source_artifact_ids",
        "results", "payable", "submission_supported", "payment_submission_supported",
    }
    result_fields = {
        "company_id", "employee_id", "employee_display", "account_id",
        "account_display", "status",
    }
    for item in value["items"]:
        if (
            not isinstance(item, dict)
            or set(item) != verification_fields
            or item.get("company_id") != company_id
            or not _payroll_identifier(item.get("verification_id"))
            or not _payroll_identifier(item.get("batch_id"))
            or not isinstance(item.get("status"), str)
            or not isinstance(item.get("source_artifact_ids"), list)
            or any(not _payroll_identifier(ref) for ref in item["source_artifact_ids"])
            or not isinstance(item.get("results"), list)
            or item.get("payable") is not False
            or item.get("submission_supported") is not False
            or item.get("payment_submission_supported") is not False
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        for result in item["results"]:
            if (
                not isinstance(result, dict)
                or set(result) != result_fields
                or result.get("company_id") != company_id
                or not _payroll_identifier(result.get("employee_id"))
                or not _payroll_identifier(result.get("account_id"))
                or not isinstance(result.get("employee_display"), str)
                or not isinstance(result.get("account_display"), str)
                or not isinstance(result.get("status"), str)
            ):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))


def _validate_payroll_command_receipt_data(
    value: object,
    *,
    company_id: str,
    resource_ref: str,
    idempotency_key: str,
) -> None:
    fields = {
        "schema_version", "company_id", "resource_id", "action", "audit_event_id",
        "audit_hash", "occurred_at", "idempotency_key", "replayed", "audit_closure",
    }
    closure_fields = {
        "company_id", "resource_id", "action", "actor_subject", "actor_id",
        "audit_event_id", "audit_hash", "occurred_at",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value.get("schema_version") != "payroll-ledgerbridge-command-receipt/v1"
        or value.get("company_id") != company_id
        or value.get("resource_id") != resource_ref
        or value.get("action") != "payroll.receipts.verify"
        or value.get("idempotency_key") != idempotency_key
        or type(value.get("replayed")) is not bool
        or not isinstance(value.get("audit_hash"), str)
        or PAYROLL_PROJECTION_REVISION.fullmatch(str(value.get("audit_hash"))) is None
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    closure = value.get("audit_closure")
    if (
        not isinstance(closure, dict)
        or set(closure) != closure_fields
        or closure.get("company_id") != company_id
        or closure.get("resource_id") != resource_ref
        or closure.get("action") != "payroll.receipts.verify"
        or closure.get("audit_event_id") != value.get("audit_event_id")
        or closure.get("audit_hash") != value.get("audit_hash")
        or closure.get("occurred_at") != value.get("occurred_at")
    ):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    _reject_unsafe_payroll_values(value)


def _payroll_identifier(value: object) -> bool:
    return isinstance(value, str) and PAYROLL_RESOURCE_REF.fullmatch(value) is not None


def _payroll_account_identifier(value: object) -> bool:
    return (
        _payroll_identifier(value)
        and (
            PAYROLL_CANONICAL_ACCOUNT_ID.fullmatch(str(value)) is not None
            or sum(character.isdigit() for character in str(value)) < 12
        )
    )


def _reject_unsafe_payroll_values(value: object) -> None:
    pending = [value]
    forbidden_keys = {
        "employee_name", "filename", "file_name", "file_path", "filepath",
        "source_path", "local_path", "account_number", "raw_account",
    }
    while pending:
        item = pending.pop()
        if type(item) is float:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if isinstance(item, str) and (
            "artifact_demo_" in item.lower()
            or "receipt_demo_" in item.lower()
            or "demo_mode" in item.lower()
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        if isinstance(item, dict):
            if any(str(key).lower() in forbidden_keys for key in item):
                raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _payroll_available_evidence_by_id(
    payload: dict[str, object],
) -> dict[str, dict[str, object]]:
    data = payload.get("data")
    available = data.get("available_evidence") if isinstance(data, dict) else None
    if not isinstance(available, list):
        raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
    available_by_id: dict[str, dict[str, object]] = {}
    expected_fields = {
        "company_id",
        "artifact_id",
        "period",
        "evidence_type",
        "status",
        "display_label",
    }
    for item in available:
        if (
            not isinstance(item, dict)
            or set(item) != expected_fields
            or item.get("company_id") != payload.get("company_id")
            or item.get("status") != "READY_FOR_MATCHING"
            or item.get("evidence_type")
            not in {
                "MYBANK_STATEMENT",
                "BOC_RECEIPT",
                "WECHAT_RECEIPT",
                "CASH_SIGNOFF",
                "BANK_RECEIPT",
            }
            or not isinstance(item.get("artifact_id"), str)
            or PAYROLL_RESOURCE_REF.fullmatch(str(item.get("artifact_id"))) is None
            or str(item.get("artifact_id")).startswith(("artifact_demo_", "receipt_demo_"))
            or not isinstance(item.get("period"), str)
            or PAYROLL_PERIOD.fullmatch(str(item.get("period"))) is None
            or item.get("display_label")
            != f"{item.get('evidence_type')} · {item.get('period')}"
        ):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        artifact_id = str(item["artifact_id"])
        if artifact_id in available_by_id:
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        available_by_id[artifact_id] = item
    return available_by_id


def _payroll_available_evidence_ids(payload: dict[str, object]) -> set[str]:
    return set(_payroll_available_evidence_by_id(payload))

def _payroll_resource_ref(value: object) -> str:
    if not isinstance(value, str) or PAYROLL_RESOURCE_REF.fullmatch(value) is None:
        raise CoreBackendError(400, _problem(400, "INVALID_PAYROLL_RESOURCE_REF"))
    return value


def _problem(status: int, code: str) -> dict[str, object]:
    return {
        "type": "about:blank",
        "title": "LedgerBridge Core request failed",
        "status": status,
        "code": code,
    }
