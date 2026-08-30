"""Controlled Accounting Owner to Managed Account registry contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.models import EntityType
from ledgerbridge.text import contains_unstorable_text

ACCOUNT_REGISTRY_CONTRACT_VERSION: Final = "ledgerbridge.account-registry.v1"

_SAFE_REF: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_INSTITUTION_CODE: Final = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
_ACCOUNT_SUFFIX: Final = re.compile(r"^[0-9]{4,8}$")
_UPPER_CODE: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")


class AccountRegistryError(ValueError):
    """A registry value or controlled operation violated the contract."""


class AccountRegistryPersistenceError(RuntimeError):
    """A controlled registry operation failed at the persistence seam."""


@dataclass(frozen=True, slots=True)
class AccountAliasRegistration:
    alias_ref: UUID
    alias_kind: str
    alias_value: str

    def __post_init__(self) -> None:
        if _UPPER_CODE.fullmatch(self.alias_kind) is None:
            raise AccountRegistryError("account alias kind is invalid")
        _require_text("account alias value", self.alias_value, 300)

    def to_request(self) -> dict[str, object]:
        return {
            "alias_ref": str(self.alias_ref),
            "alias_kind": self.alias_kind,
            "alias_value": self.alias_value,
        }


@dataclass(frozen=True, slots=True)
class ManagedAccountRegistration:
    managed_account_ref: UUID
    admission_evidence_ref: UUID
    account_key: str
    institution_code: str
    account_suffix: str
    account_kind: str
    aliases: tuple[AccountAliasRegistration, ...]

    def __post_init__(self) -> None:
        if _SAFE_REF.fullmatch(self.account_key) is None:
            raise AccountRegistryError("managed account key is invalid")
        if _INSTITUTION_CODE.fullmatch(self.institution_code) is None:
            raise AccountRegistryError("institution code is invalid")
        if _ACCOUNT_SUFFIX.fullmatch(self.account_suffix) is None:
            raise AccountRegistryError("account suffix is invalid")
        if _UPPER_CODE.fullmatch(self.account_kind) is None:
            raise AccountRegistryError("account kind is invalid")
        if not self.aliases:
            raise AccountRegistryError("managed account requires an explicit alias")
        if len({alias.alias_ref for alias in self.aliases}) != len(self.aliases):
            raise AccountRegistryError("account alias refs must be unique")
        normalized = {
            (alias.alias_kind, _normalize_alias(alias.alias_value)) for alias in self.aliases
        }
        if len(normalized) != len(self.aliases):
            raise AccountRegistryError("account aliases must be unique")

    def to_request(self) -> dict[str, object]:
        return {
            "managed_account_ref": str(self.managed_account_ref),
            "admission_evidence_ref": str(self.admission_evidence_ref),
            "account_key": self.account_key,
            "institution_code": self.institution_code,
            "account_suffix": self.account_suffix,
            "account_kind": self.account_kind,
            "aliases": [alias.to_request() for alias in self.aliases],
        }


@dataclass(frozen=True, slots=True)
class AccountBusinessUnitAssignment:
    assignment_ref: UUID
    managed_account_ref: UUID
    business_unit_id: UUID
    business_unit_ref_snapshot: str
    business_unit_label_snapshot: str
    effective_from: date
    effective_to: date | None = None

    def __post_init__(self) -> None:
        _require_text("business-unit ref snapshot", self.business_unit_ref_snapshot, 100)
        _require_text("business-unit label snapshot", self.business_unit_label_snapshot, 200)
        if (
            not isinstance(self.effective_from, date)
            or isinstance(self.effective_from, datetime)
            or (
                self.effective_to is not None
                and (
                    not isinstance(self.effective_to, date)
                    or isinstance(self.effective_to, datetime)
                    or self.effective_to <= self.effective_from
                )
            )
        ):
            raise AccountRegistryError("business-unit assignment dates are invalid")

    def to_request(self) -> dict[str, object]:
        return {
            "assignment_ref": str(self.assignment_ref),
            "managed_account_ref": str(self.managed_account_ref),
            "business_unit_id": str(self.business_unit_id),
            "business_unit_ref_snapshot": self.business_unit_ref_snapshot,
            "business_unit_label_snapshot": self.business_unit_label_snapshot,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
        }


@dataclass(frozen=True, slots=True)
class FactAllocationItem:
    business_unit_id: UUID
    business_unit_ref_snapshot: str
    business_unit_label_snapshot: str
    basis_points: int

    def __post_init__(self) -> None:
        _require_text("business-unit ref snapshot", self.business_unit_ref_snapshot, 100)
        _require_text("business-unit label snapshot", self.business_unit_label_snapshot, 200)
        if (
            isinstance(self.basis_points, bool)
            or not isinstance(self.basis_points, int)
            or not 1 <= self.basis_points <= 10_000
        ):
            raise AccountRegistryError("fact allocation basis points are invalid")

    def to_request(self) -> dict[str, object]:
        return {
            "business_unit_id": str(self.business_unit_id),
            "business_unit_ref_snapshot": self.business_unit_ref_snapshot,
            "business_unit_label_snapshot": self.business_unit_label_snapshot,
            "basis_points": self.basis_points,
        }


@dataclass(frozen=True, slots=True)
class FactAllocationRegistration:
    allocation_set_ref: UUID
    managed_account_ref: UUID
    fact_ref: UUID
    items: tuple[FactAllocationItem, ...]

    def __post_init__(self) -> None:
        if not self.items or sum(item.basis_points for item in self.items) != 10_000:
            raise AccountRegistryError("fact allocation must total 10000 basis points")
        if len({item.business_unit_id for item in self.items}) != len(self.items):
            raise AccountRegistryError("fact allocation business units must be unique")

    def to_request(self) -> dict[str, object]:
        return {
            "allocation_set_ref": str(self.allocation_set_ref),
            "managed_account_ref": str(self.managed_account_ref),
            "fact_ref": str(self.fact_ref),
            "items": [item.to_request() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class AccountRegistryPlan:
    operation_id: UUID
    owner_entity_ref: UUID
    expected_owner_kind: EntityType
    expected_registry_revision: int
    actor_ref: str
    reason: str
    accounts: tuple[ManagedAccountRegistration, ...] = ()
    business_unit_assignments: tuple[AccountBusinessUnitAssignment, ...] = ()
    fact_allocations: tuple[FactAllocationRegistration, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.expected_owner_kind, EntityType):
            raise AccountRegistryError("expected owner kind is invalid")
        if (
            isinstance(self.expected_registry_revision, bool)
            or not isinstance(self.expected_registry_revision, int)
            or self.expected_registry_revision < 0
        ):
            raise AccountRegistryError("expected registry revision is invalid")
        _require_text("actor ref", self.actor_ref, 200)
        _require_text("reason", self.reason, 1_000)
        if not (self.accounts or self.business_unit_assignments or self.fact_allocations):
            raise AccountRegistryError("account registry plan has no actions")
        if len({account.managed_account_ref for account in self.accounts}) != len(self.accounts):
            raise AccountRegistryError("managed account refs must be unique")
        if len({account.account_key for account in self.accounts}) != len(self.accounts):
            raise AccountRegistryError("managed account keys must be unique")

    def to_request(self) -> dict[str, Any]:
        return {
            "contract_version": ACCOUNT_REGISTRY_CONTRACT_VERSION,
            "operation_id": str(self.operation_id),
            "owner_entity_ref": str(self.owner_entity_ref),
            "expected_owner_kind": self.expected_owner_kind.value,
            "expected_registry_revision": self.expected_registry_revision,
            "actor_ref": self.actor_ref,
            "reason": self.reason,
            "accounts": [account.to_request() for account in self.accounts],
            "business_unit_assignments": [
                assignment.to_request() for assignment in self.business_unit_assignments
            ],
            "fact_allocations": [allocation.to_request() for allocation in self.fact_allocations],
        }


@dataclass(frozen=True, slots=True)
class AccountRegistryPlanResult:
    operation_id: UUID
    owner_entity_ref: UUID
    registry_revision: int
    created: bool
    managed_account_refs: tuple[UUID, ...]


class AccountRegistryOperator:
    """Apply an explicit, owner-scoped registry plan through the command seam."""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def apply(
        self,
        plan: AccountRegistryPlan,
        *,
        principal: WorkloadPrincipal,
        session: Session | None = None,
    ) -> AccountRegistryPlanResult:
        _authorize_registry_owner(
            principal,
            owner_entity_ref=plan.owner_entity_ref,
            capability=Capability.ACCOUNT_REGISTRY_WRITE,
        )
        request = plan.to_request()
        request["workload_principal_ref"] = principal.principal_ref
        request["policy_generation"] = principal.policy_generation
        try:
            if session is not None:
                return self._execute_plan(session, request)
            with self._sessions() as session:
                result = self._execute_plan(session, request)
                session.commit()
                return result
        except AccountRegistryPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise AccountRegistryPersistenceError("account registry plan failed") from exc

    def _execute_plan(self, session: Session, request: dict[str, Any]) -> AccountRegistryPlanResult:
        raw = session.execute(
            text("SELECT internal_command.apply_account_registry_plan(CAST(:request AS jsonb))"),
            {
                "request": json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        ).scalar_one()
        return _parse_plan_result(raw)


@dataclass(frozen=True, slots=True)
class AccountAliasProjection:
    alias_ref: UUID
    alias_kind: str
    masked_value: str


@dataclass(frozen=True, slots=True)
class AccountBusinessUnitAssignmentProjection:
    assignment_ref: UUID
    business_unit_id: UUID
    business_unit_ref_snapshot: str
    business_unit_label_snapshot: str
    effective_from: date
    effective_to: date | None


@dataclass(frozen=True, slots=True)
class FactAllocationItemProjection:
    business_unit_id: UUID
    business_unit_ref_snapshot: str
    business_unit_label_snapshot: str
    basis_points: int


@dataclass(frozen=True, slots=True)
class FactAllocationProjection:
    allocation_set_ref: UUID
    fact_ref: UUID
    revision: int
    items: tuple[FactAllocationItemProjection, ...]


@dataclass(frozen=True, slots=True)
class ManagedAccountProjection:
    managed_account_ref: UUID
    admission_evidence_ref: UUID
    account_key: str
    institution_code: str
    account_suffix: str
    account_kind: str
    aliases: tuple[AccountAliasProjection, ...]
    business_unit_assignments: tuple[AccountBusinessUnitAssignmentProjection, ...]
    fact_allocations: tuple[FactAllocationProjection, ...]


@dataclass(frozen=True, slots=True)
class AccountRegistryProjection:
    contract_version: str
    owner_entity_ref: UUID
    owner_kind: EntityType
    registry_revision: int
    accounts: tuple[ManagedAccountProjection, ...]


class AccountRegistryReader:
    """Read an audit-horizon-bound owner registry projection."""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def get_owner_registry(
        self,
        owner_entity_ref: UUID,
        *,
        principal: WorkloadPrincipal,
        audit_horizon_sequence: int,
        audit_horizon_hash: bytes,
    ) -> AccountRegistryProjection:
        _authorize_registry_owner(
            principal,
            owner_entity_ref=owner_entity_ref,
            capability=Capability.ACCOUNT_REGISTRY_READ,
        )
        if (
            isinstance(audit_horizon_sequence, bool)
            or not isinstance(audit_horizon_sequence, int)
            or audit_horizon_sequence < 1
            or not isinstance(audit_horizon_hash, bytes)
            or len(audit_horizon_hash) != 32
        ):
            raise AccountRegistryError("account registry audit horizon is invalid")
        try:
            with self._sessions() as session:
                raw = session.execute(
                    text(
                        "SELECT internal_read.get_account_registry_projection("
                        ":owner_entity_ref, :horizon_sequence, :horizon_hash)"
                    ),
                    {
                        "owner_entity_ref": owner_entity_ref,
                        "horizon_sequence": audit_horizon_sequence,
                        "horizon_hash": audit_horizon_hash,
                    },
                ).scalar_one()
                return _parse_projection(raw)
        except AccountRegistryPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise AccountRegistryPersistenceError("account registry read failed") from exc


def _authorize_registry_owner(
    principal: WorkloadPrincipal,
    *,
    owner_entity_ref: UUID,
    capability: Capability,
) -> None:
    require_capability(principal, capability)
    if not any(
        grant.entity_ref == owner_entity_ref and grant.allow_account_registry
        for grant in principal.grants
    ):
        raise ResourceNotVisible("resource was not found")


def _parse_projection(raw: object) -> AccountRegistryProjection:
    if not isinstance(raw, Mapping):
        raise AccountRegistryPersistenceError("account registry projection is invalid")
    try:
        contract_version = _required_string(raw, "contract_version")
        owner_entity_ref = _required_uuid(raw, "owner_entity_ref")
        owner_kind = EntityType(_required_string(raw, "owner_kind"))
        registry_revision = _required_nonnegative_int(raw, "registry_revision")
        accounts = tuple(
            _parse_account_projection(item) for item in _required_mapping_list(raw, "accounts")
        )
    except (AccountRegistryPersistenceError, ValueError, TypeError) as exc:
        raise AccountRegistryPersistenceError("account registry projection is invalid") from exc
    if contract_version != ACCOUNT_REGISTRY_CONTRACT_VERSION:
        raise AccountRegistryPersistenceError("account registry projection version is unsupported")
    return AccountRegistryProjection(
        contract_version=contract_version,
        owner_entity_ref=owner_entity_ref,
        owner_kind=owner_kind,
        registry_revision=registry_revision,
        accounts=accounts,
    )


def _parse_account_projection(raw: Mapping[object, object]) -> ManagedAccountProjection:
    return ManagedAccountProjection(
        managed_account_ref=_required_uuid(raw, "managed_account_ref"),
        admission_evidence_ref=_required_uuid(raw, "admission_evidence_ref"),
        account_key=_required_string(raw, "account_key"),
        institution_code=_required_string(raw, "institution_code"),
        account_suffix=_required_string(raw, "account_suffix"),
        account_kind=_required_string(raw, "account_kind"),
        aliases=tuple(
            AccountAliasProjection(
                alias_ref=_required_uuid(item, "alias_ref"),
                alias_kind=_required_string(item, "alias_kind"),
                masked_value=_required_string(item, "masked_value"),
            )
            for item in _required_mapping_list(raw, "aliases")
        ),
        business_unit_assignments=tuple(
            _parse_assignment_projection(item)
            for item in _required_mapping_list(raw, "business_unit_assignments")
        ),
        fact_allocations=tuple(
            _parse_fact_allocation_projection(item)
            for item in _required_mapping_list(raw, "fact_allocations")
        ),
    )


def _parse_assignment_projection(
    raw: Mapping[object, object],
) -> AccountBusinessUnitAssignmentProjection:
    effective_to_raw = raw.get("effective_to")
    effective_to = None
    if effective_to_raw is not None:
        if not isinstance(effective_to_raw, str):
            raise AccountRegistryPersistenceError("account registry projection is invalid")
        effective_to = date.fromisoformat(effective_to_raw)
    return AccountBusinessUnitAssignmentProjection(
        assignment_ref=_required_uuid(raw, "assignment_ref"),
        business_unit_id=_required_uuid(raw, "business_unit_id"),
        business_unit_ref_snapshot=_required_string(raw, "business_unit_ref_snapshot"),
        business_unit_label_snapshot=_required_string(raw, "business_unit_label_snapshot"),
        effective_from=date.fromisoformat(_required_string(raw, "effective_from")),
        effective_to=effective_to,
    )


def _parse_fact_allocation_projection(
    raw: Mapping[object, object],
) -> FactAllocationProjection:
    items = tuple(
        FactAllocationItemProjection(
            business_unit_id=_required_uuid(item, "business_unit_id"),
            business_unit_ref_snapshot=_required_string(item, "business_unit_ref_snapshot"),
            business_unit_label_snapshot=_required_string(item, "business_unit_label_snapshot"),
            basis_points=_required_positive_int(item, "basis_points"),
        )
        for item in _required_mapping_list(raw, "items")
    )
    if not items or sum(item.basis_points for item in items) != 10_000:
        raise AccountRegistryPersistenceError("account registry allocation is invalid")
    return FactAllocationProjection(
        allocation_set_ref=_required_uuid(raw, "allocation_set_ref"),
        fact_ref=_required_uuid(raw, "fact_ref"),
        revision=_required_positive_int(raw, "revision"),
        items=items,
    )


def _parse_plan_result(raw: object) -> AccountRegistryPlanResult:
    if not isinstance(raw, Mapping):
        raise AccountRegistryPersistenceError("account registry plan receipt is invalid")
    try:
        contract_version = raw["contract_version"]
        operation_id = UUID(cast(str, raw["operation_id"]))
        owner_entity_ref = UUID(cast(str, raw["owner_entity_ref"]))
        registry_revision = raw["registry_revision"]
        created = raw["created"]
        account_refs = raw["managed_account_refs"]
        managed_account_refs = tuple(UUID(cast(str, value)) for value in account_refs)
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountRegistryPersistenceError("account registry plan receipt is invalid") from exc
    if (
        contract_version != ACCOUNT_REGISTRY_CONTRACT_VERSION
        or isinstance(registry_revision, bool)
        or not isinstance(registry_revision, int)
        or registry_revision < 1
        or not isinstance(created, bool)
        or not isinstance(account_refs, list)
    ):
        raise AccountRegistryPersistenceError("account registry plan receipt is invalid")
    return AccountRegistryPlanResult(
        operation_id=operation_id,
        owner_entity_ref=owner_entity_ref,
        registry_revision=registry_revision,
        created=created,
        managed_account_refs=managed_account_refs,
    )


def _required_mapping_list(
    raw: Mapping[object, object], key: str
) -> tuple[Mapping[object, object], ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise AccountRegistryPersistenceError("account registry projection is invalid")
    return tuple(cast(Mapping[object, object], item) for item in value)


def _required_string(raw: Mapping[object, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise AccountRegistryPersistenceError("account registry projection is invalid")
    return value


def _required_uuid(raw: Mapping[object, object], key: str) -> UUID:
    return UUID(_required_string(raw, key))


def _required_positive_int(raw: Mapping[object, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AccountRegistryPersistenceError("account registry projection is invalid")
    return value


def _required_nonnegative_int(raw: Mapping[object, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AccountRegistryPersistenceError("account registry projection is invalid")
    return value


def _normalize_alias(value: str) -> str:
    return re.sub(r"[\s-]+", "", value).casefold()


def _require_text(label: str, value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise AccountRegistryError(f"{label} is invalid")
