from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.company_transaction_classification import (
    CompanyTransactionCategory,
    CompanyTransactionClassification,
    CompanyTransactionClassificationReviewReceipt,
    CompanyTransactionClassificationReviewRequest,
    DatabaseCompanyTransactionClassificationService,
)
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from scripts.backfill_company_transaction_classifications import (
    Transaction,
    classify,
    migration_database_url,
)
from scripts.backup_restore import (
    CASH_RECONCILIATION_CLASSIFICATION_STATE_COLUMNS,
    CASH_RECONCILIATION_CLASSIFICATION_STATE_TABLES,
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
        "company_transaction_reporting_item_tables": [
            {"table": table, "owner": owner, "kind": "r"}
            for table in (
                "company_transaction_reporting_item",
                "company_transaction_reporting_item_match",
                "cash_reconciliation_adjustment_scope",
                "cash_reconciliation_projection_activation",
            )
        ],
        "company_transaction_reporting_item_triggers": [
            {
                "table": table,
                "name": name,
                "enabled": "O",
                "function_schema": "public",
                "function_name": function_name,
            }
            for table, name, function_name in (
                (
                    "company_transaction_reporting_item",
                    "company_transaction_reporting_item_append_only",
                    "r1_bank_statement_append_only",
                ),
                (
                    "company_transaction_reporting_item",
                    "validate_company_transaction_reporting_item",
                    "r1_validate_company_transaction_reporting_item",
                ),
                (
                    "company_transaction_reporting_item_match",
                    "company_transaction_reporting_item_match_append_only",
                    "r1_bank_statement_append_only",
                ),
                (
                    "company_transaction_reporting_item_match",
                    "validate_company_transaction_reporting_item_match",
                    "r1_validate_company_transaction_reporting_item_match",
                ),
                (
                    "cash_reconciliation_adjustment_scope",
                    "cash_reconciliation_adjustment_scope_append_only",
                    "r1_bank_statement_append_only",
                ),
                (
                    "cash_reconciliation_adjustment_scope",
                    "validate_cash_reconciliation_adjustment_scope",
                    "r1_validate_cash_reconciliation_adjustment_scope",
                ),
                (
                    "cash_reconciliation_projection_activation",
                    "cash_reconciliation_projection_activation_append_only",
                    "r1_bank_statement_append_only",
                ),
                (
                    "cash_reconciliation_projection_activation",
                    "validate_cash_reconciliation_projection_activation",
                    "r1_validate_cash_reconciliation_projection_activation",
                ),
            )
        ],
        "cash_reconciliation_classification_state_columns": [
            {
                "table": table,
                "column": column,
                "data_type": data_type,
                "not_null": not_null,
            }
            for table, columns in CASH_RECONCILIATION_CLASSIFICATION_STATE_COLUMNS.items()
            for column, (data_type, not_null) in columns.items()
        ],
        "cash_reconciliation_classification_state_constraints": [
            {"table": table, "name": f"{table}_pkey", "type": "p", "validated": True}
            for table in CASH_RECONCILIATION_CLASSIFICATION_STATE_TABLES
        ]
        + [
            {"table": table, "name": f"{table}_fk", "type": "f", "validated": True}
            for table in (
                "company_transaction_reporting_item",
                "company_transaction_reporting_item_match",
                "cash_reconciliation_adjustment_scope",
            )
        ]
        + [
            {
                "table": "cash_reconciliation_projection_activation",
                "name": "activation_check",
                "type": "c",
                "validated": True,
            }
        ],
        "cash_reconciliation_classification_state_table_acls": [
            {
                "table": table,
                "grantee": owner,
                "privilege": "SELECT",
                "grantable": False,
            }
            for table in CASH_RECONCILIATION_CLASSIFICATION_STATE_TABLES
        ],
        "cash_reconciliation_classification_state_effective_privileges": [
            {
                "role": role,
                "table": table,
                "select": False,
                "insert": False,
                "update": False,
                "delete": False,
            }
            for role in R1_ROLES
            for table in CASH_RECONCILIATION_CLASSIFICATION_STATE_TABLES
        ],
        "company_transaction_reporting_item_row_count": 17,
        "company_transaction_reporting_item_match_row_count": 8,
        "cash_reconciliation_adjustment_row_count": 1,
        "cash_reconciliation_adjustment_scope_row_count": 1,
        "cash_reconciliation_projection_activation_latest_status": "ACTIVE",
        "company_transaction_confirmed_unassigned_count": 0,
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
        classify(Transaction(UUID(int=1), 100, "宁波薇旭酒店管理有限公司", "陈明哲转账"), companies)
        == "INTERNAL_TRANSFER"
    )
    assert classify(Transaction(UUID(int=2), 100, "陈明哲", "转入"), companies) == (
        "RELATED_PARTY_CURRENT"
    )
    assert classify(Transaction(UUID(int=5), 100, "陈明哲", "资金归集"), companies) == (
        "RELATED_PARTY_CURRENT"
    )
    assert classify(Transaction(UUID(int=3), 100, "陈明哲贸易", "转入"), companies) is None
    assert classify(
        Transaction(UUID(int=4), 100, "支付宝支付科技有限公司", "飞猪房款结算"), companies
    ) == ("PLATFORM_ROOM_REVENUE")


