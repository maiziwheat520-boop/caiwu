"""Every refusal the account registry makes, on the way in and on the way back.

The registry is the only place that says which bank account belongs to which
accounting owner, so two directions have to be closed.  Going in, a plan is
checked before it is ever sent: identifiers, aliases, effective dates and
allocation shares.  Coming back, neither a projection nor a plan receipt is
trusted just because the database produced it - every field is re-read and
anything malformed becomes a persistence error rather than a silently wrong
registry.  Each test below names one of those refusals.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountBusinessUnitAssignment,
    AccountRegistryError,
    AccountRegistryOperator,
    AccountRegistryPersistenceError,
    AccountRegistryPlan,
    AccountRegistryReader,
    FactAllocationItem,
    FactAllocationRegistration,
    ManagedAccountRegistration,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.models import EntityType
from tests.test_account_registry import (
    ACCOUNT,
    ALIAS,
    ALLOCATION_SET,
    BUSINESS_UNIT_A,
    BUSINESS_UNIT_B,
    EVIDENCE,
    FACT,
    OPERATION,
    OWNER,
    _plan,
)

ASSIGNMENT = UUID("83000000-0000-4000-8000-00000000000a")
OTHER_ACCOUNT = UUID("83000000-0000-4000-8000-00000000000b")
OTHER_ALIAS = UUID("83000000-0000-4000-8000-00000000000c")
OTHER_OWNER = UUID("83000000-0000-4000-8000-00000000000d")
HORIZON_HASH = bytes(32)


# --- scaffolding -------------------------------------------------------------


class _Answer:
    """One database answer: a scripted value, or a failure to produce one."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class _ScriptedDatabase:
    """A session that answers every query with the same scripted result."""

    def __init__(self, value: object) -> None:
        self._value = value
        self.commits = 0

    def __enter__(self) -> _ScriptedDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _Answer:
        return _Answer(self._value)

    def commit(self) -> None:
        self.commits += 1


def _sessions(database: _ScriptedDatabase) -> Any:
    return lambda: cast(Session, database)


def _principal(capability: Capability, *, owner: UUID = OWNER) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:registry-test",
        san_uri="spiffe://ledgerbridge.test/registry-test",
        policy_generation=1,
        capabilities=frozenset({capability}),
        grants=(EntityGrant(entity_ref=owner, allow_account_registry=True),),
    )


def _database_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection lost"))


def _read(value: object, **overrides: Any) -> Any:
    database = _ScriptedDatabase(value)
    reader = AccountRegistryReader(_sessions(database))
    arguments: dict[str, Any] = {
        "principal": _principal(Capability.ACCOUNT_REGISTRY_READ),
        "audit_horizon_sequence": 1,
        "audit_horizon_hash": HORIZON_HASH,
    }
    arguments.update(overrides)
    return reader.get_owner_registry(OWNER, **arguments)


def _apply(value: object, **overrides: Any) -> Any:
    database = _ScriptedDatabase(value)
    operator = AccountRegistryOperator(_sessions(database))
    arguments: dict[str, Any] = {"principal": _principal(Capability.ACCOUNT_REGISTRY_WRITE)}
    arguments.update(overrides)
    return operator.apply(_plan(), **arguments)


def _alias(**overrides: Any) -> AccountAliasRegistration:
    fields: dict[str, Any] = {
        "alias_ref": ALIAS,
        "alias_kind": "ACCOUNT_NUMBER",
        "alias_value": "0000 0000 0000 1234",
    }
    fields.update(overrides)
    return AccountAliasRegistration(**fields)


def _registration(**overrides: Any) -> ManagedAccountRegistration:
    fields: dict[str, Any] = {
        "managed_account_ref": ACCOUNT,
        "admission_evidence_ref": EVIDENCE,
        "account_key": "managed-account:synthetic-personal",
        "institution_code": "synthetic_bank",
        "account_suffix": "1234",
        "account_kind": "BANK_CHECKING",
        "aliases": (_alias(),),
    }
    fields.update(overrides)
    return ManagedAccountRegistration(**fields)


