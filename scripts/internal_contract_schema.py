"""Emit the internal Core read/command contract as a reviewable JSON artifact.

The Web BFF is a separate service that re-validates every Core response by
hand.  Nothing made a wire-shape change *visible*, so drift was discovered in
production instead of in review.  Committing the generated schema means any
change to an internal response model shows up as a diff in this file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ledgerbridge.candidate_contract import CandidateProjection
from ledgerbridge.internal_candidate_command import (
    CandidateClassificationBatchReceipt,
    CandidateDecisionReceipt,
    CandidateEventPage,
    ClassificationGroupPage,
)
from ledgerbridge.internal_read_contract import (
    AccountingDimensions,
    CandidatePage,
    CapabilitiesResponse,
    LedgerSummary,
    ReconciliationProjection,
)
from ledgerbridge.personal_finance_summary import PersonalFinanceSummary

CONTRACT_VERSION = "ledgerbridge.internal-contract.v1"

# Every model returned by an /internal/v1 route. Adding a route here is part of
# adding the route itself.
MODELS: tuple[type[BaseModel], ...] = (
    AccountingDimensions,
    CandidateClassificationBatchReceipt,
    CandidateDecisionReceipt,
    CandidateEventPage,
    CandidatePage,
    CandidateProjection,
    CapabilitiesResponse,
    ClassificationGroupPage,
    LedgerSummary,
    PersonalFinanceSummary,
    ReconciliationProjection,
)

DEFAULT_OUTPUT = Path("contracts/internal-contract.json")


def build_contract() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "models": {
            model.__name__: model.model_json_schema(mode="serialization")
            for model in sorted(MODELS, key=lambda item: item.__name__)
        },
    }


def render(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the committed artifact is stale",
    )
    args = parser.parse_args()
    rendered = render(build_contract())
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != rendered:
            raise SystemExit(
                f"{args.output} is stale; regenerate with "
                "`python scripts/internal_contract_schema.py`"
            )
        print(f"{args.output} matches the internal contract")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
