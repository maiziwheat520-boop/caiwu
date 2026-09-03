from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.company_transaction_classification import (
    CompanyTransactionClassification,
)
from scripts.backfill_company_transaction_classifications import Transaction, classify
from scripts.backup_restore import (
    COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS,
    COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_RESULTS,
    COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES,
    COMPANY_TRANSACTION_CLASSIFICATION_REQUIRED_COLUMNS,
    COMPANY_TRANSACTION_CLASSIFICATION_SECURITY_DEFINER_FUNCTIONS,
    COMPANY_TRANSACTION_CLASSIFICATION_SECURITY_SQL,
    COMPANY_TRANSACTION_CLASSIFICATION_TABLE,
    COMPANY_TRANSACTION_CLASSIFICATION_TRIGGER_CONTRACT,
    MYBANK_CUTOVER_SCHEMA_REVISIONS,
    R1_ROLES,
    BackupError,
    _validate_company_transaction_classification_security,
)

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
    assert "company_transaction_classification_row_count" in (
        COMPANY_TRANSACTION_CLASSIFICATION_SECURITY_SQL
    )


def _restore_metadata() -> dict[str, object]:
    owner = "ledgerbridge_owner"
    functions = [
        {
            "schema": schema,
            "name": name,
            "identity_arguments": arguments,
            "result": COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_RESULTS[(schema, name)],
            "owner": owner,
            "security_definer": (
                (schema, name) in COMPANY_TRANSACTION_CLASSIFICATION_SECURITY_DEFINER_FUNCTIONS
            ),
            "proconfig": ["search_path=pg_catalog"],
        }
        for (schema, name), arguments in (
            COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES.items()
        )
    ]
    return {
        "database_owner": owner,
        "r1_role_matrix": [{"role": role} for role in R1_ROLES],
        "company_transaction_classification_row_count": 1033,
        "company_transaction_classification_table": {
            "table": COMPANY_TRANSACTION_CLASSIFICATION_TABLE,
            "owner": owner,
            "kind": "r",
        },
        "company_transaction_classification_functions": functions,
        "company_transaction_classification_triggers": [
            {
                "name": name,
                "enabled": "O",
                "trigger_type": trigger_type,
                "function_schema": "public",
                "function_name": function_name,
            }
            for name, (function_name, trigger_type) in (
                COMPANY_TRANSACTION_CLASSIFICATION_TRIGGER_CONTRACT.items()
            )
        ],
        "company_transaction_classification_columns": [
            {"column": column, "data_type": data_type, "not_null": not_null}
            for column, (data_type, not_null) in (
                COMPANY_TRANSACTION_CLASSIFICATION_REQUIRED_COLUMNS.items()
            )
        ],
        "company_transaction_classification_constraints": [
            {
                "name": f"constraint_{kind}",
                "type": kind,
                "validated": True,
                "deferrable": False,
                "initially_deferred": False,
                "definition": (
                    "FOREIGN KEY (audit_event_id) REFERENCES audit_event(id) ON DELETE RESTRICT"
                    if kind == "f"
                    else "fixture"
                ),
            }
            for kind in ("p", "u", "f", "c")
        ],
        "company_transaction_classification_table_acls": [
            {
                "grantee": owner,
                "privilege": "SELECT",
                "grantable": False,
            }
        ],
        "company_transaction_classification_function_acls": [
            {
                "schema": schema,
                "name": name,
                "identity_arguments": arguments,
                "grantee": grantee,
                "privilege": "EXECUTE",
                "grantable": False,
            }
            for (schema, name), arguments in (
                COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES.items()
            )
            for grantee in (
                [owner, COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS[(schema, name)]]
                if (schema, name) in COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS
                else [owner]
            )
        ],
        "company_transaction_classification_effective_table_privileges": [
            {
                "role": role,
                "select": False,
                "insert": False,
                "update": False,
                "delete": False,
            }
            for role in R1_ROLES
        ],
        "company_transaction_classification_effective_function_privileges": [
            {
                "role": role,
                "schema": schema,
                "name": name,
                "identity_arguments": arguments,
                "execute": role
                == COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS.get((schema, name)),
            }
            for role in R1_ROLES
            for (schema, name), arguments in (
                COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES.items()
            )
        ],
    }


def test_restore_inventory_covers_classification_facts_and_privileges() -> None:
    metadata = _restore_metadata()

    _validate_company_transaction_classification_security(metadata)

    privileges = metadata["company_transaction_classification_effective_table_privileges"]
    assert isinstance(privileges, list)
    drifted = {
        **metadata,
        "company_transaction_classification_effective_table_privileges": [
            {**item, "select": True} if item["role"] == "ledgerbridge_api" else item
            for item in privileges
        ],
    }
    with pytest.raises(BackupError, match="table privilege matrix"):
        _validate_company_transaction_classification_security(drifted)


def test_approved_rule_precedence_and_exact_related_party_match() -> None:
    companies = frozenset({"宁波薇旭酒店管理有限公司"})

    assert (
        classify(Transaction(UUID(int=1), "宁波薇旭酒店管理有限公司", "陈明哲转账"), companies)
        == "INTERNAL_TRANSFER"
    )
    assert classify(Transaction(UUID(int=2), "陈明哲", "转入"), companies) == (
        "RELATED_PARTY_CURRENT"
    )
    assert classify(Transaction(UUID(int=5), "陈明哲", "资金归集"), companies) == (
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
