"""Personal rules propose; each confirmation is still one audited decision.

Synthetic rules and synthetic candidates only. The decision service is the
process-local synthetic one, so no database is touched; what is under test is
the matching, the grouping and the batch loop on top of the ordinary decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

import scripts.local_mode as local_mode_script
from ledgerbridge.candidate_contract import CandidateProjection, create_candidate_aggregate
from ledgerbridge.config import get_settings
from ledgerbridge.internal_candidate_command import (
    CandidateDecision,
    CandidateDecisionRequest,
    SyntheticInternalReviewService,
)
from ledgerbridge.internal_candidate_command_routes import get_candidate_command_service
from ledgerbridge.internal_read_contract import (
    AccountingDimensions,
    BusinessUnitDimension,
    ReportingCategoryDimension,
    WorkloadPrincipal,
)
from ledgerbridge.local_commands import LOCAL_ACTOR, get_local_rules_path, local_assertion_id
from ledgerbridge.local_mode import LocalBook, LocalModeRefused, LocalProfile, local_settings
from ledgerbridge.local_rules import (
    LocalRulesInvalid,
    LocalRulesNotConfigured,
    RuleCategory,
    build_suggestions,
    load_rules,
    match,
    match_text,
    parse_rules,
    rule_id,
    spread,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
UNIT_REF = "unit-demo-a"
UNIT = UUID("10000000-0000-4000-8000-00000000000a")
LOCAL_BASE_URL = "http://127.0.0.1:8661"
LOCAL_DATABASE = "postgresql+psycopg://ledgerbridge:local@127.0.0.1:5432/ledgerbridge"

FOOD = {"code": "P-0000food", "label": "合成餐饮", "nature": "EXPENSE"}
MOVE = {"code": "P-0000move", "label": "合成转账", "nature": "TRANSFER"}
RULES = {
    "version": 1,
    "categories": [FOOD, MOVE],
    "rules": [
        {"pattern": "合成商户A", "category_code": "P-0000food", "priority": 50, "note": "合成"},
        {"pattern": "合成转账", "category_code": "P-0000move", "priority": 50},
        {"pattern": "合成商户", "category_code": "P-0000move", "priority": 10},
    ],
}


def summary(counterparty: str, kind: str = "商户消费") -> str:
    return f"合成平台 | 2026-08-01 | 支出 | {kind} | {counterparty} | 零钱 | 交易成功"


def rules_bytes(document: object = RULES) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


# --- C3: the file ---------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["categories"].append(dict(FOOD, label="合成其他")), "code is defined twice"),
        (lambda d: d["categories"].append(dict(FOOD, code="P-other")), "label is used twice"),
        (lambda d: d["categories"][0].update(nature="SAVINGS"), "nature"),
        (lambda d: d["rules"][0].update(category_code="P-missing"), "not a defined category"),
        (lambda d: d["rules"][0].update(pattern="  "), "pattern must be non-empty"),
        (lambda d: d["rules"].append(dict(d["rules"][0])), "repeats an earlier"),
        (lambda d: d.update(version=2), "version must be 1"),
        (lambda d: d["rules"][0].update(priority="high"), "priority"),
        (lambda d: d.update(extra=True), "unknown field"),
    ],
)
def test_an_invalid_file_is_refused_with_a_clear_error(mutate: object, message: str) -> None:
    document = json.loads(rules_bytes())
    mutate(document)  # type: ignore[operator]
    with pytest.raises(LocalRulesInvalid, match=message):
        parse_rules(rules_bytes(document))


def test_a_non_json_file_is_invalid_and_a_missing_one_is_not_configured(tmp_path: Path) -> None:
    with pytest.raises(LocalRulesInvalid):
        parse_rules(b"{not json")
    with pytest.raises(LocalRulesNotConfigured):
        load_rules(tmp_path / "absent.json")


def test_ids_and_version_are_digests_of_the_contracted_inputs() -> None:
    content = rules_bytes()
    ruleset = parse_rules(content)

    assert ruleset.rules_version == hashlib.sha256(content).hexdigest()[:16]
    expected = hashlib.sha256("合成商户A\x1fP-0000food".encode()).hexdigest()[:12]
    assert ruleset.rules[0].rule_id == expected == rule_id("合成商户A", "P-0000food")
    # Reordering rules changes the file version, never a rule's id.
    reordered = dict(RULES, rules=list(reversed(RULES["rules"])))
    again = parse_rules(rules_bytes(reordered))
    assert again.rules_version != ruleset.rules_version
    assert {rule.rule_id for rule in again.rules} == {rule.rule_id for rule in ruleset.rules}


def test_highest_priority_wins_and_ties_go_to_the_earlier_rule() -> None:
    ruleset = parse_rules(rules_bytes())

    # Both "合成商户A" (50) and "合成商户" (10) occur: priority decides.
    assert match(ruleset, summary("合成商户A")) == ruleset.rules[0]
    assert match(ruleset, summary("合成商户B")) == ruleset.rules[2]
    # Equal priority 50, both occur: file order decides.
    assert match(ruleset, summary("合成商户A", kind="合成转账")) == ruleset.rules[0]


def test_only_counterparty_type_funding_and_status_are_matched() -> None:
    ruleset = parse_rules(rules_bytes())

    assert match_text("a | b | c") is None
    assert match(ruleset, "合成商户A | 2026-08-01") is None
    # The platform field is not searched.
    assert match(ruleset, "合成商户A | 2026-08-01 | 支出 | 消费 | 某人 | 零钱 | 成功") is None


def test_samples_spread_across_the_group() -> None:
    assert spread(3) == (0, 1, 2)
    assert spread(12) == (0, 3, 6, 8, 11)
    assert spread(200)[0] == 0 and spread(200)[-1] == 199 and len(spread(200)) == 5


# --- synthetic candidates ------------------------------------------------


def _template() -> CandidateProjection:
    fixture = SyntheticInternalReviewService()._fixture
    return next(item for item in fixture.candidates if str(item.status) == "PENDING")


def make_candidate(number: int, text: str, category: str = "SUPPLIES") -> CandidateProjection:
    labels = {"SUPPLIES": "Synthetic supplies", FOOD["code"]: FOOD["label"]}
    return _template().model_copy(
        update={
            "candidate_ref": UUID(f"50000000-0000-4000-8000-{number:012d}"),
            "short_id": f"C-S{number:05d}",
            "summary": text,
            "amount_minor": -100 * number,
            "category_code": category,
            "category_label": labels[category],
        }
    )


class RuleTestService(SyntheticInternalReviewService):
    """The synthetic review service, holding only this module's candidates."""

    def __init__(self, candidates: list[CandidateProjection], categories: tuple[dict, ...]) -> None:
        super().__init__()
        self._aggregates = {
            item.candidate_ref: create_candidate_aggregate(item) for item in candidates
        }
        self._order = [item.candidate_ref for item in candidates]
        self._categories = categories

    def list_candidates(
        self, principal, *, month=None, status=None, business_unit=None, cursor=None
    ):  # type: ignore[no-untyped-def,override]
        from ledgerbridge.internal_read_contract import CandidatePage

        items = [self._aggregates[ref].projection for ref in self._order]
        return CandidatePage(
            items=tuple(
                item
                for item in items
                if (status is None or item.status == status)
                and (business_unit is None or item.business_unit_ref == business_unit)
            ),
            next_cursor=None,
        )

    def get_candidate(self, principal, candidate_ref):  # type: ignore[no-untyped-def,override]
        return self._aggregates[candidate_ref].projection

    def get_accounting_dimensions(self, principal, *, entity_ref):  # type: ignore[no-untyped-def,override]
        categories = sorted(
            ({"code": "SUPPLIES", "label": "Synthetic supplies"}, *self._categories),
            key=lambda item: item["code"],
        )
        return AccountingDimensions(
            entity_ref=entity_ref,
            business_units=(BusinessUnitDimension(ref=UNIT_REF, label="Demo unit A"),),
            categories=tuple(
                ReportingCategoryDimension(code=item["code"], label=item["label"])
                for item in categories
            ),
        )


