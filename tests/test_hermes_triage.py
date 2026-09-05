"""The triage seam must never delete bytes or open a candidate on its own."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from ledgerbridge.hermes_message import (
    HermesMessageDecision,
    HermesMessageDisposition,
    HermesMessageReason,
    HermesPrivateMessage,
)
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    HermesTriageClassifier,
    HermesTriageLabel,
    SyntheticKeywordHermesTriageClassifier,
    UnavailableHermesTriageClassifier,
    triage_admitted_message,
)

SENT_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _message(text: str = "这是一张发票") -> HermesPrivateMessage:
    return HermesPrivateMessage(
        message_id="hermes-0001",
        profile_ref="telegram-8906289598",
        profile_kind="primary",
        chat_kind="private",
        sender_kind="user",
        sent_at=SENT_AT,
        text=text,
    )


def _admitted() -> HermesMessageDecision:
    return HermesMessageDecision(
        HermesMessageDisposition.RETAIN_FOR_TRIAGE,
        HermesMessageReason.ELIGIBLE_PRIVATE,
    )


class _FixedClassifier:
    def __init__(self, label: object) -> None:
        self._label = label

    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        del message
        return cast(HermesTriageLabel, self._label)


class _FailingClassifier:
    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        del message
        raise RuntimeError("the reviewed model is unreachable")


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        (HermesMessageDisposition.DELETE_TOMBSTONE, HermesMessageReason.NON_PRIVATE_CHAT),
        (HermesMessageDisposition.IGNORE_HISTORY, HermesMessageReason.BEFORE_ACTIVATION),
    ],
)
def test_a_message_the_boundary_did_not_admit_is_never_classified(
    disposition: HermesMessageDisposition,
    reason: HermesMessageReason,
) -> None:
    # The classifier would say FINANCIAL; triage must not even ask it.
    result = triage_admitted_message(
        _message(),
        HermesMessageDecision(disposition, reason),
        classifier=_FixedClassifier(HermesTriageLabel.FINANCIAL),
    )
    assert result.action is HermesTriageAction.SKIP
    assert result.label is HermesTriageLabel.AMBIGUOUS
    assert result.reason == reason.value


@pytest.mark.parametrize(
    ("label", "action", "reason"),
    [
        (HermesTriageLabel.FINANCIAL, HermesTriageAction.CANDIDATE, "classifier_financial"),
        (
            HermesTriageLabel.NON_FINANCIAL,
            HermesTriageAction.DELETE_TOMBSTONE,
            "classifier_non_financial",
        ),
        (
            HermesTriageLabel.AMBIGUOUS,
            HermesTriageAction.AMBIGUOUS_RETAIN,
            "classifier_ambiguous",
        ),
    ],
)
def test_each_label_maps_to_exactly_one_action(
    label: HermesTriageLabel,
    action: HermesTriageAction,
    reason: str,
) -> None:
    result = triage_admitted_message(_message(), _admitted(), classifier=_FixedClassifier(label))
    assert (result.label, result.action, result.reason) == (label, action, reason)


def test_a_classifier_that_raises_retains_the_message_instead_of_deleting_it() -> None:
    result = triage_admitted_message(_message(), _admitted(), classifier=_FailingClassifier())
    assert result.label is HermesTriageLabel.AMBIGUOUS
    assert result.action is HermesTriageAction.AMBIGUOUS_RETAIN


@pytest.mark.parametrize("label", ["FINANCIAL", None, 1, object()])
def test_a_classifier_that_answers_off_contract_is_treated_as_ambiguous(label: object) -> None:
    # A bare "FINANCIAL" string is the dangerous case: it compares equal to the
    # StrEnum member, so only an isinstance check keeps it out of CANDIDATE.
    result = triage_admitted_message(_message(), _admitted(), classifier=_FixedClassifier(label))
    assert result.label is HermesTriageLabel.AMBIGUOUS
    assert result.action is HermesTriageAction.AMBIGUOUS_RETAIN


def test_the_default_classifier_admits_nothing_by_itself() -> None:
    classifier: HermesTriageClassifier = UnavailableHermesTriageClassifier()
    assert classifier.classify(_message()) is HermesTriageLabel.AMBIGUOUS
    result = triage_admitted_message(_message(), _admitted(), classifier=classifier)
    assert result.action is HermesTriageAction.AMBIGUOUS_RETAIN


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("这是一张发票", HermesTriageLabel.FINANCIAL),
        ("请帮我报销", HermesTriageLabel.FINANCIAL),
        ("八月账单", HermesTriageLabel.FINANCIAL),
        ("Here is the RECEIPT", HermesTriageLabel.FINANCIAL),
        ("Invoice attached", HermesTriageLabel.FINANCIAL),
        ("please reimburse me", HermesTriageLabel.FINANCIAL),
        ("晚饭吃什么", HermesTriageLabel.NON_FINANCIAL),
        ("   ", HermesTriageLabel.AMBIGUOUS),
        ("", HermesTriageLabel.AMBIGUOUS),
    ],
)
def test_the_synthetic_fixture_classifier_is_case_folded_and_deterministic(
    text: str,
    label: HermesTriageLabel,
) -> None:
    classifier = SyntheticKeywordHermesTriageClassifier()
    assert classifier.classify(_message(text)) is label
    assert classifier.classify(_message(text)) is label
