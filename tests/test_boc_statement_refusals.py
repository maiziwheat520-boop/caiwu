"""Every way an encrypted BOC personal PDF export can be refused.

The parser is the only thing standing between a bank PDF and the ledger, and
the file it reads is encrypted, layout-extracted and summed by the bank itself.
Each refusal below is a fact the statement has to prove about itself: that it
is the file whose digest was approved, that its password came from the external
registry, that every page agrees about whose account it is, and that the rows
reconcile against the totals the bank printed on them.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from ledgerbridge.boc_statement import (
    BocStatementError,
    _bounded_text,
    _clean_layout_cells,
    _combined_transaction_name,
    _correct_amount_signs_by_balance,
    _find_footer,
    _join_parts,
    _money_minor,
    _parse_date,
    _parse_datetime,
    _ParsedTransaction,
    parse_boc_personal_pdf,
)
from tests.test_boc_bank_statement import (
    _ACCOUNT,
    _CARD,
    _HEADERS,
    _PASSWORD,
    _SUFFIX,
    _line,
)

_ENV = "LEDGERBRIDGE_BANK_STATEMENT_PASSWORD_REGISTRY"
_PDF_BYTES = b"%PDF-1.7\nsynthetic-boc"
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")


# --- page construction -----------------------------------------------------


def _row(
    *,
    date_text: str = "2026-05-02",
    time_text: str = "10:00:00",
    amount: str = "20.00",
    balance: str = "107.66",
    name: str = "转入",
    channel: str = "手机银行",
    branch: str = "测试网点",
    note: str = "合成附言",
    counterparty: str = "乙",
    counterparty_account: str = "6222000000000002",
    counterparty_institution: str = "测试银行乙",
) -> str:
    return _line(
        (
            date_text,
            time_text,
            "人民币",
            amount,
            balance,
            name,
            channel,
            branch,
            note,
            counterparty,
            counterparty_account,
            counterparty_institution,
        )
    )


_OLDER_ROW = _row(
    date_text="2026-05-01",
    time_text="09:00:00",
    amount="-12.34",
    balance="87.66",
    name="消费",
    channel="快捷支付",
    counterparty="甲",
    counterparty_account="6222000000000001",
    counterparty_institution="测试银行甲",
)


def _continuation(**cells: str) -> str:
    """A layout line that carries only text columns, starting at column 60."""

    values = ["", "", "", "", ""] + [cells.get(header, "") for header in _HEADERS[5:]]
    return _line(tuple(values))


def _page(
    *,
    page_number: int = 1,
    page_count: int = 1,
    start: str = "2026-05-01",
    end: str = "2026-05-02",
    holder: str = "合成测试用户",
    card: str = _CARD,
    account: str = _ACCOUNT,
    printed: str = "2026/06/03 12:00:00",
    debit: str = "12.34",
    credit: str = "20.00",
    rows: str = "2",
    body: tuple[str, ...] | None = None,
    header_count: int = 1,
    range_count: int = 1,
    footer: str | None = "END",
) -> str:
    lines = ["中国银行交易明细"]
    range_line = f"交易区间: {start} 至 {end} 客户姓名: {holder} 页数: {page_number} / {page_count}"
    lines.extend([range_line] * range_count)
    lines.append(f"借记卡号: {card} 借方发生数: {debit} 贷方发生数: {credit} 行数: {rows}")
    lines.append(f"账号: {account} 打印时间: {printed}")
    lines.extend([_line(_HEADERS)] * header_count)
    lines.extend(body if body is not None else (_row(), _OLDER_ROW))
    if footer is not None:
        lines.append(footer)
    return "\n".join(lines)


# --- stub PDF reader -------------------------------------------------------


class _StubPage:
    def __init__(self, text: object, error: Exception | None = None) -> None:
        self._text = text
        self._error = error

    def extract_text(self, *, extraction_mode: str) -> object:
        assert extraction_mode == "layout"
        if self._error is not None:
            raise self._error
        return self._text


class _StubReader:
    def __init__(
        self,
        pages: list[_StubPage],
        *,
        encrypted: bool = True,
        decrypted: int = 1,
        decrypt_error: Exception | None = None,
    ) -> None:
        self.is_encrypted = encrypted
        self.pages = pages
        self._decrypted = decrypted
        self._decrypt_error = decrypt_error

    def decrypt(self, password: str) -> int:
        if self._decrypt_error is not None:
            raise self._decrypt_error
        return self._decrypted if password == _PASSWORD else 0


class _UnreadablePagesReader:
    is_encrypted = True

    @property
    def pages(self) -> list[_StubPage]:
        raise RuntimeError("the page tree is damaged")

    def decrypt(self, password: str) -> int:
        del password
        return 1


def _reader_for(*texts: str) -> _StubReader:
    return _StubReader([_StubPage(text) for text in texts])


# --- fixture plumbing ------------------------------------------------------


def _registry_payload(digest: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "entries": [{"status": "verified", "attachment_sha256": digest, "password": _PASSWORD}],
        }
    )


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader: object = None,
    reader_error: Exception | None = None,
    source_bytes: bytes = _PDF_BYTES,
    registry_text: str | None = None,
    set_env: bool = True,
) -> tuple[Path, str]:
    source = (tmp_path / "synthetic.pdf").resolve()
    source.write_bytes(source_bytes)
    digest = hashlib.sha256(source_bytes).hexdigest()
    registry = (tmp_path / "passwords.json").resolve()
    registry.write_text(
        registry_text if registry_text is not None else _registry_payload(digest),
        encoding="utf-8",
    )
    registry.chmod(0o600)
    if set_env:
        monkeypatch.setenv(_ENV, str(registry))
    else:
        monkeypatch.delenv(_ENV, raising=False)

    def _factory(_: object) -> object:
        if reader_error is not None:
            raise reader_error
        return reader if reader is not None else _reader_for(_page())

    monkeypatch.setattr("ledgerbridge.boc_statement.PdfReader", _factory)
    return source, digest


def _refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    *,
    suffix: str = _SUFFIX,
    **prepare: Any,
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch, **prepare)
    with pytest.raises(BocStatementError, match=message):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=suffix)


# --- the arguments the caller supplies -------------------------------------


def test_the_expected_digest_must_be_a_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(BocStatementError, match="expected source digest is invalid"):
        parse_boc_personal_pdf(source, expected_sha256="0" * 63, managed_account_suffix=_SUFFIX)


def test_the_managed_account_suffix_must_be_four_to_eight_digits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    with pytest.raises(BocStatementError, match="managed account suffix is invalid"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix="321")


def test_a_source_whose_bytes_moved_since_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The digest is what the operator approved; the file has to still be it.
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(BocStatementError, match="source digest changed"):
        parse_boc_personal_pdf(source, expected_sha256="a" * 64, managed_account_suffix=_SUFFIX)


def test_a_source_that_is_not_a_pdf_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(tmp_path, monkeypatch, "statement is not a PDF", source_bytes=b"PK\x03\x04not-a-pdf")


def test_a_relative_source_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(BocStatementError, match="input path must be absolute"):
        parse_boc_personal_pdf(
            Path("synthetic.pdf"), expected_sha256="a" * 64, managed_account_suffix=_SUFFIX
        )


# --- the external password registry ----------------------------------------


def test_a_missing_password_registry_stops_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(tmp_path, monkeypatch, "password registry is unavailable", set_env=False)


def test_a_padded_registry_path_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    monkeypatch.setenv(_ENV, f"  {tmp_path / 'passwords.json'}  ")
    with pytest.raises(BocStatementError, match="password registry is unavailable"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


@pytest.mark.parametrize(
    "registry_text",
    [
        "not json at all",
        "[]",
        json.dumps({"schema_version": 2, "entries": []}),
        json.dumps({"schema_version": 1, "entries": {}}),
    ],
)
def test_a_registry_that_is_not_the_expected_document_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_text: str
) -> None:
    _refuse(tmp_path, monkeypatch, "password registry is invalid", registry_text=registry_text)


def test_a_registry_that_is_not_utf8_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    (tmp_path / "passwords.json").write_bytes(b"\xff\xfe{}")
    with pytest.raises(BocStatementError, match="password registry is invalid"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_registry_listing_too_many_entries_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk = json.dumps({"schema_version": 1, "entries": [{"status": "verified"}] * 1001})
    _refuse(tmp_path, monkeypatch, "password registry is invalid", registry_text=bulk)


@pytest.mark.parametrize("password", ["", "p" * 201, "pass\x00word", 1234, None])
def test_a_matched_entry_without_a_storable_password_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, password: object
) -> None:
    source = (tmp_path / "synthetic.pdf").resolve()
    source.write_bytes(_PDF_BYTES)
    digest = hashlib.sha256(_PDF_BYTES).hexdigest()
    _refuse(
        tmp_path,
        monkeypatch,
        "password registry match is invalid",
        registry_text=json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {"status": "verified", "attachment_sha256": digest, "password": password}
                ],
            }
        ),
    )


def test_an_oversize_registry_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    padding = json.dumps({"schema_version": 1, "entries": [], "pad": "x" * (64 * 1024)})
    _refuse(tmp_path, monkeypatch, "input file size is invalid", registry_text=padding)


def test_an_empty_registry_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(tmp_path, monkeypatch, "input file size is invalid", registry_text="")


def test_a_registry_that_is_a_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    directory = tmp_path / "registry-dir"
    directory.mkdir()
    monkeypatch.setenv(_ENV, str(directory.resolve()))
    with pytest.raises(BocStatementError, match="input file must be regular"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_registry_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    monkeypatch.setenv(_ENV, str((tmp_path / "absent.json").resolve()))
    with pytest.raises(BocStatementError, match="input file is unavailable"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


@_POSIX_ONLY
def test_a_world_readable_registry_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry holds statement passwords; a mode anyone can read is a leak.
    source, digest = _prepare(tmp_path, monkeypatch)
    (tmp_path / "passwords.json").chmod(0o644)
    with pytest.raises(BocStatementError, match="permissions are too broad"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


@_POSIX_ONLY
def test_a_registry_reached_through_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "passwords.json")
    monkeypatch.setenv(_ENV, str(link.resolve(strict=False).parent / "link.json"))
    with pytest.raises(BocStatementError, match="input file must be regular"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_open = os.open

    def _refusing_open(path: object, flags: int, *args: object) -> int:
        if str(path).endswith("synthetic.pdf"):
            raise OSError("permission denied")
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _refusing_open)
    with pytest.raises(BocStatementError, match="input file cannot be opened"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_fails_mid_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_fdopen = os.fdopen

    class _FailingStream:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "rb")

        def __enter__(self) -> _FailingStream:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def read(self, size: int) -> bytes:
            del size
            raise OSError("the device disappeared")

    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _FailingStream(descriptor))
    with pytest.raises(BocStatementError, match="input file cannot be read"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_shrinks_between_stat_and_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The size is checked before the bytes are hashed, so the two have to agree.
    source, digest = _prepare(tmp_path, monkeypatch)
    real_fdopen = os.fdopen

    class _TruncatingStream:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "rb")

        def __enter__(self) -> _TruncatingStream:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def read(self, size: int) -> bytes:
            return self._stream.read(size)[:-1]

    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _TruncatingStream(descriptor))
    with pytest.raises(BocStatementError, match="input file changed while reading"):
        parse_boc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


# --- opening the encrypted document ----------------------------------------


def test_a_document_pypdf_cannot_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement PDF is invalid",
        reader_error=RuntimeError("the xref table is damaged"),
    )


def test_a_decryption_that_raises_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement PDF cannot be decrypted",
        reader=_StubReader([_StubPage(_page())], decrypt_error=RuntimeError("unsupported handler")),
    )


def test_a_password_the_document_rejects_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement PDF cannot be decrypted",
        reader=_StubReader([_StubPage(_page())], decrypted=0),
    )


def test_a_document_whose_page_tree_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path, monkeypatch, "statement pages cannot be read", reader=_UnreadablePagesReader()
    )


def test_a_document_with_no_pages_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(tmp_path, monkeypatch, "statement page count is invalid", reader=_StubReader([]))


def test_a_reader_without_layout_extraction_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Column positions are the only thing that separates the text cells, so a
    # pypdf that cannot produce a layout render must stop the import outright.
    _refuse(
        tmp_path,
        monkeypatch,
        "PDF layout extraction is unavailable",
        reader=_StubReader([_StubPage("", TypeError("unexpected keyword extraction_mode"))]),
    )


def test_a_page_whose_text_cannot_be_extracted_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page text cannot be extracted",
        reader=_StubReader([_StubPage("", RuntimeError("font program is broken"))]),
    )


@pytest.mark.parametrize("text", ["", "   \n  ", "第一页\x00", 42])
def test_a_page_whose_text_is_not_storable_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: object
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page text is invalid",
        reader=_StubReader([_StubPage(text)]),
    )


# --- page structure --------------------------------------------------------


def test_a_page_numbered_out_of_place_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page number is invalid",
        reader=_reader_for(_page(page_number=2)),
    )


def test_a_page_whose_period_runs_backwards_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement period is invalid",
        reader=_reader_for(_page(start="2026-05-03", end="2026-05-01")),
    )


def test_a_page_whose_card_is_not_an_account_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "page account identity is invalid",
        reader=_reader_for(_page(card="620")),
    )


def test_a_repeated_header_block_is_ambiguous_rather_than_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page range is invalid",
        reader=_reader_for(_page(range_count=2)),
    )


@pytest.mark.parametrize("header_count", [0, 2])
def test_a_column_header_that_is_missing_or_doubled_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header_count: int
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "column header is missing or ambiguous",
        reader=_reader_for(_page(header_count=header_count)),
    )


def test_a_page_without_a_footer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page footer is missing",
        reader=_reader_for(_page(footer=None)),
    )


@pytest.mark.parametrize(("rows", "message"), [("0", "page row count"), ("3", "dated-row count")])
def test_a_page_whose_declared_row_count_disagrees_with_its_body_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: str, message: str
) -> None:
    _refuse(tmp_path, monkeypatch, message, reader=_reader_for(_page(rows=rows)))


def test_pages_that_disagree_about_the_account_holder_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two pages of the same export must describe one account, or the totals
    # printed on each of them cannot be attributed to anything.
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page metadata is inconsistent",
        reader=_reader_for(
            _page(page_number=1, page_count=2),
            _page(page_number=2, page_count=2, holder="另一位用户"),
        ),
    )


# --- transaction rows ------------------------------------------------------


def test_blank_and_text_only_body_lines_are_folded_into_the_row_above(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(
        tmp_path,
        monkeypatch,
        reader=_reader_for(
            _page(
                body=(
                    _row(),
                    "",
                    _continuation(附言="续写附言"),
                    "小计".ljust(60),
                    _OLDER_ROW,
                )
            )
        ),
    )
    statement = parse_boc_personal_pdf(
        source, expected_sha256=digest, managed_account_suffix=_SUFFIX
    )
    assert statement.transactions[0].transaction_name.endswith("合成附言 续写附言")


def test_a_continuation_line_before_any_row_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "page body prefix is invalid",
        reader=_reader_for(_page(body=(_continuation(附言="孤儿续行"), _row(), _OLDER_ROW))),
    )


def test_a_continuation_line_carrying_dated_or_money_columns_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anything date-, time- or amount-shaped left of the text columns means the
    # layout render broke a row apart, not that the bank wrapped a long field.
    spill = "2026-05-02".ljust(60) + "续写"
    _refuse(
        tmp_path,
        monkeypatch,
        "transaction continuation is invalid",
        reader=_reader_for(_page(body=(_row(), spill, _OLDER_ROW))),
    )


def test_a_row_without_a_transaction_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "transaction name is missing",
        reader=_reader_for(_page(rows="1", debit="0.00", body=(_row(name=""),))),
    )


def test_a_balance_chain_that_does_not_reconcile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "balance chain does not reconcile",
        reader=_reader_for(_page(body=(_row(balance="200.00"), _OLDER_ROW))),
    )


def test_rows_that_run_forwards_in_time_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "not reverse-date ordered",
        reader=_reader_for(
            _page(
                body=(
                    _row(date_text="2026-05-01", time_text="09:00:00"),
                    _row(
                        date_text="2026-05-02",
                        time_text="10:00:00",
                        amount="-12.34",
                        balance="87.66",
                        name="消费",
                    ),
                )
            )
        ),
    )


def test_two_identical_facts_on_one_statement_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Identical rows cannot be told apart later, so the statement is rejected
    # rather than imported as one deduplicated fact.
    zero = _row(date_text="2026-05-01", time_text="09:00:00", amount="0.00", balance="87.66")
    _refuse(
        tmp_path,
        monkeypatch,
        "duplicate transaction fact",
        reader=_reader_for(_page(debit="0.00", credit="0.00", body=(zero, zero))),
    )


def test_a_page_credit_total_that_does_not_match_its_rows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "page credit total does not reconcile",
        reader=_reader_for(_page(credit="30.00")),
    )


# --- the helpers, stated directly ------------------------------------------


def test_an_empty_run_of_transactions_needs_no_sign_correction() -> None:
    assert _correct_amount_signs_by_balance(()) == ()


def test_the_combined_description_is_bounded() -> None:
    parsed = _ParsedTransaction(
        sequence=1,
        occurred_at=_parse_datetime("2026-05-01 09:00:00", "%Y-%m-%d %H:%M:%S"),
        amount_minor=100,
        balance_minor=100,
        transaction_name="名" * 100,
        channel="道" * 100,
        branch_name="点" * 100,
        note="言" * 100,
        counterparty_name="",
        counterparty_account="",
        counterparty_institution="",
        source_page=1,
    )
    with pytest.raises(BocStatementError, match="transaction description is too long"):
        _combined_transaction_name(parsed)


@pytest.mark.parametrize("value", ["1.5", "12", "abc", "1.234"])
def test_only_two_decimal_places_of_renminbi_are_money(value: str) -> None:
    with pytest.raises(BocStatementError, match="statement amount is invalid"):
        _money_minor(value)


def test_an_amount_beyond_the_safe_integer_range_is_refused() -> None:
    with pytest.raises(BocStatementError, match="amount is out of range"):
        _money_minor("99999999999999999.00")


def test_a_thousands_separated_amount_is_read_as_minor_units() -> None:
    assert _money_minor("1,234.56") == 123456


def test_a_date_or_timestamp_that_does_not_exist_is_refused() -> None:
    with pytest.raises(BocStatementError, match="statement date is invalid"):
        _parse_date("2026-13-01", "%Y-%m-%d")
    with pytest.raises(BocStatementError, match="statement timestamp is invalid"):
        _parse_datetime("2026-05-01 99:00:00", "%Y-%m-%d %H:%M:%S")


@pytest.mark.parametrize("value", ["", "  "])
def test_a_required_text_field_may_not_be_blank(value: str) -> None:
    with pytest.raises(BocStatementError, match="statement text field is invalid"):
        _bounded_text(value, required=True)


@pytest.mark.parametrize("value", ["x" * 301, "备注\x00"])
def test_an_unstorable_or_oversize_text_field_is_refused(value: str) -> None:
    with pytest.raises(BocStatementError, match="statement text field is invalid"):
        _bounded_text(value, required=False)


def test_text_fields_are_whitespace_collapsed() -> None:
    assert _bounded_text("  转  入  ", required=True) == "转 入"


def test_a_text_cell_row_of_the_wrong_width_is_refused() -> None:
    with pytest.raises(BocStatementError, match="transaction text width is invalid"):
        _clean_layout_cells(["a", "b", "c"])


def test_an_account_spill_that_does_not_rebuild_an_account_number_is_refused() -> None:
    with pytest.raises(BocStatementError, match="counterparty account spill is invalid"):
        _clean_layout_cells(["转入", "", "", "", "乙 1234", "1" * 30, "", ""][:7])


def test_a_body_without_a_footer_marker_is_refused() -> None:
    with pytest.raises(BocStatementError, match="statement page footer is missing"):
        _find_footer(["第一行", "第二行"], 0)


def test_a_hint_footer_ends_the_body_just_like_end() -> None:
    assert _find_footer(["行", "温馨提示: 请核对", "END"], 0) == 1


def test_continuation_parts_are_joined_with_a_single_space() -> None:
    assert _join_parts("", "续") == "续"
    assert _join_parts("附言", "续") == "附言 续"
