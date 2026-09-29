from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server.monthly_review import MAX_PACKAGE_BYTES, MAX_SAFE_INTEGER, MonthlyReviewUnavailable, load_monthly_review


def package() -> dict:
    return {
        "schema_version": "ledgerbridge.monthly-review-package.v1",
        "revision": "synthetic-review-v1",
        "confirmed_on": "2026-09-06",
        "policy": {
            "expense_basis": "ACTUAL_PAYMENT_MONTH",
            "income_basis": "SCREENSHOT_SETTLEMENT_PERIOD",
            "legacy_payroll_through": "2026-07",
            "workbench_payroll_from": "2026-08",
            "workbench_status": "NOT_CONNECTED",
        },
        "adjustments": [{"id": "adjust-1", "label": "测试补记", "month": "2026-09", "amount_minor": 100,
                         "balance_effect_minor": -100, "cash_effect_minor": 0, "status": "PENDING_ENTRY", "note": "只补账"}],
        "bridges": [{"id": "bridge-1", "label": "测试衔接", "month": "2026-08", "amount_minor": 200,
                     "balance_effect_minor": 200, "cash_effect_minor": 0, "status": "PENDING_BRIDGE", "note": "不改变现金"}],
        "pending": [{"id": "pending-1", "label": "测试待付", "month": "2026-09", "amount_minor": 50,
                     "status": "AWAITING_PAYMENT", "note": "尚未支付"}],
        "historical_payroll": [
            {"period": f"2026-{month:02}", "total_minor": 300,
             "stores": [{"label": "测试甲", "amount_minor": 100}, {"label": "测试乙", "amount_minor": 200},
                        {"label": "测试空项", "amount_minor": None}]}
            for month in range(1, 8)
        ],
        "backtest": [{"month": "2026-07", "label": "测试公司 / 渠道", "original_minor": 300,
                      "matched_minor": 350, "difference_minor": 50, "status": "ACCEPTED", "note": "测试差额"}],
        "accepted_exceptions": [{"id": "except-1", "label": "测试未闭合", "note": "已接受待后续处理", "status": "ACCEPTED_OPEN"}],
    }


class MonthlyReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "review.json"

    def write(self, value: object) -> None:
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def assert_invalid(self, value: object, month: str = "2026-09") -> None:
        self.write(value)
        with self.assertRaisesRegex(MonthlyReviewUnavailable, "^Monthly review evidence is unavailable$"):
            load_monthly_review(self.path, month)

    def test_filters_month_but_keeps_all_payroll_and_accepted_exceptions(self) -> None:
        self.write(package())
        original = self.path.read_bytes()
        september = load_monthly_review(self.path, "2026-09")
        self.assertEqual(september["contract_version"], "ledgerbridge.monthly-review.v1")
        self.assertEqual(len(september["adjustments"]), 1)
        self.assertEqual(len(september["pending"]), 1)
        self.assertEqual(september["bridges"], [])
        self.assertEqual(september["backtest"], [])
        self.assertEqual(len(september["historical_payroll"]), 7)
        self.assertEqual(len(september["accepted_exceptions"]), 1)
        self.assertFalse(september["production_posted"])
        self.assertEqual(load_monthly_review(self.path, "2026-08")["bridges"][0]["amount_minor"], 200)
        self.assertEqual(load_monthly_review(self.path, "2026-07")["backtest"][0]["difference_minor"], 50)
        self.assertEqual(self.path.read_bytes(), original)
        september["policy"]["expense_basis"] = "MUTATED"
        self.assertEqual(load_monthly_review(self.path, "2026-09")["policy"]["expense_basis"], "ACTUAL_PAYMENT_MONTH")

    def test_signed_reversal_and_nullable_comparison(self) -> None:
        value = package()
        value["adjustments"][0].update(amount_minor=-100, balance_effect_minor=100)
        value["backtest"][0].update(original_minor=None, matched_minor=None, difference_minor=None)
        self.write(value)
        self.assertEqual(load_monthly_review(self.path, "2026-09")["adjustments"][0]["amount_minor"], -100)

    def test_unknown_missing_keys_at_every_level_fail_closed(self) -> None:
        for target in (None, "policy", "adjustments", "bridges", "pending", "historical_payroll", "backtest", "accepted_exceptions", "store"):
            for operation in ("extra", "missing"):
                with self.subTest(target=target, operation=operation):
                    value = package()
                    item = value if target is None else value["historical_payroll"][0]["stores"][0] if target == "store" else value[target]
                    if isinstance(item, list):
                        item = item[0]
                    if operation == "extra":
                        item["private_path"] = "secret"
                    else:
                        item.pop(next(iter(item)))
                    self.assert_invalid(value)

    def test_duplicate_ids_periods_labels_and_comparisons_rejected(self) -> None:
        for key in ("adjustments", "bridges", "pending", "historical_payroll", "backtest", "accepted_exceptions"):
            with self.subTest(key=key):
                value = package()
                value[key].append(deepcopy(value[key][0]))
                self.assert_invalid(value)
        value = package()
        value["bridges"][0]["id"] = value["adjustments"][0]["id"]
        self.assert_invalid(value)
        value = package()
        value["historical_payroll"][0]["stores"][1]["label"] = "测试甲"
        self.assert_invalid(value)

    def test_safe_integer_and_monetary_invariants(self) -> None:
        for invalid in (True, False, "100", 1.5, None, 2**53, -(2**53)):
            with self.subTest(invalid=invalid):
                value = package()
                value["adjustments"][0]["amount_minor"] = invalid
                self.assert_invalid(value)
        changes = (("adjustments", "cash_effect_minor", 1), ("adjustments", "cash_effect_minor", False),
                   ("adjustments", "balance_effect_minor", 100), ("bridges", "balance_effect_minor", -200),
                   ("bridges", "amount_minor", -200), ("pending", "amount_minor", -1),
                   ("backtest", "difference_minor", 49), ("historical_payroll", "total_minor", 301))
        for collection, field, amount in changes:
            with self.subTest(collection=collection, field=field, amount=amount):
                value = package()
                value[collection][0][field] = amount
                self.assert_invalid(value)

    def test_policy_status_schema_and_period_validation(self) -> None:
        for key, wrong in (("expense_basis", "SERVICE_MONTH"), ("income_basis", "CASH_MONTH"),
                           ("legacy_payroll_through", "2026-08"), ("workbench_status", "READY")):
            value = package()
            value["policy"][key] = wrong
            self.assert_invalid(value)
        for collection in ("adjustments", "bridges", "pending", "accepted_exceptions"):
            value = package()
            value[collection][0]["status"] = "POSTED"
            self.assert_invalid(value)
        for month in ("2026-13", "2026-00", "0000-01", "2026-1", "2026-09-01", "2026-09\n", "2026/09"):
            self.assert_invalid(package(), month)
            value = package()
            value["adjustments"][0]["month"] = month
            self.assert_invalid(value)
        for confirmed in ("2026-02-30", "2026-2-01", "2026-09-06T00:00:00", "20260906"):
            value = package()
            value["confirmed_on"] = confirmed
            self.assert_invalid(value)
        value = package()
        value["schema_version"] = "other"
        self.assert_invalid(value)
        value = package()
        value["historical_payroll"].pop()
        self.assert_invalid(value)

    def test_same_month_totals_cannot_exceed_js_safe_integer(self) -> None:
        for collection in ("adjustments", "bridges", "pending"):
            for sign in ((1, -1) if collection == "adjustments" else (1,)):
                with self.subTest(collection=collection, sign=sign):
                    value = package()
                    first = value[collection][0]
                    first["amount_minor"] = sign * MAX_SAFE_INTEGER
                    second = deepcopy(first)
                    second.update(id=f"{collection}-second", amount_minor=sign)
                    if collection != "pending":
                        for entry in (first, second):
                            entry["balance_effect_minor"] = entry["amount_minor"] * (-1 if collection == "adjustments" else 1)
                    value[collection].append(second)
                    self.assert_invalid(value)
                    # The limit is scoped to a collection and a month, not the package.
                    second["month"] = "2026-10"
                    self.write(value)
                    self.assertFalse(load_monthly_review(self.path, "2026-10")["production_posted"])

    def test_safe_boundary_allowed_but_cancelled_intermediate_overflow_rejected(self) -> None:
        value = package()
        first = value["adjustments"][0]
        first.update(amount_minor=MAX_SAFE_INTEGER - 1, balance_effect_minor=-(MAX_SAFE_INTEGER - 1))
        second = {**first, "id": "adjust-2", "amount_minor": 1, "balance_effect_minor": -1}
        value["adjustments"].append(second)
        self.write(value)
        result = load_monthly_review(self.path, "2026-09")
        self.assertEqual(sum(item["amount_minor"] for item in result["adjustments"]), MAX_SAFE_INTEGER)
        first.update(amount_minor=MAX_SAFE_INTEGER, balance_effect_minor=-MAX_SAFE_INTEGER)
        value["adjustments"].append({**second, "id": "adjust-3", "amount_minor": -1, "balance_effect_minor": 1})
        self.assert_invalid(value)

    def test_unsafe_text_and_length_limits(self) -> None:
        for text in ("C:\\private\\source.xlsx", "file:///private/source", "https://example.test/private",
                     "/etc/private.json", "../../private", "\\\\server\\private", "bad\ntext", "bad\x00text", "bad\u202etext", "x" * 2001):
            with self.subTest(text=text):
                value = package()
                value["adjustments"][0]["note"] = text
                self.assert_invalid(value)
        for revision in ("", "  ", "x" * 101, True):
            value = package()
            value["revision"] = revision
            self.assert_invalid(value)

    def test_nested_types_collection_limits_and_nonfinite_values(self) -> None:
        for key in ("adjustments", "bridges", "pending", "historical_payroll", "backtest", "accepted_exceptions"):
            for invalid in ({}, None, "records", [None]):
                with self.subTest(key=key, invalid=invalid):
                    value = package()
                    value[key] = invalid
                    self.assert_invalid(value)
        value = package()
        value["pending"] *= 1001
        self.assert_invalid(value)
        for invalid in (True, float("nan"), float("inf"), -1):
            value = package()
            value["historical_payroll"][0]["stores"][0]["amount_minor"] = invalid
            self.assert_invalid(value)

    def test_malformed_missing_oversized_duplicate_json_and_nonfile(self) -> None:
        with self.assertRaises(MonthlyReviewUnavailable):
            load_monthly_review(self.path, "2026-09")
        for raw in (b"{broken", b"\xff", b'{"schema_version":1,"schema_version":2}', b"x" * (MAX_PACKAGE_BYTES + 1), b"[NaN]"):
            self.path.write_bytes(raw)
            with self.assertRaises(MonthlyReviewUnavailable):
                load_monthly_review(self.path, "2026-09")
        with self.assertRaises(MonthlyReviewUnavailable):
            load_monthly_review(Path(self.directory.name), "2026-09")

    def test_symlink_rejected_and_read_errors_sanitized(self) -> None:
        self.write(package())
        link = Path(self.directory.name) / "link.json"
        try:
            link.symlink_to(self.path)
        except OSError:
            pass  # Windows hosts may not grant symlink creation to the test process.
        else:
            with self.assertRaises(MonthlyReviewUnavailable):
                load_monthly_review(link, "2026-09")
        with patch("server.monthly_review.os.open", side_effect=PermissionError("SECRET_PATH")):
            with self.assertRaisesRegex(MonthlyReviewUnavailable, "^Monthly review evidence is unavailable$"):
                load_monthly_review(self.path, "2026-09")
        with patch("server.monthly_review.stat.S_ISLNK", return_value=True), patch("server.monthly_review.os.open") as open_file:
            with self.assertRaises(MonthlyReviewUnavailable):
                load_monthly_review(self.path, "2026-09")
            open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
