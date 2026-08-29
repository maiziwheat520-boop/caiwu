import crypto from "node:crypto";

const PLATFORM_SUBACCOUNTS = new Set(["花呗", "余额宝", "账户余额", "零钱", "零钱通"]);
const TRANSFER_TERMS = /转账|提现|投资理财|信用卡还款|信用借还|余额互转/;
const DAY_MS = 86_400_000;
const CLOSED_COLLECTION_EXCEPTIONS = Object.freeze([
  Object.freeze({
    institution: "农业银行",
    suffix: "2061",
    lifecycleStatus: "closed",
    excludedFromCollection: true,
    lifecycleReason: "账户已注销；保留历史交易与证据，不再要求补充后续流水",
  }),
]);

function normalizeIdentity(value) {
  return String(value ?? "").normalize("NFKC").replace(/\s+/g, " ").trim().toLocaleLowerCase("und");
}

function isPlatformInternalMovement(record) {
  return /余额互转|充值|赎回|花呗还款/.test(record.category ?? "")
    && PLATFORM_SUBACCOUNTS.has(String(record.counterparty ?? "").trim())
    && PLATFORM_SUBACCOUNTS.has(String(record.paymentMethod ?? "").trim());
}

function shortHash(value) {
  return crypto.createHash("sha256").update(String(value), "utf8").digest("hex").slice(0, 12);
}

function refundMinorFromStatus(status) {
  const match = String(status ?? "").match(/^已退款\([¥￥]?([0-9,]+(?:\.[0-9]{1,2})?)\)$/);
  if (!match) return null;
  const [whole, fraction = ""] = match[1].replaceAll(",", "").split(".");
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

function sameRefundIdentity(payment, refund) {
  return payment.sourceAccountKey === refund.sourceAccountKey
    && payment.holder === refund.holder
    && payment.counterparty === refund.counterparty
    && payment.description === refund.description
    && refund.date >= payment.date
    && (new Date(`${refund.date}T00:00:00Z`) - new Date(`${payment.date}T00:00:00Z`)) / DAY_MS <= 180;
}

export function platformSubaccountEvidence(name, evidence) {
  if (!PLATFORM_SUBACCOUNTS.has(name)) {
    throw new Error("unknown platform subaccount");
  }
  return {
    status: "已由所属平台完整流水覆盖",
    evidence,
  };
}

export function accountLifecycle(account) {
  const institution = normalizeIdentity(account.institution).replaceAll(" ", "");
  const identifier = normalizeIdentity(account.identifier).replaceAll(" ", "");
  const matched = CLOSED_COLLECTION_EXCEPTIONS.find((item) =>
    (institution.startsWith(`中国${item.institution}`) || institution.startsWith(item.institution))
    && identifier.endsWith(item.suffix)
  );
  if (matched) {
    return {
      lifecycleStatus: matched.lifecycleStatus,
      excludedFromCollection: matched.excludedFromCollection,
      lifecycleReason: matched.lifecycleReason,
    };
  }
  return {
    lifecycleStatus: "active",
    excludedFromCollection: false,
    lifecycleReason: "",
  };
}

export function accountNeedsCollection(account) {
  const lifecycle = account.lifecycleStatus
    ? account
    : { ...account, ...accountLifecycle(account) };
  return !lifecycle.excludedFromCollection
    && ["待补独立流水", "待补独立账单"].includes(account.evidenceStatus);
}

export function counterpartyProjection(record) {
  if (isPlatformInternalMovement(record)) return null;
  const identity = normalizeIdentity(record.counterparty);
  if (!identity) return null;
  return {
    counterpartyRef: `cp_${crypto.createHash("sha256").update(identity, "utf8").digest("hex")}`,
    counterpartyClass: "unknown",
  };
}

export function platformRecordPreference(record) {
  return TRANSFER_TERMS.test(`${record.category ?? ""} ${record.description ?? ""}`)
    ? "bank"
    : "platform";
}

export function matchWechatRefunds(records) {
  const payments = records.filter((record) =>
    record.source === "微信"
    && record.effect
    && record.direction === "支出"
    && record.signedMinor < 0
    && refundMinorFromStatus(record.status) !== null
  );
  const refunds = records.filter((record) =>
    record.source === "微信"
    && record.effect
    && record.direction === "退款收入"
    && record.signedMinor > 0
    && refundMinorFromStatus(record.status) === record.signedMinor
  );
  const paymentCandidates = new Map(
    payments.map((payment) => [
      payment.recordId,
      refunds.filter((refund) =>
        sameRefundIdentity(payment, refund)
        && refund.signedMinor <= Math.abs(payment.signedMinor)
        && refund.signedMinor === refundMinorFromStatus(payment.status)
      ),
    ]),
  );
  const refundCandidates = new Map(
    refunds.map((refund) => [
      refund.recordId,
      payments.filter((payment) => paymentCandidates.get(payment.recordId)?.includes(refund)),
    ]),
  );
  const links = [];
  for (const payment of payments) {
    const candidates = paymentCandidates.get(payment.recordId) ?? [];
    if (candidates.length !== 1) continue;
    const refund = candidates[0];
    if ((refundCandidates.get(refund.recordId) ?? []).length !== 1) continue;
    const netMinor = Math.abs(payment.signedMinor) - refund.signedMinor;
    const note = `原支出${(Math.abs(payment.signedMinor) / 100).toFixed(2)}元，退款${(refund.signedMinor / 100).toFixed(2)}元，净支出${(netMinor / 100).toFixed(2)}元；两条流水均保留`;
    const reason = `部分退款已匹配：${payment.recordId} ↔ ${refund.recordId}；净支出${(netMinor / 100).toFixed(2)}元`;
    payment.reviewReason = reason;
    refund.reviewReason = reason;
    links.push({
      groupId: `REFUND-${shortHash(`${payment.recordId}:${refund.recordId}`)}`,
      date: refund.date,
      amountMinor: refund.signedMinor,
      left: payment.recordId,
      right: refund.recordId,
      type: "部分退款",
      status: "原支付与退款已匹配",
      note,
    });
  }
  return links;
}
