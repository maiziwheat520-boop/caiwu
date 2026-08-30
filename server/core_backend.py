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
SAFE_HEADER_VALUE = re.compile(r"^[\x21-\x7e]+$")
EVIDENCE_UNLOCK_CORE_PATH = "/internal/v1/evidence/unlocks"
EVIDENCE_UNLOCK_STATUSES = {"NOT_REQUIRED", "PASSWORD_REQUIRED", "UNLOCKED"}


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
        assertion_key: bytes,
        assertion_issuer: str,
        assertion_audience: str,
        workload_principal: str,
        policy_generation: int,
        user_subject: str,
        authentication_generation: int,
        entity_ref: str,
        business_unit_ref: str,
        evidence_unlock_path: str | None = None,
    ) -> None:
        if not 32 <= len(assertion_key) <= 256:
            raise ValueError("CORE_USER_ASSERTION_KEY must contain 32 to 256 bytes")
        if policy_generation < 1 or authentication_generation < 1:
            raise ValueError("Core policy and authentication generations must be positive")
        self.client = client
        self.assertion_key = assertion_key
        self.assertion_issuer = _bounded(assertion_issuer)
        self.assertion_audience = _bounded(assertion_audience)
        self.workload_principal = _bounded(workload_principal)
        self.policy_generation = policy_generation
        self.user_subject = _bounded(user_subject)
        self.authentication_generation = authentication_generation
        self.entity_ref = str(uuid.UUID(entity_ref))
        self.business_unit_ref = _bounded(business_unit_ref, maximum=100)
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
        query = {
            key: value
            for key, value in {
                "status": status,
                "month": month,
                "business_unit": self.business_unit_ref,
                "cursor": cursor,
            }.items()
            if value is not None
        }
        payload = self.client.json("GET", f"/internal/v1/candidates?{urlencode(query)}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise CoreBackendError(503, _problem(503, "CORE_CONTRACT_INVALID"))
        return {
            "items": [_candidate_from_core(item) for item in items],
            "next_cursor": payload.get("next_cursor"),
        }

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
        layers = []
        for basis in _COMPANY_REPORT_BASES:
            query = urlencode(
                {
                    "from_month": from_month,
                    "to_month": to_month,
                    "basis": basis,
                }
            )
            payload = self.client.json(
                "GET",
                f"/internal/v1/company-reports?{query}",
            )
            layers.append(
                _company_report_layer_from_core(
                    payload,
                    basis,
                    from_month,
                    to_month,
                )
            )
        _validate_company_report_layer_identities(layers)
        return {
            "contract_version": "ledgerbridge.company-reports-bff.v1",
            "from_month": from_month,
            "to_month": to_month,
            "layers": layers,
        }

    def append_decision(
        self,
        candidate_id: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        candidate_ref = str(uuid.UUID(candidate_id))
        operation_id = str(uuid.UUID(idempotency_key))
        path = f"/internal/v1/candidates/{candidate_ref}/decisions"
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        revision = request.get("expected_revision")
        if type(revision) is not int:
            return 422, _problem(422, "INVALID_REVISION")
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


_COMPANY_REPORT_BASES = (
    "CONFIRMED_CANDIDATE",
    "ACCOUNT_STATEMENT",
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
    channel = str(source.get("ingest_channel", "")).lower()
    return {
        "id": value.get("candidate_ref"),
        "short_id": value.get("short_id"),
        "revision": value.get("revision"),
        "status": value.get("status"),
        "source_channel": channel,
        "source_message_id": source.get("source_event_ref"),
        "received_at": value.get("created_at"),
        "business_unit": value.get("business_unit_label") or "",
        "category": value.get("category_label") or "",
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
    mapped_changes: list[dict[str, object]] = []
    field_map = {
        "business_unit_label": "business_unit",
        "category_label": "category",
        "amount_minor": "amount_minor",
        "accounting_month": "accounting_month",
        "status": "status",
    }
    for change in changes:
        if not isinstance(change, dict) or change.get("field") not in field_map:
            continue
        mapped_changes.append(
            {
                "field": field_map[str(change["field"])],
                "previous_value": change.get("previous_value"),
                "new_value": change.get("new_value"),
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


def _problem(status: int, code: str) -> dict[str, object]:
    return {
        "type": "about:blank",
        "title": "LedgerBridge Core request failed",
        "status": status,
        "code": code,
    }