def _assignment(**overrides: Any) -> AccountBusinessUnitAssignment:
    fields: dict[str, Any] = {
        "assignment_ref": ASSIGNMENT,
        "managed_account_ref": ACCOUNT,
        "business_unit_id": BUSINESS_UNIT_A,
        "business_unit_ref_snapshot": "store-a",
        "business_unit_label_snapshot": "Synthetic Store A",
        "effective_from": date(2026, 1, 1),
    }
    fields.update(overrides)
    return AccountBusinessUnitAssignment(**fields)


def _allocation_item(**overrides: Any) -> FactAllocationItem:
    fields: dict[str, Any] = {
        "business_unit_id": BUSINESS_UNIT_A,
        "business_unit_ref_snapshot": "store-a",
        "business_unit_label_snapshot": "Synthetic Store A",
        "basis_points": 10_000,
    }
    fields.update(overrides)
    return FactAllocationItem(**fields)


def _allocation(**overrides: Any) -> FactAllocationRegistration:
    fields: dict[str, Any] = {
        "allocation_set_ref": ALLOCATION_SET,
        "managed_account_ref": ACCOUNT,
        "fact_ref": FACT,
        "items": (_allocation_item(),),
    }
    fields.update(overrides)
    return FactAllocationRegistration(**fields)


def _registry_plan(**overrides: Any) -> AccountRegistryPlan:
    fields: dict[str, Any] = {
        "operation_id": OPERATION,
        "owner_entity_ref": OWNER,
        "expected_owner_kind": EntityType.PERSON,
        "expected_registry_revision": 0,
        "actor_ref": "operator:synthetic",
        "reason": "register a synthetic statement-backed account",
        "accounts": (_registration(),),
    }
    fields.update(overrides)
    return AccountRegistryPlan(**fields)


# --- projection and receipt payloads -----------------------------------------


def _alias_projection(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "alias_ref": str(ALIAS),
        "alias_kind": "ACCOUNT_NUMBER",
        "masked_value": "************1234",
    }
    payload.update(overrides)
    return payload


def _assignment_projection(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "assignment_ref": str(ASSIGNMENT),
        "business_unit_id": str(BUSINESS_UNIT_A),
        "business_unit_ref_snapshot": "store-a",
        "business_unit_label_snapshot": "Synthetic Store A",
        "effective_from": "2026-01-01",
        "effective_to": None,
    }
    payload.update(overrides)
    return payload