def ready_service(candidates: list[CandidateProjection], *, ready: bool = True) -> RuleTestService:
    categories = ({"code": FOOD["code"], "label": FOOD["label"]},) if ready else ()
    return RuleTestService(candidates, categories)


def client_for(service: RuleTestService, rules_file: Path) -> TestClient:
    profile = LocalProfile(
        database_url=LOCAL_DATABASE,
        api_database_url=LOCAL_DATABASE,
        artifact_root=Path(gettempdir()) / "ledgerbridge-local-rules-test",
        books=(LocalBook(entity_ref=ENTITY, business_units=((UNIT_REF, UNIT),)),),
    )
    app = local_mode_script._build_app(profile)
    app.dependency_overrides[get_candidate_command_service] = lambda: service
    app.dependency_overrides[get_local_rules_path] = lambda: rules_file
    return TestClient(app, base_url=LOCAL_BASE_URL)


@pytest.fixture
def rules_file(tmp_path: Path) -> Path:
    path = tmp_path / "personal.json"
    path.write_bytes(rules_bytes())
    return path


def suggestions(client: TestClient) -> dict:  # type: ignore[type-arg]
    response = client.get(
        "/internal/v1/local/rule-suggestions",
        params={"entity_ref": str(ENTITY), "business_unit": UNIT_REF},
    )
    assert response.status_code == 200, response.text
    return response.json()


