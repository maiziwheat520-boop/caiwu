"""Fail-closed financial-triage seam for admitted Hermes private messages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ledgerbridge.hermes_message import (
    HermesMessageDecision,
    HermesMessageDisposition,
    HermesPrivateMessage,
)


class HermesTriageLabel(StrEnum):
    FINANCIAL = "FINANCIAL"
    NON_FINANCIAL = "NON_FINANCIAL"
    AMBIGUOUS = "AMBIGUOUS"


class HermesTriageAction(StrEnum):
    CANDIDATE = "CANDIDATE"
    DELETE_TOMBSTONE = "DELETE_TOMBSTONE"
    AMBIGUOUS_RETAIN = "AMBIGUOUS_RETAIN"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class HermesTriageResult:
    label: HermesTriageLabel
    action: HermesTriageAction
    reason: str


class HermesTriageClassifier(Protocol):
    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel: ...


class UnavailableHermesTriageClassifier:
    """Default fail-closed classifier when no reviewed model/rule engine exists."""

    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        _ = message
        return HermesTriageLabel.AMBIGUOUS


class SyntheticKeywordHermesTriageClassifier:
    """Deterministic fixture classifier; never used by production composition."""

    _financial_markers = ("发票", "报销", "账单", "receipt", "invoice", "reimburse")

    def classify(self, message: HermesPrivateMessage) -> HermesTriageLabel:
        lowered = message.text.casefold()
        if any(marker.casefold() in lowered for marker in self._financial_markers):
            return HermesTriageLabel.FINANCIAL
        if message.text.strip():
            return HermesTriageLabel.NON_FINANCIAL
        return HermesTriageLabel.AMBIGUOUS


def triage_admitted_message(
    message: HermesPrivateMessage,
    admission: HermesMessageDecision,
    *,
    classifier: HermesTriageClassifier,
) -> HermesTriageResult:
    """Convert an admission decision into a safe triage action.

    Classifier failures and unknown labels retain the message as ambiguous; no
    code path here deletes bytes or creates a financial candidate by itself.
    """

    if admission.disposition is not HermesMessageDisposition.RETAIN_FOR_TRIAGE:
        return HermesTriageResult(
            label=HermesTriageLabel.AMBIGUOUS,
            action=HermesTriageAction.SKIP,
            reason=admission.reason.value,
        )
    try:
        label = classifier.classify(message)
    except Exception:
        label = HermesTriageLabel.AMBIGUOUS
    if not isinstance(label, HermesTriageLabel):
        label = HermesTriageLabel.AMBIGUOUS
    if label is HermesTriageLabel.FINANCIAL:
        return HermesTriageResult(label, HermesTriageAction.CANDIDATE, "classifier_financial")
    if label is HermesTriageLabel.NON_FINANCIAL:
        return HermesTriageResult(
            label, HermesTriageAction.DELETE_TOMBSTONE, "classifier_non_financial"
        )
    return HermesTriageResult(label, HermesTriageAction.AMBIGUOUS_RETAIN, "classifier_ambiguous")
