from __future__ import annotations

import json
from datetime import date
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountBusinessUnitAssignment,
    AccountRegistryError,
    AccountRegistryOperator,
    AccountRegistryPlan,
    AccountRegistryReader,
    FactAllocationItem,
    FactAllocationRegistration,
    ManagedAccountRegistration,
)
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.models import EntityType

OWNER = UUID("83000000-0000-4000-8000-000000000001")
ACCOUNT = UUID("83000000-0000-4000-8000-000000000002")
EVIDENCE = UUID("83000000-0000-4000-8000-000000000003")
ALIAS = UUID("83000000-0000-4000-8000-000000000004")
OPERATION = UUID("83000000-0000-4000-8000-000000000005")
BUSINESS_UNIT_A = UUID("83000000-0000-4000-8000-000000000006")
BUSINESS_UNIT_B = UUID("83000000-0000-4000-8000-000000000007")
FACT = UUID("83000000-0000-4000-8000-000000000008")
ALLOCATION_SET = UUID("83000000-0000-4000-8000-000000000009")


def _plan(owner_kind: EntityType = EntityType.PERSON) -> AccountRegistryPlan:
    return AccountRegistryPlan(
        operation_id=OPERATION,
        owner_entity_ref=OWNER,
        expected_owner_kind=owner_kind,
        expected_registry_revision=0,
        actor_ref="operator:synthetic",
        reason="register a synthetic statement-backed account",
        accounts=(
            ManagedAccountRegistration(
                managed_account_ref=ACCOUNT,
                admission_evidence_ref=EVIDENCE,
                account_key="managed-account:synthetic-personal",
                institution_code="synthetic_bank",
                account_suffix="1234",
                account_kind="BANK_CHECKING",
                aliases=(
                    AccountAliasRegistration(
                        alias_ref=ALIAS,
                        alias_kind="ACCOUNT_NUMBER",
                        alias_value="0000 0000 0000 1234",
                    ),
                ),
            ),
        ),
    )


def test_registration_plan_requires_explicit_owner_account_and_statement_evidence() -> None:
    plan = _plan()

    request = plan.to_request()

    assert request["owner_entity_ref"] == str(OWNER)
    assert request["expected_owner_kind"] == "PERSON"
    assert request["accounts"][0]["managed_account_ref"] == str(ACCOUNT)
    assert request["accounts"][0]["admission_evidence_ref"] == str(EVIDENCE)


def test_registration_rejects_format_variants_of_the_same_account_alias() -> None:
    with pytest.raises(AccountRegistryError, match="aliases must be unique"):
        ManagedAccountRegistration(
            managed_account_ref=ACCOUNT,
            admission_evidence_ref=EVIDENCE,
            account_key="managed-account:synthetic-personal",
            institution_code="synthetic_bank",
            account_suffix="1234",
            account_kind="BANK_CHECKING",
            aliases=(
                AccountAliasRegistration(
                    alias_ref=ALIAS,
                    alias_kind="ACCOUNT_NUMBER",
                    alias_value="0000-0000-0000-1234",
                ),
                AccountAliasRegistration(
                    alias_ref=BUSINESS_UNIT_A,
                    alias_kind="ACCOUNT_NUMBER",
                    alias_value="0000 0000 0000 1234",
                ),
            ),
        )


class _Result:
    def scalar_one(self) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.account-registry.v1",
            "operation_id": str(OPERATION),
            "owner_entity_ref": str(OWNER),
            "registry_revision": 1,
            "created": True,
            "managed_account_refs": [str(ACCOUNT)],
        }


class _OperatorDatabase:
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None
        self.commits = 0

    def __enter__(self) -> _OperatorDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _Result:
        assert "internal_command.apply_account_registry_plan" in str(statement)
        self.request = json.loads(params["request"])
        return _Result()

    def commit(self) -> None:
        self.commits += 1


def test_operator_applies_plan_only_with_owner_scoped_registry_permission() -> None:
    database = _OperatorDatabase()
    principal = WorkloadPrincipal(
        principal_ref="workload:registry-operator",
        san_uri="spiffe://ledgerbridge.test/registry-operator",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
        grants=(EntityGrant(entity_ref=OWNER, allow_account_registry=True),),
    )

    result = AccountRegistryOperator(lambda: cast(Session, database)).apply(
        _plan(), principal=principal
    )

    assert result.owner_entity_ref == OWNER
    assert result.registry_revision == 1
    assert result.created is True
    assert result.managed_account_refs == (ACCOUNT,)
    assert database.request is not None
    assert database.request["workload_principal_ref"] == principal.principal_ref
    assert database.request["policy_generation"] == 1
    assert database.commits == 1


def test_operator_can_join_an_externally_owned_database_transaction() -> None:
    database = _OperatorDatabase()
    principal = WorkloadPrincipal(
        principal_ref="workload:registry-operator",
        san_uri="spiffe://ledgerbridge.test/registry-operator",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
        grants=(EntityGrant(entity_ref=OWNER, allow_account_registry=True),),
    )

    result = AccountRegistryOperator(lambda: cast(Session, database)).apply(
        _plan(), principal=principal, session=cast(Session, database)
    )

    assert result.created is True
    assert database.commits == 0


