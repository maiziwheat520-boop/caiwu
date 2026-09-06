"""What the Hermes triage seam does when the classifier is not trustworthy.

Triage is fail-closed: nothing here deletes bytes or creates a financial
candidate on its own, and every uncertain answer -- a refusal, an exception, a
value that is not a label at all -- has to land on retaining the message.
"""

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

SENT = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)


class _FixedClassifier:
    """Answers with whatever the test decided, label or not."""

    def __init__(self, answer: object) -> None:
        self._answer = answer

    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        return cast("HermesTriageLabel", self._answer)


class _BrokenClassifier:
    """Stands in for a classifier that is down or misconfigured."""

    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        raise RuntimeError("classifier is unavailable")


def _message(text: str = "这是一张发票") -> HermesPrivateMessage:
    return HermesPrivateMessage(
        message_id="m-1",
        profile_ref="telegram-8906289598",
        profile_kind="primary",
        chat_kind="private",
        sender_kind="user",
        sent_at=SENT,
        text=text,
    )


def _admitted() -> HermesMessageDecision:
    return HermesMessageDecision(
        HermesMessageDisposition.RETAIN_FOR_TRIAGE,
        HermesMessageReason.ELIGIBLE_PRIVATE,
    )


def _triage(
    classifier: HermesTriageClassifier,
    *,
    admission: HermesMessageDecision | None = None,
    text: str = "这是一张发票",
) -> tuple[HermesTriageLabel, HermesTriageAction, str]:
    result = triage_admitted_message(
        _message(text), admission or _admitted(), classifier=classifier
    )
    return result.label, result.action, result.reason


def test_a_message_that_was_never_admitted_is_skipped_without_classifying() -> None:
    admission = HermesMessageDecision(
        HermesMessageDisposition.DELETE_TOMBSTONE,
        HermesMessageReason.NON_PRIVATE_CHAT,
    )

    assert _triage(_BrokenClassifier(), admission=admission) == (
        HermesTriageLabel.AMBIGUOUS,
        HermesTriageAction.SKIP,
        HermesMessageReason.NON_PRIVATE_CHAT.value,
    )


def test_a_financial_label_earns_a_candidate() -> None:
    assert _triage(_FixedClassifier(HermesTriageLabel.FINANCIAL)) == (
        HermesTriageLabel.FINANCIAL,
        HermesTriageAction.CANDIDATE,
        "classifier_financial",
    )


def test_a_non_financial_label_earns_a_tombstone() -> None:
    assert _triage(_FixedClassifier(HermesTriageLabel.NON_FINANCIAL)) == (
        HermesTriageLabel.NON_FINANCIAL,
        HermesTriageAction.DELETE_TOMBSTONE,
        "classifier_non_financial",
    )


def test_an_ambiguous_label_is_retained() -> None:
    assert _triage(_FixedClassifier(HermesTriageLabel.AMBIGUOUS)) == (
        HermesTriageLabel.AMBIGUOUS,
        HermesTriageAction.AMBIGUOUS_RETAIN,
        "classifier_ambiguous",
    )


def test_a_classifier_that_raises_retains_rather_than_deletes() -> None:
    assert _triage(_BrokenClassifier()) == (
        HermesTriageLabel.AMBIGUOUS,
        HermesTriageAction.AMBIGUOUS_RETAIN,
        "classifier_ambiguous",
    )


@pytest.mark.parametrize("answer", ["FINANCIAL", None, 1])
def test_an_answer_that_is_not_a_label_retains_rather_than_deletes(answer: object) -> None:
    assert _triage(_FixedClassifier(answer)) == (
        HermesTriageLabel.AMBIGUOUS,
        HermesTriageAction.AMBIGUOUS_RETAIN,
        "classifier_ambiguous",
    )


def test_the_default_classifier_calls_everything_ambiguous() -> None:
    assert UnavailableHermesTriageClassifier().classify(_message()) is HermesTriageLabel.AMBIGUOUS


@pytest.mark.parametrize(
    "text",
    [
        "这是一张发票",
        "报销单",
        "本月账单",
        "here is a receipt",
        "INVOICE 42",
        "please reimburse me",
    ],
)
def test_the_fixture_classifier_reads_its_markers_in_either_script(text: str) -> None:
    classifier = SyntheticKeywordHermesTriageClassifier()

    assert classifier.classify(_message(text)) is HermesTriageLabel.FINANCIAL


def test_the_fixture_classifier_calls_ordinary_text_non_financial() -> None:
    classifier = SyntheticKeywordHermesTriageClassifier()

    assert classifier.classify(_message("晚上吃什么")) is HermesTriageLabel.NON_FINANCIAL


def test_the_fixture_classifier_will_not_guess_at_blank_text() -> None:
    classifier = SyntheticKeywordHermesTriageClassifier()

    assert classifier.classify(_message("   ")) is HermesTriageLabel.AMBIGUOUS
