# ruff: noqa: E501
"""Atomically apply the user-approved P01-P07 historical classifications."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast
from uuid import UUID, uuid5

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

RULE_VERSION = "company-bank-classification.2026-09.v1"
BACKFILL_VERSION = "ledgerbridge.historical-classification-backfill.2026-09.v1"
NAMESPACE = UUID("46207179-a491-579f-9f9e-92b5824d86fd")
BATCH_REF = uuid5(NAMESPACE, BACKFILL_VERSION)
EXPECTED_SCHEMA = "20260904_0039"
ACTOR = "user:maiziwheat520"


class BackfillError(RuntimeError):
    """The reviewed classification batch failed closed."""


@dataclass(frozen=True, slots=True)
class CompanyBatch:
    code: str
    category: str
    expected_count: int
    expected_total: int
    expected_digest: str
    selector: str


@dataclass(frozen=True, slots=True)
class RulePlan:
    code: str
    rule_id: UUID
    rule_key: str
    source_kind: str
    account_key: str | None
    source_system_id: str | None
    business_unit_label: str
    item_label: str
    pattern: str
    effective_from: date
    expected_count: int
    expected_total: int
    expected_digest: str


COMPANY_BATCHES = (
    CompanyBatch(
        "P03",
        "RELATED_PARTY_CURRENT",
        50,
        213_465_416,
        "6a401854bee4de1a6fae4386136f2ddc50e903460dc33dd47262df8347413a82",
        "t.amount_minor>0 AND regexp_replace(concat_ws('|',t.counterparty_name,t.transaction_name),'\\s','','g') LIKE '%陈明哲%'",
    ),
    CompanyBatch(
        "P04",
        "PAYROLL",
        38,
        -136_599_356,
        "b3a7c9ed79b875bd930b3d76deeaa8d817c424fbb4617e3e2007468439607736",
        "regexp_replace(concat_ws('|',t.counterparty_name,t.transaction_name),'\\s','','g') LIKE '%企业代发过渡户%' AND regexp_replace(concat_ws('|',t.counterparty_name,t.transaction_name),'\\s','','g') LIKE '%批量代发%'",
    ),
    CompanyBatch(
        "P06",
        "FINANCING",
        23,
        -32_414_036,
        "c5ee59084504de2ac3d7304a7c0f38f88bb6036ec2f734788c593653f9c48988",
        "t.amount_minor<0 AND regexp_replace(concat_ws('|',t.counterparty_name,t.transaction_name),'\\s','','g') LIKE '%浙江网商银行%' AND regexp_replace(concat_ws('|',t.counterparty_name,t.transaction_name),'\\s','','g') LIKE '%贷款还款%'",
    ),
)

RULES = (
    RulePlan(
        "P01",
        uuid5(NAMESPACE, "weixu-personal-alipay-account2-fliggy"),
        "weixu_personal_alipay_account2_fliggy",
        "CANDIDATE",
        None,
        "alipay_export_account2",
        "薇旭",
        "飞猪",
        "飞猪国际旅行社",
        date(2026, 1, 1),
        1574,
        21_362_070,
        "fce1a691e2b9bbd727e9d602aefc96bc4eb7f0d7e235bf4d2f6bd8797e292778",
    ),
    RulePlan(
        "P02",
        UUID("44c39d6b-1d89-48c4-9613-d1457c5a7655"),
        "jingyi_meituan",
        "BANK_TRANSACTION",
        "ccb:personal:7564",
        None,
        "景怡",
        "美团",
        "钱袋宝",
        date(2025, 12, 7),
        49,
        247_106_114,
        "fd9a763e8abf7951160b3ba4d516d575e5fcd6bef3b8f1aa2a29e6da054f5430",
    ),
    RulePlan(
        "P05",
        UUID("c51451c1-71fa-4b1d-a37f-d95a6c10cac1"),
        "weixu_bank_receipts",
        "BANK_TRANSACTION",
        "abc:personal:7177",
        None,
        "薇旭",
        "银行收款",
        "乐刷",
        date(2026, 1, 1),
        28,
        321_834,
        "bf3f735cac0f7d8312b615b5d6d1281b1218e5a597b474a43004c925067d8977",
    ),
    RulePlan(
        "P07",
        UUID("404ba28f-08d2-437a-ac85-9e755b95a36a"),
        "weixu_ctrip",
        "BANK_TRANSACTION",
        "boc:personal:1075",
        None,
        "薇旭",
        "携程",
        "赫[[:space:]]*程|携[[:space:]]*程",
        date(2025, 9, 1),
        11,
        12_532_545,
        "4549b442642292fcb506f39cffef5bbd6e834000c8cc4b7708a121472973098f",
    ),
)


def operation_id(batch: CompanyBatch, transaction_ref: UUID) -> UUID:
    return uuid5(NAMESPACE, f"{batch.code}:{transaction_ref}")


def company_digest(rows: list[dict[str, object]]) -> str:
    rendered = "\n".join(
        str(
            row.get("canonical")
            or "|".join(
                (
                    str(row["transaction_ref"]),
                    str(row["entity_id"]),
                    str(row["account_key"]),
                    str(row["amount_minor"]),
                    str(row["occurred_at"]),
                    str(row["counterparty_name"] or ""),
                    str(row["transaction_name"] or ""),
                )
            )
        )
        for row in sorted(rows, key=lambda value: str(value["transaction_ref"]))
    )
    return hashlib.sha256(rendered.encode()).hexdigest()


def load_company_batch(connection: Connection, batch: CompanyBatch) -> list[dict[str, object]]:
    query = f"""
        SELECT t.transaction_ref,ma.entity_id,ma.account_key,t.amount_minor,t.occurred_at,
               t.counterparty_name,t.transaction_name,c.operation_id,c.status,c.category_code,
               t.transaction_ref::text||'|'||ma.entity_id::text||'|'||ma.account_key||'|'||
               t.amount_minor::text||'|'||t.occurred_at::text||'|'||
               coalesce(t.counterparty_name,'')||'|'||coalesce(t.transaction_name,'') AS canonical
          FROM public.bank_statement_transaction t
          JOIN public.managed_account ma ON ma.managed_account_ref=t.managed_account_ref
          LEFT JOIN LATERAL (SELECT * FROM public.company_transaction_classification x
            WHERE x.transaction_ref=t.transaction_ref ORDER BY x.revision DESC LIMIT 1) c ON true
         WHERE ma.owner_kind='COMPANY' AND ({batch.selector})
           AND EXISTS (SELECT 1 FROM public.bank_statement_observation o
             JOIN LATERAL (SELECT status FROM public.bank_statement_review r
               WHERE r.statement_ref=o.statement_ref ORDER BY revision DESC LIMIT 1) review ON true
             WHERE o.transaction_ref=t.transaction_ref AND review.status='CONFIRMED')
         ORDER BY t.transaction_ref
    """
    selected: list[dict[str, object]] = []
    for raw in connection.execute(text(query)).mappings():
        row = dict(raw)
        expected_operation = operation_id(batch, cast(UUID, row["transaction_ref"]))
        if row["operation_id"] is None or (
            row["operation_id"],
            row["status"],
            row["category_code"],
        ) == (expected_operation, "CONFIRMED", batch.category):
            selected.append(row)
    if (
        len(selected),
        sum(cast(int, row["amount_minor"]) for row in selected),
        company_digest(selected),
    ) != (batch.expected_count, batch.expected_total, batch.expected_digest):
        raise BackfillError(f"{batch.code} exact approved set changed")
    return selected


def desired_rule_tuple(plan: RulePlan) -> tuple[object, ...]:
    return (
        plan.rule_id,
        plan.rule_key,
        "ACTIVE",
        plan.source_kind,
        plan.account_key,
        plan.source_system_id,
        "INCOME",
        plan.business_unit_label,
        plan.item_label,
        plan.pattern,
        "CREDIT",
        plan.effective_from,
        None,
    )


def ensure_rule(connection: Connection, plan: RulePlan) -> int:
    row = connection.execute(
        text("""
        SELECT rule_id,revision,rule_key,status,source_kind,account_key,source_system_id,
               flow_kind,business_unit_label,item_label,match_pattern,amount_direction,
               effective_from,effective_to FROM public.cash_reconciliation_rule
         WHERE rule_id=:rule ORDER BY revision DESC LIMIT 1 FOR UPDATE
    """),
        {"rule": plan.rule_id},
    ).one_or_none()
    if row is not None and (tuple(row[:1]) + tuple(row[2:])) == desired_rule_tuple(plan):
        return int(row.revision)
    if row is None:
        if plan.code != "P01":
            raise BackfillError(f"{plan.code} expected existing rule is missing")
        revision = 1
    else:
        known_old = {
            "P02": (date(2026, 1, 1), "钱袋宝"),
            "P07": (date(2026, 1, 1), "赫程|携程"),
        }.get(plan.code)
        if (
            known_old is None
            or int(row.revision) != 1
            or (row.effective_from, row.match_pattern) != known_old
        ):
            raise BackfillError(f"{plan.code} existing rule conflicts with approved revision")
        revision = 2
    payload = {
        "batch_ref": str(BATCH_REF),
        "rule_id": str(plan.rule_id),
        "revision": revision,
        "rule_key": plan.rule_key,
        "status": "ACTIVE",
        "source_kind": plan.source_kind,
        "account_key": plan.account_key,
        "source_system_id": plan.source_system_id,
        "flow_kind": "INCOME",
        "business_unit_label": plan.business_unit_label,
        "item_label": plan.item_label,
        "match_pattern": plan.pattern,
        "amount_direction": "CREDIT",
        "effective_from": plan.effective_from.isoformat(),
        "effective_to": None,
    }
    audit = connection.execute(
        text(
            "SELECT public.append_audit_event(:actor,:action,:reason,:rule,CAST(:payload AS jsonb))"
        ),
        {
            "actor": ACTOR,
            "action": "cash_reconciliation_rule.backfill",
            "reason": "user approved high-confidence historical classification batch P01-P07",
            "rule": BACKFILL_VERSION,
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    ).scalar_one()
    connection.execute(
        text("""
        INSERT INTO public.cash_reconciliation_rule(rule_id,revision,rule_key,status,
          source_kind,account_key,source_system_id,flow_kind,business_unit_label,item_label,
          match_pattern,amount_direction,effective_from,effective_to,audit_event_id)
        VALUES(:rule_id,:revision,:rule_key,'ACTIVE',:source_kind,:account_key,:source_system_id,
          'INCOME',:business_unit_label,:item_label,:match_pattern,'CREDIT',:effective_from,NULL,:audit)
    """),
        {**payload, "effective_from": plan.effective_from, "audit": audit},
    )
    return revision


def rule_hits(connection: Connection, plan: RulePlan) -> tuple[int, int, list[str]]:
    if plan.source_kind == "CANDIDATE":
        sql = """WITH latest AS (SELECT DISTINCT ON(candidate_id) * FROM public.candidate_revision ORDER BY candidate_id,revision DESC)
          SELECT count(*),coalesce(sum(l.amount_minor),0),array_agg(
            'CANDIDATE:'||l.candidate_id::text||'|'||l.amount_minor||'|'||
            l.accounting_month||'|'||s.source_system_id ORDER BY l.candidate_id)
          FROM latest l JOIN public.candidate_source s ON s.candidate_id=l.candidate_id
          WHERE s.source_system_id=:source AND l.status='CONFIRMED' AND l.accounting_month>=:from_date
            AND concat_ws(' ',l.summary,s.display_label)~*:pattern AND l.amount_minor>0"""
        params = {
            "source": plan.source_system_id,
            "from_date": plan.effective_from,
            "pattern": plan.pattern,
        }
    else:
        sql = """WITH reviews AS (SELECT DISTINCT ON(statement_ref) statement_ref,status FROM public.bank_statement_review ORDER BY statement_ref,revision DESC)
          SELECT count(*),coalesce(sum(t.amount_minor),0),array_agg(
            'BANK_TRANSACTION:'||t.transaction_ref::text||'|'||t.amount_minor||'|'||
            (t.occurred_at AT TIME ZONE 'Asia/Shanghai')::date||'|'||m.account_key
            ORDER BY t.transaction_ref)
          FROM public.bank_statement_transaction t JOIN public.managed_account m ON m.managed_account_ref=t.managed_account_ref
          WHERE m.account_key=:account AND (t.occurred_at AT TIME ZONE 'Asia/Shanghai')::date>=:from_date
            AND concat_ws(' ',t.counterparty_name,t.transaction_name,t.counterparty_institution)~*:pattern AND t.amount_minor>0
            AND EXISTS(SELECT 1 FROM public.bank_statement_observation o JOIN reviews r USING(statement_ref) WHERE o.transaction_ref=t.transaction_ref AND r.status='CONFIRMED')"""
        params = {
            "account": plan.account_key,
            "from_date": plan.effective_from,
            "pattern": plan.pattern,
        }
    row = connection.execute(text(sql), params).one()
    return int(row[0]), int(row[1]), list(row[2] or [])


def completed(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM public.audit_event WHERE action='classification.backfill.p01_p07' AND rule_version=:rule AND payload->>'batch_ref'=:batch)"
            ),
            {"rule": BACKFILL_VERSION, "batch": str(BATCH_REF)},
        ).scalar_one()
    )


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackfillError("production proof must be a JSON object")
    return value


def verify_live_inventory(connection: Connection, value: object) -> None:
    if not isinstance(value, dict) or value.get("schema_revision") != EXPECTED_SCHEMA:
        raise BackfillError("backup inventory schema is invalid")
    row_counts = value.get("row_counts")
    if not isinstance(row_counts, dict) or not row_counts:
        raise BackfillError("backup inventory counts are invalid")
    for table_name, expected in row_counts.items():
        if (
            not isinstance(table_name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", table_name) is None
            or not isinstance(expected, int)
        ):
            raise BackfillError("backup inventory entry is invalid")
        actual = connection.execute(
            text(f'SELECT count(*) FROM public."{table_name}"')
        ).scalar_one()
        if actual != expected:
            raise BackfillError("live database no longer matches the backup")
    summaries = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM public.candidate),"
            "(SELECT count(*) FROM public.audit_event),"
            "(SELECT count(*) FROM public.candidate_revision r WHERE r.status='PENDING' "
            "AND NOT EXISTS (SELECT 1 FROM public.candidate_revision newer "
            "WHERE newer.candidate_id=r.candidate_id AND newer.revision>r.revision))"
        )
    ).one()
    if tuple(summaries) != (
        value.get("candidate_total"),
        value.get("audit_events"),
        value.get("latest_pending_candidates"),
    ):
        raise BackfillError("backup inventory summary is invalid")


def verify_production_gate(
    connection: Connection,
    *,
    backup_directory: Path,
    restore_receipt: Path,
    deployed_revision_file: Path,
    completed_replay: bool,
) -> None:
    backup_directory = backup_directory.resolve(strict=True)
    restore_receipt = restore_receipt.resolve(strict=True)
    if backup_directory.is_symlink() or not backup_directory.is_dir():
        raise BackfillError("backup directory is invalid")
    backup_receipt = backup_directory / "backup.json"
    ciphertext = backup_directory / "ledgerbridge-backup.tar.gpg"
    checksum = backup_directory / "SHA256SUMS"
    if (
        restore_receipt.parent != backup_directory
        or ciphertext.is_symlink()
        or checksum.is_symlink()
        or not ciphertext.is_file()
        or not checksum.is_file()
    ):
        raise BackfillError("backup proof files are invalid")
    backup = read_json(backup_receipt)
    restore = read_json(restore_receipt)
    deployed_revision = deployed_revision_file.read_text(encoding="ascii").strip()
    ciphertext_sha256 = file_digest(ciphertext)
    state = connection.execute(
        text(
            "SELECT current_database(),current_user,session_user,"
            "current_setting('transaction_read_only'),pg_is_in_recovery(),"
            "(SELECT version_num FROM public.alembic_version),"
            "(SELECT count(*) FROM public.journal_entry),"
            "(SELECT count(*) FROM public.posting)"
        )
    ).one()
    if tuple(state) != (
        "ledgerbridge",
        "ledgerbridge_owner",
        "ledgerbridge_owner",
        "off",
        False,
        EXPECTED_SCHEMA,
        0,
        0,
    ):
        raise BackfillError("production database gate failed")
    if (
        backup.get("format") != "ledgerbridge-encrypted-backup-v3"
        or backup.get("revision") != deployed_revision
        or re.fullmatch(r"[0-9a-f]{40}", deployed_revision) is None
        or backup.get("ciphertext") != ciphertext.name
        or not hmac.compare_digest(str(backup.get("ciphertext_sha256")), ciphertext_sha256)
        or not hmac.compare_digest(
            checksum.read_text(encoding="utf-8"),
            f"{ciphertext_sha256}  {ciphertext.name}\n",
        )
        or restore.get("format") != "ledgerbridge-restore-rehearsal-v3"
        or restore.get("revision") != deployed_revision
        or restore.get("status") != "passed"
        or restore.get("source_format") != "v3"
        or restore.get("production_unchanged") is not True
        or restore.get("isolated_resources_removed") is not True
        or restore.get("backup") != backup_receipt.parent.name
        or restore.get("unpaired_database_observation_fields") != []
    ):
        raise BackfillError("backup or isolated restore receipt is invalid")
    compared = restore.get("database_compared_fields")
    source = restore.get("source_database_metadata")
    restored = restore.get("post_restore_database_observations")
    if (
        not isinstance(compared, list)
        or "cutover_inventory" not in compared
        or not isinstance(source, dict)
        or not isinstance(restored, dict)
        or source.get("cutover_inventory") != restored.get("cutover_inventory")
        or restore.get("source_artifact_control")
        != restore.get("post_restore_artifact_observations")
    ):
        raise BackfillError("isolated restore comparison is invalid")
    if not completed_replay:
        verify_live_inventory(connection, source.get("cutover_inventory"))


def apply(connection: Connection) -> dict[str, object]:
    company_rows = {batch.code: load_company_batch(connection, batch) for batch in COMPANY_BATCHES}
    rule_revisions = {plan.code: ensure_rule(connection, plan) for plan in RULES}
    hit_refs: set[str] = set()
    for plan in RULES:
        count, total, canonical_rows = rule_hits(connection, plan)
        digest = hashlib.sha256("\n".join(canonical_rows).encode()).hexdigest()
        if (count, total, digest) != (
            plan.expected_count,
            plan.expected_total,
            plan.expected_digest,
        ):
            raise BackfillError(f"{plan.code} rule hit set changed")
        refs = {row.split("|", 1)[0] for row in canonical_rows}
        if hit_refs.intersection(refs):
            raise BackfillError("approved rule hit sets overlap")
        hit_refs.update(refs)
    created = 0
    for batch in COMPANY_BATCHES:
        for row in company_rows[batch.code]:
            receipt = connection.execute(
                text(
                    "SELECT internal_import.seed_company_transaction_classification(:transaction,:operation,'CONFIRMED',:category,:actor,:reason,:rule)"
                ),
                {
                    "transaction": row["transaction_ref"],
                    "operation": operation_id(batch, cast(UUID, row["transaction_ref"])),
                    "category": batch.category,
                    "actor": ACTOR,
                    "reason": f"user approved high-confidence historical classification {batch.code}",
                    "rule": RULE_VERSION,
                },
            ).scalar_one()
            created += int(bool(receipt["created"]))
    payload = {
        "batch_ref": str(BATCH_REF),
        "company_counts": {b.code: b.expected_count for b in COMPANY_BATCHES},
        "company_totals": {b.code: b.expected_total for b in COMPANY_BATCHES},
        "company_digests": {b.code: b.expected_digest for b in COMPANY_BATCHES},
        "rule_revisions": rule_revisions,
        "rule_counts": {r.code: r.expected_count for r in RULES},
        "rule_totals": {r.code: r.expected_total for r in RULES},
        "rule_digests": {r.code: r.expected_digest for r in RULES},
        "journal_entries": 0,
        "postings": 0,
    }
    existing = connection.execute(
        text(
            "SELECT count(*) FROM public.audit_event WHERE action='classification.backfill.p01_p07' AND rule_version=:rule AND payload=CAST(:payload AS jsonb)"
        ),
        {"rule": BACKFILL_VERSION, "payload": json.dumps(payload, sort_keys=True)},
    ).scalar_one()
    if existing == 0:
        connection.execute(
            text(
                "SELECT public.append_audit_event(:actor,'classification.backfill.p01_p07',:reason,:rule,CAST(:payload AS jsonb))"
            ),
            {
                "actor": ACTOR,
                "reason": "user approved all high-confidence historical classifications P01-P07",
                "rule": BACKFILL_VERSION,
                "payload": json.dumps(payload, sort_keys=True),
            },
        ).scalar_one()
    elif existing != 1:
        raise BackfillError("batch audit receipt conflicts")
    counts = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM public.journal_entry),(SELECT count(*) FROM public.posting)"
        )
    ).one()
    if tuple(counts) != (0, 0):
        raise BackfillError("classification backfill must not create postings")
    payload["created_company_classifications"] = created
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-approved-backfill-v1", action="store_true")
    parser.add_argument("--rehearse-and-rollback-v1", action="store_true")
    parser.add_argument("--backup-directory", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--deployed-revision-file", type=Path)
    args = parser.parse_args()
    if args.execute_approved_backfill_v1 == args.rehearse_and_rollback_v1:
        raise BackfillError("select exactly one execution mode")
    url = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL", "").strip()
    if not url or make_url(url).username != "ledgerbridge_owner":
        raise BackfillError("owner migration database URL is required")
    if args.execute_approved_backfill_v1 and not all(
        (args.backup_directory, args.restore_receipt, args.deployed_revision_file)
    ):
        raise BackfillError("production execution requires backup and restore proof")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                if args.execute_approved_backfill_v1:
                    verify_production_gate(
                        connection,
                        backup_directory=cast(Path, args.backup_directory),
                        restore_receipt=cast(Path, args.restore_receipt),
                        deployed_revision_file=cast(Path, args.deployed_revision_file),
                        completed_replay=completed(connection),
                    )
                result = apply(connection)
                connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                if args.rehearse_and_rollback_v1:
                    transaction.rollback()
                else:
                    transaction.commit()
            except BaseException:
                if transaction.is_active:
                    transaction.rollback()
                raise
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
