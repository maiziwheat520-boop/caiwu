"""Personal classification rules, carried over as proposals and nothing more.

A rules file maps a substring of a payment row onto a reporting category. It
lives outside the repository (``~/.ledgerbridge-local/rules/personal.json``) and
is the user's own, so this module only reads it: it loads and validates the
file, matches candidate summaries against it and groups the matches into
suggestions. It opens no connection and writes nothing. Confirming a suggestion
is a separate step that goes, candidate by candidate, through the ordinary
decision service with its revision check and append-only audit.

Two identities keep a suggestion honest across time. ``rules_version`` is a
digest of the file bytes, so a batch confirmed against one version of the file
is refused once the file has changed underneath it. ``rule_id`` is a digest of
the pattern and target category, so the same rule keeps its id when unrelated
rules are added or reordered, and a group key names exactly one rule.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus

RULES_ENV: Final = "LEDGERBRIDGE_LOCAL_RULES"
DEFAULT_RULES_PATH: Final = Path("~/.ledgerbridge-local/rules/personal.json")

NATURES: Final = frozenset({"INCOME", "EXPENSE", "TRANSFER"})
Nature = Literal["INCOME", "EXPENSE", "TRANSFER"]

#: A rules file is a hand-edited list; anything this large is not one.
_MAX_FILE_BYTES: Final = 1024 * 1024
#: The positional candidate summary:
#: `平台 | 日期 | 收支 | 交易类型 | 交易对方 | 支付方式 | 交易状态`.
_SUMMARY_SEPARATOR: Final = "|"
_SUMMARY_FIELDS: Final = 7
#: Samples shown per group; enough to spot a wrong rule, few enough to read.
MAX_SAMPLES: Final = 5
#: Members one batch may decide, matching the Web client's chunk size.
MAX_BATCH_MEMBERS: Final = 200

SUGGESTIONS_CONTRACT: Final = "ledgerbridge.local-rule-suggestions.v1"
BATCH_CONTRACT: Final = "ledgerbridge.local-rule-batch-decision.v1"


class LocalRulesNotConfigured(RuntimeError):
    """No rules file exists at the configured path."""


class LocalRulesInvalid(ValueError):
    """The rules file exists but cannot be trusted; no suggestion is made from it."""


@dataclass(frozen=True, slots=True)
class RuleCategory:
    code: str
    label: str
    nature: Nature


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    pattern: str
    category_code: str
    priority: int
    note: str
    #: Position in the file; the tie-break between equal priorities.
    position: int


@dataclass(frozen=True, slots=True)
class RuleSet:
    rules_version: str
    categories: Mapping[str, RuleCategory]
    rules: tuple[Rule, ...]

    def rule_for_group(self, group_key: str) -> Rule | None:
        return next((rule for rule in self.rules if group_key_of(rule) == group_key), None)


def rules_path(environ: Mapping[str, str] | None = None) -> Path:
    """The rules file this process reads: the environment override, else the default."""
    environ = os.environ if environ is None else environ
    override = environ.get(RULES_ENV)
    return Path(override).expanduser() if override else DEFAULT_RULES_PATH.expanduser()


def rule_id(pattern: str, category_code: str) -> str:
    return hashlib.sha256(f"{pattern}\x1f{category_code}".encode()).hexdigest()[:12]


def group_key_of(rule: Rule) -> str:
    return f"{rule.category_code}:{rule.rule_id}"


def load_rules(path: Path) -> RuleSet:
    """Read and validate one rules file.

    A file that is merely absent is "not configured", which the page presents
    differently from a file that is present and wrong: the second must never
    produce a partial set of suggestions.
    """
    if not path.is_file():
        raise LocalRulesNotConfigured("no local rules file is configured")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LocalRulesInvalid("the rules file could not be read") from exc
    return parse_rules(content)


def parse_rules(content: bytes) -> RuleSet:
    if len(content) > _MAX_FILE_BYTES:
        raise LocalRulesInvalid("the rules file is larger than 1 MiB")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalRulesInvalid("the rules file is not UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise LocalRulesInvalid("the rules file must be a JSON object")
    _closed_keys(document, {"version", "categories", "rules"}, "the rules file")
    if document.get("version") != 1 or isinstance(document.get("version"), bool):
        raise LocalRulesInvalid("version must be 1")

    raw_categories = document.get("categories")
    if not isinstance(raw_categories, list):
        raise LocalRulesInvalid("categories must be a list")
    categories: dict[str, RuleCategory] = {}
    labels: set[str] = set()
    for index, item in enumerate(raw_categories):
        where = f"categories[{index}]"
        if not isinstance(item, dict):
            raise LocalRulesInvalid(f"{where} must be an object")
        _closed_keys(item, {"code", "label", "nature"}, where)
        code = _text(item.get("code"), f"{where}.code", 100)
        label = _text(item.get("label"), f"{where}.label", 200)
        nature = item.get("nature")
        if nature not in NATURES:
            raise LocalRulesInvalid(f"{where}.nature must be INCOME, EXPENSE or TRANSFER")
        if code in categories:
            raise LocalRulesInvalid(f"{where}.code is defined twice")
        if label in labels:
            raise LocalRulesInvalid(f"{where}.label is used twice")
        labels.add(label)
        categories[code] = RuleCategory(code=code, label=label, nature=nature)

    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list):
        raise LocalRulesInvalid("rules must be a list")
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_rules):
        where = f"rules[{index}]"
        if not isinstance(item, dict):
            raise LocalRulesInvalid(f"{where} must be an object")
        _closed_keys(item, {"pattern", "category_code", "priority", "note"}, where)
        pattern = _text(item.get("pattern"), f"{where}.pattern", 200)
        category_code = item.get("category_code")
        if not isinstance(category_code, str) or category_code not in categories:
            raise LocalRulesInvalid(f"{where}.category_code is not a defined category")
        priority = item.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise LocalRulesInvalid(f"{where}.priority must be an integer")
        note = item.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise LocalRulesInvalid(f"{where}.note must be text of at most 500 characters")
        identifier = rule_id(pattern, category_code)
        # Two identical rules would give two groups the same key.
        if identifier in seen_ids:
            raise LocalRulesInvalid(f"{where} repeats an earlier pattern and category")
        seen_ids.add(identifier)
        rules.append(
            Rule(
                rule_id=identifier,
                pattern=pattern,
                category_code=category_code,
                priority=priority,
                note=note,
                position=index,
            )
        )
    return RuleSet(
        rules_version=hashlib.sha256(content).hexdigest()[:16],
        categories=categories,
        rules=tuple(rules),
    )


def _closed_keys(item: Mapping[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise LocalRulesInvalid(f"{where} has unknown field(s): {', '.join(unknown)}")


def _text(value: object, where: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalRulesInvalid(f"{where} must be non-empty text")
    if len(value) > limit:
        raise LocalRulesInvalid(f"{where} must be at most {limit} characters")
    return value


def match_text(summary: str) -> str | None:
    """The part of a summary a rule looks at, or None if it is not a platform row.

    Only the counterparty, transaction type, funding and status are matched.
    The platform, date and direction are excluded so that a pattern cannot
    accidentally match every row of a month or of one platform.
    """
    fields = [field.strip() for field in summary.split(_SUMMARY_SEPARATOR)]
    if len(fields) < _SUMMARY_FIELDS:
        return None
    return " ".join((fields[4], fields[3], fields[5], fields[6]))


def match(ruleset: RuleSet, summary: str) -> Rule | None:
    """The highest-priority rule whose pattern occurs; ties go to the earlier rule."""
    text = match_text(summary)
    if text is None:
        return None
    best: Rule | None = None
    for rule in ruleset.rules:
        if rule.pattern in text and (best is None or rule.priority > best.priority):
            best = rule
    return best


def spread(count: int, limit: int = MAX_SAMPLES) -> tuple[int, ...]:
    """Indices of up to ``limit`` samples: the first, the last and evenly between."""
    if count <= limit:
        return tuple(range(count))
    return tuple(sorted({round(step * (count - 1) / (limit - 1)) for step in range(limit)}))


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleGroupMember(_Frozen):
    candidate_ref: UUID
    expected_revision: int = Field(ge=1)


class RuleGroupSample(_Frozen):
    candidate_ref: UUID
    short_id: str
    summary: str
    amount_minor: int | None
    current_category_label: str


class RuleSuggestionGroup(_Frozen):
    group_key: str
    category_code: str
    category_label: str
    nature: Nature
    category_ready: bool
    rule_id: str
    rule_pattern: str
    rule_note: str
    count: int
    members: tuple[RuleGroupMember, ...]
    samples: tuple[RuleGroupSample, ...]


class RuleSuggestions(_Frozen):
    contract_version: Literal["ledgerbridge.local-rule-suggestions.v1"] = SUGGESTIONS_CONTRACT
    business_unit: str
    rules_version: str
    pending_total: int
    unmatched_count: int
    groups: tuple[RuleSuggestionGroup, ...]


def build_suggestions(
    ruleset: RuleSet,
    candidates: Iterable[CandidateProjection],
    *,
    business_unit: str,
    ready_category_codes: Iterable[str],
) -> RuleSuggestions:
    """Group the pending candidates by the rule each one matches."""
    ready = frozenset(ready_category_codes)
    pending = [item for item in candidates if item.status == CandidateStatus.PENDING]
    matched: dict[str, tuple[Rule, list[CandidateProjection]]] = {}
    unmatched = 0
    for candidate in pending:
        rule = match(ruleset, candidate.summary)
        if rule is None:
            unmatched += 1
            continue
        matched.setdefault(group_key_of(rule), (rule, []))[1].append(candidate)

    groups: list[RuleSuggestionGroup] = []
    for key, (rule, members) in matched.items():
        category = ruleset.categories[rule.category_code]
        groups.append(
            RuleSuggestionGroup(
                group_key=key,
                category_code=category.code,
                category_label=category.label,
                nature=category.nature,
                category_ready=category.code in ready,
                rule_id=rule.rule_id,
                rule_pattern=rule.pattern,
                rule_note=rule.note,
                count=len(members),
                members=tuple(
                    RuleGroupMember(
                        candidate_ref=item.candidate_ref, expected_revision=item.revision
                    )
                    for item in members
                ),
                samples=tuple(_sample(members[index]) for index in spread(len(members))),
            )
        )
    groups.sort(key=lambda group: (-group.count, group.group_key))
    return RuleSuggestions(
        business_unit=business_unit,
        rules_version=ruleset.rules_version,
        pending_total=len(pending),
        unmatched_count=unmatched,
        groups=tuple(groups),
    )


def _sample(candidate: CandidateProjection) -> RuleGroupSample:
    return RuleGroupSample(
        candidate_ref=candidate.candidate_ref,
        short_id=candidate.short_id,
        summary=candidate.summary,
        amount_minor=candidate.amount_minor,
        current_category_label=candidate.category_label or "",
    )


Outcome = Literal["CONFIRMED", "NOT_MATCHED", "NOT_PENDING", "STALE", "REJECTED"]


class RuleBatchRequest(_Frozen):
    rules_version: str = Field(pattern=r"^[0-9a-f]{16}$")
    group_key: str = Field(min_length=14, max_length=113, pattern=r"^.+:[0-9a-f]{12}$")
    #: Leaves room for the " [规则 <rule_id>]" suffix inside the 1000-character reason.
    reason: str = Field(min_length=1, max_length=960)
    members: tuple[RuleGroupMember, ...] = Field(min_length=1, max_length=MAX_BATCH_MEMBERS)

    @model_validator(mode="after")
    def unique_members(self) -> RuleBatchRequest:
        refs = [member.candidate_ref for member in self.members]
        if len(refs) != len(set(refs)):
            raise ValueError("rule batch members must be unique")
        if not self.reason.strip():
            raise ValueError("rule batch reason must not be blank")
        return self


class RuleBatchOutcome(_Frozen):
    candidate_ref: UUID
    outcome: Outcome
    problem_code: str | None = None
    replayed: bool = False


class RuleBatchReceipt(_Frozen):
    contract_version: Literal["ledgerbridge.local-rule-batch-decision.v1"] = BATCH_CONTRACT
    group_key: str
    outcomes: tuple[RuleBatchOutcome, ...]


def decision_reason(reason: str, rule: Rule) -> str:
    return f"{reason} [规则 {rule.rule_id}]"


def category_of_group(group_key: str) -> str:
    return group_key.rpartition(":")[0]


__all__ = [
    "BATCH_CONTRACT",
    "DEFAULT_RULES_PATH",
    "MAX_BATCH_MEMBERS",
    "MAX_SAMPLES",
    "NATURES",
    "RULES_ENV",
    "SUGGESTIONS_CONTRACT",
    "LocalRulesInvalid",
    "LocalRulesNotConfigured",
    "Rule",
    "RuleBatchOutcome",
    "RuleBatchReceipt",
    "RuleBatchRequest",
    "RuleCategory",
    "RuleGroupMember",
    "RuleGroupSample",
    "RuleSet",
    "RuleSuggestionGroup",
    "RuleSuggestions",
    "build_suggestions",
    "category_of_group",
    "decision_reason",
    "group_key_of",
    "load_rules",
    "match",
    "match_text",
    "parse_rules",
    "rule_id",
    "rules_path",
    "spread",
]
