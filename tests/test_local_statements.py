"""A batch manifest says whose statements these are. It has to say it exactly.

The manifest is the one place where a person types which account a file belongs
to, so every test here is about a manifest that is wrong being refused rather
than guessed at. Binding a statement to the wrong book is not an error the
ledger can detect later: the rows would be real, the amounts would balance, and
they would be in someone else's accounts.

No statement file is read here. The fixtures are synthetic manifests; the files
they point at do not need to exist, because loading is meant to fail before
anything is opened.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import pytest

from ledgerbridge.bank_statement_contract import BankStatementParserProfile
from ledgerbridge.local_statements import (
    MAX_STATEMENTS,
    STATEMENT_BATCH_SCHEMA,
    LocalStatementError,
    evidence_ref_for,
    load_statement_batch,
)
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement_cutover import (
    _SUPPORTED_SCHEMA_REVISIONS,
    production_counts_from_cutover_inventory,
)

ENTITY = "20000000-0000-4000-8000-000000000001"
UNIT = "20000000-0000-4000-8000-000000000002"
ACCOUNT = "20000000-0000-4000-8000-000000000003"
# "Absolute" on Windows is not a leading slash, and the manifest requires it.
STATEMENT_PATH = str(Path(gettempdir()) / "statement.xlsx")

INVENTORY = {
    "schema_revision": next(iter(sorted(_SUPPORTED_SCHEMA_REVISIONS))),
    "candidate_total": 0,
    "latest_pending_candidates": 0,
    "audit_events": 0,
    "row_counts": dict.fromkeys(
        (
            "evidence_object",
            "encrypted_object_identity",
            "encrypted_blob_version",
            "managed_account",
            "managed_account_lifecycle",
            "account_registry_operation",
            "managed_account_alias",
            "account_business_unit_assignment",
            "fact_business_unit_allocation_set",
            "fact_business_unit_allocation_item",
            "bank_statement",
            "bank_statement_transaction",
            "bank_statement_observation",
            "bank_statement_review",
            "journal_entry",
            "posting",
        ),
        0,
    ),
}


def manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": STATEMENT_BATCH_SCHEMA,
        "book": {
            "entity_ref": ENTITY,
            "business_unit_ref": UNIT,
            "owner_kind": "COMPANY",
        },
        "accounts": [{"account_suffix": "1234", "managed_account_ref": ACCOUNT}],
        "statements": [
            {
                "path": STATEMENT_PATH,
                "parser_profile": "mybank_company_daily_xlsx_v2",
                "account_suffix": "1234",
            }
        ],
        "audit": {"actor": "local-single-user", "reason": "test"},
    }
    payload.update(overrides)
    return payload


def write(tmp_path: Path, payload: object) -> Path:
    target = tmp_path / "batch.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def test_loads_a_well_formed_batch(tmp_path: Path) -> None:
    batch = load_statement_batch(write(tmp_path, manifest()))

    assert batch.entity_ref == UUID(ENTITY)
    assert batch.business_unit_ref == UUID(UNIT)
    assert batch.owner_kind is EntityType.COMPANY
    assert batch.accounts == {"1234": UUID(ACCOUNT)}
    assert len(batch.statements) == 1
    assert batch.statements[0].path == Path(STATEMENT_PATH)
    assert batch.statements[0].reuse_existing_evidence is False
    assert batch.statements[0].expected_new_transaction_count is None


def test_optional_fields_are_carried_through(tmp_path: Path) -> None:
    payload = manifest()
    statement = payload["statements"][0]  # type: ignore[index]
    statement["reuse_existing_evidence"] = True  # type: ignore[index]
    statement["expected_new_transaction_count"] = 3  # type: ignore[index]

    batch = load_statement_batch(write(tmp_path, payload))

    assert batch.statements[0].reuse_existing_evidence is True
    assert batch.statements[0].expected_new_transaction_count == 3


def test_refuses_an_unknown_schema(tmp_path: Path) -> None:
    with pytest.raises(LocalStatementError, match=STATEMENT_BATCH_SCHEMA):
        load_statement_batch(write(tmp_path, manifest(schema_version="something.else")))


def test_refuses_an_unknown_top_level_key(tmp_path: Path) -> None:
    """A key nobody reads is a key somebody believes is being honoured."""

    with pytest.raises(LocalStatementError, match="keys must be exactly"):
        load_statement_batch(write(tmp_path, manifest(notes="import these carefully")))


def test_refuses_an_unknown_statement_key(tmp_path: Path) -> None:
    payload = manifest()
    payload["statements"][0]["expected_transaction_count"] = 9  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="must hold"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_a_statement_naming_an_unregistered_account(tmp_path: Path) -> None:
    """The suffix on the file must be one the manifest actually registered.

    This is the misfiling that matters: a statement bound to an account the
    batch never names would otherwise be bound to whichever account the
    importer happened to resolve.
    """

    payload = manifest()
    payload["statements"][0]["account_suffix"] = "9999"  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="unregistered account suffix 9999"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_a_duplicated_account_suffix(tmp_path: Path) -> None:
    payload = manifest(
        accounts=[
            {"account_suffix": "1234", "managed_account_ref": ACCOUNT},
            {"account_suffix": "1234", "managed_account_ref": ENTITY},
        ]
    )

    with pytest.raises(LocalStatementError, match="appears twice"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_a_relative_statement_path(tmp_path: Path) -> None:
    payload = manifest()
    payload["statements"][0]["path"] = "statements/january.xlsx"  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="must be absolute"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_an_unknown_parser_profile(tmp_path: Path) -> None:
    """Only profiles Core publishes can read an official statement.

    `wechat_xlsx_v1` is the real case: a profile that exists in the frozen
    finance-desk program and has no counterpart here.
    """

    payload = manifest()
    payload["statements"][0]["parser_profile"] = "wechat_xlsx_v1"  # type: ignore[index]

    with pytest.raises(ValueError, match="wechat_xlsx_v1"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_an_invalid_owner_kind(tmp_path: Path) -> None:
    with pytest.raises(LocalStatementError, match="owner kind is invalid"):
        load_statement_batch(
            write(
                tmp_path,
                manifest(
                    book={
                        "entity_ref": ENTITY,
                        "business_unit_ref": UNIT,
                        "owner_kind": "INDIVIDUAL",
                    }
                ),
            )
        )


def test_refuses_a_negative_expected_new_transaction_count(tmp_path: Path) -> None:
    payload = manifest()
    payload["statements"][0]["expected_new_transaction_count"] = -1  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="expected new transaction count"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_a_boolean_expected_new_transaction_count(tmp_path: Path) -> None:
    """`True` is an `int` in Python, and it is not a count of anything."""

    payload = manifest()
    payload["statements"][0]["expected_new_transaction_count"] = True  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="expected new transaction count"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_a_batch_larger_than_one_rollback_boundary(tmp_path: Path) -> None:
    statement = {
        "path": STATEMENT_PATH,
        "parser_profile": "mybank_company_daily_xlsx_v2",
        "account_suffix": "1234",
    }
    payload = manifest(statements=[dict(statement) for _ in range(MAX_STATEMENTS + 1)])

    with pytest.raises(LocalStatementError, match="Split it"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_an_empty_statement_list(tmp_path: Path) -> None:
    with pytest.raises(LocalStatementError, match="names no statement"):
        load_statement_batch(write(tmp_path, manifest(statements=[])))


def test_refuses_an_empty_account_list(tmp_path: Path) -> None:
    with pytest.raises(LocalStatementError, match="names no account"):
        load_statement_batch(write(tmp_path, manifest(accounts=[])))


def test_refuses_a_malformed_uuid(tmp_path: Path) -> None:
    payload = manifest()
    payload["accounts"][0]["managed_account_ref"] = "not-a-uuid"  # type: ignore[index]

    with pytest.raises(LocalStatementError, match="invalid uuid"):
        load_statement_batch(write(tmp_path, payload))


def test_refuses_json_that_is_not_json(tmp_path: Path) -> None:
    target = tmp_path / "batch.json"
    target.write_text("{", encoding="utf-8")

    with pytest.raises(LocalStatementError, match="not valid JSON"):
        load_statement_batch(target)


def test_refuses_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(LocalStatementError, match="unreadable"):
        load_statement_batch(tmp_path / "absent.json")


def test_evidence_ref_is_derived_from_the_file_and_is_stable() -> None:
    """Running the same manifest twice must present the same identities.

    A minted ref would make the second run look like a different import of the
    same bytes, and the cutover would refuse it as a half-matching batch
    instead of replaying it.

    The literal is pinned on purpose. Changing the derivation would not fail
    anything visible - it would mint fresh refs for statements already in the
    ledger, so a re-run of an imported manifest would present unknown evidence
    and be refused rather than replayed.
    """

    digest = "a" * 64
    assert evidence_ref_for(digest) == UUID("dc56c7ec-444f-5d03-fe51-8efa6590426a")
    assert evidence_ref_for(digest) != evidence_ref_for("b" * 64)


def plan(digest: str, *, transactions: int = 2, new: int | None = None) -> object:
    """One existing-account plan, with only the fields the count gate reads."""

    from datetime import date

    from ledgerbridge.bank_statement_cutover_plan import (
        BankStatementExistingAccountPlan,
        ExistingStatementEvidenceMode,
    )

    return BankStatementExistingAccountPlan(
        source_path=Path(STATEMENT_PATH),
        expected_sha256=digest,
        expected_size=1024,
        parser_profile=BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
        evidence_ref=evidence_ref_for(digest),
        evidence_mode=ExistingStatementEvidenceMode.CREATE_NEW,
        entity_ref=UUID(ENTITY),
        business_unit_ref=UUID(UNIT),
        managed_account_ref=UUID(ACCOUNT),
        institution_code="mybank",
        account_suffix="1234",
        expected_owner_kind=EntityType.COMPANY,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        expected_transaction_count=transactions,
        expected_transaction_set_sha256="c" * 64,
        expected_parser_facts_sha256="d" * 64,
        expected_monthly_transaction_counts=(("2026-01", transactions),),
        expected_new_transaction_count=new,
        actor="local-single-user",
        reason="test",
    )


def test_counts_before_a_batch_reverse_exactly_what_it_added() -> None:
    """The reversal has to be Core's, applied once per plan, in reverse order.

    This is what finds the backup a replay must present: the pre-batch
    inventory. Getting it wrong does not corrupt anything - the gate refuses -
    but it turns every replay into an unexplained refusal.
    """

    from ledgerbridge.local_statements import counts_before_batch
    from ledgerbridge.mybank_statement_cutover import _expected_after_existing_account

    plans = (plan("a" * 64, transactions=2), plan("b" * 64, transactions=3))
    before = production_counts_from_cutover_inventory(
        INVENTORY, expected_schema_revision=INVENTORY["schema_revision"]
    )
    completed = before
    for item in plans:
        completed = _expected_after_existing_account(completed, item)

    assert completed != before
    assert counts_before_batch(completed, plans) == before