def test_user_approved_company_transaction_rules() -> None:
    companies = frozenset()

    assert (
        classify(Transaction(UUID(int=6), 100, "陈明毅", "转账"), companies)
        == "RELATED_PARTY_CURRENT"
    )
    assert (
        classify(Transaction(UUID(int=7), 100, "陈婵娟", "转账"), companies)
        == "RELATED_PARTY_CURRENT"
    )
    assert (
        classify(Transaction(UUID(int=8), 100, "某公司", "往来款"), companies)
        == "RELATED_PARTY_CURRENT"
    )
    assert (
        classify(Transaction(UUID(int=9), -100, "深圳市汇泽丰酒业有限公司", "货款"), companies)
        == "BOTTLED_WATER"
    )
    assert classify(Transaction(UUID(int=10), 100, "租户", "5月房租"), companies) == "RENTAL_INCOME"
    assert (
        classify(Transaction(UUID(int=11), 100, "租户", "租金水电费"), companies) == "RENTAL_INCOME"
    )
    assert classify(Transaction(UUID(int=12), -100, "租户", "支付房租"), companies) == "RENT"
    assert (
        classify(Transaction(UUID(int=13), -100, "深圳市邦厨生鲜配送有限公司", "货款"), companies)
        == "OPERATING_FEE"
    )
    assert (
        classify(Transaction(UUID(int=14), -100, "深圳市港泰酒店用品有限公司", "货款"), companies)
        == "OPERATING_FEE"
    )
    assert (
        classify(Transaction(UUID(int=15), 100, "太平财产保险有限公司", "退款"), companies)
        == "OPERATING_FEE"
    )
    assert (
        classify(
            Transaction(UUID(int=16), 100, "深圳市紫元造境科技传媒有限公司", "他行转入"),
            companies,
        )
        == "RENTAL_INCOME"
    )
    assert (
        classify(Transaction(UUID(int=17), -100, "租户", "退还宿舍押金"), companies)
        == "RELATED_PARTY_CURRENT"
    )


def test_backfill_requires_the_migration_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL is required"):
        migration_database_url()

    monkeypatch.setenv(
        "LEDGERBRIDGE_MIGRATION_DATABASE_URL",
        "postgresql+psycopg://ledgerbridge_worker:secret@db/ledgerbridge",
    )
    with pytest.raises(RuntimeError, match="must identify ledgerbridge_owner"):
        migration_database_url()

    owner_url = "postgresql+psycopg://ledgerbridge_owner:secret@db/ledgerbridge"
    monkeypatch.setenv("LEDGERBRIDGE_MIGRATION_DATABASE_URL", owner_url)
    assert migration_database_url() == owner_url


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


def test_confirmed_wire_item_accepts_reporting_item_backfill_source() -> None:
    item = CompanyTransactionClassification.model_validate(
        {
            "transaction_ref": UUID(int=1),
            "entity_ref": UUID(int=2),
            "occurred_at": "2026-09-01T00:00:00Z",
            "amount_minor": 100,
            "currency": "CNY",
            "counterparty_name": "已核对",
            "transaction_name": "转账",
            "status": "CONFIRMED",
            "category_code": "FINANCING",
            "cashflow_role": "NON_OPERATING",
            "revision": 2,
            "source": "BACKFILL",
            "rule_version": "reporting-item-backfill.v1",
        }
    )

    assert item.source == "BACKFILL"


def test_review_receipt_accepts_a_versioned_reporting_item() -> None:
    receipt = CompanyTransactionClassificationReviewReceipt.model_validate(
        {
            "transaction_ref": UUID(int=1),
            "status": "CONFIRMED",
            "category_code": "BOTTLED_WATER",
            "reporting_item_code": "BOTTLED_WATER",
            "reporting_item_revision": 1,
            "revision": 2,
            "created": True,
        }
    )

    assert receipt.reporting_item_code == "BOTTLED_WATER"
    assert receipt.reporting_item_revision == 1


def test_review_receipt_rejects_an_incomplete_reporting_item_pair() -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        CompanyTransactionClassificationReviewReceipt.model_validate(
            {
                "transaction_ref": UUID(int=1),
                "status": "CONFIRMED",
                "category_code": "BOTTLED_WATER",
                "reporting_item_code": "BOTTLED_WATER",
                "revision": 2,
                "created": True,
            }
        )


def test_review_service_commits_a_receipt_with_reporting_item_fields() -> None:
    entity_ref = UUID(int=2)

    class Result:
        @staticmethod
        def scalar_one() -> dict[str, object]:
            return {
                "transaction_ref": UUID(int=1),
                "status": "CONFIRMED",
                "category_code": "BOTTLED_WATER",
                "reporting_item_code": "BOTTLED_WATER",
                "reporting_item_revision": 1,
                "revision": 2,
                "created": True,
            }

    class Session:
        committed = False

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

        def commit(self) -> None:
            self.committed = True

    session = Session()
    service = DatabaseCompanyTransactionClassificationService(
        reader_factory=lambda: session, api_factory=lambda: session  # type: ignore[arg-type]
    )
    principal = WorkloadPrincipal(
        principal_ref="test-reviewer",
        san_uri="spiffe://ledgerbridge.test/reviewer",
        policy_generation=1,
        capabilities=frozenset({Capability.BANK_STATEMENT_REVIEW_DECIDE}),
        grants=(EntityGrant(entity_ref=entity_ref, allow_account_registry=True),),
    )

    receipt = service.review(
        principal,
        transaction_ref=UUID(int=1),
        operation_id=UUID(int=3),
        assertion_jti=UUID(int=4),
        actor_ref="test-reviewer",
        command=CompanyTransactionClassificationReviewRequest(
            entity_ref=entity_ref,
            expected_revision=1,
            category_code=CompanyTransactionCategory.BOTTLED_WATER,
            reason="verified",
        ),
    )

    assert receipt.reporting_item_code == "BOTTLED_WATER"
    assert session.committed is True
