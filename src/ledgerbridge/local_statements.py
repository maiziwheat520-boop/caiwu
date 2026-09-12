"""Import one book's bank statements on a single machine.

This is the thin part. Everything that decides what a statement fact *is* -
the parsers, the account authorization, the evidence encryption, the receipt
and count discipline, the idempotent replay - already exists and is used here
unchanged. What was missing locally was only the arrangement: a way to say
"these ninety files belong to that book" without hand-writing a plan per file,
and a proof of backup that a laptop can actually produce.

The batch runs as one transaction. Either every statement in it lands or none
does, and the encrypted artifacts staged along the way are aborted together.
That is deliberate for a first import of a year of statements: a half-imported
book is worse than an unimported one, because the counts no longer say which.

Nothing here posts anything. Statements arrive with their review PENDING, and
``Ledger Draft -> Posted Entry`` still needs a person.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Engine, text

from ledgerbridge.bank_statement_contract import BankStatementParserProfile
from ledgerbridge.bank_statement_cutover_plan import (
    BankStatementExistingAccountPlan,
    ExistingStatementEvidenceMode,
)
from ledgerbridge.bank_statement_parsers import parse_bank_statement
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement_cutover import (
    MyBankCutoverSafetyProof,
    MyBankStatementCutoverGates,
    MyBankStatementCutoverReceipt,
    ProductionCounts,
    _expected_before_existing_account,
    run_transactional_database_bank_statement_existing_account_batch_import,
)

STATEMENT_BATCH_SCHEMA: Final = "ledgerbridge.local-statement-batch.v1"

#: The batch cutover accepts at most a hundred statements under one rollback
#: boundary. A year of daily company statements can exceed that, so a larger
#: manifest is refused here with a message that says to split it rather than
#: failing deep inside the cutover on a number nobody chose.
MAX_STATEMENTS: Final = 100

_MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024


class LocalStatementError(RuntimeError):
    """A local statement batch could not be read or imported."""


@dataclass(frozen=True, slots=True)
class LocalStatementSource:
    """One statement file, and the account it belongs to."""

    path: Path
    parser_profile: BankStatementParserProfile
    account_suffix: str
    #: True when this file was already published as Evidence - which is the
    #: case for the statement a book's account admission used to prove the
    #: account exists. Re-publishing it would be a second encrypted copy of the
    #: same bytes under a second ref, so the import reuses the first.
    reuse_existing_evidence: bool = False
    #: How many of this statement's transactions are expected to be new. A
    #: monthly export that overlaps dailies already imported carries fewer new
    #: facts than it has rows; leaving this unset asserts that all of them are
    #: new, which is right for a first import and wrong for a re-export.
    expected_new_transaction_count: int | None = None


@dataclass(frozen=True, slots=True)
class LocalStatementBatch:
    """One book's statements: whose they are, and which account each names."""

    entity_ref: UUID
    business_unit_ref: UUID
    owner_kind: EntityType
    #: Account suffix -> the managed account ref registered for it. Suffixes
    #: rather than refs appear on the statements themselves, and a book with
    #: two accounts at the same bank is ordinary.
    accounts: dict[str, UUID]
    statements: tuple[LocalStatementSource, ...]
    actor: str
    reason: str


