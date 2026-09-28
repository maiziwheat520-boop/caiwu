"""Database-workbench payroll domain rules.

The public Interface deliberately stops at reviewed draft validation, immutable
locking, and bank-file export.  Material parsing and persistence are adapters
around this module; they must not weaken these invariants.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum


class EmployeeType(StrEnum):
    REGULAR = "REGULAR"
    TEMPORARY = "TEMPORARY"
    PAYEE_ONLY = "PAYEE_ONLY"


class PaymentChannel(StrEnum):
    MYBANK = "MYBANK"
    BOC = "BOC"
    WECHAT = "WECHAT"
    CASH = "CASH"


@dataclass(frozen=True, slots=True)
class PayrollComponent:
    code: str
    amount_minor: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PayrollDraftLine:
    employee_id: str
    employee_name: str
    payee_name: str
    account_number: str
    employee_type: EmployeeType
    payment_channel: PaymentChannel
    location: str
    job_group: str
    attendance_days: str
    components: tuple[PayrollComponent, ...]
    cash_amount_minor: int = 0
    supplemental_amount_minor: int = 0
    memo: str = ""

    @property
    def net_amount_minor(self) -> int:
        return sum(item.amount_minor for item in self.components)

    @property
    def bank_amount_minor(self) -> int:
        return self.net_amount_minor - self.cash_amount_minor

    @property
    def regular_bank_amount_minor(self) -> int:
        return self.bank_amount_minor - self.supplemental_amount_minor


@dataclass(frozen=True, slots=True)
class PayrollDraft:
    company_id: str
    period: str
    revision: int
    rules_version: str
    lines: tuple[PayrollDraftLine, ...]
    fixed_remittances: tuple[FixedRemittance, ...] = ()


@dataclass(frozen=True, slots=True)
class PayrollIssue:
    code: str
    message: str
    employee_id: str | None = None


@dataclass(frozen=True, slots=True)
class LockedPayrollVersion:
    company_id: str
    period: str
    revision: int
    rules_version: str
    content_sha256: str
    lines: tuple[PayrollDraftLine, ...]
    fixed_remittances: tuple[FixedRemittance, ...] = ()


@dataclass(frozen=True, slots=True)
class FixedRemittance:
    payee_name: str
    account_number: str
    amount_minor: int
    memo: str


class PayrollValidationError(ValueError):
    def __init__(self, issues: tuple[PayrollIssue, ...]) -> None:
        super().__init__("payroll draft contains blocking issues")
        self.issues = issues


_PERIOD = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GROUP_LOCATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("星汇", ("星汇",)),
    ("薇旭", ("薇旭",)),
    ("雅阁", ("雅阁",)),
    ("逸豪景怡", ("逸豪", "同富")),
    ("青居客一品餐饮", ("青居客", "一品", "粥店")),
)
_LOCATION_TO_GROUP = {
    location: group for group, locations in _GROUP_LOCATIONS for location in locations
}


def normalize_account(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def format_amount_minor(amount_minor: int) -> str:
    sign = "-" if amount_minor < 0 else ""
    absolute = abs(amount_minor)
    yuan, cents = divmod(absolute, 100)
    if cents == 0:
        return f"{sign}{yuan}"
    if cents % 10 == 0:
        return f"{sign}{yuan}.{cents // 10}"
    return f"{sign}{yuan}.{cents:02d}"


def compact_remittance_memo(memo: str, amount_minor: int, limit: int = 40) -> str:
    """Preserve the payable amount while fitting the bank's 40-character field."""

    text = re.sub(r"\s+", " ", memo.strip())
    final = f"应发{format_amount_minor(amount_minor)}"
    if not text:
        return final
    prefix = text.split("应发", 1)[0].strip()
    parts = [part for part in prefix.split(" ") if part]
    candidate = " ".join([*parts, final]).strip()
    if len(candidate) <= limit:
        return candidate
    for removable in ("住宿", "餐补", "全勤", "夜班", "好评", "绩效", "加班"):
        for index in range(len(parts) - 1, -1, -1):
            if parts[index].startswith(removable):
                parts.pop(index)
                candidate = " ".join([*parts, final]).strip()
                if len(candidate) <= limit:
                    return candidate
    kept: list[str] = []
    for part in parts:
        trial = " ".join([*kept, part, final]).strip()
        if len(trial) <= limit:
            kept.append(part)
    return " ".join([*kept, final]).strip() or final


def remittance_group(location: str) -> str | None:
    return _LOCATION_TO_GROUP.get(location.strip())


