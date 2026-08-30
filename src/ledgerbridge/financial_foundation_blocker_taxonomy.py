"""Versioned Shared Financial Foundation classification for missing materials."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

FINANCIAL_FOUNDATION_BLOCKER_TAXONOMY_VERSION: Literal[
    "ledgerbridge.financial-foundation-blocker-taxonomy.v1"
] = "ledgerbridge.financial-foundation-blocker-taxonomy.v1"


class MissingMaterialClass(StrEnum):
    EVIDENCE = "EVIDENCE"
    MANAGED_ACCOUNT = "MANAGED_ACCOUNT"
    ACCOUNT_STATEMENT = "ACCOUNT_STATEMENT"


_MISSING_MATERIAL_BY_EXISTING_CODE: dict[str, MissingMaterialClass] = {
    "EVIDENCE_INCOMPLETE": MissingMaterialClass.EVIDENCE,
    "ACCOUNT_UNREGISTERED": MissingMaterialClass.MANAGED_ACCOUNT,
    "COUNTERPARTY_STATEMENT_REQUIRED": MissingMaterialClass.ACCOUNT_STATEMENT,
    "FUNDING_STATEMENT_REQUIRED": MissingMaterialClass.ACCOUNT_STATEMENT,
    "RELATED_ACCOUNT_STATEMENT_REQUIRED": MissingMaterialClass.ACCOUNT_STATEMENT,
    "HOTEL_PAYOUT_STATEMENT_REQUIRED": MissingMaterialClass.ACCOUNT_STATEMENT,
}


def classify_missing_material(code: str) -> MissingMaterialClass | None:
    """Classify one existing blocker/risk code; unknown codes remain review-only."""

    return _MISSING_MATERIAL_BY_EXISTING_CODE.get(code)
