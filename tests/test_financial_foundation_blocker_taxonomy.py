from ledgerbridge.financial_foundation_blocker_taxonomy import (
    FINANCIAL_FOUNDATION_BLOCKER_TAXONOMY_VERSION,
    MissingMaterialClass,
    classify_missing_material,
)


def test_taxonomy_classifies_only_existing_material_codes_and_unknown_stays_pending() -> None:
    assert (
        FINANCIAL_FOUNDATION_BLOCKER_TAXONOMY_VERSION
        == "ledgerbridge.financial-foundation-blocker-taxonomy.v1"
    )
    assert classify_missing_material("EVIDENCE_INCOMPLETE") is MissingMaterialClass.EVIDENCE
    assert classify_missing_material("ACCOUNT_UNREGISTERED") is MissingMaterialClass.MANAGED_ACCOUNT
    assert (
        classify_missing_material("RELATED_ACCOUNT_STATEMENT_REQUIRED")
        is MissingMaterialClass.ACCOUNT_STATEMENT
    )
    assert classify_missing_material("PASSWORD_REQUIRED") is None
    assert classify_missing_material("FUTURE_UNKNOWN_BLOCKER") is None