def validate_draft(draft: PayrollDraft) -> tuple[PayrollIssue, ...]:
    issues: list[PayrollIssue] = []
    if not _STABLE_ID.fullmatch(draft.company_id):
        issues.append(PayrollIssue("INVALID_COMPANY_ID", "公司编号格式无效"))
    if not _PERIOD.fullmatch(draft.period):
        issues.append(PayrollIssue("INVALID_PERIOD", "工资月份必须使用 YYYY-MM"))
    if draft.revision < 1:
        issues.append(PayrollIssue("INVALID_REVISION", "工资版本必须大于零"))
    if not draft.rules_version.strip():
        issues.append(PayrollIssue("MISSING_RULES_VERSION", "工资规则版本不能为空"))
    if not draft.lines:
        issues.append(PayrollIssue("EMPTY_PAYROLL", "工资明细不能为空"))

    employee_ids: set[str] = set()
    account_payees: dict[str, str] = {}
    payee_accounts: dict[str, str] = {}
    for line in draft.lines:
        employee_id = line.employee_id.strip()
        if not _STABLE_ID.fullmatch(employee_id):
            issues.append(
                PayrollIssue("INVALID_EMPLOYEE_ID", "员工编号格式无效", employee_id)
            )
        elif employee_id in employee_ids:
            issues.append(
                PayrollIssue("DUPLICATE_EMPLOYEE", "同一工资版本包含重复员工", employee_id)
            )
        employee_ids.add(employee_id)
        if not line.employee_name.strip():
            issues.append(PayrollIssue("MISSING_EMPLOYEE_NAME", "员工姓名不能为空", employee_id))
        if line.employee_type is EmployeeType.PAYEE_ONLY:
            issues.append(
                PayrollIssue("PAYEE_ONLY_HAS_WAGE", "代领工资人不能生成独立工资明细", employee_id)
            )
        if line.employee_type in (EmployeeType.REGULAR, EmployeeType.TEMPORARY):
            if not line.location.strip():
                issues.append(PayrollIssue("MISSING_LOCATION", "工作地点不能为空", employee_id))
            elif remittance_group(line.location) is None:
                issues.append(
                    PayrollIssue("UNKNOWN_LOCATION", "工作地点没有代发表分组", employee_id)
                )
        if line.employee_type is EmployeeType.REGULAR:
            if not line.job_group.strip():
                issues.append(
                    PayrollIssue("MISSING_JOB_GROUP", "正式工岗位不能为空", employee_id)
                )
            if not line.attendance_days.strip():
                issues.append(
                    PayrollIssue("MISSING_ATTENDANCE", "正式工考勤不能为空", employee_id)
                )
        if not line.components:
            issues.append(PayrollIssue("MISSING_COMPONENTS", "工资组成不能为空", employee_id))
        elif any(not item.code.strip() for item in line.components):
            issues.append(
                PayrollIssue("INVALID_COMPONENT", "工资组成项目编码不能为空", employee_id)
            )

        net = line.net_amount_minor
        cash = line.cash_amount_minor
        bank = line.bank_amount_minor
        if net <= 0:
            issues.append(PayrollIssue("NONPOSITIVE_NET_PAY", "实发工资必须大于零", employee_id))
        if cash < 0 or cash > net:
            issues.append(
                PayrollIssue("INVALID_CASH_SPLIT", "现金金额必须在实发工资范围内", employee_id)
            )
        if line.supplemental_amount_minor < 0 or (
            0 <= cash <= net and line.supplemental_amount_minor > bank
        ):
            issues.append(
                PayrollIssue(
                    "INVALID_SUPPLEMENTAL_SPLIT", "补发金额必须在银行代发金额范围内", employee_id
                )
            )
        if line.payment_channel is PaymentChannel.CASH:
            if cash != net:
                issues.append(
                    PayrollIssue(
                        "CASH_TOTAL_MISMATCH",
                        "全额现金人员的现金金额必须等于实发",
                        employee_id,
                    )
                )
        elif bank <= 0:
            issues.append(
                PayrollIssue("NONPOSITIVE_BANK_PAY", "银行代发金额必须大于零", employee_id)
            )
        else:
            account = normalize_account(line.account_number)
            payee = line.payee_name.strip()
            if not payee:
                issues.append(PayrollIssue("MISSING_PAYEE", "银行代发收款人不能为空", employee_id))
            if not account:
                issues.append(PayrollIssue("MISSING_ACCOUNT", "银行代发账号不能为空", employee_id))
            if account and payee:
                prior_payee = account_payees.setdefault(account, payee)
                if prior_payee != payee:
                    issues.append(
                        PayrollIssue(
                            "ACCOUNT_PAYEE_CONFLICT",
                            "同一账号对应多个收款人",
                            employee_id,
                        )
                    )
                prior_account = payee_accounts.setdefault(payee, account)
                if prior_account != account:
                    issues.append(
                        PayrollIssue(
                            "PAYEE_ACCOUNT_CONFLICT",
                            "同一收款人对应多个账号",
                            employee_id,
                        )
                    )
            memo = compact_remittance_memo(line.memo, bank)
            if not memo or len(memo) > 40:
                issues.append(PayrollIssue("INVALID_MEMO", "代发表附言不符合限制", employee_id))
    fixed_keys: set[tuple[str, str]] = set()
    for item in draft.fixed_remittances:
        payee = item.payee_name.strip()
        account = normalize_account(item.account_number)
        if not payee or not account or item.amount_minor <= 0:
            issues.append(PayrollIssue("INVALID_FIXED_REMITTANCE", "固定代发规则不完整"))
        if len(compact_remittance_memo(item.memo, item.amount_minor)) > 40:
            issues.append(PayrollIssue("INVALID_FIXED_MEMO", "固定代发附言超过限制"))
        key = (payee, account)
        if key in fixed_keys:
            issues.append(PayrollIssue("DUPLICATE_FIXED_REMITTANCE", "固定代发收款人重复"))
        fixed_keys.add(key)
        if account in account_payees and account_payees[account] != payee:
            issues.append(PayrollIssue("ACCOUNT_PAYEE_CONFLICT", "同一账号对应多个收款人"))
        if payee in payee_accounts and payee_accounts[payee] != account:
            issues.append(PayrollIssue("PAYEE_ACCOUNT_CONFLICT", "同一收款人对应多个账号"))
    return tuple(issues)