def _allocation_projection(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
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
    payload.update(overrides)
    return payload


def _account_projection(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "managed_account_ref": str(ACCOUNT),
        "admission_evidence_ref": str(EVIDENCE),
        "account_key": "managed-account:synthetic-company",
        "institution_code": "synthetic_bank",
        "account_suffix": "1234",
        "account_kind": "BANK_CHECKING",
        "aliases": [_alias_projection()],
        "business_unit_assignments": [_assignment_projection()],
        "fact_allocations": [_allocation_projection()],
    }
    payload.update(overrides)
    return payload


def _projection(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "ledgerbridge.account-registry.v1",
        "owner_entity_ref": str(OWNER),
        "owner_kind": "COMPANY",
        "registry_revision": 2,
        "accounts": [_account_projection()],
    }
    payload.update(overrides)
    return payload


def _receipt(**overrides: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "contract_version": "ledgerbridge.account-registry.v1",
        "operation_id": str(OPERATION),
        "owner_entity_ref": str(OWNER),
        "registry_revision": 1,
        "created": True,
        "managed_account_refs": [str(ACCOUNT)],
    }
    payload.update(overrides)
    return payload


# --- what a registration will accept as an identifier ------------------------


@pytest.mark.parametrize("alias_kind", ["account_number", "1ACCOUNT", "", "A" * 33])
def test_an_alias_kind_that_is_not_an_upper_case_code_is_refused(alias_kind: str) -> None:
    """Alias kinds are a closed vocabulary, so free-form text cannot enter one."""
    with pytest.raises(AccountRegistryError, match="account alias kind is invalid"):
        _alias(alias_kind=alias_kind)


@pytest.mark.parametrize(
    "alias_value",
    ["", "   ", "x" * 301, "0000\x000000"],
)
def test_an_alias_value_that_cannot_be_stored_is_refused(alias_value: str) -> None:
    """Blank, oversized and NUL-bearing text would all survive as a broken alias."""
    with pytest.raises(AccountRegistryError, match="account alias value is invalid"):
        _alias(alias_value=alias_value)


@pytest.mark.parametrize("account_key", ["Managed-Account", "-leading", "", "a" * 201])
def test_an_account_key_outside_the_safe_reference_shape_is_refused(account_key: str) -> None:
    """The key is used as a stable reference, so its shape is fixed."""
    with pytest.raises(AccountRegistryError, match="managed account key is invalid"):
        _registration(account_key=account_key)


@pytest.mark.parametrize("institution_code", ["Synthetic_Bank", "_bank", "", "b" * 33])
def test_an_institution_code_outside_its_shape_is_refused(institution_code: str) -> None:
    """Institution codes are lower-case identifiers, not display names."""
    with pytest.raises(AccountRegistryError, match="institution code is invalid"):
        _registration(institution_code=institution_code)


@pytest.mark.parametrize("account_suffix", ["123", "123456789", "12a4", ""])
def test_an_account_suffix_that_is_not_four_to_eight_digits_is_refused(
    account_suffix: str,
) -> None:
    """The suffix is the only part of an account number the registry keeps."""
    with pytest.raises(AccountRegistryError, match="account suffix is invalid"):
        _registration(account_suffix=account_suffix)


@pytest.mark.parametrize("account_kind", ["bank_checking", "1BANK", ""])
def test_an_account_kind_that_is_not_an_upper_case_code_is_refused(account_kind: str) -> None:
    """Account kinds are a closed vocabulary too."""
    with pytest.raises(AccountRegistryError, match="account kind is invalid"):
        _registration(account_kind=account_kind)


def test_an_account_registered_without_an_alias_is_refused() -> None:
    """Without an alias nothing arriving from a bank could ever match the account."""
    with pytest.raises(AccountRegistryError, match="requires an explicit alias"):
        _registration(aliases=())


def test_two_aliases_sharing_one_reference_are_refused() -> None:
    """Two aliases under one reference could not be revoked independently."""
    with pytest.raises(AccountRegistryError, match="alias refs must be unique"):
        _registration(
            aliases=(
                _alias(alias_value="0000 0000 0000 1234"),
                _alias(alias_value="0000 0000 0000 5678"),
            )
        )


# --- effective dates and allocation shares -----------------------------------


@pytest.mark.parametrize(
    ("effective_from", "effective_to"),
    [
        (datetime(2026, 1, 1), None),
        ("2026-01-01", None),
        (date(2026, 1, 1), datetime(2026, 6, 1)),
        (date(2026, 1, 1), "2026-06-01"),
        (date(2026, 1, 1), date(2026, 1, 1)),
        (date(2026, 6, 1), date(2026, 1, 1)),
    ],
)
def test_an_assignment_whose_dates_do_not_form_a_half_open_period_is_refused(
    effective_from: object, effective_to: object
) -> None:
    """A datetime carries a time zone question the registry has no answer for, and a
    period that ends before it starts covers nothing."""
    with pytest.raises(AccountRegistryError, match="assignment dates are invalid"):
        _assignment(effective_from=effective_from, effective_to=effective_to)


@pytest.mark.parametrize("basis_points", [True, "10000", 0, -1, 10_001, 1.5])
def test_an_allocation_share_outside_one_to_ten_thousand_basis_points_is_refused(
    basis_points: object,
) -> None:
    """A share is a whole number of basis points; `True` is not a share."""
    with pytest.raises(AccountRegistryError, match="basis points are invalid"):
        _allocation_item(basis_points=basis_points)


@pytest.mark.parametrize(
    "items",
    [
        (),
        "one item under ten thousand",
        "one item over ten thousand",
    ],
)
def test_an_allocation_that_does_not_total_ten_thousand_basis_points_is_refused(
    items: object,
) -> None:
    """Anything but a full allocation would leave part of a fact unattributed."""
    if items == "one item under ten thousand":
        items = (_allocation_item(basis_points=9_999),)
    elif items == "one item over ten thousand":
        items = (
            _allocation_item(basis_points=6_000),
            _allocation_item(business_unit_id=BUSINESS_UNIT_B, basis_points=5_000),
        )

    with pytest.raises(AccountRegistryError, match="must total 10000 basis points"):
        _allocation(items=items)


def test_an_allocation_naming_one_business_unit_twice_is_refused() -> None:
    """Two shares for one unit would make the unit's own total ambiguous."""
    with pytest.raises(AccountRegistryError, match="business units must be unique"):
        _allocation(
            items=(
                _allocation_item(basis_points=5_000),
                _allocation_item(basis_points=5_000),
            )
        )


# --- the plan itself ---------------------------------------------------------


@pytest.mark.parametrize("owner_kind", ["PERSON", None, 1])
def test_a_plan_whose_owner_kind_is_not_the_enumeration_is_refused(owner_kind: object) -> None:
    """The owner kind decides which registry rules apply, so a string will not do."""
    with pytest.raises(AccountRegistryError, match="expected owner kind is invalid"):
        _registry_plan(expected_owner_kind=owner_kind)


@pytest.mark.parametrize("revision", [True, "0", -1, 1.0])
def test_a_plan_without_a_whole_expected_revision_is_refused(revision: object) -> None:
    """The expected revision is what makes the plan refuse to apply twice."""
    with pytest.raises(AccountRegistryError, match="expected registry revision is invalid"):
        _registry_plan(expected_registry_revision=revision)


@pytest.mark.parametrize(
    ("field_name", "label"), [("actor_ref", "actor ref"), ("reason", "reason")]
)
@pytest.mark.parametrize("value", ["", "   ", "x\x00y"])
def test_a_plan_without_a_storable_actor_or_reason_is_refused(
    field_name: str, label: str, value: str
) -> None:
    """Who asked and why are the audit trail; neither may be blank or unstorable."""
    with pytest.raises(AccountRegistryError, match=f"{label} is invalid"):
        _registry_plan(**{field_name: value})


def test_a_plan_that_would_change_nothing_is_refused() -> None:
    """An empty plan would consume a revision without recording an action."""
    with pytest.raises(AccountRegistryError, match="plan has no actions"):
        _registry_plan(accounts=())


def test_a_plan_registering_one_account_reference_twice_is_refused() -> None:
    """Two registrations of one reference would leave the later one unexplained."""
    with pytest.raises(AccountRegistryError, match="managed account refs must be unique"):
        _registry_plan(
            accounts=(
                _registration(),
                _registration(
                    account_key="managed-account:synthetic-second",
                    aliases=(_alias(alias_ref=OTHER_ALIAS, alias_value="0000 0000 0000 5678"),),
                ),
            )
        )


def test_a_plan_registering_one_account_key_twice_is_refused() -> None:
    """The key is the stable name of an account, so it cannot name two of them."""
    with pytest.raises(AccountRegistryError, match="managed account keys must be unique"):
        _registry_plan(
            accounts=(
                _registration(),
                _registration(
                    managed_account_ref=OTHER_ACCOUNT,
                    aliases=(_alias(alias_ref=OTHER_ALIAS, alias_value="0000 0000 0000 5678"),),
                ),
            )
        )


# --- the authorisation seam --------------------------------------------------


def test_an_owner_the_principal_holds_no_registry_grant_for_is_not_visible() -> None:
    """Holding the capability is not enough: a grant has to name this owner."""
    database = _ScriptedDatabase(_receipt())
    operator = AccountRegistryOperator(_sessions(database))

    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        operator.apply(
            _plan(),
            principal=_principal(Capability.ACCOUNT_REGISTRY_WRITE, owner=OTHER_OWNER),
        )


# --- what the operator does when the seam misbehaves -------------------------


def test_a_plan_receipt_the_database_could_not_produce_becomes_a_persistence_error() -> None:
    """A failed statement is reported as a persistence failure, not as a result."""
    with pytest.raises(AccountRegistryPersistenceError, match="account registry plan failed"):
        _apply(_database_error())


def test_a_malformed_plan_receipt_is_not_reported_as_a_failed_statement() -> None:
    """The receipt error survives the outer handler, so the cause stays readable."""
    with pytest.raises(AccountRegistryPersistenceError, match="plan receipt is invalid"):
        _apply(["not", "a", "receipt"])


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"contract_version": "ledgerbridge.account-registry.v1"},
        "not a mapping at all",
    ],
)
def test_a_receipt_missing_the_fields_it_promised_is_refused(receipt: object) -> None:
    """Every field is read out; an absent one is a malformed receipt, not a default."""
    with pytest.raises(AccountRegistryPersistenceError, match="plan receipt is invalid"):
        _apply(receipt)


