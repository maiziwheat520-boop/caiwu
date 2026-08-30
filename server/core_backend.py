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
ACCOUNTING_DIMENSIONS_CORE_PATH = "/internal/v1/accounting-dimensions"


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
