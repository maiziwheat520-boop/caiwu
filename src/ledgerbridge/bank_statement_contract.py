"""Neutral contract shared by fail-closed bank-statement parser adapters."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo


class BankStatementParserProfile(StrEnum):
    """Versioned parser identities admitted by the persistence boundary."""

    MYBANK_XLSX_V1 = "mybank_xlsx_v1"
    MYBANK_COMPANY_DAILY_XLSX_V2 = "mybank_company_daily_xlsx_v2"
    MYBANK_COMPANY_RANGE_XLSX_V3 = "mybank_company_range_xlsx_v3"
    CCB_PERSONAL_XLS_V1 = "ccb_personal_xls_v1"
    BOC_PERSONAL_PDF_V1 = "boc_personal_pdf_v1"
    BOC_COMPANY_XLS_V1 = "boc_company_xls_v1"
    ABC_PERSONAL_PDF_V1 = "abc_personal_pdf_v1"
    ABC_COMPANY_XLS_V1 = "abc_company_xls_v1"


@dataclass(frozen=True, slots=True)
class BankStatementParserSpec:
    profile: BankStatementParserProfile
    institution_code: str
    source_system: str
    declared_media_type: str
    display_extension: str
    allowed_owner_kinds: frozenset[str]


MYBANK_XLSX_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.MYBANK_XLSX_V1,
    institution_code="mybank",
    source_system="mybank_xlsx_export",
    declared_media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    display_extension=".xlsx",
    allowed_owner_kinds=frozenset({"PERSON", "COMPANY"}),
)
MYBANK_COMPANY_DAILY_XLSX_V2: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
    institution_code="mybank",
    source_system="mybank_daily_statement",
    declared_media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    display_extension=".xlsx",
    allowed_owner_kinds=frozenset({"COMPANY"}),
)
MYBANK_COMPANY_RANGE_XLSX_V3: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.MYBANK_COMPANY_RANGE_XLSX_V3,
    institution_code="mybank",
    source_system="mybank_company_statement",
    declared_media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    display_extension=".xlsx",
    allowed_owner_kinds=frozenset({"COMPANY"}),
)
CCB_PERSONAL_XLS_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.CCB_PERSONAL_XLS_V1,
    institution_code="ccb",
    source_system="ccb_personal_xls_export",
    declared_media_type="application/vnd.ms-excel",
    display_extension=".xls",
    allowed_owner_kinds=frozenset({"PERSON"}),
)
BOC_PERSONAL_PDF_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.BOC_PERSONAL_PDF_V1,
    institution_code="boc",
    source_system="boc_transaction_statement",
    declared_media_type="application/pdf",
    display_extension=".pdf",
    allowed_owner_kinds=frozenset({"PERSON"}),
)
BOC_COMPANY_XLS_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.BOC_COMPANY_XLS_V1,
    institution_code="boc",
    source_system="boc_company_xls_export",
    declared_media_type="application/vnd.ms-excel",
    display_extension=".xls",
    allowed_owner_kinds=frozenset({"COMPANY"}),
)
ABC_PERSONAL_PDF_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.ABC_PERSONAL_PDF_V1,
    institution_code="abc",
    source_system="abc_personal_pdf_export",
    declared_media_type="application/pdf",
    display_extension=".pdf",
    allowed_owner_kinds=frozenset({"PERSON"}),
)
ABC_COMPANY_XLS_V1: Final = BankStatementParserSpec(
    profile=BankStatementParserProfile.ABC_COMPANY_XLS_V1,
    institution_code="abc",
    source_system="abc_company_xls_export",
    declared_media_type="application/vnd.ms-excel",
    display_extension=".xls",
    allowed_owner_kinds=frozenset({"COMPANY"}),
)

_SPECS: Final = {
    spec.profile: spec
    for spec in (
        MYBANK_XLSX_V1,
        MYBANK_COMPANY_DAILY_XLSX_V2,
        MYBANK_COMPANY_RANGE_XLSX_V3,
        CCB_PERSONAL_XLS_V1,
        BOC_PERSONAL_PDF_V1,
        BOC_COMPANY_XLS_V1,
        ABC_PERSONAL_PDF_V1,
        ABC_COMPANY_XLS_V1,
    )
}
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")


def parser_spec(profile: BankStatementParserProfile) -> BankStatementParserSpec:
    """Return the immutable allowlisted identity for one parser profile."""

    try:
        return _SPECS[profile]
    except (KeyError, TypeError) as exc:
        raise ValueError("bank statement parser profile is unsupported") from exc


@dataclass(frozen=True, slots=True)
class BankStatementTransaction:
    source_event_ref: UUID
    source_row_number: int
    source_row_sha256: str
    occurred_at: datetime
    amount_minor: int
    balance_minor: int
    counterparty_name: str
    counterparty_account: str
    counterparty_institution: str
    transaction_serial: str
    transaction_name: str


@dataclass(frozen=True, slots=True)
class BankStatement:
    statement_ref: UUID
    source_sha256: str
    source_size: int
    declared_media_type: str
    currency: str
    institution_code: str
    account_suffix: str
    worksheet_index: int
    header_row_number: int
    transactions: tuple[BankStatementTransaction, ...]
    parser_profile: BankStatementParserProfile = BankStatementParserProfile.MYBANK_XLSX_V1
    source_system: str = "mybank_xlsx_export"
    parser_facts_sha256: str = ""

    def __post_init__(self) -> None:
        spec = parser_spec(self.parser_profile)
        if (
            self.institution_code != spec.institution_code
            or self.source_system != spec.source_system
            or self.declared_media_type != spec.declared_media_type
        ):
            raise ValueError("bank statement parser identity conflicts")
        if (
            self.parser_profile
            in {
                BankStatementParserProfile.CCB_PERSONAL_XLS_V1,
                BankStatementParserProfile.BOC_PERSONAL_PDF_V1,
                BankStatementParserProfile.BOC_COMPANY_XLS_V1,
                BankStatementParserProfile.ABC_PERSONAL_PDF_V1,
                BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
                BankStatementParserProfile.MYBANK_COMPANY_RANGE_XLSX_V3,
                BankStatementParserProfile.ABC_COMPANY_XLS_V1,
            }
            and _DIGEST.fullmatch(self.parser_facts_sha256) is None
        ):
            raise ValueError("bank statement parser facts are invalid")

    @property
    def period_start(self) -> date:
        return min(item.occurred_at.astimezone(_SHANGHAI).date() for item in self.transactions)

    @property
    def period_end(self) -> date:
        return max(item.occurred_at.astimezone(_SHANGHAI).date() for item in self.transactions)

    @property
    def transaction_set_sha256(self) -> str:
        return hashlib.sha256(
            "|".join(
                f"{item.source_event_ref}:{item.source_row_sha256}:{item.source_row_number}"
                for item in sorted(
                    self.transactions, key=lambda transaction: transaction.source_row_number
                )
            ).encode("ascii")
        ).hexdigest()

    @property
    def monthly_transaction_counts(self) -> tuple[tuple[str, int], ...]:
        months = Counter(
            item.occurred_at.astimezone(_SHANGHAI).strftime("%Y-%m") for item in self.transactions
        )
        return tuple(sorted(months.items()))


class BankStatementParser(Protocol):
    def __call__(
        self,
        source_path: Path,
        *,
        expected_sha256: str,
        managed_account_suffix: str,
    ) -> BankStatement: ...