def post_batch(client: TestClient, body: dict, key: str | None = None):  # type: ignore[no-untyped-def,type-arg]
    return client.post(
        f"/internal/v1/local/rule-batches/decisions?entity_ref={ENTITY}",
        content=json.dumps(body),
        headers={"Content-Type": "application/json", "Idempotency-Key": key or str(uuid4())},
    )


def test_suggestions_group_pending_candidates_by_rule(rules_file: Path) -> None:
    candidates = [make_candidate(n, summary("合成商户A")) for n in range(1, 13)]
    candidates += [make_candidate(20, summary("合成商户B")), make_candidate(21, summary("无关"))]
    with client_for(ready_service(candidates), rules_file) as client:
        page = suggestions(client)

    ruleset = parse_rules(rules_bytes())
    assert page["contract_version"] == "ledgerbridge.local-rule-suggestions.v1"
    assert page["rules_version"] == ruleset.rules_version
    assert (page["pending_total"], page["unmatched_count"]) == (14, 1)
    first, second = page["groups"]
    assert first["group_key"] == f"P-0000food:{ruleset.rules[0].rule_id}"
    assert (first["count"], first["nature"], first["category_label"]) == (12, "EXPENSE", "合成餐饮")
    assert first["category_ready"] is True and second["category_ready"] is False
    assert second["nature"] == "TRANSFER" and second["count"] == 1
    assert len(first["members"]) == 12
    sampled = [sample["short_id"] for sample in first["samples"]]
    assert sampled == ["C-S00001", "C-S00004", "C-S00007", "C-S00009", "C-S00012"]
    assert first["samples"][0]["current_category_label"] == "Synthetic supplies"


def test_suggestions_refuse_a_missing_or_invalid_file(tmp_path: Path) -> None:
    service = ready_service([make_candidate(1, summary("合成商户A"))])
    with client_for(service, tmp_path / "absent.json") as client:
        response = client.get(
            "/internal/v1/local/rule-suggestions",
            params={"entity_ref": str(ENTITY), "business_unit": UNIT_REF},
        )
    assert (response.status_code, response.json()["code"]) == (404, "LOCAL_RULES_NOT_CONFIGURED")

    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    with client_for(service, broken) as client:
        response = client.get(
            "/internal/v1/local/rule-suggestions",
            params={"entity_ref": str(ENTITY), "business_unit": UNIT_REF},
        )
    assert (response.status_code, response.json()["code"]) == (422, "LOCAL_RULES_INVALID")


def _batch_body(page: dict, group: dict, members: list[dict] | None = None) -> dict:  # type: ignore[type-arg]
    return {
        "rules_version": page["rules_version"],
        "group_key": group["group_key"],
        "reason": "合成抽样确认",
        "members": members if members is not None else group["members"],
    }


