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


def _problem(status: int, code: str) -> dict[str, object]:
    return {
        "type": "about:blank",
        "title": "LedgerBridge Core request failed",
        "status": status,
        "code": code,
    }
