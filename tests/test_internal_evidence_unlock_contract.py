from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_unlock_openapi_is_one_closed_mtls_post_contract() -> None:
    document = yaml.safe_load(
        (ROOT / "docs/contracts/internal-evidence-unlock-v1.openapi.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {"/internal/v1/evidence/unlocks"}
    operation = document["paths"]["/internal/v1/evidence/unlocks"]["post"]
    assert operation["x-ledgerbridge-capability"] == "evidence:unlock"
    assert operation["x-ledgerbridge-reject-unknown-query"] is True
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "413",
        "415",
        "422",
        "503",
    }
    request = document["components"]["schemas"]["EvidenceUnlockRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["contract_version", "source_ref", "password"]
