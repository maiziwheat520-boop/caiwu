"""Versioned dispatch for admitted bank-statement parser adapters."""

from __future__ import annotations

from pathlib import Path

from ledgerbridge.bank_statement_contract import (
    BankStatement,
    BankStatementParserProfile,
)


def parse_bank_statement(
    profile: BankStatementParserProfile,
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Parse through an explicit profile; unknown versions fail closed."""

    if profile is BankStatementParserProfile.MYBANK_XLSX_V1:
        from ledgerbridge.mybank_statement import parse_mybank_xlsx

        parser = parse_mybank_xlsx
    elif profile is BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2:
        from ledgerbridge.mybank_statement import parse_mybank_company_daily_xlsx

        parser = parse_mybank_company_daily_xlsx
    elif profile is BankStatementParserProfile.MYBANK_COMPANY_RANGE_XLSX_V3:
        from ledgerbridge.mybank_statement import parse_mybank_company_range_xlsx

        parser = parse_mybank_company_range_xlsx
    elif profile is BankStatementParserProfile.CCB_PERSONAL_XLS_V1:
        from ledgerbridge.ccb_statement import parse_ccb_personal_xls

        parser = parse_ccb_personal_xls
    elif profile is BankStatementParserProfile.BOC_PERSONAL_PDF_V1:
        from ledgerbridge.boc_statement import parse_boc_personal_pdf

        parser = parse_boc_personal_pdf
    elif profile is BankStatementParserProfile.ABC_PERSONAL_PDF_V1:
        from ledgerbridge.abc_statement import parse_abc_personal_pdf

        parser = parse_abc_personal_pdf
    elif profile is BankStatementParserProfile.ABC_COMPANY_XLS_V1:
        from ledgerbridge.abc_company_statement import parse_abc_company_xls

        parser = parse_abc_company_xls
    else:  # pragma: no cover - StrEnum construction normally rejects this first
        raise ValueError("bank statement parser profile is unsupported")
    statement = parser(
        source_path,
        expected_sha256=expected_sha256,
        managed_account_suffix=managed_account_suffix,
    )
    if statement.parser_profile is not profile:
        raise ValueError("bank statement parser returned the wrong profile")
    return statement