@pytest.mark.parametrize(
    "field_name",
    ["operation_id", "owner_entity_ref"],
)
def test_a_receipt_whose_identifiers_are_not_uuids_is_refused(field_name: str) -> None:
    """An identifier that will not parse cannot be matched back to the plan."""
    with pytest.raises(AccountRegistryPersistenceError, match="plan receipt is invalid"):
        _apply(_receipt(**{field_name: "not-a-uuid"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"contract_version": "ledgerbridge.account-registry.v2"},
        {"registry_revision": True},
        {"registry_revision": "1"},
        {"registry_revision": 0},
        {"created": "yes"},
        {"managed_account_refs": ()},
    ],
)
def test_a_receipt_whose_values_contradict_the_contract_is_refused(
    overrides: dict[str, object],
) -> None:
    """A receipt is only evidence of what happened if its own shape holds."""
    with pytest.raises(AccountRegistryPersistenceError, match="plan receipt is invalid"):
        _apply(_receipt(**overrides))


def test_a_well_formed_receipt_is_still_accepted() -> None:
    """None of the refusals above have closed the door on a real plan."""
    result = _apply(_receipt())

    assert result.operation_id == OPERATION
    assert result.registry_revision == 1
    assert result.created is True
    assert result.managed_account_refs == (ACCOUNT,)


