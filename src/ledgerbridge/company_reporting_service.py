"""Database-backed, allowlist-scoped company reporting reader."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.company_reporting_composition_contract import (
    CompanyReportCompositionItem,
    CompanyReportCompositionPage,
)
from ledgerbridge.company_reporting_contract import (
    MAX_REPORT_BUSINESS_UNITS,
    MAX_REPORT_COMPANIES,
    CompanyReportBasis,
    CompanyReportItem,
    CompanyReportPage,
    validate_report_month_range,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable

_COMPOSITION_ITEM_ADAPTER: TypeAdapter[CompanyReportCompositionItem] = TypeAdapter(
    CompanyReportCompositionItem
)


class DatabaseCompanyReportingService:
    """Read one basis at one immutable audit horizon for the principal's grants."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def report(
        self,
        principal: WorkloadPrincipal,
        *,
        basis: CompanyReportBasis,
        from_month: str,
        to_month: str,
        company_ref: UUID | None = None,
    ) -> CompanyReportPage:
        require_capability(principal, Capability.LEDGER_READ)
        validate_report_month_range(from_month, to_month)
        try:
            selected_basis = CompanyReportBasis(basis)
        except (TypeError, ValueError) as exc:
            raise ValueError("company report basis is invalid") from exc

        grants = self._validated_grants(principal.grants)
        if company_ref is not None:
            grants = tuple(grant for grant in grants if grant.entity_ref == company_ref)
            if not grants:
                raise ResourceNotVisible("resource was not found")
        elif not grants:
            raise ResourceNotVisible("resource was not found")

        start = date.fromisoformat(f"{from_month}-01")
        end = date.fromisoformat(f"{to_month}-01")
        items: list[CompanyReportItem] = []
        try:
            with self._session_factory() as session:
                audit_sequence, audit_hash = self._audit_horizon(session)
                for grant in grants:
                    rows = (
                        session.execute(
                            text(
                                "SELECT report FROM "
                                "company_reporting_read.get_company_report_v1_as_of("
                                ":entity_ref, :business_unit_ids, :include_unassigned, "
                                ":basis, :from_month, :to_month, :audit_sequence, :audit_hash)"
                            ),
                            {
                                "entity_ref": grant.entity_ref,
                                "business_unit_ids": sorted(
                                    grant.business_unit_ids,
                                    key=lambda value: value.int,
                                ),
                                "include_unassigned": grant.allow_unassigned_candidates,
                                "basis": selected_basis.value,
                                "from_month": start,
                                "to_month": end,
                                "audit_sequence": audit_sequence,
                                "audit_hash": audit_hash,
                            },
                        )
                        .mappings()
                        .all()
                    )
                    if not rows:
                        if company_ref is None:
                            # Entity grants may include people as well as companies. The
                            # SECURITY DEFINER reader deliberately returns no row for a
                            # non-company entity, so an unfiltered collection omits it.
                            continue
                        raise ResourceNotVisible("resource was not found")
                    if len(rows) != 1:
                        raise InternalReadBackendUnavailable(
                            "company report projection returned an invalid row count"
                        )
                    items.append(self._validated_item(rows[0], grant, selected_basis))
        except (ResourceNotVisible, InternalReadBackendUnavailable):
            raise
        except SQLAlchemyError as exc:
            raise InternalReadBackendUnavailable(
                "company report projection is unavailable"
            ) from exc

        try:
            return CompanyReportPage(
                basis=selected_basis,
                from_month=from_month,
                to_month=to_month,
                items=tuple(items),
            )
        except ValidationError as exc:
            raise InternalReadBackendUnavailable(
                "company report page failed contract validation"
            ) from exc

    def composition(
        self,
        principal: WorkloadPrincipal,
        *,
        basis: CompanyReportBasis,
        from_month: str,
        to_month: str,
        company_ref: UUID | None = None,
    ) -> CompanyReportCompositionPage:
        require_capability(principal, Capability.LEDGER_READ)
        validate_report_month_range(from_month, to_month)
        try:
            selected_basis = CompanyReportBasis(basis)
        except (TypeError, ValueError) as exc:
            raise ValueError("company report composition basis is invalid") from exc
        if selected_basis is CompanyReportBasis.ACCOUNT_STATEMENT:
            raise ValueError("account statements do not define income or expense categories")

        grants = self._validated_grants(principal.grants)
        if company_ref is not None:
            grants = tuple(grant for grant in grants if grant.entity_ref == company_ref)
            if not grants:
                raise ResourceNotVisible("resource was not found")
        elif not grants:
            raise ResourceNotVisible("resource was not found")

        start = date.fromisoformat(f"{from_month}-01")
        end = date.fromisoformat(f"{to_month}-01")
        items: list[CompanyReportCompositionItem] = []
        try:
            with self._session_factory() as session:
                audit_sequence, audit_hash = self._audit_horizon(session)
                for grant in grants:
                    rows = (
                        session.execute(
                            text(
                                "SELECT composition FROM "
                                "company_reporting_read."
                                "get_company_report_composition_v1_as_of("
                                ":entity_ref, :business_unit_ids, :include_unassigned, "
                                ":basis, :from_month, :to_month, :audit_sequence, :audit_hash)"
                            ),
                            {
                                "entity_ref": grant.entity_ref,
                                "business_unit_ids": sorted(
                                    grant.business_unit_ids,
                                    key=lambda value: value.int,
                                ),
                                "include_unassigned": grant.allow_unassigned_candidates,
                                "basis": selected_basis.value,
                                "from_month": start,
                                "to_month": end,
                                "audit_sequence": audit_sequence,
                                "audit_hash": audit_hash,
                            },
                        )
                        .mappings()
                        .all()
                    )
                    if not rows:
                        if company_ref is None:
                            continue
                        raise ResourceNotVisible("resource was not found")
                    if len(rows) != 1:
                        raise InternalReadBackendUnavailable(
                            "company report composition returned an invalid row count"
                        )
                    items.append(self._validated_composition_item(rows[0], grant, selected_basis))
        except (ResourceNotVisible, InternalReadBackendUnavailable):
            raise
        except SQLAlchemyError as exc:
            raise InternalReadBackendUnavailable(
                "company report composition is unavailable"
            ) from exc

        try:
            return CompanyReportCompositionPage(
                basis=selected_basis,
                from_month=from_month,
                to_month=to_month,
                items=tuple(items),
            )
        except ValidationError as exc:
            raise InternalReadBackendUnavailable(
                "company report composition page failed contract validation"
            ) from exc

    @staticmethod
    def _validated_grants(grants: Sequence[EntityGrant]) -> tuple[EntityGrant, ...]:
        if len(grants) > MAX_REPORT_COMPANIES:
            raise InternalReadBackendUnavailable("company report grant limit exceeded")

        entity_refs = [grant.entity_ref for grant in grants]
        if len(entity_refs) != len(set(entity_refs)):
            raise InternalReadBackendUnavailable("company report grants duplicate an entity")

        for grant in grants:
            if len(grant.business_unit_ids) > MAX_REPORT_BUSINESS_UNITS:
                raise InternalReadBackendUnavailable("company report unit limit exceeded")
            if grant.business_unit_refs or grant.business_unit_ids:
                bindings = grant.business_unit_bindings
                if (
                    not bindings
                    or len(bindings) != len(set(bindings))
                    or frozenset(ref for ref, _ in bindings) != grant.business_unit_refs
                    or frozenset(value for _, value in bindings) != grant.business_unit_ids
                ):
                    raise InternalReadBackendUnavailable(
                        "company report grants require immutable unit bindings"
                    )
            elif not grant.allow_unassigned_candidates:
                raise InternalReadBackendUnavailable("company report grant has no scope")

        return tuple(sorted(grants, key=lambda grant: grant.entity_ref.int))

    @staticmethod
    def _audit_horizon(session: Session) -> tuple[int, bytes]:
        row = (
            session.execute(
                text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
            )
            .mappings()
            .first()
        )
        if row is None:
            raise InternalReadBackendUnavailable("database audit horizon is unavailable")
        sequence = row.get("sequence")
        horizon_hash = row.get("hash")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(horizon_hash, (bytes, bytearray))
            or len(horizon_hash) != 32
        ):
            raise InternalReadBackendUnavailable("database audit horizon is malformed")
        return sequence, bytes(horizon_hash)

    @staticmethod
    def _validated_item(
        row: Mapping[str, Any] | RowMapping,
        grant: EntityGrant,
        basis: CompanyReportBasis,
    ) -> CompanyReportItem:
        report = row.get("report")
        if not isinstance(report, Mapping):
            raise InternalReadBackendUnavailable("company report payload is malformed")
        try:
            item = CompanyReportItem.model_validate(report)
        except ValidationError as exc:
            raise InternalReadBackendUnavailable(
                "company report payload failed contract validation"
            ) from exc
        if item.company_ref != grant.entity_ref or item.metrics.basis is not basis:
            raise InternalReadBackendUnavailable("company report escaped its authorized scope")
        granted_refs = grant.business_unit_refs
        for month in item.months:
            if month.business_units is None:
                continue
            if any(
                business_unit.business_unit_ref not in granted_refs
                for business_unit in month.business_units
            ):
                raise InternalReadBackendUnavailable(
                    "company report business unit escaped its authorized scope"
                )
        return item

    @staticmethod
    def _validated_composition_item(
        row: Mapping[str, Any] | RowMapping,
        grant: EntityGrant,
        basis: CompanyReportBasis,
    ) -> CompanyReportCompositionItem:
        composition = row.get("composition")
        if not isinstance(composition, Mapping):
            raise InternalReadBackendUnavailable("company report composition payload is malformed")
        try:
            item = _COMPOSITION_ITEM_ADAPTER.validate_python(composition)
        except ValidationError as exc:
            raise InternalReadBackendUnavailable(
                "company report composition payload failed contract validation"
            ) from exc
        if item.company_ref != grant.entity_ref or item.basis is not basis:
            raise InternalReadBackendUnavailable(
                "company report composition escaped its authorized scope"
            )
        return item
