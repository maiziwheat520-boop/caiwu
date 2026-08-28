"""Offline OCR and deterministic bill-field extraction.

The OCR engine is an optional adapter.  Parsing remains side-effect free and
never turns an uncertain recognition into an approved accounting fact.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MIN_REQUIRED_CONFIDENCE_BP = 8500

_MONEY = re.compile(r"^(?:RMB\s*)?[¥￥]?([0-9][0-9,]*\.[0-9]{2})(?:元)?$")
_MOBILE_PERIOD = re.compile(r"账单周期[\uff1a:]\s*(\d{2}/\d{2})-(\d{2}/\d{2})")
_FULL_PERIOD = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})\s*(?:至|-)\s*"
    r"(\d{4})[-/](\d{2})[-/](\d{2})"
)
_DESKTOP_PERIOD_PREFIX = re.compile(r"\d{4}/\d{2}/\d{2}\s*至\s*\d{4}/\d{2}/\d{1,2}")
_BILL_ID = re.compile(r"账单ID[\uff1a:]\s*(\d{12,24})")
_ACCOUNT_NUMBER = re.compile(r"银行账号[\uff1a:]\s*(\d{8,30})")


class PreprocessingError(ValueError):
    """Raised when an image or OCR result violates the bounded contract."""


class BillSourceKind(StrEnum):
    MEITUAN_MOBILE = "MEITUAN_MOBILE"
    MEITUAN_DESKTOP = "MEITUAN_DESKTOP"
    CTRIP_EBOOKING = "CTRIP_EBOOKING"
    BANK_SUMMARY = "BANK_SUMMARY"
    UNKNOWN = "UNKNOWN"


class PreprocessingBlocker(StrEnum):
    MISSING_BILL_ID = "MISSING_BILL_ID"
    MISSING_PERIOD = "MISSING_PERIOD"
    MISSING_AMOUNT = "MISSING_AMOUNT"
    LOW_FIELD_CONFIDENCE = "LOW_FIELD_CONFIDENCE"
    CONTEXT_ONLY_IMAGE = "CONTEXT_ONLY_IMAGE"
    UNSUPPORTED_LAYOUT = "UNSUPPORTED_LAYOUT"


@dataclass(frozen=True, slots=True)
class OcrToken:
    text: str
    confidence_basis_points: int
    box: tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]

    def __post_init__(self) -> None:
        if not self.text.strip() or not 0 <= self.confidence_basis_points <= 10_000:
            raise PreprocessingError("OCR token is invalid")


@dataclass(frozen=True, slots=True)
class ExtractedBill:
    source_kind: BillSourceKind
    source_name: str
    hotel_label: str | None
    bill_id: str | None
    period_start: str | None
    period_end: str | None
    amount_minor: int | None
    payment_status: str | None
    account_last4: str | None
    confidence_basis_points: int
    blockers: tuple[PreprocessingBlocker, ...]

    @property
    def review_ready(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class BillExtraction:
    source_kind: BillSourceKind
    source_name: str
    token_count: int
    bills: tuple[ExtractedBill, ...]
    blockers: tuple[PreprocessingBlocker, ...] = ()


class OcrEngine(Protocol):
    def recognize(self, image_path: Path) -> tuple[OcrToken, ...]: ...


class RapidOcrEngine:
    """RapidOCR adapter; dependency import is delayed for non-OCR runtimes."""

    def __init__(self) -> None:
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise PreprocessingError("RapidOCR dependency is unavailable") from exc
        self._engine = RapidOCR()

    def recognize(self, image_path: Path) -> tuple[OcrToken, ...]:
        _require_image(image_path)
        result: Any = self._engine(str(image_path))
        texts = tuple(result.txts or ())
        scores = tuple(result.scores or ())
        boxes = tuple(result.boxes) if result.boxes is not None else ()
        if not (len(texts) == len(scores) == len(boxes)):
            raise PreprocessingError("OCR output arrays have different lengths")
        tokens: list[OcrToken] = []
        for text, score, box in zip(texts, scores, boxes, strict=True):
            normalized_box = tuple(
                (round(float(point[0])), round(float(point[1]))) for point in box
            )
            if len(normalized_box) != 4:
                raise PreprocessingError("OCR box must contain four points")
            first, second, third, fourth = normalized_box
            tokens.append(
                OcrToken(
                    text=" ".join(text.split()),
                    confidence_basis_points=max(0, min(10_000, round(score * 10_000))),
                    box=(first, second, third, fourth),
                )
            )
        return tuple(tokens)


def preprocess_image(image_path: Path, engine: OcrEngine) -> BillExtraction:
    """Recognize one image and extract reviewable bill rows."""

    _require_image(image_path)
    tokens = engine.recognize(image_path)
    source_kind = classify_source(tokens)
    return extract_bills(image_path.name, source_kind, tokens)


def classify_source(tokens: tuple[OcrToken, ...]) -> BillSourceKind:
    joined = "\n".join(token.text for token in tokens)
    if "eBooking" in joined and "结算账期" in joined:
        return BillSourceKind.CTRIP_EBOOKING
    if "预付账单" in joined and "账单ID" in joined:
        return BillSourceKind.MEITUAN_MOBILE
    if "美团酒店商家" in joined and "付款单ID" in joined:
        return BillSourceKind.MEITUAN_DESKTOP
    if "年度" in joined and "月收入均值" in joined:
        return BillSourceKind.BANK_SUMMARY
    return BillSourceKind.UNKNOWN


def extract_bills(
    source_name: str,
    source_kind: BillSourceKind,
    tokens: tuple[OcrToken, ...],
) -> BillExtraction:
    if source_kind is BillSourceKind.MEITUAN_MOBILE:
        bills = _extract_meituan_mobile(source_name, tokens)
        return BillExtraction(source_kind, source_name, len(tokens), bills)
    if source_kind is BillSourceKind.CTRIP_EBOOKING:
        bills = _extract_ctrip(source_name, tokens)
        return BillExtraction(source_kind, source_name, len(tokens), bills)
    if source_kind is BillSourceKind.MEITUAN_DESKTOP:
        bills = _extract_meituan_desktop(source_name, tokens)
        return BillExtraction(source_kind, source_name, len(tokens), bills)
    blocker = (
        PreprocessingBlocker.CONTEXT_ONLY_IMAGE
        if source_kind is BillSourceKind.BANK_SUMMARY
        else PreprocessingBlocker.UNSUPPORTED_LAYOUT
    )
    return BillExtraction(source_kind, source_name, len(tokens), (), (blocker,))


def _extract_meituan_mobile(
    source_name: str, tokens: tuple[OcrToken, ...]
) -> tuple[ExtractedBill, ...]:
    year = next((token.text[:4] for token in tokens if re.fullmatch(r"\d{4}年", token.text)), None)
    hotel = _hotel_label(tokens)
    bills: list[ExtractedBill] = []
    for index, period_token in enumerate(tokens):
        match = _MOBILE_PERIOD.search(period_token.text)
        if match is None:
            continue
        window = tokens[index + 1 : index + 9]
        amount_token = next((item for item in window if _money_minor(item.text) is not None), None)
        bill_token = next((item for item in window if _BILL_ID.search(item.text)), None)
        status_token = next(
            (
                item
                for item in window
                if any(word in item.text for word in ("未出账", "付款中", "已付款", "付款异常"))
            ),
            None,
        )
        start = f"{year}-{match.group(1).replace('/', '-')}" if year else None
        end = f"{year}-{match.group(2).replace('/', '-')}" if year else None
        bill_match = _BILL_ID.search(bill_token.text) if bill_token else None
        required = [period_token, amount_token, bill_token]
        blockers = _required_blockers(
            period_start=start,
            amount_minor=_money_minor(amount_token.text) if amount_token else None,
            bill_id=bill_match.group(1) if bill_match else None,
            tokens=required,
        )
        bills.append(
            ExtractedBill(
                BillSourceKind.MEITUAN_MOBILE,
                source_name,
                hotel,
                bill_match.group(1) if bill_match else None,
                start,
                end,
                _money_minor(amount_token.text) if amount_token else None,
                status_token.text if status_token else None,
                None,
                _confidence(required),
                blockers,
            )
        )
    return tuple(bills)


def _extract_ctrip(source_name: str, tokens: tuple[OcrToken, ...]) -> tuple[ExtractedBill, ...]:
    hotel = _hotel_label(tokens)
    account_last4 = None
    for token in tokens:
        match = _ACCOUNT_NUMBER.search(token.text)
        if match:
            account_last4 = match.group(1)[-4:]
            break
    hotel_id = next((token.text for token in tokens if re.fullmatch(r"\d{8}", token.text)), None)
    bills: list[ExtractedBill] = []
    for index, period_token in enumerate(tokens):
        period = _FULL_PERIOD.fullmatch(period_token.text.replace(" ", ""))
        if period is None:
            continue
        window = tokens[index + 1 : index + 9]
        amount_token = next(
            (item for item in window if item.text.startswith("RMB") and _money_minor(item.text)),
            None,
        )
        status_token = next(
            (
                item
                for item in window
                if any(word in item.text for word in ("已付款", "付款中", "账单生成中"))
            ),
            None,
        )
        start, end = _period_values(period)
        bill_id = f"{hotel_id}:{start}:{end}" if hotel_id else None
        required = [period_token, amount_token]
        blockers = _required_blockers(
            period_start=start,
            amount_minor=_money_minor(amount_token.text) if amount_token else None,
            bill_id=bill_id,
            tokens=required,
        )
        bills.append(
            ExtractedBill(
                BillSourceKind.CTRIP_EBOOKING,
                source_name,
                hotel,
                bill_id,
                start,
                end,
                _money_minor(amount_token.text) if amount_token else None,
                status_token.text if status_token else None,
                account_last4,
                _confidence(required),
                blockers,
            )
        )
    return tuple(bills)


def _extract_meituan_desktop(
    source_name: str, tokens: tuple[OcrToken, ...]
) -> tuple[ExtractedBill, ...]:
    hotel = _hotel_label(tokens)
    bills: list[ExtractedBill] = []
    for index in range(len(tokens) - 2):
        id_head, id_tail, period_token = tokens[index : index + 3]
        if not (re.fullmatch(r"\d{8}", id_head.text) and re.fullmatch(r"\d", id_tail.text)):
            continue
        if _DESKTOP_PERIOD_PREFIX.fullmatch(period_token.text) is None:
            continue
        window = tokens[index + 3 : index + 16]
        amount_token = next(
            (
                item
                for item in window
                if _money_minor(item.text) is not None and _money_minor(item.text) != 0
            ),
            None,
        )
        status_token = next((item for item in window if "付款" in item.text), None)
        period = _FULL_PERIOD.fullmatch(period_token.text.replace(" ", ""))
        start, end = _period_values(period) if period else (None, None)
        bill_id = id_head.text + id_tail.text
        required = [id_head, id_tail, period_token, amount_token]
        blockers = _required_blockers(
            period_start=start,
            amount_minor=_money_minor(amount_token.text) if amount_token else None,
            bill_id=bill_id,
            tokens=required,
        )
        bills.append(
            ExtractedBill(
                BillSourceKind.MEITUAN_DESKTOP,
                source_name,
                hotel,
                bill_id,
                start,
                end,
                _money_minor(amount_token.text) if amount_token else None,
                status_token.text if status_token else None,
                None,
                _confidence(required),
                blockers,
            )
        )
    return tuple(bills)


def _required_blockers(
    *,
    period_start: str | None,
    amount_minor: int | None,
    bill_id: str | None,
    tokens: list[OcrToken | None],
) -> tuple[PreprocessingBlocker, ...]:
    blockers: list[PreprocessingBlocker] = []
    if bill_id is None:
        blockers.append(PreprocessingBlocker.MISSING_BILL_ID)
    if period_start is None:
        blockers.append(PreprocessingBlocker.MISSING_PERIOD)
    if amount_minor is None:
        blockers.append(PreprocessingBlocker.MISSING_AMOUNT)
    if _confidence(tokens) < MIN_REQUIRED_CONFIDENCE_BP:
        blockers.append(PreprocessingBlocker.LOW_FIELD_CONFIDENCE)
    return tuple(blockers)


def _confidence(tokens: list[OcrToken | None]) -> int:
    present = [token.confidence_basis_points for token in tokens if token is not None]
    return min(present, default=0)


def _period_values(match: re.Match[str]) -> tuple[str, str]:
    groups = match.groups()
    return (
        f"{groups[0]}-{groups[1]}-{groups[2]}",
        f"{groups[3]}-{groups[4]}-{groups[5]}",
    )


def _money_minor(text: str) -> int | None:
    match = _MONEY.fullmatch(text.strip())
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _hotel_label(tokens: tuple[OcrToken, ...]) -> str | None:
    for token in tokens:
        if (
            ("酒店" in token.text or "公寓" in token.text)
            and len(token.text) >= 4
            and token.text not in {"美团酒店商家", "酒店收款信息", "酒店选择"}
        ):
            return token.text[:100]
    return None


def _require_image(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PreprocessingError("source image is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PreprocessingError("source image must be a regular file")
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise PreprocessingError("source image type is unsupported")
    if metadata.st_size <= 0 or metadata.st_size > MAX_IMAGE_BYTES:
        raise PreprocessingError("source image size is out of bounds")
