from __future__ import annotations

import os

from server.core_backend import CoreBackedState, CoreHttpClient
from server.evidence_preview import build_evidence_preview


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required setting: {name}")
    return value


def build_state() -> CoreBackedState:
    client = CoreHttpClient(
        base_url=required("CORE_BASE_URL"),
        ca_file=required("CORE_CA_FILE"),
        certificate_file=required("CORE_CERT_FILE"),
        private_key_file=required("CORE_KEY_FILE"),
        timeout_seconds=float(os.environ.get("CORE_TIMEOUT_SECONDS", "10")),
    )
    return CoreBackedState(
        client,
        assertion_key=required("CORE_USER_ASSERTION_KEY").encode("utf-8"),
        assertion_issuer=required("CORE_ASSERTION_ISSUER"),
        assertion_audience=required("CORE_ASSERTION_AUDIENCE"),
        workload_principal=required("CORE_WORKLOAD_PRINCIPAL"),
        policy_generation=int(required("CORE_POLICY_GENERATION")),
        user_subject=required("CORE_USER_SUBJECT"),
        authentication_generation=int(required("CORE_AUTHENTICATION_GENERATION")),
        entity_ref=required("CORE_ENTITY_REF"),
        business_unit_ref=required("CORE_BUSINESS_UNIT_REF"),
    )


def main() -> None:
    state = build_state()
    candidates: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        page = state.list_candidates(status=None, month=None, cursor=cursor)
        items = page.get("items")
        if not isinstance(items, list):
            raise SystemExit("candidate page was invalid")
        candidates.extend(item for item in items if isinstance(item, dict))
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str):
            raise SystemExit("candidate cursor was invalid")
        cursor = next_cursor

    target = next(
        (
            item
            for item in candidates
            if "TX-0139" in str(item.get("summary", ""))
        ),
        None,
    )
    if target is None:
        raise SystemExit("TX-0139 was not found")
    references = target.get("evidence")
    if not isinstance(references, list) or not references:
        raise SystemExit("TX-0139 had no evidence")

    spreadsheet_matches = 0
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("id"), str):
            raise SystemExit("TX-0139 evidence reference was invalid")
        evidence = state.evidence(reference["id"])
        preview = build_evidence_preview(evidence, reference="TX-0139")
        if preview.get("kind") == "spreadsheet" and preview.get("matched") is True:
            records = preview.get("records")
            if isinstance(records, list) and records:
                fields = records[0].get("fields")
                if isinstance(fields, list) and len(fields) >= 4:
                    spreadsheet_matches += 1
    if spreadsheet_matches < 1:
        raise SystemExit("TX-0139 spreadsheet content did not render inline")

    print(f"EVIDENCE_PREVIEW_OK candidate=TX-0139 matched_workbooks={spreadsheet_matches}")


if __name__ == "__main__":
    main()