def lock_draft(draft: PayrollDraft) -> LockedPayrollVersion:
    issues = validate_draft(draft)
    if issues:
        raise PayrollValidationError(issues)
    payload = {
        "company_id": draft.company_id,
        "period": draft.period,
        "revision": draft.revision,
        "rules_version": draft.rules_version,
        "fixed_remittances": [
            {
                "payee_name": item.payee_name.strip(),
                "account_number": normalize_account(item.account_number),
                "amount_minor": item.amount_minor,
                "memo": item.memo,
            }
            for item in sorted(
                draft.fixed_remittances,
                key=lambda value: (value.payee_name, normalize_account(value.account_number)),
            )
        ],
        "lines": [
            {
                "employee_id": line.employee_id,
                "employee_name": line.employee_name,
                "payee_name": line.payee_name,
                "account_number": normalize_account(line.account_number),
                "employee_type": line.employee_type.value,
                "payment_channel": line.payment_channel.value,
                "location": line.location,
                "job_group": line.job_group,
                "attendance_days": line.attendance_days,
                "components": [
                    {"code": item.code, "amount_minor": item.amount_minor, "reason": item.reason}
                    for item in line.components
                ],
                "cash_amount_minor": line.cash_amount_minor,
                "supplemental_amount_minor": line.supplemental_amount_minor,
                "memo": line.memo,
            }
            for line in sorted(draft.lines, key=lambda item: item.employee_id)
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return LockedPayrollVersion(
        company_id=draft.company_id,
        period=draft.period,
        revision=draft.revision,
        rules_version=draft.rules_version,
        content_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        lines=draft.lines,
        fixed_remittances=draft.fixed_remittances,
    )


def remittance_rows(
    locked: LockedPayrollVersion,
) -> dict[str, tuple[tuple[str, str, int, str], ...]]:
    rows: dict[str, list[tuple[str, str, int, str]]] = {
        group: [] for group, _locations in _GROUP_LOCATIONS
    }
    for line in locked.lines:
        if line.payment_channel is PaymentChannel.CASH:
            continue
        group = remittance_group(line.location)
        if group is None:
            raise PayrollValidationError(
                (PayrollIssue("UNKNOWN_LOCATION", "工作地点没有代发表分组", line.employee_id),)
            )
        bank = line.regular_bank_amount_minor
        if bank > 0:
            rows[group].append(
                (
                    line.payee_name.strip(),
                    normalize_account(line.account_number),
                    bank,
                    compact_remittance_memo(line.memo, bank),
                )
            )
        if line.supplemental_amount_minor:
            rows.setdefault("补发", []).append(
                (
                    line.payee_name.strip(),
                    normalize_account(line.account_number),
                    line.supplemental_amount_minor,
                    compact_remittance_memo("补发 " + line.memo, line.supplemental_amount_minor),
                )
            )
    if locked.fixed_remittances:
        rows["其他"] = []
        for item in locked.fixed_remittances:
            account = normalize_account(item.account_number)
            memo = compact_remittance_memo(item.memo, item.amount_minor)
            if (
                not item.payee_name.strip()
                or not account
                or item.amount_minor <= 0
                or len(memo) > 40
            ):
                raise PayrollValidationError(
                    (PayrollIssue("INVALID_FIXED_REMITTANCE", "固定代发规则不完整"),)
                )
            rows["其他"].append(
                (item.payee_name.strip(), account, item.amount_minor, memo)
            )
    return {key: tuple(value) for key, value in rows.items()}