# --- what the reader does when the seam misbehaves ---------------------------


@pytest.mark.parametrize(
    ("sequence", "horizon_hash"),
    [
        (True, HORIZON_HASH),
        ("1", HORIZON_HASH),
        (0, HORIZON_HASH),
        (1, "not bytes"),
        (1, bytes(31)),
        (1, bytes(33)),
    ],
)
def test_a_read_without_a_whole_audit_horizon_is_refused(
    sequence: object, horizon_hash: object
) -> None:
    """The horizon is what makes the projection an as-of answer rather than a guess."""
    with pytest.raises(AccountRegistryError, match="audit horizon is invalid"):
        _read(_projection(), audit_horizon_sequence=sequence, audit_horizon_hash=horizon_hash)


def test_a_projection_the_database_could_not_produce_becomes_a_persistence_error() -> None:
    """A failed read is reported as a persistence failure, not as an empty registry."""
    with pytest.raises(AccountRegistryPersistenceError, match="account registry read failed"):
        _read(_database_error())


def test_a_malformed_projection_is_not_reported_as_a_failed_read() -> None:
    """The projection error survives the outer handler, so the cause stays readable."""
    with pytest.raises(AccountRegistryPersistenceError, match="projection is invalid"):
        _read(["not", "a", "projection"])