def test_company_account_can_remain_unassigned_while_one_fact_is_allocated_to_many_units() -> None:
    plan = AccountRegistryPlan(
        operation_id=OPERATION,
        owner_entity_ref=OWNER,
        expected_owner_kind=EntityType.COMPANY,
        expected_registry_revision=1,
        actor_ref="operator:synthetic",
        reason="allocate one synthetic fact without assigning the company account",
        fact_allocations=(
            FactAllocationRegistration(
                allocation_set_ref=ALLOCATION_SET,
                managed_account_ref=ACCOUNT,
                fact_ref=FACT,
                items=(
                    FactAllocationItem(
                        business_unit_id=BUSINESS_UNIT_A,
                        business_unit_ref_snapshot="store-a",
                        business_unit_label_snapshot="Synthetic Store A",
                        basis_points=6_000,
                    ),
                    FactAllocationItem(
                        business_unit_id=BUSINESS_UNIT_B,
                        business_unit_ref_snapshot="store-b",
                        business_unit_label_snapshot="Synthetic Store B",
                        basis_points=4_000,
                    ),
                ),
            ),
        ),
    )

    request = plan.to_request()

    assert request["business_unit_assignments"] == []
    assert request["fact_allocations"][0]["managed_account_ref"] == str(ACCOUNT)
    assert request["fact_allocations"][0]["fact_ref"] == str(FACT)
    assert [item["basis_points"] for item in request["fact_allocations"][0]["items"]] == [
        6_000,
        4_000,
    ]


def test_account_business_unit_assignment_uses_half_open_effective_dates() -> None:
    assignment = AccountBusinessUnitAssignment(
        assignment_ref=ALLOCATION_SET,
        managed_account_ref=ACCOUNT,
        business_unit_id=BUSINESS_UNIT_A,
        business_unit_ref_snapshot="store-a",
        business_unit_label_snapshot="Synthetic Store A",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 2, 1),
    )

    assert assignment.to_request()["effective_from"] == "2026-01-01"
    assert assignment.to_request()["effective_to"] == "2026-02-01"
    assert assignment.to_request()["business_unit_ref_snapshot"] == "store-a"
    assert assignment.to_request()["business_unit_label_snapshot"] == "Synthetic Store A"


class _ProjectionResult:
    def scalar_one(self) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.account-registry.v1",
            "owner_entity_ref": str(OWNER),
            "owner_kind": "COMPANY",
            "registry_revision": 2,
            "accounts": [
                {
                    "managed_account_ref": str(ACCOUNT),
                    "admission_evidence_ref": str(EVIDENCE),
                    "account_key": "managed-account:synthetic-company",
                    "institution_code": "synthetic_bank",
                    "account_suffix": "1234",
                    "account_kind": "BANK_CHECKING",
                    "aliases": [
                        {
                            "alias_ref": str(ALIAS),
                            "alias_kind": "ACCOUNT_NUMBER",
                            "masked_value": "************1234",
                        }
                    ],
                    "business_unit_assignments": [],
                    "fact_allocations": [
                        {
                            "allocation_set_ref": str(ALLOCATION_SET),
                            "fact_ref": str(FACT),
                            "revision": 1,
                            "items": [
                                {
                                    "business_unit_id": str(BUSINESS_UNIT_A),
                                    "business_unit_ref_snapshot": "store-a",
                                    "business_unit_label_snapshot": "Synthetic Store A",
                                    "basis_points": 6_000,
                                },
                                {
                                    "business_unit_id": str(BUSINESS_UNIT_B),
                                    "business_unit_ref_snapshot": "store-b",
                                    "business_unit_label_snapshot": "Synthetic Store B",
                                    "basis_points": 4_000,
                                },
                            ],
                        }
                    ],
                }
            ],
        }


class _ProjectionDatabase:
    def __enter__(self) -> _ProjectionDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _ProjectionResult:
        assert "internal_read.get_account_registry_projection" in str(statement)
        assert params["owner_entity_ref"] == OWNER
        return _ProjectionResult()


class _EmptyProjectionResult:
    def scalar_one(self) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.account-registry.v1",
            "owner_entity_ref": str(OWNER),
            "owner_kind": "PERSON",
            "registry_revision": 0,
            "accounts": [],
        }


class _EmptyProjectionDatabase:
    def __enter__(self) -> _EmptyProjectionDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _EmptyProjectionResult:
        return _EmptyProjectionResult()


def test_versioned_projection_keeps_unassigned_company_account_visible_at_owner_scope() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:company-reporting",
        san_uri="spiffe://ledgerbridge.test/company-reporting",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_READ}),
        grants=(EntityGrant(entity_ref=OWNER, allow_account_registry=True),),
    )

    projection = AccountRegistryReader(
        lambda: cast(Session, _ProjectionDatabase())
    ).get_owner_registry(
        OWNER,
        principal=principal,
        audit_horizon_sequence=9,
        audit_horizon_hash=b"h" * 32,
    )

    assert projection.contract_version == "ledgerbridge.account-registry.v1"
    assert projection.owner_kind is EntityType.COMPANY
    assert projection.registry_revision == 2
    assert len(projection.accounts) == 1
    account = projection.accounts[0]
    assert account.business_unit_assignments == ()
    assert account.fact_allocations[0].items[0].basis_points == 6_000


def test_existing_person_owner_can_have_an_empty_registry_projection() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:personal-finance",
        san_uri="spiffe://ledgerbridge.test/personal-finance",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_READ}),
        grants=(EntityGrant(entity_ref=OWNER, allow_account_registry=True),),
    )

    projection = AccountRegistryReader(
        lambda: cast(Session, _EmptyProjectionDatabase())
    ).get_owner_registry(
        OWNER,
        principal=principal,
        audit_horizon_sequence=9,
        audit_horizon_hash=b"h" * 32,
    )

    assert projection.owner_kind is EntityType.PERSON
    assert projection.registry_revision == 0
    assert projection.accounts == ()
