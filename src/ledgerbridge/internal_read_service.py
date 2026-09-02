"""Fixture-backed, synthetic-only implementation of the R1 internal read contract.

The service deliberately has no database, artifact-store, connector, or network
dependency.  It loads a packaged R0 fixture into validated DTOs and applies the
authorization contract before filtering or reading any evidence bytes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from importlib import resources
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.artifacts import ArtifactStoreError, PublishedArtifact, storage_key_for_digest
from ledgerbridge.candidate_contract import (
    Blocker,
    CandidateProjection,
    CandidateStatus,
    EvidenceReference,
    IngestChannel,
    ReviewRisk,
    ReviewRiskCode,
    ReviewSummary,
    SourceProjection,
)
from ledgerbridge.counterparty import CounterpartyClass
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactError,
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.internal_read_audit import (
    EvidenceReadReceipt,
    InternalReadReceiptSink,
)
from ledgerbridge.internal_read_contract import (
    AccountingDimensions,
    CandidatePage,
    CapabilitiesResponse,
    Capability,
    EntityGrant,
    LedgerSummary,
    ReconciliationProjection,
    ResourceNotVisible,
    WorkloadPrincipal,
    authorize_candidate_read,
    authorize_collection_read,
    authorize_read,
    require_candidate_visible_scope,
    require_capability,
)
from ledgerbridge.internal_read_cursor import CursorInvalid, ReadCursorSigner
from ledgerbridge.keyring import KeyProviderError, WrappedKey
from ledgerbridge.review_risk import derive_review_risks

_RESOURCE_PACKAGE = "ledgerbridge.synthetic_read_data"
_FIXTURE_NAME = "r0_contract_fixture.json"
_FIXTURE_SHA256 = "5c5892dd1c79cca0eb8ea280b35b5a1860e5320d5d52249f1a8a15acc4ce4807"
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MAX_PAGE_ITEMS = 100


class SyntheticResourceIntegrityError(RuntimeError):
    """A packaged synthetic resource failed its complete integrity check."""


class InternalReadBackendUnavailable(RuntimeError):
    """The database reader boundary is not available or returned malformed data."""


class AccountingDimensionsInvalid(InternalReadBackendUnavailable):
    """Active dimension labels are ambiguous and require registry governance."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceContent(_FrozenModel):
    """Verified evidence bytes and allowlisted download metadata."""

    content: bytes
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    media_type: Literal["application/octet-stream"] = "application/octet-stream"
    filename: str = Field(pattern=r"^[A-Za-z0-9._-]+$", min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def metadata_matches_content(self) -> EvidenceContent:
        if len(self.content) != self.byte_size:
            raise ValueError("evidence byte_size does not match content")
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise ValueError("evidence sha256 does not match content")
        return self


@dataclass(frozen=True, slots=True)
class _DatabaseEvidenceMetadata:
    evidence_ref: UUID
    blob_ref: UUID
    entity_ref: UUID
    business_unit_id: UUID
    business_unit_ref: str
    object_ref: str
    filename: str
    plaintext_sha256: bytes
    plaintext_size: int
    ciphertext_sha256: bytes
    ciphertext_size: int
    storage_key: str
    envelope_metadata: EncryptedEnvelopeMetadata


class _Provenance(_FrozenModel):
    kind: Literal["synthetic"]
    contract_version: Literal["ledgerbridge.candidate.v1"]
    state_graph_version: Literal["ledgerbridge.candidate-state.v1"]
    contains_real_data: Literal[False]


class _Entity(_FrozenModel):
    entity_ref: UUID
    business_unit_refs: tuple[str, ...] = Field(min_length=1)


class _EvidenceMetadata(_FrozenModel):
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    path: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_size: Annotated[int, Field(strict=True, ge=0)]
    declared_media_type: str = Field(min_length=1, max_length=200)
    served_media_type: Literal["text/plain", "application/octet-stream"]

    @model_validator(mode="after")
    def path_is_safe_resource_name(self) -> _EvidenceMetadata:
        if _RESOURCE_NAME.fullmatch(self.path) is None:
            raise ValueError("evidence path must be a safe packaged-resource basename")
        return self


class _LedgerEntry(_FrozenModel):
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    status: Literal["POSTED", "DRAFT", "REVERSED"]
    amount_minor: Annotated[int, Field(strict=True)]
    currency: Literal["CNY"]


class _CandidatePageFixture(_FrozenModel):
    candidate_indexes: tuple[Annotated[int, Field(strict=True, ge=0)], ...]
    next_cursor: None


class _ReadResponses(_FrozenModel):
    capabilities: CapabilitiesResponse
    accounting_dimensions: tuple[AccountingDimensions, ...] = Field(min_length=1)
    candidate_page: _CandidatePageFixture
    reconciliation: ReconciliationProjection
    ledger_summary: LedgerSummary


class _SyntheticFixture(_FrozenModel):
    provenance: _Provenance
    entities: tuple[_Entity, ...] = Field(min_length=1)
    evidence_objects: tuple[_EvidenceMetadata, ...] = Field(min_length=1)
    candidates: tuple[CandidateProjection, ...] = Field(min_length=1)
    ledger_entries: tuple[_LedgerEntry, ...]
    read_responses: _ReadResponses

    @model_validator(mode="after")
    def references_are_closed_and_unique(self) -> _SyntheticFixture:
        entity_scopes = {
            (entity.entity_ref, business_unit_ref)
            for entity in self.entities
            for business_unit_ref in entity.business_unit_refs
        }
        dimensions_by_entity = {
            dimensions.entity_ref: dimensions
            for dimensions in self.read_responses.accounting_dimensions
        }
        if len(dimensions_by_entity) != len(self.read_responses.accounting_dimensions):
            raise ValueError("accounting dimension entities must be unique")
        for entity in self.entities:
            dimensions = dimensions_by_entity.get(entity.entity_ref)
            if dimensions is None or {item.ref for item in dimensions.business_units} != set(
                entity.business_unit_refs
            ):
                raise ValueError("accounting dimension business units must match entity scope")
        evidence_by_ref = {item.evidence_ref: item for item in self.evidence_objects}
        if len(evidence_by_ref) != len(self.evidence_objects):
            raise ValueError("evidence refs must be unique")
        candidate_refs = {item.candidate_ref for item in self.candidates}
        if len(candidate_refs) != len(self.candidates):
            raise ValueError("candidate refs must be unique")
        if len(self.candidates) > _MAX_PAGE_ITEMS:
            raise ValueError("synthetic candidate snapshot exceeds the bounded page")
        if any(
            (item.entity_ref, item.business_unit_ref) not in entity_scopes
            for item in self.evidence_objects
        ):
            raise ValueError("evidence scope must exist in fixture entities")
        if any(
            reference.evidence_ref not in evidence_by_ref
            for candidate in self.candidates
            for reference in candidate.evidence
        ):
            raise ValueError("candidate evidence ref is missing from fixture")
        indexes = self.read_responses.candidate_page.candidate_indexes
        if len(set(indexes)) != len(indexes) or any(
            index >= len(self.candidates) for index in indexes
        ):
            raise ValueError("candidate page indexes must be unique and in range")
        return self


class SyntheticInternalReadService:
    """Read-only service over immutable, packaged synthetic fixture data."""

    def __init__(self) -> None:
        try:
            fixture_bytes = _read_resource_bytes(_FIXTURE_NAME)
            if hashlib.sha256(fixture_bytes).hexdigest() != _FIXTURE_SHA256:
                raise ValueError("synthetic read fixture digest does not match")
            self._fixture = _SyntheticFixture.model_validate_json(fixture_bytes, strict=True)
            self._validate_posted_only_invariants()
        except (OSError, ValueError) as exc:
            raise SyntheticResourceIntegrityError(
                "packaged synthetic read fixture failed validation"
            ) from exc

    def capabilities(self, principal: WorkloadPrincipal) -> CapabilitiesResponse:
        authorize_read(principal, Capability.SYSTEM_READ)
        return self._fixture.read_responses.capabilities

    def get_accounting_dimensions(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
    ) -> AccountingDimensions:
        require_capability(principal, Capability.CANDIDATE_DECIDE)
        matching = [grant for grant in principal.grants if grant.entity_ref == entity_ref]
        if len(matching) != 1:
            raise ResourceNotVisible("resource was not found")
        grant = matching[0]
        registered = next(
            (
                dimensions
                for dimensions in self._fixture.read_responses.accounting_dimensions
                if dimensions.entity_ref == entity_ref
            ),
            None,
        )
        if registered is None:
            raise SyntheticResourceIntegrityError("synthetic accounting dimensions are not closed")
        units = {item.ref: item for item in registered.business_units}
        if any(ref not in units for ref in grant.business_unit_refs):
            raise SyntheticResourceIntegrityError("synthetic accounting dimensions are not closed")
        return AccountingDimensions(
            entity_ref=entity_ref,
            business_units=tuple(units[ref] for ref in sorted(grant.business_unit_refs)),
            categories=registered.categories,
        )

    def list_candidates(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None = None,
        status: CandidateStatus | None = None,
        business_unit: str | None = None,
        cursor: str | None = None,
    ) -> CandidatePage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
        if cursor is not None:
            raise ValueError("synthetic reader does not accept cursors")
        if month is not None:
            _validate_month(month)
        if business_unit is not None and not (1 <= len(business_unit) <= 100):
            raise ValueError("business_unit must contain 1 to 100 characters")

        visible: list[CandidateProjection] = []
        for candidate in self._fixture.candidates:
            try:
                authorize_candidate_read(
                    principal,
                    entity_ref=candidate.entity_ref,
                    business_unit_ref=candidate.business_unit_ref,
                )
            except ResourceNotVisible:
                continue
            if month is not None and candidate.accounting_month != month:
                continue
            if status is not None and candidate.status != status:
                continue
            if business_unit is not None and candidate.business_unit_ref != business_unit:
                continue
            visible.append(candidate)

        ordered = sorted(visible, key=lambda item: (item.created_at, item.candidate_ref.int))
        return CandidatePage(items=tuple(ordered[:_MAX_PAGE_ITEMS]), next_cursor=None)

    def get_candidate(
        self,
        principal: WorkloadPrincipal,
        candidate_ref: UUID,
    ) -> CandidateProjection:
        require_capability(principal, Capability.CANDIDATE_READ)
        visible_candidate: CandidateProjection | None = None
        for candidate in self._fixture.candidates:
            try:
                require_candidate_visible_scope(
                    principal,
                    entity_ref=candidate.entity_ref,
                    business_unit_ref=candidate.business_unit_ref,
                )
            except ResourceNotVisible:
                continue
            if candidate.candidate_ref == candidate_ref:
                visible_candidate = candidate
        if visible_candidate is None:
            raise ResourceNotVisible("resource was not found")
        return visible_candidate

    def get_evidence(
        self,
        principal: WorkloadPrincipal,
        evidence_ref: UUID,
    ) -> EvidenceContent:
        require_capability(principal, Capability.EVIDENCE_READ)
        metadata: _EvidenceMetadata | None = None
        for item in self._fixture.evidence_objects:
            try:
                authorize_read(
                    principal,
                    Capability.EVIDENCE_READ,
                    entity_ref=item.entity_ref,
                    business_unit_ref=item.business_unit_ref,
                )
            except ResourceNotVisible:
                continue
            if item.evidence_ref == evidence_ref:
                metadata = item
        if metadata is None:
            raise ResourceNotVisible("resource was not found")

        try:
            content = _read_resource_bytes(metadata.path)
        except OSError as exc:
            raise SyntheticResourceIntegrityError(
                "packaged synthetic evidence failed integrity verification"
            ) from exc
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != metadata.byte_size or digest != metadata.sha256:
            raise SyntheticResourceIntegrityError(
                "packaged synthetic evidence failed integrity verification"
            )
        return EvidenceContent(
            content=content,
            entity_ref=metadata.entity_ref,
            business_unit_ref=metadata.business_unit_ref,
            filename=f"evidence-{metadata.evidence_ref.hex}.bin",
            sha256=digest,
            byte_size=len(content),
        )

    def get_reconciliation(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str,
        entity_ref: UUID,
        business_unit_ref: str,
    ) -> ReconciliationProjection:
        _validate_month(month)
        authorize_read(
            principal,
            Capability.RECONCILIATION_READ,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        result = self._fixture.read_responses.reconciliation
        if (
            result.month != month
            or result.entity_ref != entity_ref
            or result.business_unit_ref != business_unit_ref
        ):
            raise ResourceNotVisible("resource was not found")
        return result

    def get_ledger_summary(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        from_month: str,
        to_month: str,
    ) -> LedgerSummary:
        """Project the single 2026-08 synthetic snapshot into an inclusive month range.

        This method does not infer missing months or query a live ledger. A range
        outside the fixture snapshot is therefore a valid, empty synthetic result.
        """

        _validate_month(from_month)
        _validate_month(to_month)
        if from_month > to_month:
            raise ValueError("from_month must be less than or equal to to_month")
        authorize_read(
            principal,
            Capability.LEDGER_READ,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        source = self._fixture.read_responses.ledger_summary
        if source.entity_ref != entity_ref or source.business_unit_ref != business_unit_ref:
            raise ResourceNotVisible("resource was not found")
        included = from_month <= source.from_month and source.to_month <= to_month
        return LedgerSummary.model_validate(
            {
                **source.model_dump(),
                "from_month": from_month,
                "to_month": to_month,
                "totals_minor": source.totals_minor if included else {},
            }
        )

    def _validate_posted_only_invariants(self) -> None:
        reconciliation = self._fixture.read_responses.reconciliation
        posted = sum(
            row.amount_minor
            for row in self._fixture.ledger_entries
            if row.status == "POSTED"
            and row.entity_ref == reconciliation.entity_ref
            and row.business_unit_ref == reconciliation.business_unit_ref
        )
        if posted != reconciliation.posted_amount_minor:
            raise ValueError("reconciliation total is not POSTED-only")

        summary = self._fixture.read_responses.ledger_summary
        posted = sum(
            row.amount_minor
            for row in self._fixture.ledger_entries
            if row.status == "POSTED"
            and row.entity_ref == summary.entity_ref
            and row.business_unit_ref == summary.business_unit_ref
        )
        if summary.posting_status != "POSTED" or sum(summary.totals_minor.values()) != posted:
            raise ValueError("ledger summary is not POSTED-only")


class DatabaseInternalReadService:
    """Reader-role adapter for the closed R1 PostgreSQL function surface.

    The login used by this service is intentionally separate from the general
    API session.  Candidate and reconciliation facts are obtained only through
    the SECURITY DEFINER functions installed by migration 0015; no public base
    table or unscoped projection is queried here.  Evidence decryption and the
    ledger aggregate function are separate gates and therefore fail closed
    until their reviewed application boundary is installed.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        cursor_signer: ReadCursorSigner | None = None,
        encrypted_artifact_store: EncryptedArtifactStore | None = None,
        receipt_sink: InternalReadReceiptSink | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cursor_signer = cursor_signer
        self._encrypted_artifact_store = encrypted_artifact_store
        self._receipt_sink = receipt_sink

    def capabilities(self, principal: WorkloadPrincipal) -> CapabilitiesResponse:
        authorize_read(principal, Capability.SYSTEM_READ)
        return CapabilitiesResponse(
            data_mode="database",
            enabled_modules=(
                "accounting-dimensions",
                "candidates",
                "evidence",
                "reconciliations",
                "ledger-summary",
                "company-reporting",
                "personal-finance",
            ),
        )

    def get_accounting_dimensions(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
    ) -> AccountingDimensions:
        require_capability(principal, Capability.CANDIDATE_DECIDE)
        matching = [grant for grant in principal.grants if grant.entity_ref == entity_ref]
        if len(matching) != 1:
            raise ResourceNotVisible("resource was not found")
        grant = matching[0]
        if (grant.business_unit_refs or grant.business_unit_ids) and not (
            grant.business_unit_bindings
        ):
            raise InternalReadBackendUnavailable(
                "database grants require explicit business-unit ref/UUID bindings"
            )
        sorted_bindings = sorted(grant.business_unit_bindings, key=lambda item: item[0])
        # Psycopg adapts tuples as PostgreSQL composite values, not arrays.
        # Keep these bindings as lists so the explicit uuid[]/varchar[] casts
        # receive valid array literals in the database-backed runtime.
        business_unit_refs = [ref for ref, _ in sorted_bindings]
        business_unit_ids = [value for _, value in sorted_bindings]
        try:
            with self._session_factory() as session:
                row = (
                    session.execute(
                        text(
                            "SELECT internal_read.get_accounting_dimensions("
                            "CAST(:entity_ref AS uuid), "
                            "CAST(:business_unit_ids AS uuid[]), "
                            "CAST(:business_unit_refs AS varchar[])) AS dimensions"
                        ),
                        {
                            "entity_ref": entity_ref,
                            "business_unit_ids": business_unit_ids,
                            "business_unit_refs": business_unit_refs,
                        },
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise InternalReadBackendUnavailable(
                        "database accounting dimensions returned no result"
                    )
                dimensions = AccountingDimensions.model_validate(row["dimensions"])
        except InternalReadBackendUnavailable:
            raise
        except SQLAlchemyError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate == "LB005":
                raise AccountingDimensionsInvalid(
                    "active accounting dimension labels require registry governance"
                ) from exc
            raise InternalReadBackendUnavailable(
                "database accounting dimensions read failed"
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable(
                "database accounting dimensions read failed"
            ) from exc
        returned_refs = {item.ref for item in dimensions.business_units}
        if dimensions.entity_ref != entity_ref or not returned_refs.issubset(
            set(business_unit_refs)
        ):
            raise InternalReadBackendUnavailable(
                "database accounting dimension scope binding is invalid"
            )
        return dimensions

    def list_candidates(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None = None,
        status: CandidateStatus | None = None,
        business_unit: str | None = None,
        cursor: str | None = None,
    ) -> CandidatePage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
        if month is not None:
            _validate_month(month)
        if business_unit is not None and not (1 <= len(business_unit) <= 100):
            raise ValueError("business_unit must contain 1 to 100 characters")
        scopes_by_grant: list[tuple[EntityGrant, UUID | None]] = []
        for grant in principal.grants:
            if (grant.business_unit_refs or grant.business_unit_ids) and not (
                grant.business_unit_bindings
            ):
                raise InternalReadBackendUnavailable(
                    "database grants require explicit business-unit ref/UUID bindings"
                )
            selected_bindings = tuple(
                (ref, value)
                for ref, value in grant.business_unit_bindings
                if business_unit is None or ref == business_unit
            )
            if business_unit is None and len(selected_bindings) > 1:
                raise InternalReadBackendUnavailable(
                    "database candidate pagination requires one bound business unit"
                )
            scopes_by_grant.extend((grant, value) for _, value in selected_bindings)
            if grant.allow_unassigned_candidates:
                scopes_by_grant.append((grant, None))
        if len(scopes_by_grant) > 1:
            raise InternalReadBackendUnavailable(
                "database candidate pagination does not yet support multiple scopes"
            )

        if cursor is not None and self._cursor_signer is None:
            raise InternalReadBackendUnavailable("signed cursor key is unavailable")
        try:
            with self._session_factory() as session:
                if cursor is None:
                    sequence, horizon_hash = self._audit_horizon(session)
                    last_created_at = None
                    last_candidate_id = None
                else:
                    cursor_signer = self._cursor_signer
                    if cursor_signer is None:
                        raise InternalReadBackendUnavailable("signed cursor key is unavailable")
                    cursor_claims = cursor_signer.verify(
                        cursor,
                        principal,
                        month=month,
                        status=status.value if status is not None else None,
                        business_unit=business_unit,
                    )
                    sequence = cursor_claims["horizon_sequence"]
                    horizon_hash = cursor_claims["horizon_hash"]
                    last_created_at = cursor_claims["last_created_at"]
                    last_candidate_id = cursor_claims["last_candidate_id"]
                rows: list[Mapping[str, object]] = []
                seen: set[UUID] = set()
                raw_has_more = False
                for grant, business_unit_id in scopes_by_grant:
                    if business_unit is not None and business_unit_id is None:
                        continue
                    query_last_created_at = last_created_at
                    query_last_candidate_id = last_candidate_id
                    while True:
                        params = {
                            "entity_id": grant.entity_ref,
                            "business_unit_id": business_unit_id,
                            "status": status.value if status is not None else None,
                            "horizon_sequence": sequence,
                            "horizon_hash": horizon_hash,
                            "last_created_at": query_last_created_at,
                            "last_candidate_id": query_last_candidate_id,
                            "limit": 100,
                        }
                        result = session.execute(
                            text(
                                """
                                SELECT * FROM internal_read.list_candidates_as_of(
                                    :entity_id, :business_unit_id, :status,
                                    :horizon_sequence, :horizon_hash,
                                    :last_created_at, :last_candidate_id, :limit
                                )
                                """
                            ),
                            params,
                        )
                        raw_rows = [
                            cast(Mapping[str, object], dict(row)) for row in result.mappings()
                        ]
                        raw_has_more = len(raw_rows) > 100
                        for row_map in raw_rows:
                            candidate = self._candidate(row_map)
                            if candidate.entity_ref != grant.entity_ref:
                                raise InternalReadBackendUnavailable(
                                    "database candidate scope binding is invalid"
                                )
                            if business_unit_id is None:
                                if candidate.business_unit_ref is not None:
                                    raise InternalReadBackendUnavailable(
                                        "database candidate scope binding is invalid"
                                    )
                            else:
                                expected_ref = next(
                                    ref
                                    for ref, value in grant.business_unit_bindings
                                    if value == business_unit_id
                                )
                                if candidate.business_unit_ref != expected_ref:
                                    raise InternalReadBackendUnavailable(
                                        "database candidate scope binding is invalid"
                                    )
                            if candidate.candidate_ref in seen:
                                continue
                            if (
                                business_unit is not None
                                and candidate.business_unit_ref != business_unit
                            ):
                                continue
                            if month is not None and candidate.accounting_month != month:
                                continue
                            seen.add(candidate.candidate_ref)
                            rows.append(row_map)
                        if not (month is not None and raw_has_more and len(rows) < 100):
                            break
                        boundary = self._candidate(raw_rows[99])
                        query_last_created_at = boundary.created_at
                        query_last_candidate_id = boundary.candidate_ref
                satisfied_by_candidate: dict[UUID, frozenset[ReviewRiskCode]] = {}
                counterparties_by_candidate: dict[UUID, tuple[str, CounterpartyClass]] = {}
                if rows:
                    scope_grant, scope_business_unit_id = scopes_by_grant[0]
                    if scope_business_unit_id is not None:
                        candidate_ids = tuple(UUID(str(row["candidate_ref"])) for row in rows)
                        satisfied_by_candidate = self._candidate_risk_satisfactions(
                            session,
                            entity_ref=scope_grant.entity_ref,
                            business_unit_id=scope_business_unit_id,
                            candidate_ids=candidate_ids,
                            horizon_sequence=sequence,
                            horizon_hash=horizon_hash,
                        )
                        counterparties_by_candidate = self._candidate_counterparty_facts(
                            session,
                            entity_ref=scope_grant.entity_ref,
                            business_unit_id=scope_business_unit_id,
                            candidate_ids=candidate_ids,
                            horizon_sequence=sequence,
                            horizon_hash=horizon_hash,
                        )
                candidates = [
                    self._candidate(
                        row,
                        satisfied_review_risk_codes=satisfied_by_candidate.get(
                            UUID(str(row["candidate_ref"])), frozenset()
                        ),
                        counterparty=counterparties_by_candidate.get(
                            UUID(str(row["candidate_ref"]))
                        ),
                    )
                    for row in rows
                ]
        except (InternalReadBackendUnavailable, CursorInvalid):
            raise
        except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable("database candidate read failed") from exc

        candidates.sort(key=lambda item: (item.created_at, item.candidate_ref.int))
        has_more = len(candidates) > 100 or raw_has_more
        page_items = tuple(candidates[:100])
        next_cursor = None
        if has_more:
            if self._cursor_signer is None:
                raise InternalReadBackendUnavailable("signed cursor key is unavailable")
            boundary = page_items[-1]
            try:
                next_cursor = self._cursor_signer.issue(
                    principal,
                    month=month,
                    status=status.value if status is not None else None,
                    business_unit=business_unit,
                    horizon_sequence=sequence,
                    horizon_hash=horizon_hash,
                    last_created_at=boundary.created_at,
                    last_candidate_id=boundary.candidate_ref,
                )
            except CursorInvalid as exc:
                raise InternalReadBackendUnavailable("signed cursor could not be issued") from exc
        return CandidatePage(items=page_items, next_cursor=next_cursor)

    def get_candidate(
        self,
        principal: WorkloadPrincipal,
        candidate_ref: UUID,
    ) -> CandidateProjection:
        require_capability(principal, Capability.CANDIDATE_READ)
        # Migration C intentionally exposes a bounded list function rather than
        # a broad candidate SELECT.  Resolve a single object through that same
        # allowlisted path, then apply object scope before returning it.
        cursor: str | None = None
        while True:
            page = self.list_candidates(principal, cursor=cursor)
            for candidate in page.items:
                if candidate.candidate_ref == candidate_ref:
                    return candidate
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        raise ResourceNotVisible("resource was not found")

    def get_evidence(
        self,
        principal: WorkloadPrincipal,
        evidence_ref: UUID,
    ) -> EvidenceContent:
        require_capability(principal, Capability.EVIDENCE_READ)
        store = self._encrypted_artifact_store
        if store is None:
            raise InternalReadBackendUnavailable(
                "database evidence retrieval requires the reviewed S1 decryptor boundary"
            )
        try:
            with self._session_factory() as session:
                row = (
                    session.execute(
                        text(
                            """
                            SELECT * FROM internal_read.resolve_active_evidence_blob(
                                :evidence_ref
                            )
                            """
                        ),
                        {"evidence_ref": evidence_ref},
                    )
                    .mappings()
                    .first()
                )
        except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable("database evidence read failed") from exc
        if row is None:
            raise ResourceNotVisible("resource was not found")

        try:
            metadata = self._evidence_metadata(cast(Mapping[str, object], row))
            if metadata.evidence_ref != evidence_ref:
                raise InternalReadBackendUnavailable("database evidence identity is invalid")
            authorize_read(
                principal,
                Capability.EVIDENCE_READ,
                entity_ref=metadata.entity_ref,
                business_unit_ref=metadata.business_unit_ref,
            )
            if (
                self._business_unit_id(principal, metadata.entity_ref, metadata.business_unit_ref)
                != metadata.business_unit_id
            ):
                raise InternalReadBackendUnavailable("database evidence scope binding is invalid")
            artifact = EncryptedPublishedArtifact(
                object_ref=metadata.object_ref,
                plaintext_sha256=metadata.plaintext_sha256,
                plaintext_size=metadata.plaintext_size,
                ciphertext=PublishedArtifact(
                    sha256=metadata.ciphertext_sha256,
                    byte_size=metadata.ciphertext_size,
                    storage_key=metadata.storage_key,
                    created=False,
                ),
            )
            with store.open_verified(
                artifact, envelope_metadata=metadata.envelope_metadata
            ) as stream:
                content = stream.read()
            digest = hashlib.sha256(content).digest()
            if len(content) != metadata.plaintext_size or digest != metadata.plaintext_sha256:
                raise InternalReadBackendUnavailable("database evidence plaintext is invalid")
            if self._receipt_sink is not None:
                try:
                    self._receipt_sink.append(
                        EvidenceReadReceipt(
                            principal_ref=principal.principal_ref,
                            principal_san_uri=principal.san_uri,
                            policy_generation=str(principal.policy_generation),
                            evidence_ref=metadata.evidence_ref,
                            entity_ref=metadata.entity_ref,
                            business_unit_id=metadata.business_unit_id,
                            blob_ref=metadata.blob_ref,
                            byte_size=len(content),
                            sha256=digest.hex(),
                        )
                    )
                except Exception as exc:
                    raise InternalReadBackendUnavailable(
                        "database evidence receipt could not be recorded"
                    ) from exc
            return EvidenceContent(
                content=content,
                entity_ref=metadata.entity_ref,
                business_unit_ref=metadata.business_unit_ref,
                media_type="application/octet-stream",
                filename=metadata.filename,
                sha256=digest.hex(),
                byte_size=len(content),
            )
        except ResourceNotVisible:
            raise
        except (
            ValueError,
            TypeError,
            KeyError,
            ArtifactStoreError,
            EncryptedArtifactError,
            KeyProviderError,
        ) as exc:
            raise InternalReadBackendUnavailable("database evidence payload is invalid") from exc

    def get_reconciliation(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str,
        entity_ref: UUID,
        business_unit_ref: str,
    ) -> ReconciliationProjection:
        _validate_month(month)
        authorize_read(
            principal,
            Capability.RECONCILIATION_READ,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        business_unit_id = self._business_unit_id(principal, entity_ref, business_unit_ref)
        try:
            with self._session_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                row = (
                    session.execute(
                        text(
                            """
                        SELECT * FROM internal_read.get_reconciliation_as_of(
                            :entity_id, :business_unit_id, :accounting_month,
                            :horizon_sequence, :horizon_hash
                        )
                        """
                        ),
                        {
                            "entity_id": entity_ref,
                            "business_unit_id": business_unit_id,
                            "accounting_month": date.fromisoformat(f"{month}-01"),
                            "horizon_sequence": sequence,
                            "horizon_hash": horizon_hash,
                        },
                    )
                    .mappings()
                    .first()
                )
        except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable("database reconciliation read failed") from exc
        if row is None:
            raise ResourceNotVisible("resource was not found")
        try:
            projection = ReconciliationProjection.model_validate(dict(row), strict=True)
        except (ValueError, TypeError) as exc:
            raise InternalReadBackendUnavailable(
                "database reconciliation projection is invalid"
            ) from exc
        if (
            projection.entity_ref != entity_ref
            or projection.business_unit_ref != business_unit_ref
            or projection.month != month
        ):
            raise InternalReadBackendUnavailable(
                "database reconciliation projection is out of scope"
            )
        return projection

    def get_ledger_summary(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        from_month: str,
        to_month: str,
    ) -> LedgerSummary:
        _validate_month(from_month)
        _validate_month(to_month)
        if from_month > to_month:
            raise ValueError("from_month must be less than or equal to to_month")
        authorize_read(
            principal,
            Capability.LEDGER_READ,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        business_unit_id = self._business_unit_id(principal, entity_ref, business_unit_ref)
        try:
            with self._session_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                rows = list(
                    session.execute(
                        text(
                            """
                            SELECT * FROM internal_read.get_ledger_summary_as_of(
                                :entity_id, :business_unit_id, :from_month, :to_month,
                                :horizon_sequence, :horizon_hash
                            )
                            """
                        ),
                        {
                            "entity_id": entity_ref,
                            "business_unit_id": business_unit_id,
                            "from_month": date.fromisoformat(f"{from_month}-01"),
                            "to_month": date.fromisoformat(f"{to_month}-01"),
                            "horizon_sequence": sequence,
                            "horizon_hash": horizon_hash,
                        },
                    ).mappings()
                )
        except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable("database ledger summary read failed") from exc

        totals: dict[str, int] = {}
        for raw in rows:
            try:
                row = dict(raw)
                if (
                    row["entity_ref"] != entity_ref
                    or row["business_unit_ref"] != business_unit_ref
                    or row["from_month"] != from_month
                    or row["to_month"] != to_month
                    or row["posting_status"] != "POSTED"
                    or row["currency"] != "CNY"
                ):
                    raise InternalReadBackendUnavailable(
                        "database ledger summary projection is out of scope"
                    )
                category = row["category_code"]
                amount = row["amount_minor"]
                if not isinstance(category, str) or not category or type(amount) is not int:
                    raise InternalReadBackendUnavailable(
                        "database ledger summary projection is invalid"
                    )
                totals[category] = totals.get(category, 0) + amount
            except InternalReadBackendUnavailable:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise InternalReadBackendUnavailable(
                    "database ledger summary projection is invalid"
                ) from exc
        try:
            return LedgerSummary.model_validate(
                {
                    "entity_ref": entity_ref,
                    "business_unit_ref": business_unit_ref,
                    "from_month": from_month,
                    "to_month": to_month,
                    "posting_status": "POSTED",
                    "currency": "CNY",
                    "totals_minor": totals,
                },
                strict=True,
            )
        except (ValueError, TypeError) as exc:
            raise InternalReadBackendUnavailable("database ledger summary is invalid") from exc

    @staticmethod
    def _evidence_metadata(row: Mapping[str, object]) -> _DatabaseEvidenceMetadata:
        def require_uuid(name: str) -> UUID:
            value = row.get(name)
            if not isinstance(value, UUID):
                raise ValueError(f"evidence metadata {name} is invalid")
            return value

        def require_bytes(name: str, length: int) -> bytes:
            value = row.get(name)
            if not isinstance(value, (bytes, bytearray)) or len(value) != length:
                raise ValueError(f"evidence metadata {name} is invalid")
            return bytes(value)

        def require_text(name: str, pattern: re.Pattern[str], max_length: int) -> str:
            value = row.get(name)
            if not isinstance(value, str) or not 1 <= len(value) <= max_length:
                raise ValueError(f"evidence metadata {name} is invalid")
            if pattern.fullmatch(value) is None:
                raise ValueError(f"evidence metadata {name} is invalid")
            return value

        entity_ref = require_uuid("entity_id")
        business_unit_id = require_uuid("business_unit_id")
        business_unit_ref_value = row.get("business_unit_ref")
        if not isinstance(business_unit_ref_value, str) or not (
            1 <= len(business_unit_ref_value) <= 100
        ):
            raise ValueError("evidence metadata business_unit_ref is invalid")
        business_unit_ref = business_unit_ref_value
        object_ref = require_text("object_ref", re.compile(r"[0-9a-f]{64}\Z"), 64)
        storage_key = require_text(
            "storage_key", re.compile(r"sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\Z"), 77
        )
        media_type = row.get("media_type")
        if media_type != "application/octet-stream":
            raise ValueError("evidence media type is not allowlisted")
        filename_value = row.get("display_name")
        if filename_value is None:
            filename = f"evidence-{require_uuid('evidence_ref').hex}.bin"
        elif (
            isinstance(filename_value, str) and _RESOURCE_NAME.fullmatch(filename_value) is not None
        ):
            filename = filename_value
        else:
            raise ValueError("evidence display name is invalid")
        plaintext_size = row.get("plaintext_size")
        ciphertext_size = row.get("ciphertext_size")
        if (
            type(plaintext_size) is not int
            or not 0 <= plaintext_size <= 134217728
            or type(ciphertext_size) is not int
            or not 0 <= ciphertext_size <= 268435456
        ):
            raise ValueError("evidence size metadata is invalid")
        ciphertext_sha256 = require_bytes("ciphertext_sha256", 32)
        if storage_key != storage_key_for_digest(ciphertext_sha256):
            raise ValueError("evidence storage key is not canonical")
        if row.get("envelope_schema") != "ledgerbridge.secretstream.v1":
            raise ValueError("evidence envelope schema is invalid")
        if row.get("algorithm") != "xchacha20poly1305-secretstream":
            raise ValueError("evidence envelope algorithm is invalid")
        chunk_size = row.get("chunk_size")
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1_048_576:
            raise ValueError("evidence chunk size is invalid")
        if len(require_bytes("stream_header", 24)) != 24:
            raise ValueError("evidence stream header is invalid")
        wrapped_generation = row.get("wrapped_key_generation")
        if (
            not isinstance(wrapped_generation, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", wrapped_generation) is None
        ):
            raise ValueError("evidence wrapped key generation is invalid")
        require_bytes("wrapped_key_nonce", 24)
        require_bytes("wrapped_key_ciphertext", 48)
        if row.get("purpose") != "ledgerbridge-artifact-v2":
            raise ValueError("evidence purpose is invalid")
        if row.get("aad_scheme") != "ledgerbridge.artifact.object.v2":
            raise ValueError("evidence AAD scheme is invalid")
        envelope_metadata = EncryptedEnvelopeMetadata(
            chunk_size=chunk_size,
            stream_header=require_bytes("stream_header", 24),
            wrapped_key=WrappedKey(
                generation=wrapped_generation,
                nonce=require_bytes("wrapped_key_nonce", 24),
                ciphertext=require_bytes("wrapped_key_ciphertext", 48),
            ),
        )
        return _DatabaseEvidenceMetadata(
            evidence_ref=require_uuid("evidence_ref"),
            blob_ref=require_uuid("blob_ref"),
            entity_ref=entity_ref,
            business_unit_id=business_unit_id,
            business_unit_ref=business_unit_ref,
            object_ref=object_ref,
            filename=filename,
            plaintext_sha256=require_bytes("plaintext_sha256", 32),
            plaintext_size=plaintext_size,
            ciphertext_sha256=ciphertext_sha256,
            ciphertext_size=ciphertext_size,
            storage_key=storage_key,
            envelope_metadata=envelope_metadata,
        )

    @staticmethod
    def _audit_horizon(session: Session) -> tuple[int, bytes]:
        row = (
            session.execute(
                text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
            )
            .mappings()
            .first()
        )
        if row is None or not isinstance(row["sequence"], int):
            raise InternalReadBackendUnavailable("database audit horizon is unavailable")
        horizon_hash = row["hash"]
        if not isinstance(horizon_hash, (bytes, bytearray)) or len(horizon_hash) != 32:
            raise InternalReadBackendUnavailable("database audit horizon is malformed")
        return row["sequence"], bytes(horizon_hash)

    @staticmethod
    def _business_unit_id(
        principal: WorkloadPrincipal,
        entity_ref: UUID,
        business_unit_ref: str,
    ) -> UUID:
        for grant in principal.grants:
            if grant.entity_ref != entity_ref:
                continue
            for ref, business_unit_id in grant.business_unit_bindings:
                if ref == business_unit_ref:
                    return business_unit_id
        raise InternalReadBackendUnavailable(
            "database grants must bind the requested business-unit ref to an immutable UUID"
        )

    @staticmethod
    def _candidate_risk_satisfactions(
        session: Session,
        *,
        entity_ref: UUID,
        business_unit_id: UUID,
        candidate_ids: tuple[UUID, ...],
        horizon_sequence: int,
        horizon_hash: bytes,
    ) -> dict[UUID, frozenset[ReviewRiskCode]]:
        if not candidate_ids:
            return {}
        rows = session.execute(
            text(
                "SELECT * FROM internal_read.list_candidate_evidence_satisfactions("
                ":entity_id, :business_unit_id, :candidate_ids, "
                ":horizon_sequence, :horizon_hash)"
            ),
            {
                "entity_id": entity_ref,
                "business_unit_id": business_unit_id,
                "candidate_ids": list(candidate_ids),
                "horizon_sequence": horizon_sequence,
                "horizon_hash": horizon_hash,
            },
        ).mappings()
        values: dict[UUID, set[ReviewRiskCode]] = {}
        allowed = set(candidate_ids)
        for row in rows:
            try:
                candidate_id = UUID(str(row["candidate_id"]))
                risk_code = ReviewRiskCode(str(row["risk_code"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise InternalReadBackendUnavailable(
                    "database candidate evidence satisfaction is malformed"
                ) from exc
            if candidate_id not in allowed:
                raise InternalReadBackendUnavailable(
                    "database candidate evidence satisfaction escaped the requested scope"
                )
            values.setdefault(candidate_id, set()).add(risk_code)
        return {candidate_id: frozenset(codes) for candidate_id, codes in values.items()}

    @staticmethod
    def _candidate_counterparty_facts(
        session: Session,
        *,
        entity_ref: UUID,
        business_unit_id: UUID,
        candidate_ids: tuple[UUID, ...],
        horizon_sequence: int,
        horizon_hash: bytes,
    ) -> dict[UUID, tuple[str, CounterpartyClass]]:
        if not candidate_ids:
            return {}
        rows = session.execute(
            text(
                "SELECT * FROM internal_read.list_candidate_counterparty_facts("
                ":entity_id, :business_unit_id, :candidate_ids, "
                ":horizon_sequence, :horizon_hash)"
            ),
            {
                "entity_id": entity_ref,
                "business_unit_id": business_unit_id,
                "candidate_ids": list(candidate_ids),
                "horizon_sequence": horizon_sequence,
                "horizon_hash": horizon_hash,
            },
        ).mappings()
        values: dict[UUID, tuple[str, CounterpartyClass]] = {}
        allowed = set(candidate_ids)
        for row in rows:
            try:
                candidate_id = UUID(str(row["candidate_id"]))
                counterparty_ref = str(row["counterparty_ref"])
                counterparty_class = CounterpartyClass(str(row["counterparty_class"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise InternalReadBackendUnavailable(
                    "database candidate counterparty fact is malformed"
                ) from exc
            if candidate_id not in allowed or candidate_id in values:
                raise InternalReadBackendUnavailable(
                    "database candidate counterparty fact escaped or duplicated scope"
                )
            if re.fullmatch(r"cp_[a-z0-9_]{1,96}", counterparty_ref) is None:
                raise InternalReadBackendUnavailable(
                    "database candidate counterparty fact is malformed"
                )
            values[candidate_id] = (counterparty_ref, counterparty_class)
        return values

    @staticmethod
    def _candidate(
        row: Mapping[str, object],
        *,
        satisfied_review_risk_codes: frozenset[ReviewRiskCode] = frozenset(),
        counterparty: tuple[str, CounterpartyClass] | None = None,
    ) -> CandidateProjection:
        try:
            value = dict(row)
            value["status"] = CandidateStatus(cast(str, value["status"]))
            if counterparty is not None:
                value["counterparty_ref"], value["counterparty_class"] = counterparty
            source = dict(cast(Mapping[str, object], value["source"]))
            source["ingest_channel"] = _wire_ingest_channel(
                cast(str | IngestChannel, source["ingest_channel"])
            )
            source_projection = SourceProjection.model_validate(source)
            value["source"] = source_projection
            value["evidence"] = tuple(
                EvidenceReference.model_validate(item)
                for item in _database_json_objects(value["evidence"], field="evidence")
            )
            value["blockers"] = tuple(
                Blocker.model_validate(item)
                for item in _database_json_objects(value["blockers"], field="blockers")
            )
            value["review_risks"] = tuple(
                ReviewRisk.model_validate(item)
                for item in derive_review_risks(
                    source_system=source_projection.source_system,
                    category_code=cast(str | None, value.get("category_code")),
                    summary=cast(str, value["summary"]),
                    satisfied_codes=satisfied_review_risk_codes,
                    counterparty_class=(counterparty[1] if counterparty is not None else None),
                )
            )
            value["review_summary"] = ReviewSummary.model_validate(
                dict(cast(Mapping[str, object], value["review_summary"]))
            )
            return CandidateProjection.model_validate(value, strict=True)
        except (ValueError, TypeError, KeyError) as exc:
            raise InternalReadBackendUnavailable(
                "database candidate projection is invalid"
            ) from exc


def _database_json_objects(value: object, *, field: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"database {field} projection must be a JSON array")
    if not all(isinstance(item, Mapping) for item in value):
        raise TypeError(f"database {field} projection must contain JSON objects")
    return tuple(cast(Mapping[str, object], item) for item in value)


def _wire_ingest_channel(value: str | IngestChannel) -> IngestChannel:
    """Map canonical database registry IDs onto the versioned wire contract."""
    if isinstance(value, IngestChannel):
        return value
    mapping = {
        "controlled_upload": IngestChannel.CONTROLLED_UPLOAD,
        "hermes": IngestChannel.HERMES,
        "manual_upload": IngestChannel.CONTROLLED_UPLOAD,
        "outlook": IngestChannel.OUTLOOK,
        "synthetic_upload": IngestChannel.SYNTHETIC,
    }
    try:
        return mapping[value]
    except KeyError as exc:
        raise ValueError("database ingest channel has no wire-contract mapping") from exc


def _read_resource_bytes(name: str) -> bytes:
    if _RESOURCE_NAME.fullmatch(name) is None:
        raise OSError("invalid packaged resource name")
    try:
        return resources.files(_RESOURCE_PACKAGE).joinpath(name).read_bytes()
    except ModuleNotFoundError as exc:
        raise OSError("synthetic resource package is unavailable") from exc


def _validate_month(value: str) -> None:
    if _MONTH.fullmatch(value) is None:
        raise ValueError("month must use YYYY-MM")