def test_a_projection_announcing_another_contract_version_is_refused() -> None:
    """A version the reader does not know may mean the fields no longer mean the same."""
    with pytest.raises(AccountRegistryPersistenceError, match="version is unsupported"):
        _read(_projection(contract_version="ledgerbridge.account-registry.v2"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_kind": "PARTNERSHIP"},
        {"owner_entity_ref": "not-a-uuid"},
        {"contract_version": ""},
        {"registry_revision": -1},
        {"registry_revision": True},
        {"registry_revision": "2"},
        {"accounts": {}},
        {"accounts": ["not a mapping"]},
    ],
)
def test_a_projection_whose_fields_do_not_hold_is_refused(overrides: dict[str, object]) -> None:
    """Every scalar is re-read: an unknown owner kind or a missing list is an error."""
    with pytest.raises(AccountRegistryPersistenceError, match="projection is invalid"):
        _read(_projection(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_key": ""},
        {"account_key": 4},
        {"aliases": [{"alias_ref": str(ALIAS), "alias_kind": "ACCOUNT_NUMBER"}]},
        {"business_unit_assignments": "none"},
        {"fact_allocations": [{}]},
    ],
)
def test_an_account_projection_whose_fields_do_not_hold_is_refused(
    overrides: dict[str, object],
) -> None:
    """The same re-reading applies one level down, to each account in the list."""
    with pytest.raises(AccountRegistryPersistenceError, match="projection is invalid"):
        _read(_projection(accounts=[_account_projection(**overrides)]))


@pytest.mark.parametrize("effective_to", [5, ["2026-06-01"], "not a date"])
def test_an_assignment_projection_with_an_unreadable_end_date_is_refused(
    effective_to: object,
) -> None:
    """An end date that is present but unreadable would silently open the period."""
    account = _account_projection(
        business_unit_assignments=[_assignment_projection(effective_to=effective_to)]
    )

    with pytest.raises(AccountRegistryPersistenceError, match="projection is invalid"):
        _read(_projection(accounts=[account]))


@pytest.mark.parametrize(
    "items",
    [
        [],
        [
            {
                "business_unit_id": str(BUSINESS_UNIT_A),
                "business_unit_ref_snapshot": "store-a",
                "business_unit_label_snapshot": "Synthetic Store A",
                "basis_points": 9_999,
            }
        ],
        [
            {
                "business_unit_id": str(BUSINESS_UNIT_A),
                "business_unit_ref_snapshot": "store-a",
                "business_unit_label_snapshot": "Synthetic Store A",
                "basis_points": 0,
            }
        ],
    ],
)
def test_an_allocation_projection_that_does_not_total_ten_thousand_is_refused(
    items: list[dict[str, object]],
) -> None:
    """A projected allocation is held to the same total as a registered one."""
    account = _account_projection(fact_allocations=[_allocation_projection(items=items)])

    with pytest.raises(AccountRegistryPersistenceError, match="projection is invalid"):
        _read(_projection(accounts=[account]))


def test_a_well_formed_projection_is_still_read_in_full() -> None:
    """The assignments and allocations the refusals guard do come back when valid."""
    projection = _read(_projection())

    assert projection.owner_entity_ref == OWNER
    assert projection.owner_kind is EntityType.COMPANY
    assert projection.registry_revision == 2
    account = projection.accounts[0]
    assert account.business_unit_assignments[0].assignment_ref == ASSIGNMENT
    assert account.business_unit_assignments[0].effective_from == date(2026, 1, 1)
    assert account.business_unit_assignments[0].effective_to is None
    assert sum(item.basis_points for item in account.fact_allocations[0].items) == 10_000


def test_an_assignment_projection_keeps_an_end_date_it_can_read() -> None:
    """A half-open period that does end is preserved, not dropped."""
    account = _account_projection(
        business_unit_assignments=[_assignment_projection(effective_to="2026-06-01")]
    )

    projection = _read(_projection(accounts=[account]))

    assert projection.accounts[0].business_unit_assignments[0].effective_to == date(2026, 6, 1)
