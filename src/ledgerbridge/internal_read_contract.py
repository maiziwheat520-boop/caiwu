"""Deny-by-default authorization contract for the R0 synthetic read surface.

The certificate facts accepted here represent output from a future trusted mTLS
terminator.  R0 uses them only as deterministic test inputs; it does not install
middleware, parse identity headers, or enable an HTTP route.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import (
    JSON_SAFE_INTEGER,
    Blocker,
    CandidateAction,
    CandidateProjection,
)

READ_CONTRACT_VERSION = "ledgerbridge.internal-read.v1"


class Capability(StrEnum):
    SYSTEM_READ = "system:read"
    CANDIDATE_READ = "candidate:read"
    EVIDENCE_READ = "evidence:read"
    RECONCILIATION_READ = "reconciliation:read"
    LEDGER_READ = "ledger:read"
    CANDIDATE_CREATE = "candidate:create"
    CANDIDATE_DECIDE = "candidate:decide"
    CANDIDATE_SUPERSEDE = "candidate:supersede"
    EVIDENCE_UNLOCK = "evidence:unlock"
    ACCOUNT_REGISTRY_READ = "account-registry:read"
    ACCOUNT_REGISTRY_WRITE = "account-registry:write"
    PAYROLL_PUBLICATION_READ = "payroll-publication:read"


class ScopeMode(StrEnum):
    SYSTEM = "SYSTEM"
    COLLECTION = "COLLECTION"
    OBJECT = "OBJECT"


READ_CAPABILITIES = frozenset(
    {
        Capability.SYSTEM_READ,
        Capability.CANDIDATE_READ,
        Capability.EVIDENCE_READ,
        Capability.RECONCILIATION_READ,
        Capability.LEDGER_READ,
        Capability.ACCOUNT_REGISTRY_READ,
    }
)


READ_ROUTE_CAPABILITIES: Mapping[str, Capability] = {
    "GET /internal/v1/capabilities": Capability.SYSTEM_READ,
    "GET /internal/v1/accounting-dimensions": Capability.CANDIDATE_DECIDE,
    "GET /internal/v1/candidates": Capability.CANDIDATE_READ,
    "GET /internal/v1/candidates/{id}": Capability.CANDIDATE_READ,
    "GET /internal/v1/evidence/{id}/content": Capability.EVIDENCE_READ,
    "GET /internal/v1/reconciliations/{month}": Capability.RECONCILIATION_READ,
    "GET /internal/v1/ledger-summary": Capability.LEDGER_READ,
    "GET /internal/v1/company-reports": Capability.LEDGER_READ,
}

READ_ROUTE_SCOPE_MODES: Mapping[str, ScopeMode] = {
    "GET /internal/v1/capabilities": ScopeMode.SYSTEM,
    "GET /internal/v1/accounting-dimensions": ScopeMode.OBJECT,
    "GET /internal/v1/candidates": ScopeMode.COLLECTION,
    "GET /internal/v1/candidates/{id}": ScopeMode.OBJECT,
    "GET /internal/v1/evidence/{id}/content": ScopeMode.OBJECT,
    "GET /internal/v1/reconciliations/{month}": ScopeMode.OBJECT,
    "GET /internal/v1/ledger-summary": ScopeMode.OBJECT,
    "GET /internal/v1/company-reports": ScopeMode.COLLECTION,
}

CANDIDATE_ACTION_CAPABILITIES: Mapping[CandidateAction, Capability] = {
    CandidateAction.COMPLETE_FIELDS: Capability.CANDIDATE_DECIDE,
    CandidateAction.RESOLVE_CONFLICT: Capability.CANDIDATE_DECIDE,
    CandidateAction.CORRECT_AND_CONFIRM: Capability.CANDIDATE_DECIDE,
    CandidateAction.CONFIRM: Capability.CANDIDATE_DECIDE,
    CandidateAction.IGNORE: Capability.CANDIDATE_DECIDE,
    CandidateAction.SUPERSEDE: Capability.CANDIDATE_SUPERSEDE,
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ReadMoneyMinor = Annotated[
    int,
    Field(strict=True, ge=-JSON_SAFE_INTEGER, le=JSON_SAFE_INTEGER),
]
BusinessUnitRef = Annotated[str, Field(min_length=1, max_length=100)]


class CapabilitiesResponse(_FrozenModel):
    contract_version: Literal["ledgerbridge.internal-read.v1"] = "ledgerbridge.internal-read.v1"
    candidate_contract_version: Literal["ledgerbridge.candidate.v1"] = "ledgerbridge.candidate.v1"
    state_graph_version: Literal["ledgerbridge.candidate-state.v1"] = (
        "ledgerbridge.candidate-state.v1"
    )
    data_mode: Literal["synthetic", "database"] = "synthetic"
    enabled_modules: tuple[
        Literal[
            "accounting-dimensions",
            "candidates",
            "evidence",
            "reconciliations",
            "ledger-summary",
            "company-reporting",
        ],
        ...,
    ]


class CandidatePage(_FrozenModel):
    items: tuple[CandidateProjection, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)


class BusinessUnitDimension(_FrozenModel):
    ref: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class ReportingCategoryDimension(_FrozenModel):
    code: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)


class AccountingDimensions(_FrozenModel):
    contract_version: Literal["ledgerbridge.accounting-dimensions.v1"] = (
        "ledgerbridge.accounting-dimensions.v1"
    )
    entity_ref: UUID
    business_units: tuple[BusinessUnitDimension, ...] = Field(max_length=1_000)
    categories: tuple[ReportingCategoryDimension, ...] = Field(max_length=1_000)

    @model_validator(mode="after")
    def dimensions_are_unique_and_stably_sorted(self) -> AccountingDimensions:
        business_unit_refs = [item.ref for item in self.business_units]
        category_codes = [item.code for item in self.categories]
        business_unit_labels = [item.label for item in self.business_units]
        category_labels = [item.label for item in self.categories]
        if business_unit_refs != sorted(set(business_unit_refs)):
            raise ValueError("business unit dimensions must be unique and sorted")
        if category_codes != sorted(set(category_codes)):
            raise ValueError("reporting category dimensions must be unique and sorted")
        if len(business_unit_labels) != len(set(business_unit_labels)):
            raise ValueError("active business unit labels must be unique within an entity")
        if len(category_labels) != len(set(category_labels)):
            raise ValueError("active reporting category labels must be unique within an entity")
        return self


class ReconciliationProposal(_FrozenModel):
    proposal_ref: UUID
    relation: Literal["1:1", "1:N", "N:1"]
    status: Literal["PROPOSED", "CONFIRMED", "REJECTED"]
    amount_minor: ReadMoneyMinor
    currency: Literal["CNY"] = "CNY"


class SuspenseProjection(_FrozenModel):
    suspense_ref: UUID
    status: Literal["OPEN", "RESOLVED"]
    reason: Literal[
        "UNKNOWN_COUNTERPARTY",
        "UNMATCHED_TRANSFER",
        "BALANCE_GAP",
        "LOAN_BREAKDOWN",
    ]
    amount_minor: ReadMoneyMinor
    currency: Literal["CNY"] = "CNY"


class ReconciliationProjection(_FrozenModel):
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    snapshot_revision: int = Field(ge=1)
    blockers: tuple[Blocker, ...] = ()
    proposals: tuple[ReconciliationProposal, ...] = ()
    suspense: tuple[SuspenseProjection, ...] = ()
    posted_amount_minor: ReadMoneyMinor
    currency: Literal["CNY"] = "CNY"


class LedgerSummary(_FrozenModel):
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    from_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    to_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    posting_status: Literal["POSTED"] = "POSTED"
    currency: Literal["CNY"] = "CNY"
    totals_minor: dict[str, ReadMoneyMinor]

    @model_validator(mode="after")
    def validate_month_order(self) -> LedgerSummary:
        if self.from_month > self.to_month:
            raise ValueError("from_month must be less than or equal to to_month")
        return self


class EntityGrant(_FrozenModel):
    entity_ref: UUID
    business_unit_refs: frozenset[BusinessUnitRef] = frozenset()
    # Database-backed readers need the immutable UUID used by the scoped SQL
    # functions.  The human-readable refs remain the HTTP authorization key;
    # keeping both prevents the database layer from resolving policy through a
    # broad business_unit lookup.
    business_unit_ids: frozenset[UUID] = frozenset()
    # Database readers require an explicit, immutable ref-to-UUID binding;
    # two independent sets cannot prove that a human ref maps to the UUID
    # queried by a SECURITY DEFINER function.
    business_unit_bindings: tuple[tuple[BusinessUnitRef, UUID], ...] = ()
    allow_unassigned_candidates: bool = False
    allow_account_registry: bool = False

    @model_validator(mode="after")
    def has_at_least_one_scope(self) -> EntityGrant:
        if (
            not self.business_unit_refs
            and not self.business_unit_ids
            and not self.allow_unassigned_candidates
            and not self.allow_account_registry
        ):
            raise ValueError("entity grant must include a business unit or unassigned candidates")
        binding_refs = frozenset(ref for ref, _ in self.business_unit_bindings)
        binding_ids = frozenset(value for _, value in self.business_unit_bindings)
        if self.business_unit_bindings and (
            binding_refs != self.business_unit_refs or binding_ids != self.business_unit_ids
        ):
            raise ValueError("business-unit bindings must cover refs and immutable IDs exactly")
        return self


class WorkloadPrincipal(_FrozenModel):
    principal_ref: str = Field(min_length=1, max_length=200)
    san_uri: str = Field(pattern=r"^spiffe://ledgerbridge\.(?:test|local)/[a-z0-9/_-]+$")
    policy_generation: int = Field(ge=1)
    capabilities: frozenset[Capability]
    grants: tuple[EntityGrant, ...] = ()


class SyntheticPeerEvidence(_FrozenModel):
    """Synthetic verifier output; never a certificate or a production credential."""

    san_uri: str
    chain_verified: bool
    within_validity: bool
    client_auth_eku: bool
    revoked: bool
    policy_generation: int = Field(ge=1)


class AuthenticationDenied(RuntimeError):
    """The synthetic peer did not satisfy every mTLS identity predicate."""


class AuthorizationDenied(RuntimeError):
    """The principal lacks the required capability."""


class ResourceNotVisible(RuntimeError):
    """An object is absent or outside entity/business-unit scope (HTTP 404)."""


def resolve_synthetic_peer(
    peer: SyntheticPeerEvidence,
    *,
    policy: Mapping[str, WorkloadPrincipal],
    current_policy_generation: int,
) -> WorkloadPrincipal:
    """Resolve an already transport-verified synthetic peer through fixed SAN policy."""

    if not (
        peer.chain_verified
        and peer.within_validity
        and peer.client_auth_eku
        and not peer.revoked
        and peer.policy_generation == current_policy_generation
    ):
        raise AuthenticationDenied("synthetic mTLS peer failed closed")
    principal = policy.get(peer.san_uri)
    if (
        principal is None
        or principal.san_uri != peer.san_uri
        or principal.policy_generation != current_policy_generation
    ):
        raise AuthenticationDenied("synthetic mTLS SAN is not mapped by current policy")
    return principal


def require_capability(principal: WorkloadPrincipal, capability: Capability) -> None:
    if capability not in principal.capabilities:
        raise AuthorizationDenied("required capability is not granted")


def require_visible_scope(
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
    business_unit_ref: str,
) -> None:
    if not any(
        grant.entity_ref == entity_ref and business_unit_ref in grant.business_unit_refs
        for grant in principal.grants
    ):
        raise ResourceNotVisible("resource was not found")


def require_candidate_visible_scope(
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
    business_unit_ref: str | None,
) -> None:
    """Require candidate visibility without inferring an unassigned-unit grant.

    A normal business-unit grant covers only the units named by that grant. A
    candidate whose business unit is still unknown is visible only through the
    separate, explicit ``allow_unassigned_candidates`` permission for its entity.
    """

    if business_unit_ref is not None:
        require_visible_scope(
            principal,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        return
    if not any(
        grant.entity_ref == entity_ref and grant.allow_unassigned_candidates
        for grant in principal.grants
    ):
        raise ResourceNotVisible("resource was not found")


def authorize_candidate_read(
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
    business_unit_ref: str | None,
) -> None:
    """Authorize one candidate, including an explicitly granted unassigned candidate."""

    require_capability(principal, Capability.CANDIDATE_READ)
    require_candidate_visible_scope(
        principal,
        entity_ref=entity_ref,
        business_unit_ref=business_unit_ref,
    )


def authorize_read(
    principal: WorkloadPrincipal,
    capability: Capability,
    *,
    entity_ref: UUID | None = None,
    business_unit_ref: str | None = None,
) -> None:
    require_capability(principal, capability)
    if capability == Capability.SYSTEM_READ:
        if entity_ref is not None or business_unit_ref is not None:
            raise ResourceNotVisible("resource was not found")
        return
    if entity_ref is None or business_unit_ref is None:
        if capability == Capability.CANDIDATE_READ and entity_ref is not None:
            authorize_candidate_read(
                principal,
                entity_ref=entity_ref,
                business_unit_ref=business_unit_ref,
            )
            return
        raise ResourceNotVisible("resource was not found")
    require_visible_scope(
        principal,
        entity_ref=entity_ref,
        business_unit_ref=business_unit_ref,
    )


def authorize_collection_read(
    principal: WorkloadPrincipal,
    capability: Capability,
) -> None:
    """Authorize a collection whose query must union only the principal's grants."""

    if capability not in {Capability.CANDIDATE_READ, Capability.LEDGER_READ}:
        raise AuthorizationDenied("capability has no collection read contract")
    require_capability(principal, capability)
    if not principal.grants:
        raise ResourceNotVisible("resource was not found")


def require_candidate_workload_scope(
    principal: WorkloadPrincipal,
    action: CandidateAction,
    *,
    entity_ref: UUID,
    business_unit_ref: str,
) -> None:
    """Check only the workload half of a future D1 command authorization.

    This function is deliberately not a complete authorization decision. D1 must
    additionally verify a fresh, method/path/body-bound human assertion before any
    command route may exist.
    """

    require_capability(principal, CANDIDATE_ACTION_CAPABILITIES[action])
    require_visible_scope(
        principal,
        entity_ref=entity_ref,
        business_unit_ref=business_unit_ref,
    )


def filter_visible_scopes(
    principal: WorkloadPrincipal,
    values: Iterable[tuple[UUID, str | None]],
) -> tuple[tuple[UUID, str | None], ...]:
    """Model candidate query predicates applied before object materialization."""

    allowed: set[tuple[UUID, str | None]] = {
        (grant.entity_ref, unit) for grant in principal.grants for unit in grant.business_unit_refs
    }
    allowed.update(
        (grant.entity_ref, None) for grant in principal.grants if grant.allow_unassigned_candidates
    )
    return tuple(value for value in values if value in allowed)
