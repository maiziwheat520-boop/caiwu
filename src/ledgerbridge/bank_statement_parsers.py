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
    elif profile is BankStatementParserProfile.CCB_PERSONAL_XLS_V1:
        from ledgerbridge.ccb_statement import parse_ccb_personal_xls

        parser = parse_ccb_personal_xls
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
