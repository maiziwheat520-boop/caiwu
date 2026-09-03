from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.company_transaction_classification import (
    CompanyTransactionClassification,
)
from scripts.backfill_company_transaction_classifications import Transaction, classify
from scripts.backup_restore import MYBANK_CUTOVER_SCHEMA_REVISIONS

MIGRATION = Path("alembic/versions/20260903_0037_company_transaction_classification.py")


def test_migration_exposes_only_narrow_role_specific_functions() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "Revision ID: 20260903_0037" in source
    assert "Revises: 20260903_0036" in source
    assert "CREATE TABLE public.company_transaction_classification" in source
    assert "company_transaction_classification_append_only" in source
    assert "internal_import.seed_company_transaction_classification" in source
    assert "internal_command.review_company_transaction_classification" in source
    assert "internal_read.list_company_transaction_classifications_as_of" in source
    assert "internal_read.get_company_transaction_classification_summary_as_of" in source
    assert "TO ledgerbridge_worker" in source
    assert "TO ledgerbridge_api" in source
    assert source.count("TO ledgerbridge_reader") == 2
    assert "irreversible in production" in source
    assert "20260903_0037" in MYBANK_CUTOVER_SCHEMA_REVISIONS


def test_approved_rule_precedence_and_exact_related_party_match() -> None:
    companies = frozenset({"宁波薇旭酒店管理有限公司"})

    assert (
        classify(Transaction(UUID(int=1), "宁波薇旭酒店管理有限公司", "陈明哲转账"), companies)
        == "INTERNAL_TRANSFER"
    )
    assert classify(Transaction(UUID(int=2), "陈明哲", "转入"), companies) == (
        "RELATED_PARTY_CURRENT"
    )
    assert classify(Transaction(UUID(int=3), "陈明哲贸易", "转入"), companies) is None
    assert classify(
        Transaction(UUID(int=4), "支付宝支付科技有限公司", "飞猪房款结算"), companies
    ) == ("PLATFORM_ROOM_REVENUE")


def test_pending_wire_item_cannot_claim_a_category() -> None:
    with pytest.raises(ValidationError, match="pending classification"):
        CompanyTransactionClassification.model_validate(
            {
                "transaction_ref": UUID(int=1),
                "entity_ref": UUID(int=2),
                "occurred_at": "2026-09-01T00:00:00Z",
                "amount_minor": 100,
                "currency": "CNY",
                "counterparty_name": "待核对",
                "transaction_name": "转账",
                "status": "PENDING",
                "category_code": "FINANCING",
                "cashflow_role": "NON_OPERATING",
                "revision": 1,
                "source": "AUTO_RULE",
                "rule_version": "company-bank-classification.2026-09.v1",
            }
        )
