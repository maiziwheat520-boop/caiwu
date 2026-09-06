"""What the bill preprocessor refuses to read.

The extractor is allowed to return a bill with blockers, but it is never allowed
to invent one.  These cases hold the adapter boundary -- the optional OCR
dependency, the image on disk, the shape of the engine's answer -- and the
places where a field is absent rather than merely low quality.
"""

import os
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ledgerbridge import bill_preprocessing
from ledgerbridge.bill_preprocessing import (
    MAX_IMAGE_BYTES,
    BillSourceKind,
    OcrToken,
    PreprocessingBlocker,
    PreprocessingError,
    RapidOcrEngine,
    _money_minor,
    classify_source,
    extract_bills,
    preprocess_image,
)

_BOX = ((0, 0), (10, 0), (10, 10), (0, 10))
_FLOAT_BOX = ((0.4, 0.4), (10.2, 0.4), (10.2, 10.2), (0.4, 10.2))


class _Recognition:
    """The three parallel arrays a RapidOCR call returns."""

    def __init__(
        self,
        txts: tuple[str, ...] | None,
        scores: tuple[float, ...] | None,
        boxes: tuple[tuple[tuple[float, float], ...], ...] | None,
    ) -> None:
        self.txts = txts
        self.scores = scores
        self.boxes = boxes


class _StubRapidOcr(ModuleType):
    """Stands in for the optional ``rapidocr`` distribution.

    Built without an engine it is a module that does not export ``RapidOCR``,
    which is what an install missing the ``ocr`` extra looks like from here.
    """

    RapidOCR: type[object]

    def __init__(self, engine: type[object] | None = None) -> None:
        super().__init__("rapidocr")
        if engine is not None:
            self.RapidOCR = engine


def _install(monkeypatch: pytest.MonkeyPatch, answer: _Recognition | None) -> None:
    """Put a stub ``rapidocr`` in front of whatever the runtime really has."""

    if answer is None:
        monkeypatch.setitem(sys.modules, "rapidocr", _StubRapidOcr())
        return

    recognized = answer

    class _Engine:
        def __call__(self, image_path: str) -> _Recognition:
            return recognized

    monkeypatch.setitem(sys.modules, "rapidocr", _StubRapidOcr(_Engine))


def _image(tmp_path: Path, name: str = "bill.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


class _SilentEngine:
    """An engine the image checks are expected to run before."""

    def recognize(self, image_path: Path) -> tuple[OcrToken, ...]:
        raise AssertionError("the image should have been refused before recognition")


class _ScriptedEngine:
    """An engine that answers with tokens the test chose."""

    def __init__(self, tokens: tuple[OcrToken, ...]) -> None:
        self._tokens = tokens

    def recognize(self, image_path: Path) -> tuple[OcrToken, ...]:
        return self._tokens


def _tokens(*values: str, confidence: int = 9900) -> tuple[OcrToken, ...]:
    return tuple(OcrToken(value, confidence, _BOX) for value in values)


def test_a_token_without_text_is_refused() -> None:
    with pytest.raises(PreprocessingError, match="OCR token is invalid"):
        OcrToken("   ", 9900, _BOX)


@pytest.mark.parametrize("confidence", [-1, 10_001])
def test_a_token_confidence_outside_the_basis_point_range_is_refused(confidence: int) -> None:
    with pytest.raises(PreprocessingError, match="OCR token is invalid"):
        OcrToken("预付账单", confidence, _BOX)


def test_the_ocr_adapter_refuses_to_start_without_its_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, None)

    with pytest.raises(PreprocessingError, match="RapidOCR dependency is unavailable"):
        RapidOcrEngine()


def test_the_ocr_adapter_turns_the_engine_answer_into_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        monkeypatch,
        _Recognition(("预付  账单", "7625.20"), (0.99, 0.87), (_FLOAT_BOX, _FLOAT_BOX)),
    )

    tokens = RapidOcrEngine().recognize(_image(tmp_path))

    assert [token.text for token in tokens] == ["预付 账单", "7625.20"]
    assert [token.confidence_basis_points for token in tokens] == [9900, 8700]
    assert tokens[0].box == ((0, 0), (10, 0), (10, 10), (0, 10))


@pytest.mark.parametrize(("score", "expected"), [(-0.5, 0), (1.5, 10_000)])
def test_the_ocr_adapter_holds_a_score_inside_the_basis_point_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, score: float, expected: int
) -> None:
    _install(monkeypatch, _Recognition(("预付账单",), (score,), (_FLOAT_BOX,)))

    tokens = RapidOcrEngine().recognize(_image(tmp_path))

    assert tokens[0].confidence_basis_points == expected


