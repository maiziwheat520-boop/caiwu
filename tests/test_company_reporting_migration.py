from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
VERSIONS = ROOT / "alembic" / "versions"


def _migration_source() -> str:
    matches = list(VERSIONS.glob("20260830_0024_*.py"))
    assert len(matches) == 1, "company-reporting revision 20260830_0024 must exist exactly once"
    return matches[0].read_text(encoding="utf-8")


def _sql() -> str:
    return re.sub(r"\s+", " ", _migration_source()).lower()


def test_company_reporting_migration_is_the_next_versioned_reader_only_seam() -> None:
    source = _migration_source()
    sql = _sql()

    assert re.search(r'revision[^\n=]*=\s*["\']20260830_0024["\']', source)
    assert re.search(r'down_revision[^\n=]*=\s*["\']20260830_0023["\']', source)
    assert "create schema company_reporting_read" in sql
    assert "company_reporting_read.get_company_report_v1_as_of" in sql
    assert "returns table" in sql and "report jsonb" in sql
    assert "security definer" in sql
    assert re.search(r"set search_path\s*=\s*pg_catalog", sql)
    assert re.search(r"grant usage on schema company_reporting_read to ledgerbridge_reader", sql)
    assert re.search(
        r"grant execute on function company_reporting_read\.get_company_report_v1_as_of",
        sql,
    )
    for denied in (
        "public",
        "ledgerbridge_api",
        "ledgerbridge_worker",
        "ledgerbridge_app",
        "ledgerbridge_backup",
    ):
        assert re.search(rf"revoke all .*company_reporting_read.* from {denied}", sql)


def test_projection_selects_one_audit_horizon_basis_without_cross_layer_totalling() -> None:
    sql = _sql()

    for parameter in (
        "p_entity_ref",
        "p_business_unit_ids",
        "p_include_unassigned",
        "p_basis",
        "p_from_month",
        "p_to_month",
        "p_audit_sequence",
        "p_audit_hash",
    ):
        assert parameter in sql
    for basis in (
        "confirmed_candidate",
        "account_statement",
        "posted_ledger",
    ):
        assert f"'{basis}'" in sql
    assert "case p_basis" in sql or "if p_basis" in sql
    assert "metrics" in sql and "basis" in sql
    assert "confirmed_positive_minor" in sql
    assert "confirmed_negative_minor" in sql
    assert "confirmed_net_minor" in sql
    assert "cash_inflow_minor" in sql
    assert "cash_outflow_minor" in sql
    assert "net_cash_flow_minor" in sql
    assert "revenue_minor" in sql
    assert "expense_minor" in sql
    assert "profit_minor" in sql


def test_candidate_basis_uses_company_and_granted_unit_not_account_inference() -> None:
    sql = _sql()

    for relation in (
        "public.entity",
        "public.business_unit",
        "public.candidate",
        "public.candidate_revision",
        "public.candidate_event",
        "public.candidate_source",
        "public.audit_event",
    ):
        assert relation in sql
    assert re.search(r"entity_type\s*=\s*'company'", sql)
    assert "business_unit_id = any" in sql
    assert re.search(r"status\s*=\s*'confirmed'", sql)
    assert re.search(
        r"status\s+in\s*\(\s*'pending'\s*,\s*'incomplete'\s*,\s*'conflicted'\s*\)",
        sql,
    )
    assert re.search(
        r"count\s*\(\s*distinct\s*\([^)]*source_system_id[^)]*source_event_ref",
        sql,
    )
    assert "supersedes_candidate_id" in sql
    for forbidden_inference in (
        "candidate_counterparty",
        "counterparty_name",
        "summary_snapshot",
        "bank_name",
        "account_suffix",
    ):
        assert forbidden_inference not in sql