def test_a_batch_reports_each_members_outcome(rules_file: Path) -> None:
    correct = make_candidate(1, summary("合成商户A"))
    already = make_candidate(2, summary("合成商户A"), category=FOOD["code"])
    moved = make_candidate(3, summary("合成商户A"))
    other = make_candidate(4, summary("合成商户B"))
    stale = make_candidate(5, summary("合成商户A"))
    service = ready_service([correct, already, moved, other, stale])
    with client_for(service, rules_file) as client:
        page = suggestions(client)
        group = page["groups"][0]
        # Decided elsewhere after the page was read.
        service.append_decision(
            _principal(),
            candidate_ref=moved.candidate_ref,
            operation_id=uuid4(),
            assertion_jti=uuid4(),
            actor_ref=LOCAL_ACTOR,
            request=CandidateDecisionRequest(
                decision=CandidateDecision.IGNORE, expected_revision=1, reason="合成"
            ),
            decided_at=datetime.now(UTC),
        )
        members = [
            {"candidate_ref": str(correct.candidate_ref), "expected_revision": 1},
            {"candidate_ref": str(already.candidate_ref), "expected_revision": 1},
            {"candidate_ref": str(moved.candidate_ref), "expected_revision": 1},
            {"candidate_ref": str(other.candidate_ref), "expected_revision": 1},
            {"candidate_ref": str(stale.candidate_ref), "expected_revision": 2},
        ]
        key = str(uuid4())
        first = post_batch(client, _batch_body(page, group, members), key)
        again = post_batch(client, _batch_body(page, group, members), key)

    assert first.status_code == 200, first.text
    outcomes = [(item["outcome"], item["replayed"]) for item in first.json()["outcomes"]]
    assert outcomes == [
        ("CONFIRMED", False),
        ("CONFIRMED", False),
        ("NOT_PENDING", False),
        ("NOT_MATCHED", False),
        ("STALE", False),
    ]
    decided = service.get_candidate(_principal(), correct.candidate_ref)
    assert (str(decided.status), decided.category_code) == ("CONFIRMED", FOOD["code"])
    events = service._aggregates[correct.candidate_ref].events
    rule = parse_rules(rules_bytes()).rules[0]
    assert events[-1].reason == f"合成抽样确认 [规则 {rule.rule_id}]"
    assert events[-1].actor_ref == LOCAL_ACTOR
    # The operation id is derived from the key and the candidate.
    operation = uuid5(UUID(key), str(correct.candidate_ref))
    assert service._receipts[operation][2].operation_id == operation
    assert service._assertion_jtis[local_assertion_id(operation)] == operation

    # A retry with the same key replays both confirmations, whatever shape each took.
    assert again.status_code == 200
    replayed = [(item["outcome"], item["replayed"]) for item in again.json()["outcomes"]]
    assert replayed[:2] == [("CONFIRMED", True), ("CONFIRMED", True)]


def test_a_changed_file_or_an_unready_category_refuses_the_whole_batch(rules_file: Path) -> None:
    candidate = make_candidate(1, summary("合成商户A"))
    service = ready_service([candidate], ready=False)
    with client_for(service, rules_file) as client:
        page = suggestions(client)
        group = page["groups"][0]
        not_ready = post_batch(client, _batch_body(page, group))
        rules_file.write_bytes(rules_bytes(dict(RULES, rules=RULES["rules"][:1])))
        changed = post_batch(client, _batch_body(page, group))

    assert (not_ready.status_code, not_ready.json()["code"]) == (409, "LOCAL_CATEGORY_NOT_READY")
    assert (changed.status_code, changed.json()["code"]) == (409, "LOCAL_RULES_CHANGED")
    current = service.get_candidate(_principal(), candidate.candidate_ref)
    assert (str(current.status), current.revision) == ("PENDING", 1)


def test_more_than_200_members_is_refused(rules_file: Path) -> None:
    with client_for(ready_service([]), rules_file) as client:
        members = [{"candidate_ref": str(uuid4()), "expected_revision": 1} for _ in range(201)]
        body = {
            "rules_version": parse_rules(rules_bytes()).rules_version,
            "group_key": f"P-0000food:{rule_id('合成商户A', 'P-0000food')}",
            "reason": "合成",
            "members": members,
        }
        response = post_batch(client, body)
        duplicate = post_batch(client, dict(body, members=[members[0], members[0]]))

    assert (response.status_code, response.json()["code"]) == (422, "INVALID_COMMAND")
    assert duplicate.status_code == 422


