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

PUBLICATION_SCHEMA = "payroll-ledgerbridge-publication/v1"
STATUS_SCHEMA = "1.0"
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
            with urlopen(request, timeout=timeout_seconds) as response:
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