def load_statement_batch(path: Path) -> LocalStatementBatch:
    """Read one strict batch manifest. Unknown fields are refused."""

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise LocalStatementError(f"statement manifest is unreadable: {error}") from None
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise LocalStatementError("statement manifest is implausibly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise LocalStatementError("statement manifest is not valid JSON") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != STATEMENT_BATCH_SCHEMA:
        raise LocalStatementError(f"statement manifest must declare {STATEMENT_BATCH_SCHEMA}")
    expected = {"schema_version", "book", "accounts", "statements", "audit"}
    if set(payload) != expected:
        raise LocalStatementError(f"statement manifest keys must be exactly {sorted(expected)}")

    book = _mapping(payload["book"], {"entity_ref", "business_unit_ref", "owner_kind"})
    audit = _mapping(payload["audit"], {"actor", "reason"})
    accounts_payload = payload["accounts"]
    if not isinstance(accounts_payload, list) or not accounts_payload:
        raise LocalStatementError("statement manifest names no account")
    accounts: dict[str, UUID] = {}
    for item in accounts_payload:
        account = _mapping(item, {"account_suffix", "managed_account_ref"})
        suffix = _text(account["account_suffix"])
        if suffix in accounts:
            raise LocalStatementError(f"account suffix {suffix} appears twice")
        accounts[suffix] = _uuid(account["managed_account_ref"])

    statements_payload = payload["statements"]
    if not isinstance(statements_payload, list) or not statements_payload:
        raise LocalStatementError("statement manifest names no statement")
    if len(statements_payload) > MAX_STATEMENTS:
        raise LocalStatementError(
            f"a batch imports at most {MAX_STATEMENTS} statements under one rollback "
            f"boundary; this manifest names {len(statements_payload)}. Split it."
        )
    statements: list[LocalStatementSource] = []
    for item in statements_payload:
        source = _mapping(
            item,
            {"path", "parser_profile", "account_suffix"},
            optional={"expected_new_transaction_count", "reuse_existing_evidence"},
        )
        suffix = _text(source["account_suffix"])
        if suffix not in accounts:
            raise LocalStatementError(f"statement names unregistered account suffix {suffix}")
        path_value = Path(_text(source["path"]))
        if not path_value.is_absolute():
            raise LocalStatementError(f"statement path must be absolute: {path_value}")
        new_count = source.get("expected_new_transaction_count")
        if new_count is not None and (type(new_count) is not int or new_count < 0):
            raise LocalStatementError("expected new transaction count is invalid")
        reuse = source.get("reuse_existing_evidence", False)
        if type(reuse) is not bool:
            raise LocalStatementError("reuse_existing_evidence must be true or false")
        statements.append(
            LocalStatementSource(
                path=path_value,
                parser_profile=BankStatementParserProfile(_text(source["parser_profile"])),
                account_suffix=suffix,
                reuse_existing_evidence=reuse,
                expected_new_transaction_count=new_count,
            )
        )

    try:
        owner_kind = EntityType(_text(book["owner_kind"]))
    except ValueError:
        raise LocalStatementError("book owner kind is invalid") from None
    return LocalStatementBatch(
        entity_ref=_uuid(book["entity_ref"]),
        business_unit_ref=_uuid(book["business_unit_ref"]),
        owner_kind=owner_kind,
        accounts=accounts,
        statements=tuple(statements),
        actor=_text(audit["actor"]),
        reason=_text(audit["reason"]),
    )


def build_statement_plans(
    batch: LocalStatementBatch,
) -> tuple[BankStatementExistingAccountPlan, ...]:
    """Parse every file and bind it to explicit scope, before anything is written.

    Parsing the whole batch up front is the bounded preview: a file that cannot
    be read, or whose digest disagrees with itself, stops the run before the
    database is touched at all.
    """

    plans: list[BankStatementExistingAccountPlan] = []
    for source in batch.statements:
        path = source.path.resolve()
        try:
            digest = _file_digest(path)
        except OSError as error:
            raise LocalStatementError(f"statement file is unreadable: {error}") from None
        try:
            statement = parse_bank_statement(
                source.parser_profile,
                path,
                expected_sha256=digest,
                managed_account_suffix=source.account_suffix,
            )
        except Exception as error:
            raise LocalStatementError(f"{path.name}: {error}") from None
        plans.append(
            BankStatementExistingAccountPlan(
                source_path=path,
                expected_sha256=statement.source_sha256,
                expected_size=statement.source_size,
                parser_profile=source.parser_profile,
                evidence_ref=evidence_ref_for(statement.source_sha256),
                evidence_mode=(
                    ExistingStatementEvidenceMode.REUSE_EXISTING
                    if source.reuse_existing_evidence
                    else ExistingStatementEvidenceMode.CREATE_NEW
                ),
                entity_ref=batch.entity_ref,
                business_unit_ref=batch.business_unit_ref,
                managed_account_ref=batch.accounts[source.account_suffix],
                institution_code=statement.institution_code,
                account_suffix=statement.account_suffix,
                expected_owner_kind=batch.owner_kind,
                period_start=statement.period_start,
                period_end=statement.period_end,
                expected_transaction_count=len(statement.transactions),
                expected_transaction_set_sha256=statement.transaction_set_sha256,
                expected_parser_facts_sha256=statement.parser_facts_sha256,
                expected_monthly_transaction_counts=statement.monthly_transaction_counts,
                expected_new_transaction_count=source.expected_new_transaction_count,
                actor=batch.actor,
                reason=batch.reason,
            )
        )
    return tuple(plans)


def evidence_ref_for(source_sha256: str) -> UUID:
    """Derive one statement file's Evidence ref from the file itself.

    Deriving rather than minting is what lets the same manifest be run twice:
    the second run presents the identities the first one wrote, so the cutover
    recognises a completed batch and replays it instead of refusing a
    half-matching one.
    """

    material = b"ledgerbridge-local-evidence-v1" + bytes.fromhex(source_sha256)
    return UUID(bytes=hashlib.sha256(material).digest()[:16])


def import_local_statements(
    engine: Engine,
    plans: tuple[BankStatementExistingAccountPlan, ...],
    *,
    proof: MyBankCutoverSafetyProof,
    gates: MyBankStatementCutoverGates,
    key_file: Path,
    artifact_root: Path,
    commit: bool = True,
) -> tuple[MyBankStatementCutoverReceipt, ...]:
    """Import the whole batch under one rollback boundary."""

    return run_transactional_database_bank_statement_existing_account_batch_import(
        engine,
        plans,
        gates=gates,
        safety_proof=proof,
        key_file=key_file,
        artifact_root=artifact_root,
        commit=commit,
    )


def batch_is_already_imported(
    engine: Engine,
    plans: tuple[BankStatementExistingAccountPlan, ...],
) -> bool:
    """Has this exact batch already landed?

    Asked of the ledger directly, by source digest, rather than inferred from
    row counts. A batch lands whole or not at all, so a partial answer is not an
    interrupted run - it is two manifests naming the same file, which is worth
    stopping for.
    """

    digests = [bytes.fromhex(plan.expected_sha256) for plan in plans]
    with engine.connect() as connection:
        present = int(
            connection.execute(
                text("SELECT count(*) FROM public.bank_statement WHERE source_sha256 = ANY(:d)"),
                {"d": digests},
            ).scalar_one()
        )
    if present in (0, len(plans)):
        return present > 0
    raise LocalStatementError(
        f"{present} of {len(plans)} statements in this batch are already imported. "
        "A batch lands whole or not at all, so this is not an interrupted run - "
        "two manifests name the same file. Split them."
    )


def counts_before_batch(
    completed: ProductionCounts,
    plans: tuple[BankStatementExistingAccountPlan, ...],
) -> ProductionCounts:
    """Recover what the ledger held before an already-imported batch.

    Needed to find the backup that was taken before it. The arithmetic is
    Core's own reversal, applied once per plan in the order the batch applied
    them, so the result is the inventory that backup will be carrying.
    """

    counts = completed
    for plan in reversed(plans):
        counts = _expected_before_existing_account(counts, plan)
    return counts


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(
    value: object,
    required: set[str],
    *,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalStatementError("statement manifest section must be an object")
    allowed = required | (optional or set())
    if not required <= set(value) or not set(value) <= allowed:
        raise LocalStatementError(f"statement manifest section must hold {sorted(required)}")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalStatementError("statement manifest holds an empty value")
    return value.strip()


def _uuid(value: object) -> UUID:
    try:
        return UUID(_text(value))
    except ValueError:
        raise LocalStatementError(f"statement manifest holds an invalid uuid: {value!r}") from None


__all__ = [
    "MAX_STATEMENTS",
    "STATEMENT_BATCH_SCHEMA",
    "LocalStatementBatch",
    "LocalStatementError",
    "LocalStatementSource",
    "batch_is_already_imported",
    "build_statement_plans",
    "counts_before_batch",
    "evidence_ref_for",
    "import_local_statements",
    "load_statement_batch",
]