def test_the_rule_routes_refuse_a_deployed_configuration(rules_file: Path) -> None:
    service = ready_service([make_candidate(1, summary("合成商户A"))])
    client = client_for(service, rules_file)
    deployed = local_settings(
        LocalProfile(
            database_url=LOCAL_DATABASE,
            api_database_url=LOCAL_DATABASE,
            artifact_root=Path(gettempdir()) / "ledgerbridge-local-rules-test",
            books=(LocalBook(entity_ref=ENTITY),),
        )
    ).model_copy(update={"internal_read_transport": "unix-mtls-proxy"})
    client.app.dependency_overrides[get_settings] = lambda: deployed  # type: ignore[attr-defined]
    with client:
        read = client.get(
            "/internal/v1/local/rule-suggestions",
            params={"entity_ref": str(ENTITY), "business_unit": UNIT_REF},
        )
        write = post_batch(
            client,
            {
                "rules_version": "0" * 16,
                "group_key": f"P-0000food:{'0' * 12}",
                "reason": "合成",
                "members": [{"candidate_ref": str(uuid4()), "expected_revision": 1}],
            },
        )
    assert (read.status_code, read.json()["code"]) == (404, "CANDIDATE_COMMAND_DISABLED")
    assert (write.status_code, write.json()["code"]) == (404, "CANDIDATE_COMMAND_DISABLED")


def test_build_suggestions_ignores_non_pending_candidates() -> None:
    ruleset = parse_rules(rules_bytes())
    ignored, pending = (
        make_candidate(1, summary("合成商户A")),
        make_candidate(2, summary("合成商户A")),
    )
    service = ready_service([ignored, pending])
    service.append_decision(
        _principal(),
        candidate_ref=ignored.candidate_ref,
        operation_id=uuid4(),
        assertion_jti=uuid4(),
        actor_ref=LOCAL_ACTOR,
        request=CandidateDecisionRequest(
            decision=CandidateDecision.IGNORE, expected_revision=1, reason="合成"
        ),
        decided_at=datetime.now(UTC),
    )
    current = [
        service.get_candidate(_principal(), item.candidate_ref) for item in (ignored, pending)
    ]
    result = build_suggestions(ruleset, current, business_unit=UNIT_REF, ready_category_codes=())
    assert result.pending_total == 1 and result.groups[0].count == 1
    assert result.groups[0].category_ready is False


# --- C5: categories -------------------------------------------------------


def _category(item: dict) -> RuleCategory:  # type: ignore[type-arg]
    return RuleCategory(code=item["code"], label=item["label"], nature=item["nature"])


def test_categories_plan_inserts_sets_nature_and_leaves_matches() -> None:
    plan = local_mode_script.plan_categories(
        [_category(FOOD), _category(MOVE)],
        [{"code": MOVE["code"], "label": MOVE["label"], "nature": None, "retired": False}],
    )
    assert [item.code for item in plan.insert] == [FOOD["code"]]
    assert [item.code for item in plan.set_nature] == [MOVE["code"]]

    same = local_mode_script.plan_categories([_category(FOOD)], [dict(FOOD, retired=False)])
    assert (same.insert, same.set_nature, same.unchanged) == ((), (), 1)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (dict(FOOD, label="合成别名", retired=False), "different label"),
        (dict(FOOD, nature="INCOME", retired=False), "different nature"),
        (dict(FOOD, retired=True), "retired"),
        (dict(FOOD, code="P-other", retired=False), "label another category"),
    ],
)
def test_categories_plan_refuses_any_disagreement(row: dict, message: str) -> None:  # type: ignore[type-arg]
    with pytest.raises(LocalModeRefused, match=message):
        local_mode_script.plan_categories([_category(FOOD)], [row])


def _principal() -> WorkloadPrincipal:
    from ledgerbridge.local_mode import local_principal

    return local_principal((LocalBook(entity_ref=ENTITY, business_units=((UNIT_REF, UNIT),)),))
