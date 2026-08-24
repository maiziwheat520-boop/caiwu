"""Fixture-backed, synthetic-only implementation of the R1 internal read contract.

The service deliberately has no database, artifact-store, connector, or network
dependency.  It loads a packaged R0 fixture into validated DTOs and applies the
authorization contract before filtering or reading any evidence bytes.
"""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus
from ledgerbridge.internal_read_contract import (
    CandidatePage,
    CapabilitiesResponse,
    Capability,
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

_RESOURCE_PACKAGE = "ledgerbridge.synthetic_read_data"
_FIXTURE_NAME = "r0_contract_fixture.json"
_FIXTURE_SHA256 = "9eacf0ff534f516489bf65fc9dd83a6770a4380074990a83d7a37f9acf57d2a8"
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_MAX_PAGE_ITEMS = 100


class SyntheticResourceIntegrityError(RuntimeError):
    """A packaged synthetic resource failed its complete integrity check."""


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

    def list_candidates(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None = None,
        status: CandidateStatus | None = None,
        business_unit: str | None = None,
    ) -> CandidatePage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
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
