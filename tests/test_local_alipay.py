from __future__ import annotations

from pathlib import Path

import pytest

from ledgerbridge.local_alipay import LocalAlipayError, read_alipay_export
from ledgerbridge.local_payments import DIRECTIONLESS

_COLON = chr(0xFF1A)
_FULL_COMMA = chr(0xFF0C)

_HEADER = (
    "交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,"
    "收/付款方式,交易状态,交易订单号,商家订单号,备注,"
)

_NEUTRAL = "不计收支"


def _expenditure(serial: str = "1" * 28, amount: str = "26.00") -> tuple[str, ...]:
    return (
        "2026-03-01 09:00:00",
        "商户消费",
        "合成商户",
        "syn***@example.com",
        "合成商品",
        "支出",
        amount,
        "建设银行储蓄卡(0000)",
        "交易成功",
        serial,
        "2" * 30,
        "",
    )


def _neutral(serial: str = "3" * 28, amount: str = "100.00") -> tuple[str, ...]:
    return (
        "2026-03-02 09:00:00",
        "投资理财",
        "余额宝",
        "/",
        "支付宝转入到余额宝",
        _NEUTRAL,
        amount,
        "",
        "交易成功",
        serial,
        "",
        "",
    )


def _write_synthetic_export(
    path: Path,
    *,
    rows: tuple[tuple[str, ...], ...] | None = None,
    declared_total: int | None = None,
    expenditure_total: str = "26.00",
    account: str = "syn***@example.com",
    period: str = "2026-03-01 00:00:00] 终止时间" + _COLON + "[2026-03-31 23:59:59",
    header: str = _HEADER,
) -> bytes:
    """One synthetic export. Nothing here is a real transaction."""

    body = rows if rows is not None else (_expenditure(), _neutral())
    expenditure = [row for row in body if row[5] == "支出"]
    income = [row for row in body if row[5] == "收入"]
    neutral = [row for row in body if row[5] == _NEUTRAL]
    lines = [
        "-" * 84,
        "账户信息" + _COLON,
        "姓名" + _COLON + "合成用户",
        f"支付宝账户{_COLON}{account}",
        f"起始时间{_COLON}[{period}]",
        f"导出交易类型{_COLON}[全部]",
        f"导出时间{_COLON}[2026-04-01 03:00:00]",
        f"共{declared_total if declared_total is not None else len(body)}笔记录",
        f"收入{_COLON}{len(income)}笔 {_total(income)}元",
        f"支出{_COLON}{len(expenditure)}笔 {expenditure_total}元",
        f"{_NEUTRAL}{_COLON}{len(neutral)}笔 {_total(neutral)}元",
        "",
        "特别提示" + _COLON,
        # The footnote in which Alipay declines to stand behind its own
        # amount totals. The reader does not parse it; it is here because a
        # real file has it and the fixture should look like one.
        "6." + "因统计逻辑不同" + _FULL_COMMA + "请以实际交易金额为准",
        "",
        "-" * 24 + "支付宝支付科技有限公司  电子客户回单" + "-" * 24,
        header,
        # Alipay pads the two order-number columns with a tab.
        *(",".join((*row[:9], row[9] + "\t", row[10] + "\t", row[11], "")) for row in body),
    ]
    raw = "\n".join(lines).encode("gb18030")
    path.write_bytes(raw)
    return raw


def _total(rows: list[tuple[str, ...]]) -> str:
    return f"{sum(float(row[6]) for row in rows):.2f}"


def test_reads_an_export_and_normalises_the_direction_alipay_declines_to_give(
    tmp_path: Path,
) -> None:
    export = read_alipay_export(_write_synthetic_export(tmp_path / "bill.csv"))

    assert len(export.rows) == 2
    spent, moved = export.rows
    assert spent.amount_minor == 2600
    assert spent.signed_amount_minor == -2600
    assert spent.serial == "1" * 28
    # 不计收支 means what WeChat's "/" means, and is stored the same way so one
    # category rule covers both platforms.
    assert moved.direction == DIRECTIONLESS
    assert moved.signed_amount_minor == 10_000
    assert export.account_hint == "syn***@example.com"
    assert export.export_kind == "全部"


def test_the_counterparty_account_is_read_past_and_not_kept(tmp_path: Path) -> None:
    """It is someone else's email or phone number, and the ledger has no use for it."""

    export = read_alipay_export(
        _write_synthetic_export(tmp_path / "bill.csv", account="owner@example.com")
    )

    row = export.rows[0]
    kept = [getattr(row, field) for field in row.__slots__ if isinstance(getattr(row, field), str)]
    assert "syn***@example.com" not in kept
    # The owner's own login is kept, once, and only on the export.
    assert export.account_hint == "owner@example.com"


def test_refuses_an_export_that_miscounts_its_own_records(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "bill.csv", declared_total=3)

    with pytest.raises(LocalAlipayError, match="declares 3 records but holds 2"):
        read_alipay_export(raw)


def test_a_wrong_amount_total_is_not_a_refusal(tmp_path: Path) -> None:
    """Alipay's own footnotes say the totals may not match the rows; the counts do."""

    raw = _write_synthetic_export(tmp_path / "bill.csv", expenditure_total="99.00")

    assert len(read_alipay_export(raw).rows) == 2


def test_refuses_an_export_with_one_row_too_few_for_its_own_subtotal(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "bill.csv",
        rows=(_expenditure(), _neutral()),
        declared_total=2,
    ).replace("支出".encode("gb18030") + b"\xa3\xba1", "支出".encode("gb18030") + b"\xa3\xba2")

    with pytest.raises(LocalAlipayError, match="declares 2 支出 rows but holds 1"):
        read_alipay_export(raw)


def test_refuses_a_row_outside_the_period_the_export_claims(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "bill.csv",
        period="2026-03-01 00:00:00] 终止时间" + _COLON + "[2026-03-01 23:59:59",
    )

    with pytest.raises(LocalAlipayError, match="outside its own period"):
        read_alipay_export(raw)


def test_refuses_an_export_that_repeats_an_order_number(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "bill.csv",
        rows=(_expenditure(), _expenditure()),
        expenditure_total="52.00",
    )

    with pytest.raises(LocalAlipayError, match="repeats a transaction serial"):
        read_alipay_export(raw)


def test_refuses_a_row_carrying_no_order_number(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "bill.csv", rows=(_expenditure(serial=""),))

    with pytest.raises(LocalAlipayError, match="serial is invalid"):
        read_alipay_export(raw)


def test_refuses_an_amount_finer_than_a_cent(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "bill.csv", rows=(_expenditure(amount="26.001"),))

    with pytest.raises(LocalAlipayError, match="amount is invalid"):
        read_alipay_export(raw)


def test_refuses_an_export_that_names_no_account(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "bill.csv").replace(
        "支付宝账户".encode("gb18030"), "登录账户".encode("gb18030")
    )

    with pytest.raises(LocalAlipayError, match="does not name the account"):
        read_alipay_export(raw)


def test_refuses_a_file_whose_columns_are_not_this_bill(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "bill.csv", header=_HEADER.replace("金额", "余额"))

    with pytest.raises(LocalAlipayError, match="no transaction header row"):
        read_alipay_export(raw)