def test_statement_and_posted_bases_require_their_own_authoritative_facts() -> None:
    sql = _sql()

    for relation in (
        "public.managed_account",
        "public.bank_statement",
        "public.bank_statement_transaction",
        "public.bank_statement_observation",
        "public.bank_statement_review",
        "public.journal_entry",
        "public.posting",
    ):
        assert relation in sql
    assert re.search(r"source_system_id\s*=\s*[^;]+source_system", sql)
    assert re.search(r"source_event_ref\s*=\s*[^;]+source_event_ref", sql)
    assert re.search(r"statement_ref\s*=\s*[^;]+statement_ref", sql)
    assert re.search(r"managed_account_ref\s*=\s*[^;]+managed_account_ref", sql)
    assert re.search(r"owner_kind\s*=\s*'company'", sql)
    assert re.search(r"bank_statement_review[^;]+status\s*=\s*'confirmed'", sql)
    assert re.search(r"journal_entry[^;]+status\s*=\s*'posted'", sql)
    assert sql.count("sequence <= p_audit_sequence") >= 4


def test_projection_keeps_shared_unknowns_and_authoritative_balance_explicit() -> None:
    sql = _sql()

    for field in (
        "pending_review_count",
        "attribution_pending_count",
        "missing_material_count",
        "taxonomy_version",
        "balance_basis",
        "opening_balance_minor",
        "closing_balance_minor",
        "authoritative_balance_unavailable",
        "business_unit_breakdown_status",
        "unavailable_missing_snapshot",
        "unavailable_attribution_pending",
    ):
        assert field in sql
    assert re.search(r"'missing_material_count'\s*,\s*null", sql)
    assert re.search(r"'taxonomy_version'\s*,\s*null", sql)
    assert re.search(r"'opening_balance_minor'\s*,\s*null", sql)
    assert re.search(r"'closing_balance_minor'\s*,\s*null", sql)


def test_posted_business_unit_labels_are_captured_at_write_time_not_reconstructed() -> None:
    sql = _sql()

    assert "add column business_unit_ref_snapshot" in sql
    assert "add column business_unit_label_snapshot" in sql
    assert "r1_capture_journal_attribution_snapshot" in sql
    assert "new.business_unit_ref_snapshot" in sql
    assert "new.business_unit_label_snapshot" in sql
    assert "unit.ref as business_unit_ref" not in sql
    assert "unit.label as business_unit_label" not in sql
    assert "unavailable_missing_snapshot" in sql


def test_statement_attribution_prefers_supported_fact_allocation_then_account_assignment() -> None:
    sql = _sql()

    assert "public.fact_business_unit_allocation_set" in sql
    assert "public.fact_business_unit_allocation_item" in sql
    assert "public.account_business_unit_assignment" in sql
    assert "allocation_item_count = 1" in sql
    assert "allocation_basis_points = 10000" in sql
    assert "transaction.occurred_at::date >= item.effective_from" in sql
    assert "transaction.occurred_at::date < item.effective_to" in sql
    assert "business_unit_ref_snapshot" in sql
    assert "unavailable_attribution_pending" in sql
    assert "resolved_business_unit_id" in sql
    assert re.search(
        r"resolved_business_unit_id\s*=\s*any\s*\(p_business_unit_ids\)",
        sql,
    )
    assert "resolved_business_unit_id is null and p_include_unassigned" in sql


def test_downgrade_removes_only_the_company_reporting_read_surface() -> None:
    sql = _sql()

    assert "drop function" in sql
    assert "company_reporting_read.get_company_report_v1_as_of" in sql
    assert "drop schema company_reporting_read" in sql
    assert "drop table public." not in sql


def test_downgrade_fails_closed_before_removing_immutable_business_unit_snapshots() -> None:
    source = _migration_source().lower()
    downgrade = source.split("def downgrade() -> none:", maxsplit=1)[1]
    guard = downgrade.index(
        "nonempty r1 fact database prevents destructive company-reporting downgrade"
    )
    destructive_drop = downgrade.index("drop column business_unit_label_snapshot")

    assert guard < destructive_drop
    for relation in (
        "public.business_unit",
        "public.managed_account",
        "public.candidate",
        "public.bank_statement",
        "public.journal_entry_attribution",
    ):
        assert f"select 1 from {relation}" in downgrade
