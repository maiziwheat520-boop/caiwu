"""Owner-only, exact-fact expense corrections; preflight rolls back by default.

Input is a private, reviewed plan, never a merchant-name or amount-only matcher.
No source, payment, journal or posting mutation is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

ExpenseCategory = Literal["PAYROLL", "BOTTLED_WATER", "LINEN_LAUNDRY", "RENT", "OPERATING_FEE"]


def migration_database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL", "").strip()
    if not value or make_url(value).username != "ledgerbridge_owner":
        raise ValueError("OWNER_DATABASE_URL_REQUIRED")
    return value


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transaction_ref: UUID
    managed_account_ref: UUID
    occurred_on: date
    amount_minor: int = Field(lt=0)
    expected_revision: int = Field(gt=0)
    expected_category: ExpenseCategory
    expected_item: str = Field(min_length=1, max_length=100)
    category: ExpenseCategory
    item: str = Field(min_length=1, max_length=100)
    operation_id: UUID
    actor_ref: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def changed(self) -> Correction:
        if (self.expected_category, self.expected_item) == (self.category, self.item):
            raise ValueError("correction must change a classification")
        if any(
            not value.strip()
            for value in (self.item, self.expected_item, self.reason, self.actor_ref)
        ):
            raise ValueError("correction fields must not be blank")
        return self


def apply_correction(connection: Connection, plan: Correction) -> bool:
    if connection.execute(text("SELECT current_user")).scalar_one() != "ledgerbridge_owner":
        raise ValueError("OWNER_ROLE_REQUIRED")
    command = hashlib.sha256(plan.model_dump_json().encode()).digest()
    params = plan.model_dump()
    params["command"] = command
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": str(plan.operation_id)},
    )
    # Coordinate different operation IDs targeting the same fact as well.
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 1))"),
        {"lock_key": str(plan.transaction_ref)},
    )
    fact = (
        connection.execute(
            text("""
        SELECT managed_account_ref, amount_minor,
               (occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on
        FROM public.bank_statement_transaction WHERE transaction_ref=:transaction_ref
    """),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if fact is None or any(
        fact[key] != params[key] for key in ("managed_account_ref", "amount_minor", "occurred_on")
    ):
        raise ValueError("EXACT_SOURCE_FACT_MISMATCH")
    previous = (
        connection.execute(
            text("""
        SELECT command_sha256,transaction_ref,revision
        FROM public.company_transaction_classification WHERE operation_id=:operation_id
    """),
            params,
        )
        .mappings()
        .one_or_none()
    )
    current = (
        connection.execute(
            text("""
        SELECT * FROM public.company_transaction_classification
        WHERE transaction_ref=:transaction_ref ORDER BY revision DESC LIMIT 1 FOR UPDATE
    """),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if previous is not None:
        if (
            bytes(previous["command_sha256"]) != command
            or previous["transaction_ref"] != plan.transaction_ref
            or current is None
            or current["operation_id"] != plan.operation_id
        ):
            raise ValueError("CORRECTION_REPLAY_CONFLICT")
        return False
    if (
        current is None
        or current["status"] != "CONFIRMED"
        or current["revision"] != plan.expected_revision
        or current["category_code"] != plan.expected_category
        or current["reporting_item_code"] != plan.expected_item
    ):
        raise ValueError("CLASSIFICATION_REVISION_MISMATCH")
    target = (
        connection.execute(
            text("""
        SELECT revision,status FROM public.company_transaction_reporting_item
        WHERE category_code=:category AND item_code=:item ORDER BY revision DESC LIMIT 1
    """),
            params,
        )
        .mappings()
        .one_or_none()
    )
    if target is None or target["status"] != "ACTIVE":
        raise ValueError("TARGET_REPORTING_ITEM_INACTIVE")
    payload = {
        "transaction_ref": str(plan.transaction_ref),
        "revision": plan.expected_revision + 1,
        "status": "CONFIRMED",
        "category_code": plan.category,
        "previous_category_code": plan.expected_category,
        "previous_reporting_item_code": plan.expected_item,
        "previous_reporting_item_revision": current["reporting_item_revision"],
        "reporting_item_code": plan.item,
        "reporting_item_revision": target["revision"],
        "source": "BACKFILL",
        "rule_version": "reviewed-expense-correction.v1",
        "operation_id": str(plan.operation_id),
        "command_sha256": command.hex(),
        "source_binding": {
            key: str(params[key]) for key in ("managed_account_ref", "occurred_on", "amount_minor")
        },
    }
    params.update(
        payload=json.dumps(payload),
        revision=plan.expected_revision + 1,
        item_revision=target["revision"],
    )
    audit = connection.execute(
        text("""
        SELECT public.append_audit_event(:actor_ref,
            'company_transaction_classification.reporting_item.backfill', :reason,
            'ledgerbridge.company-transaction-classification.v1', CAST(:payload AS jsonb))
    """),
        params,
    ).scalar_one()
    params["audit"] = audit
    connection.execute(
        text("""
        INSERT INTO public.company_transaction_classification (
            transaction_ref,revision,status,category_code,reporting_item_code,
            reporting_item_revision,source,rule_version,operation_id,assertion_jti,
            actor_ref,workload_principal_ref,expected_revision,command_sha256,
            audit_event_id,classified_at)
        VALUES (:transaction_ref,:revision,'CONFIRMED',:category,:item,:item_revision,
            'BACKFILL','reviewed-expense-correction.v1',:operation_id,NULL,:actor_ref,NULL,
            :expected_revision,:command,:audit,
            (SELECT occurred_at FROM public.audit_event WHERE id=:audit))
    """),
        params,
    )
    return True


def read_plan(path: Path, expected_sha256: str) -> list[Correction]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("PLAN_HASH_MISMATCH")
    values = json.loads(raw)
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("PLAN_SIZE_INVALID")
    plans = [Correction.model_validate_json(json.dumps(value)) for value in values]
    if len({p.transaction_ref for p in plans}) != len(plans) or len(
        {p.operation_id for p in plans}
    ) != len(plans):
        raise ValueError("DUPLICATE_PLAN_IDENTITY")
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        plans = read_plan(args.plan, args.sha256)
        engine = create_engine(migration_database_url(), hide_parameters=True)
        try:
            with engine.connect() as connection, connection.begin() as transaction:
                count = sum(apply_correction(connection, plan) for plan in plans)
                if not args.apply:
                    transaction.rollback()
        finally:
            engine.dispose()
        print(
            json.dumps(
                {
                    "status": "APPLIED" if args.apply else "PREFLIGHT_ROLLED_BACK",
                    "new_revisions": count,
                    "plans": len(plans),
                }
            )
        )
    except (OSError, ValueError, RuntimeError, SQLAlchemyError):
        # Never emit model input_value, SQL parameters, connection strings or
        # driver error detail from this private financial maintenance command.
        print("CORRECTION_FAILED")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
