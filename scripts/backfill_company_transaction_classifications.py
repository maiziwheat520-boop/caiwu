"""Preflight and atomically backfill the approved 2026 company-bank classification rules."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid5

from sqlalchemy import create_engine, text

RULE_VERSION = "company-bank-classification.2026-09.v1"
OPERATION_NAMESPACE = UUID("5ab5f435-b4ab-5fd7-b7be-16e09a210de0")


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_ref: UUID
    amount_minor: int
    counterparty_name: str
    transaction_name: str


def classify(item: Transaction, company_names: frozenset[str]) -> str | None:
    counterparty = item.counterparty_name.strip()
    name = item.transaction_name.strip()
    if counterparty in {"陈明哲", "陈明毅", "陈婵娟"} or "往来款" in name:
        return "RELATED_PARTY_CURRENT"
    if counterparty in company_names or "资金归集" in name:
        return "INTERNAL_TRANSFER"
    if "企业代发过渡户" in counterparty or "工资" in name or "批量代发" in name:
        return "PAYROLL"
    if "网商银行" in counterparty or "贷款" in name:
        return "FINANCING"
    if "龙发综合商店" in counterparty or "汇泽丰酒业" in counterparty or "瓶装水" in name:
        return "BOTTLED_WATER"
    if "亿洁洗涤" in counterparty or "布草" in name:
        return "LINEN_LAUNDRY"
    if (
        "支付宝支付" in counterparty
        or "飞猪" in counterparty
        or "飞猪" in name
        or "房款结算" in name
    ):
        return "PLATFORM_ROOM_REVENUE"
    if item.amount_minor > 0 and any(marker in name for marker in ("房租", "租金", "水电费")):
        return "RENTAL_INCOME"
    if "房租" in name:
        return "RENT"
    if (
        "运营费" in name
        or "唐仁酒店" in counterparty
        or "邦厨生鲜" in counterparty
        or "港泰酒店用品" in counterparty
        or "太平财产保险" in counterparty
    ):
        return "OPERATING_FEE"
    if "活期存款应付利息" in counterparty or "结息" in name:
        return "BANK_INTEREST"
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--from-date", type=date.fromisoformat, default=date(2026, 1, 1))
    parser.add_argument("--to-date-exclusive", type=date.fromisoformat, default=date(2026, 10, 1))
    parser.add_argument("--expected-unique", type=int, default=1033)
    parser.add_argument("--expected-confirmed", type=int, default=1033)
    parser.add_argument("--expected-pending", type=int, default=0)
    parser.add_argument("--actor", default="system:company-classification-backfill")
    parser.add_argument(
        "--reason",
        default="approved 2026 company bank transaction classification backfill",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database_url = os.environ.get("LEDGERBRIDGE_WORKER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("LEDGERBRIDGE_WORKER_DATABASE_URL is required")
    if args.from_date >= args.to_date_exclusive:
        raise RuntimeError("backfill date range is invalid")
    engine = create_engine(database_url)
    with engine.connect() as connection:
        company_names = frozenset(
            str(row[0]).strip()
            for row in connection.execute(
                text("SELECT name FROM public.entity WHERE entity_type::text = 'COMPANY'")
            )
            if str(row[0]).strip()
        )
        rows = connection.execute(
            text(
                """
                SELECT DISTINCT ON (transaction.transaction_ref)
                       transaction.transaction_ref,
                       transaction.amount_minor,
                       coalesce(transaction.counterparty_name, ''),
                       transaction.transaction_name
                  FROM public.bank_statement_transaction AS transaction
                  JOIN public.managed_account AS account
                    ON account.managed_account_ref = transaction.managed_account_ref
                  JOIN public.bank_statement_observation AS observation
                    ON observation.transaction_ref = transaction.transaction_ref
                  JOIN LATERAL (
                        SELECT review.status
                          FROM public.bank_statement_review AS review
                         WHERE review.statement_ref = observation.statement_ref
                         ORDER BY review.revision DESC LIMIT 1
                  ) AS latest_review ON true
                 WHERE account.owner_kind = 'COMPANY'
                   AND latest_review.status = 'CONFIRMED'
                   AND transaction.occurred_at >= :from_date
                   AND transaction.occurred_at < :to_date
                 ORDER BY transaction.transaction_ref
                """
            ),
            {"from_date": args.from_date, "to_date": args.to_date_exclusive},
        )
        transactions = tuple(
            Transaction(UUID(str(row[0])), int(row[1]), str(row[2]), str(row[3])) for row in rows
        )
        planned = tuple((item, classify(item, company_names)) for item in transactions)
        counts = Counter(category or "UNCLASSIFIED" for _, category in planned)
        confirmed = sum(value for key, value in counts.items() if key != "UNCLASSIFIED")
        pending = counts["UNCLASSIFIED"]
        actual = (len(planned), confirmed, pending)
        expected = (args.expected_unique, args.expected_confirmed, args.expected_pending)
        receipt = {
            "rule_version": RULE_VERSION,
            "from_date": args.from_date.isoformat(),
            "to_date_exclusive": args.to_date_exclusive.isoformat(),
            "unique_transactions": len(planned),
            "confirmed": confirmed,
            "pending": pending,
            "category_counts": dict(sorted(counts.items())),
            "mode": "apply" if args.apply else "preflight",
        }
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        if actual != expected:
            raise RuntimeError(f"backfill count gate failed: actual={actual}, expected={expected}")
        if not args.apply:
            return 0
        for item, category in planned:
            operation_id = uuid5(
                OPERATION_NAMESPACE,
                f"{RULE_VERSION}:{item.transaction_ref}",
            )
            connection.execute(
                text(
                    "SELECT internal_import.seed_company_transaction_classification("
                    ":transaction_ref, :operation_id, :status, :category_code, "
                    ":actor, :reason, :rule_version)"
                ),
                {
                    "transaction_ref": item.transaction_ref,
                    "operation_id": operation_id,
                    "status": "CONFIRMED" if category is not None else "PENDING",
                    "category_code": category,
                    "actor": args.actor,
                    "reason": args.reason,
                    "rule_version": RULE_VERSION,
                },
            ).scalar_one()
        connection.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
