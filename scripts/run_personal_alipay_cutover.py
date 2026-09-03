"""Reattribute one verified Alipay receipt cohort to its personal owner.

This owner-only command deliberately separates preparation from the final cutover.
Preparation imports replacement Candidates as PENDING.  The reviewed execution then
registers the payment account and atomically confirms every replacement while
ignoring its incorrectly scoped predecessor.  It never creates journal entries or
postings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountBusinessUnitAssignment,
    AccountRegistryOperator,
    AccountRegistryPlan,
    ManagedAccountRegistration,
)
from ledgerbridge.controlled_import import (
    ImportBusinessUnit,
    ImportCandidate,
    ImportCategory,
    ImportEntity,
    SourceEvidence,
    SourceManifest,
    import_prepared_manifest_in_transaction,
    load_prepared_manifest,
    prepare_source_manifest,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.models import EntityType

_NAMESPACE = UUID("de78fc0c-036b-4f2a-84e7-0cd729d23aa1")
_EXPECTED_COUNT = 1_574
_EXPECTED_TOTAL_MINOR = 21_362_070
_EXPECTED_MONTHS = {
    date(2026, 1, 1): (184, 2_624_545),
    date(2026, 2, 1): (257, 3_226_008),
    date(2026, 3, 1): (270, 4_001_554),
    date(2026, 4, 1): (273, 4_083_187),
    date(2026, 5, 1): (309, 4_042_057),
    date(2026, 6, 1): (281, 3_384_719),
}
_SOURCE_SHA256 = "a90023a4283d90164a123cdb5249e7f6726b331f7bcc5c1a2bb4ca75116c5cb4"
_SOURCE_SIZE = 674_681
_LEGACY_ENTITY = UUID("a131ef1b-e250-5a6d-82ff-cab68f767997")
_PERSON_ENTITY = UUID("0f343ff1-8e49-5361-900a-8c5ec192503c")
_PERSON_UNIT = UUID("948bff1d-2acd-5f1b-a7d3-c6038e6610c1")
_CATEGORY = uuid5(_NAMESPACE, "personal-alipay-receipt-category")
_ACCOUNT_SUFFIX = "5002"
_BATCH_REF = uuid5(_NAMESPACE, f"personal-alipay-account2:{_SOURCE_SHA256}")
_ACCOUNT_REF = UUID("fcca4187-7b28-4131-bb08-9c3c361f5797")
_LEGACY_SET_SHA256 = "f2d982fa287a8f40a9a3a1c6ab4b7c453b7273b460d2206bdece8f863e7781af"
_EXPECTED_SCHEMA = "20260903_0038"


class PersonalAlipayCutoverError(RuntimeError):
    """The reviewed personal Alipay cutover failed closed."""


@dataclass(frozen=True, slots=True)
class CandidatePair:
    legacy_ref: UUID
    replacement_ref: UUID
    amount_minor: int
    accounting_month: date


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _require_private_source(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PersonalAlipayCutoverError("Alipay source must be a regular file")
    if metadata.st_size != _SOURCE_SIZE or _digest(path) != _SOURCE_SHA256:
        raise PersonalAlipayCutoverError("Alipay source identity does not match the reviewed bill")
    header = path.read_bytes()[:32_768].decode("gb18030")
    if "陈明哲" not in header or _ACCOUNT_SUFFIX not in header:
        raise PersonalAlipayCutoverError("Alipay source owner or account suffix is invalid")


def _validate_scope(connection: Connection) -> None:
    row = (
        connection.execute(
            text(
                "SELECT e.entity_type::text AS entity_type,e.name,b.ref,b.label "
                "FROM public.entity e JOIN public.business_unit b ON b.entity_id=e.id "
                "WHERE e.id=:entity AND b.id=:unit"
            ),
            {"entity": _PERSON_ENTITY, "unit": _PERSON_UNIT},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or dict(row) != {
        "entity_type": "PERSON",
        "name": "陈明哲",
        "ref": "personal-funds",
        "label": "个人资金",
    }:
        raise PersonalAlipayCutoverError("personal owner scope does not match the reviewed target")


def _load_legacy_rows(connection: Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        text(
            "SELECT c.id AS candidate_ref, cs.source_event_ref, r.amount_minor, "
            "r.accounting_month, r.summary, r.confidence_basis_points "
            "FROM public.candidate c "
            "JOIN public.candidate_source cs ON cs.candidate_id=c.id "
            "JOIN LATERAL (SELECT * FROM public.candidate_revision cr "
            " WHERE cr.candidate_id=c.id ORDER BY cr.revision DESC LIMIT 1) r ON true "
            "WHERE c.entity_id=:legacy AND cs.source_system_id='alipay_export' "
            "AND r.status IN ('PENDING','IGNORED') "
            "AND r.accounting_month BETWEEN DATE '2026-01-01' "
            "AND DATE '2026-06-01' "
            "AND r.summary LIKE '支付宝 | % | 收入 | 转账红包 |%' "
            "ORDER BY r.accounting_month,c.id"
        ),
        {"legacy": _LEGACY_ENTITY},
    ).mappings()
    return [dict(row) for row in rows]


def _cohort_sha256(rows: list[dict[str, object]]) -> str:
    rendered = "\n".join(
        "|".join(
            (
                str(row["candidate_ref"]),
                str(row["source_event_ref"]),
                str(row["amount_minor"]),
                cast(date, row["accounting_month"]).isoformat(),
                str(row["summary"]),
                str(row["confidence_basis_points"]),
            )
        )
        for row in sorted(rows, key=lambda value: str(value["candidate_ref"]))
    )
    return hashlib.sha256(rendered.encode()).hexdigest()


def _validate_cohort(rows: list[dict[str, object]]) -> None:
    if len(rows) != _EXPECTED_COUNT:
        raise PersonalAlipayCutoverError("reviewed Alipay cohort count changed")
    if any(
        not isinstance(row["amount_minor"], int) or not isinstance(row["accounting_month"], date)
        for row in rows
    ):
        raise PersonalAlipayCutoverError("reviewed Alipay cohort values are invalid")
    if sum(cast(int, row["amount_minor"]) for row in rows) != _EXPECTED_TOTAL_MINOR:
        raise PersonalAlipayCutoverError("reviewed Alipay cohort total changed")
    actual: dict[date, tuple[int, int]] = {}
    for month in _EXPECTED_MONTHS:
        values = [row for row in rows if row["accounting_month"] == month]
        actual[month] = (
            len(values),
            sum(cast(int, row["amount_minor"]) for row in values),
        )
    if actual != _EXPECTED_MONTHS or any(cast(int, row["amount_minor"]) <= 0 for row in rows):
        raise PersonalAlipayCutoverError("reviewed Alipay monthly cohort changed")
    if _cohort_sha256(rows) != _LEGACY_SET_SHA256:
        raise PersonalAlipayCutoverError("reviewed Alipay exact candidate set changed")


def build_source_manifest(
    connection: Connection, *, source_path: Path, account_ref: UUID
) -> tuple[SourceManifest, tuple[CandidatePair, ...]]:
    _require_private_source(source_path)
    _validate_scope(connection)
    rows = _load_legacy_rows(connection)
    _validate_cohort(rows)
    evidence_ref = uuid5(_NAMESPACE, f"{account_ref}:evidence:{_SOURCE_SHA256}")
    candidates: list[ImportCandidate] = []
    pairs: list[CandidatePair] = []
    for row in rows:
        legacy_ref = row["candidate_ref"]
        source_event_ref = row["source_event_ref"]
        if not isinstance(legacy_ref, UUID) or not isinstance(source_event_ref, UUID):
            raise PersonalAlipayCutoverError("legacy candidate identity is invalid")
        amount_minor = cast(int, row["amount_minor"])
        accounting_month = cast(date, row["accounting_month"])
        replacement_ref = uuid5(_NAMESPACE, f"{account_ref}:candidate:{legacy_ref}")
        candidates.append(
            ImportCandidate(
                candidate_ref=replacement_ref,
                operation_id=uuid5(_NAMESPACE, f"{account_ref}:create:{legacy_ref}"),
                ingest_channel="CONTROLLED_UPLOAD",
                source_system="alipay_export_account2",
                source_event_ref=uuid5(
                    _NAMESPACE, f"{account_ref}:source-event:{source_event_ref}"
                ),
                display_label="支付宝账号2收款",
                category_code="PERSONAL_ALIPAY_RECEIPT",
                amount_minor=amount_minor,
                accounting_month=accounting_month.strftime("%Y-%m"),
                summary=str(row["summary"]),
                confidence_basis_points=cast(int, row["confidence_basis_points"]),
                evidence_refs=(evidence_ref,),
            )
        )
        pairs.append(
            CandidatePair(
                legacy_ref=legacy_ref,
                replacement_ref=replacement_ref,
                amount_minor=amount_minor,
                accounting_month=accounting_month,
            )
        )
    manifest = SourceManifest(
        schema_version="ledgerbridge.controlled-review-source.v1",
        batch_ref=_BATCH_REF,
        generated_at=datetime(2026, 9, 3, 2, 12, 7, tzinfo=timezone(timedelta(hours=8))),
        source_description="Reviewed personal Alipay account 2 receipt reattribution.",
        entity=ImportEntity(entity_ref=_PERSON_ENTITY, name="陈明哲"),
        business_unit=ImportBusinessUnit(
            business_unit_ref=_PERSON_UNIT, ref="personal-funds", label="个人资金"
        ),
        categories=(
            ImportCategory(
                category_ref=_CATEGORY,
                code="PERSONAL_ALIPAY_RECEIPT",
                label="个人支付宝收款",
            ),
        ),
        evidence=(
            SourceEvidence(
                evidence_ref=evidence_ref,
                source_file=source_path.name,
                display_name="alipay-account2-annual.csv",
                declared_media_type="text/csv",
                plaintext_sha256=_SOURCE_SHA256,
                plaintext_size=_SOURCE_SIZE,
            ),
        ),
        candidates=tuple(candidates),
    )
    return manifest, tuple(pairs)


def _write_private(path: Path, payload: object) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise PersonalAlipayCutoverError("existing private plan conflicts")
        return
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _read_private_json(path: Path) -> dict[str, object]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise PersonalAlipayCutoverError("receipt must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PersonalAlipayCutoverError("receipt is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PersonalAlipayCutoverError("receipt must contain a JSON object")
    return value


def _current_candidate(connection: Connection, candidate_ref: UUID) -> dict[str, object]:
    row = (
        connection.execute(
            text(
                "SELECT c.entity_id,r.* FROM public.candidate c "
                "JOIN LATERAL (SELECT * FROM public.candidate_revision cr "
                " WHERE cr.candidate_id=c.id ORDER BY cr.revision DESC LIMIT 1) r ON true "
                "WHERE c.id=:candidate FOR UPDATE OF c"
            ),
            {"candidate": candidate_ref},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PersonalAlipayCutoverError("candidate is missing during cutover")
    return dict(row)


def _transition(connection: Connection, row: dict[str, object], action: str, reason: str) -> None:
    connection.execute(
        text(
            "SELECT internal_command.append_candidate_transition("
            ":candidate,:revision,:action,:actor,:reason,clock_timestamp(),"
            ":unit,:unit_ref,:unit_label,:category,:category_code,:category_label,"
            ":amount,:month,NULL)"
        ),
        {
            "candidate": row["candidate_id"],
            "revision": row["revision"],
            "action": action,
            "actor": "user:maiziwheat520",
            "reason": reason,
            "unit": row["business_unit_id"],
            "unit_ref": row["business_unit_ref_snapshot"],
            "unit_label": row["business_unit_label_snapshot"],
            "category": row["category_id"],
            "category_code": row["category_code_snapshot"],
            "category_label": row["category_label_snapshot"],
            "amount": row["amount_minor"],
            "month": row["accounting_month"],
        },
    ).scalar_one()


def _register_account(connection: Connection, account_ref: UUID, evidence_ref: UUID) -> None:
    existing = (
        connection.execute(
            text(
                "SELECT m.entity_id,m.account_key,m.institution_code,m.account_suffix,"
                "m.owner_kind,m.account_kind,m.admission_evidence_ref,"
                "(SELECT status FROM public.managed_account_lifecycle l "
                " WHERE l.managed_account_ref=m.managed_account_ref "
                " ORDER BY revision DESC LIMIT 1) AS lifecycle_status,"
                "(SELECT count(*) FROM public.managed_account_alias a "
                " WHERE a.managed_account_ref=m.managed_account_ref "
                " AND a.alias_kind='ACCOUNT_SUFFIX' AND a.alias_value=:suffix) AS aliases,"
                "(SELECT count(*) FROM public.account_business_unit_assignment a "
                " WHERE a.managed_account_ref=m.managed_account_ref "
                " AND a.business_unit_id=:unit AND a.effective_from=DATE '2025-09-04' "
                " AND a.effective_to IS NULL) AS assignments "
                "FROM public.managed_account m WHERE m.managed_account_ref=:account"
            ),
            {"account": account_ref, "suffix": _ACCOUNT_SUFFIX, "unit": _PERSON_UNIT},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        expected = {
            "entity_id": _PERSON_ENTITY,
            "account_key": "alipay:personal:5002",
            "institution_code": "alipay",
            "account_suffix": _ACCOUNT_SUFFIX,
            "owner_kind": "PERSONAL",
            "account_kind": "PAYMENT_ACCOUNT",
            "admission_evidence_ref": evidence_ref,
            "lifecycle_status": "ACTIVE",
            "aliases": 1,
            "assignments": 1,
        }
        if dict(existing) != expected:
            raise PersonalAlipayCutoverError("existing personal Alipay account conflicts")
        return
    revision = connection.execute(
        text(
            "SELECT coalesce(max(registry_revision),0) "
            "FROM public.account_registry_operation "
            "WHERE owner_entity_id=:entity"
        ),
        {"entity": _PERSON_ENTITY},
    ).scalar_one()
    plan = AccountRegistryPlan(
        operation_id=uuid5(_NAMESPACE, f"{account_ref}:registry"),
        owner_entity_ref=_PERSON_ENTITY,
        expected_owner_kind=EntityType.PERSON,
        expected_registry_revision=revision,
        actor_ref="user:maiziwheat520",
        reason="user confirmed annual Alipay statement as personal account 2 ending 5002",
        accounts=(
            ManagedAccountRegistration(
                managed_account_ref=account_ref,
                admission_evidence_ref=evidence_ref,
                account_key="alipay:personal:5002",
                institution_code="alipay",
                account_suffix="5002",
                account_kind="PAYMENT_ACCOUNT",
                aliases=(
                    AccountAliasRegistration(
                        alias_ref=uuid5(_NAMESPACE, f"{account_ref}:alias:5002"),
                        alias_kind="ACCOUNT_SUFFIX",
                        alias_value="5002",
                    ),
                ),
            ),
        ),
        business_unit_assignments=(
            AccountBusinessUnitAssignment(
                assignment_ref=uuid5(_NAMESPACE, f"{account_ref}:personal-funds"),
                managed_account_ref=account_ref,
                business_unit_id=_PERSON_UNIT,
                business_unit_ref_snapshot="personal-funds",
                business_unit_label_snapshot="个人资金",
                effective_from=date(2025, 9, 4),
            ),
        ),
    )
    principal = WorkloadPrincipal(
        principal_ref="operator:personal-alipay-cutover",
        san_uri="spiffe://ledgerbridge.local/operator/personal-alipay-cutover",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
        grants=(EntityGrant(entity_ref=_PERSON_ENTITY, allow_account_registry=True),),
    )
    session = Session(connection, join_transaction_mode="rollback_only")
    try:
        AccountRegistryOperator(lambda: session).apply(plan, principal=principal, session=session)
    finally:
        session.close()


def _audit_payload(account_ref: UUID, mapping_sha256: str) -> dict[str, object]:
    return {
        "batch_ref": str(_BATCH_REF),
        "source_sha256": _SOURCE_SHA256,
        "mapping_sha256": mapping_sha256,
        "candidate_count": _EXPECTED_COUNT,
        "amount_minor": _EXPECTED_TOTAL_MINOR,
        "legacy_entity_ref": str(_LEGACY_ENTITY),
        "personal_entity_ref": str(_PERSON_ENTITY),
        "business_unit_ref": str(_PERSON_UNIT),
        "managed_account_ref": str(account_ref),
    }


def _require_completed_audit(
    connection: Connection, *, account_ref: UUID, mapping_sha256: str
) -> None:
    count = connection.execute(
        text(
            "SELECT count(*) FROM public.audit_event "
            "WHERE action='candidate.personal_alipay_reattribution' "
            "AND rule_version='ledgerbridge.personal-alipay-reattribution.v1' "
            "AND payload=CAST(:payload AS jsonb)"
        ),
        {
            "payload": json.dumps(
                _audit_payload(account_ref, mapping_sha256),
                sort_keys=True,
                separators=(",", ":"),
            )
        },
    ).scalar_one()
    if count != 1:
        raise PersonalAlipayCutoverError("completed cutover audit receipt conflicts")


def execute_cutover(
    connection: Connection,
    *,
    pairs: tuple[CandidatePair, ...],
    account_ref: UUID,
    evidence_ref: UUID,
    mapping_sha256: str,
) -> None:
    statuses = []
    for pair in pairs:
        old = _current_candidate(connection, pair.legacy_ref)
        new = _current_candidate(connection, pair.replacement_ref)
        if (
            old["entity_id"] != _LEGACY_ENTITY
            or new["entity_id"] != _PERSON_ENTITY
            or old["amount_minor"] != pair.amount_minor
            or new["amount_minor"] != pair.amount_minor
            or old["accounting_month"] != pair.accounting_month
            or new["accounting_month"] != pair.accounting_month
        ):
            raise PersonalAlipayCutoverError("candidate pair identity changed")
        statuses.append((old["status"], new["status"]))
    if all(value == ("IGNORED", "CONFIRMED") for value in statuses):
        _register_account(connection, account_ref, evidence_ref)
        _require_completed_audit(connection, account_ref=account_ref, mapping_sha256=mapping_sha256)
        return
    if any(value != ("PENDING", "PENDING") for value in statuses):
        raise PersonalAlipayCutoverError("candidate cutover state is not uniformly pending")
    _register_account(connection, account_ref, evidence_ref)
    for pair in pairs:
        new = _current_candidate(connection, pair.replacement_ref)
        _transition(
            connection,
            new,
            "CONFIRM",
            "user confirmed personal Alipay account 2 receipt cohort",
        )
        old = _current_candidate(connection, pair.legacy_ref)
        _transition(
            connection,
            old,
            "IGNORE",
            "replaced by owner-scoped personal Alipay account 2 candidate",
        )
    connection.execute(
        text(
            "SELECT public.append_audit_event(:actor,:action,:reason,:rule,CAST(:payload AS jsonb))"
        ),
        {
            "actor": "user:maiziwheat520",
            "action": "candidate.personal_alipay_reattribution",
            "reason": "user identified reviewed annual statement as personal Alipay account 2",
            "rule": "ledgerbridge.personal-alipay-reattribution.v1",
            "payload": json.dumps(
                _audit_payload(account_ref, mapping_sha256),
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ).scalar_one()
    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def _verify_production_gate(
    connection: Connection, *, backup_receipt: Path, restore_receipt: Path, revision: str
) -> None:
    backup = _read_private_json(backup_receipt)
    restore = _read_private_json(restore_receipt)
    state = connection.execute(
        text(
            "SELECT current_database(),"
            "(SELECT version_num FROM public.alembic_version),"
            "(SELECT count(*) FROM public.journal_entry),"
            "(SELECT count(*) FROM public.posting)"
        )
    ).one()
    if state != ("ledgerbridge", _EXPECTED_SCHEMA, 0, 0):
        raise PersonalAlipayCutoverError("production database gate failed")
    if (
        backup.get("format") != "ledgerbridge-encrypted-backup-v3"
        or backup.get("revision") != revision
        or restore.get("format") != "ledgerbridge-restore-rehearsal-v3"
        or restore.get("revision") != revision
        or restore.get("status") != "passed"
        or restore.get("production_unchanged") is not True
        or restore.get("isolated_resources_removed") is not True
        or restore.get("backup") != backup_receipt.parent.name
    ):
        raise PersonalAlipayCutoverError("backup or isolated restore receipt is invalid")


def _verify_final_state(connection: Connection, *, pairs: tuple[CandidatePair, ...]) -> None:
    values = connection.execute(
        text(
            "SELECT "
            "(SELECT count(*) FROM public.candidate_revision r JOIN LATERAL "
            " (SELECT revision FROM public.candidate_revision x "
            "  WHERE x.candidate_id=r.candidate_id "
            "  ORDER BY revision DESC LIMIT 1) latest ON latest.revision=r.revision "
            " WHERE r.status='CONFIRMED' AND r.candidate_id=ANY(:new_ids)),"
            "(SELECT count(*) FROM public.candidate_revision r JOIN LATERAL "
            " (SELECT revision FROM public.candidate_revision x "
            "  WHERE x.candidate_id=r.candidate_id "
            "  ORDER BY revision DESC LIMIT 1) latest ON latest.revision=r.revision "
            " WHERE r.status='IGNORED' AND r.candidate_id=ANY(:old_ids)),"
            "(SELECT count(*) FROM public.journal_entry),"
            "(SELECT count(*) FROM public.posting)"
        ),
        {
            "new_ids": [pair.replacement_ref for pair in pairs],
            "old_ids": [pair.legacy_ref for pair in pairs],
        },
    ).one()
    if values != (_EXPECTED_COUNT, _EXPECTED_COUNT, 0, 0):
        raise PersonalAlipayCutoverError("final candidate or zero-posting invariant failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--execute-reviewed-cutover-v1", action="store_true")
    parser.add_argument("--rehearse-and-rollback-v1", action="store_true")
    parser.add_argument("--production-revision")
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    args = parser.parse_args()
    if args.execute_reviewed_cutover_v1 and args.rehearse_and_rollback_v1:
        raise PersonalAlipayCutoverError("execution modes are mutually exclusive")
    if args.execute_reviewed_cutover_v1 and not (
        args.production_revision and args.backup_receipt and args.restore_receipt
    ):
        raise PersonalAlipayCutoverError("production execution requires backup and restore gates")
    database_url = os.environ.get("LEDGERBRIDGE_IMPORT_DATABASE_URL")
    if not database_url:
        raise PersonalAlipayCutoverError("LEDGERBRIDGE_IMPORT_DATABASE_URL is required")
    account_ref = _ACCOUNT_REF
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        args.working_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        if args.source.resolve().parent != args.working_directory.resolve():
            raise PersonalAlipayCutoverError("source must be inside the private working directory")
        with engine.connect() as connection:
            manifest, pairs = build_source_manifest(
                connection, source_path=args.source.resolve(), account_ref=account_ref
            )
        source_manifest = args.working_directory / "source-manifest.json"
        prepared_manifest = args.working_directory / "prepared-manifest.json"
        mapping_receipt = args.working_directory / "candidate-reattribution-map.json"
        _write_private(source_manifest, manifest.model_dump(mode="json"))
        mapping_payload = {
            "schema_version": "ledgerbridge.personal-alipay-reattribution-map.v1",
            "batch_ref": str(_BATCH_REF),
            "source_sha256": _SOURCE_SHA256,
            "pairs": [
                {
                    "legacy_candidate_ref": str(pair.legacy_ref),
                    "replacement_candidate_ref": str(pair.replacement_ref),
                    "amount_minor": pair.amount_minor,
                    "accounting_month": pair.accounting_month.isoformat(),
                }
                for pair in pairs
            ],
        }
        _write_private(mapping_receipt, mapping_payload)
        mapping_sha256 = _digest(mapping_receipt)
        if prepared_manifest.exists():
            prepared, _ = load_prepared_manifest(prepared_manifest)
            if (
                prepared.batch_ref != manifest.batch_ref
                or prepared.source_manifest_sha256 != _digest(source_manifest)
                or tuple(item.candidate_ref for item in prepared.candidates)
                != tuple(item.candidate_ref for item in manifest.candidates)
            ):
                raise PersonalAlipayCutoverError("existing prepared manifest conflicts")
        else:
            prepare_source_manifest(
                source_manifest,
                key_file=args.key_file.resolve(),
                artifact_root=args.artifact_root.resolve(),
                prepared_manifest_path=prepared_manifest,
            )
        should_cutover = args.execute_reviewed_cutover_v1 or args.rehearse_and_rollback_v1
        if should_cutover:
            connection = engine.connect()
            transaction = connection.begin()
            try:
                if args.execute_reviewed_cutover_v1:
                    _verify_production_gate(
                        connection,
                        backup_receipt=args.backup_receipt.resolve(),
                        restore_receipt=args.restore_receipt.resolve(),
                        revision=args.production_revision,
                    )
                result = import_prepared_manifest_in_transaction(connection, prepared_manifest)
                if result.candidate_count != _EXPECTED_COUNT:
                    raise PersonalAlipayCutoverError("replacement import count is invalid")
                execute_cutover(
                    connection,
                    pairs=pairs,
                    account_ref=account_ref,
                    evidence_ref=manifest.evidence[0].evidence_ref,
                    mapping_sha256=mapping_sha256,
                )
                _verify_final_state(connection, pairs=pairs)
                if args.rehearse_and_rollback_v1:
                    transaction.rollback()
                else:
                    transaction.commit()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
            finally:
                connection.close()
        print(
            "PERSONAL_ALIPAY_CUTOVER_OK "
            f"prepared={_EXPECTED_COUNT} executed={args.execute_reviewed_cutover_v1} "
            f"rehearsed={args.rehearse_and_rollback_v1}"
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