def test_the_ocr_adapter_reads_an_answer_without_boxes_as_no_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Recognition(None, None, None))

    assert RapidOcrEngine().recognize(_image(tmp_path)) == ()


def test_the_ocr_adapter_refuses_arrays_of_different_lengths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Recognition(("预付账单", "7625.20"), (0.99,), (_FLOAT_BOX,)))

    with pytest.raises(PreprocessingError, match="different lengths"):
        RapidOcrEngine().recognize(_image(tmp_path))


def test_the_ocr_adapter_refuses_a_box_that_is_not_four_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Recognition(("预付账单",), (0.99,), (_FLOAT_BOX[:3],)))

    with pytest.raises(PreprocessingError, match="four points"):
        RapidOcrEngine().recognize(_image(tmp_path))


def test_the_ocr_adapter_checks_the_image_before_calling_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _Recognition(("预付账单",), (0.99,), (_FLOAT_BOX,)))

    with pytest.raises(PreprocessingError, match="source image is unavailable"):
        RapidOcrEngine().recognize(tmp_path / "absent.png")


def test_preprocessing_classifies_and_extracts_what_the_engine_read(tmp_path: Path) -> None:
    engine = _ScriptedEngine(
        _tokens(
            "预付账单",
            "测试商务公寓(车站店)",
            "2026年",
            "账单周期:05/18-05/24",
            "7625.20",
            "账单ID:2056095067379466251",
            "美团已付款",
        )
    )

    result = preprocess_image(_image(tmp_path), engine)

    assert result.source_kind is BillSourceKind.MEITUAN_MOBILE
    assert result.source_name == "bill.png"
    assert result.token_count == 7
    assert result.bills[0].bill_id == "2056095067379466251"
    assert result.bills[0].review_ready


def test_an_image_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PreprocessingError, match="source image is unavailable"):
        preprocess_image(tmp_path / "absent.png", _SilentEngine())


def test_a_directory_is_not_accepted_as_an_image(tmp_path: Path) -> None:
    folder = tmp_path / "bill.png"
    folder.mkdir()

    with pytest.raises(PreprocessingError, match="must be a regular file"):
        preprocess_image(folder, _SilentEngine())


def test_an_empty_image_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")

    with pytest.raises(PreprocessingError, match="size is out of bounds"):
        preprocess_image(empty, _SilentEngine())


def test_an_image_over_the_size_bound_is_refused(tmp_path: Path) -> None:
    oversized = _image(tmp_path, "oversized.png")
    os.truncate(oversized, MAX_IMAGE_BYTES + 1)

    with pytest.raises(PreprocessingError, match="size is out of bounds"):
        preprocess_image(oversized, _SilentEngine())


def test_a_layout_no_rule_recognises_is_classified_as_unknown() -> None:
    tokens = _tokens("收款凭证", "金额", "1234.00")

    kind = classify_source(tokens)

    assert kind is BillSourceKind.UNKNOWN
    assert extract_bills("bill.png", kind, tokens).blockers == (
        PreprocessingBlocker.UNSUPPORTED_LAYOUT,
    )


def test_a_desktop_row_without_a_settlement_period_is_not_read_as_a_bill() -> None:
    tokens = _tokens(
        "美团酒店商家",
        "付款单ID",
        "20260518",
        "7",
        "结算中",
        "7625.20",
    )

    result = extract_bills("bill.png", classify_source(tokens), tokens)

    assert result.source_kind is BillSourceKind.MEITUAN_DESKTOP
    assert result.bills == ()


def test_a_mobile_bill_without_an_id_or_an_amount_states_both_blockers() -> None:
    tokens = _tokens("预付账单", "账单ID", "2026年", "账单周期:05/18-05/24")

    result = extract_bills("bill.png", classify_source(tokens), tokens)

    bill = result.bills[0]
    assert bill.bill_id is None
    assert bill.amount_minor is None
    assert bill.period_start == "2026-05-18"
    assert bill.blockers == (
        PreprocessingBlocker.MISSING_BILL_ID,
        PreprocessingBlocker.MISSING_AMOUNT,
    )
    assert not bill.review_ready


def test_an_amount_the_decimal_parser_refuses_is_not_read_as_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The money pattern already excludes this, and the parser still checks."""

    monkeypatch.setattr(bill_preprocessing, "_MONEY", re.compile(r"(.+)"))

    assert _money_minor("七千六百二十五元") is None
