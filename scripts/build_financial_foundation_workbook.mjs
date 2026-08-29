#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
const { FileBlob, SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const DAY_MS = 86_400_000;

function parseArgs(argv) {
  const result = { wechat: [], alipay: [], year: 2026, month: 5 };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key.startsWith("--") || value === undefined) {
      throw new Error(`invalid argument near ${key}`);
    }
    index += 1;
    if (key === "--wechat") result.wechat.push(value);
    else if (key === "--alipay") result.alipay.push(value);
    else if (key === "--boc-workbook") result.bocWorkbook = value;
    else if (key === "--output") result.output = value;
    else if (key === "--manifest") result.manifest = value;
    else if (key === "--normalized-output") result.normalizedOutput = value;
    else if (key === "--year") result.year = Number(value);
    else if (key === "--month") result.month = Number(value);
    else throw new Error(`unknown argument ${key}`);
  }
  if (!result.wechat.length || !result.alipay.length || !result.bocWorkbook || !result.output) {
    throw new Error("wechat, alipay, boc-workbook, and output are required");
  }
  if (!Number.isInteger(result.year) || result.year < 2026 || result.year > 2100) {
    throw new Error("year is invalid");
  }
  if (!Number.isInteger(result.month) || result.month < 1 || result.month > 12) {
    throw new Error("month is invalid");
  }
  return result;
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function shortHash(value) {
  return sha256(Buffer.from(String(value), "utf8")).slice(0, 12);
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function maskIdentifier(value) {
  const text = normalizeText(value);
  if (!text) return "未提供";
  if (text.includes("@")) {
    const [local, domain] = text.split("@", 2);
    return `${local.slice(0, 2)}***@${domain}`;
  }
  const compact = text.replace(/\s+/g, "");
  if (compact.length <= 4) return `****${compact}`;
  return `${compact.slice(0, 2)}****${compact.slice(-4)}`;
}

function lastFour(value) {
  const digits = normalizeText(value).replace(/\D/g, "");
  return digits.slice(-4) || "未知";
}

function canonicalInstitution(value) {
  const raw = normalizeText(value);
  if (/中国银行|中行/.test(raw)) return "中行";
  const normalized = raw
    .replace(/股份有限公司|有限责任公司|有限公司/g, "")
    .replace(/中国/g, "")
    .replace(/银行|信用卡中心|支行|分行/g, "");
  if (normalized.includes("建设")) return "建设";
  if (normalized.includes("中信")) return "中信";
  if (normalized.includes("农业")) return "农业";
  if (normalized.includes("网商")) return "网商";
  if (normalized.includes("工商")) return "工商";
  if (normalized.includes("招商")) return "招商";
  if (normalized.includes("交通")) return "交通";
  if (normalized.includes("邮政") || normalized.includes("邮储")) return "邮储";
  if (normalized.includes("平安")) return "平安";
  if (normalized.includes("民生")) return "民生";
  if (normalized.includes("光大")) return "光大";
  if (normalized.includes("浦发")) return "浦发";
  if (normalized.includes("广发")) return "广发";
  if (normalized.includes("兴业")) return "兴业";
  return normalized;
}

function fundingInstrument(method) {
  const match = normalizeText(method).match(/^(.+?)(储蓄卡|信用卡|银行卡|卡)\((\d{4})\)/);
  if (!match) return null;
  return {
    institution: match[1],
    institutionKey: canonicalInstitution(match[1]),
    kind: match[2],
    suffix: match[3],
  };
}

function parseMoney(value) {
  if (typeof value === "number") return Math.round(value * 100);
  const cleaned = normalizeText(value).replace(/[¥￥,]/g, "");
  if (!cleaned || cleaned === "/") return 0;
  const match = cleaned.match(/-?\d+(?:\.\d+)?/);
  if (!match) return 0;
  return Math.round(Number(match[0]) * 100);
}

function formatMoney(minor) {
  return minor / 100;
}

function excelSerialToDate(value) {
  if (value instanceof Date) return value;
  if (typeof value === "number") {
    return new Date(Date.UTC(1899, 11, 30) + value * DAY_MS);
  }
  const text = normalizeText(value).replace(" ", "T");
  const parsed = new Date(`${text}${text.endsWith("Z") ? "" : "+08:00"}`);
  if (Number.isNaN(parsed.getTime())) throw new Error("invalid transaction date");
  return parsed;
}

function localDateString(value) {
  const date = excelSerialToDate(value);
  return date.toISOString().slice(0, 10);
}

function dateOnly(value) {
  if (typeof value === "number" || value instanceof Date) return localDateString(value);
  const text = normalizeText(value);
  const match = text.match(/\d{4}-\d{2}-\d{2}/);
  if (!match) throw new Error("transaction date is missing");
  return match[0];
}

function inMonth(dateText, year, month) {
  return dateText.startsWith(`${year}-${String(month).padStart(2, "0")}-`);
}

function metadata(rows, headerIndex) {
  const result = new Map();
  for (const row of rows.slice(0, headerIndex)) {
    const text = row.map(normalizeText).filter(Boolean).join(" ");
    const match = text.match(/^([^：:]{1,20})[：:]\s*(.+)$/);
    if (match) result.set(match[1].trim(), match[2].trim());
  }
  return result;
}

function cleanHolder(value) {
  return normalizeText(value).replace(/^\[|\]$/g, "") || "账户持有人";
}

function headerIndex(rows, required) {
  const index = rows.findIndex((row) => required.every((name) => row.map(normalizeText).includes(name)));
  if (index < 0) throw new Error(`required header not found: ${required.join(", ")}`);
  return index;
}

function rowObject(headers, row) {
  return Object.fromEntries(headers.map((header, index) => [normalizeText(header), row[index]]));
}

async function importXlsx(filePath) {
  return SpreadsheetFile.importXlsx(await FileBlob.load(filePath));
}

async function readBytes(filePath) {
  return new Uint8Array(await fs.readFile(filePath));
}

function accountKey(institution, identifier) {
  return `${institution}:${shortHash(identifier)}`;
}

function evidenceRef(source, digest) {
  return `${source}-${digest.slice(0, 12)}`;
}

async function parseWechat(filePath, year, month) {
  const bytes = await readBytes(filePath);
  const digest = sha256(bytes);
  const workbook = await importXlsx(filePath);
  const rows = workbook.worksheets.getItemAt(0).getUsedRange().values;
  const hi = headerIndex(rows, ["交易时间", "交易对方", "收/支", "金额(元)", "交易单号"]);
  const headers = rows[hi].map(normalizeText);
  const meta = metadata(rows, hi);
  const holder = cleanHolder(meta.get("微信昵称") || meta.get("姓名") || "微信账户持有人");
  const identifier = meta.get("微信号") || meta.get("微信账户") || holder;
  const sourceAccountKey = accountKey("wechat", identifier);
  const records = [];
  for (const row of rows.slice(hi + 1)) {
    if (!normalizeText(row[0])) continue;
    const item = rowObject(headers, row);
    const date = localDateString(item["交易时间"]);
    if (!inMonth(date, year, month)) continue;
    const directionRaw = normalizeText(item["收/支"]);
    const status = normalizeText(item["当前状态"]);
    const gross = Math.abs(parseMoney(item["金额(元)"]));
    let signed = directionRaw === "收入" ? gross : directionRaw === "支出" ? -gross : 0;
    let effect = signed !== 0;
    let direction = directionRaw || "不计收支";
    let reviewReason = "";
    if (directionRaw === "/" && /^已退款\(/.test(status)) {
      signed = gross;
      effect = true;
      direction = "退款收入";
    } else if (status === "已全额退款") {
      signed = 0;
      effect = false;
      direction = "已退款";
      reviewReason = "全额退款，原交易不计入净额";
    } else if (directionRaw === "/") {
      effect = false;
      reviewReason = "平台标记为不计收支";
    }
    const external = normalizeText(item["交易单号"]) || `${date}:${records.length + 1}`;
    records.push({
      recordId: `WX-${shortHash(`${digest}:${external}`)}`,
      date,
      source: "微信",
      sourceAccountKey,
      sourceAccountName: "微信支付账户",
      holder,
      direction,
      signedMinor: signed,
      effect,
      category: normalizeText(item["交易类型"]),
      counterparty: normalizeText(item["交易对方"]),
      counterpartyAccount: "",
      counterpartyInstitution: "",
      description: normalizeText(item["商品"]),
      paymentMethod: normalizeText(item["支付方式"]),
      status,
      evidence: evidenceRef("WX", digest),
      reviewReason,
    });
  }
  return {
    records,
    account: {
      key: sourceAccountKey,
      displayName: "微信支付账户",
      institution: "微信支付",
      type: "电子钱包",
      holder,
      identifier: maskIdentifier(identifier),
      evidenceStatus: "已提供独立流水",
      evidence: evidenceRef("WX", digest),
    },
  };
}

async function parseAlipay(filePath, year, month) {
  const bytes = await readBytes(filePath);
  const digest = sha256(bytes);
  const csvText = new TextDecoder("gbk").decode(bytes);
  const workbook = await Workbook.fromCSV(csvText, { sheetName: "支付宝" });
  const rows = workbook.worksheets.getItemAt(0).getUsedRange().values;
  const hi = headerIndex(rows, ["交易时间", "交易对方", "收/支", "金额", "交易订单号"]);
  const headers = rows[hi].map(normalizeText);
  const meta = metadata(rows, hi);
  const holder = cleanHolder(meta.get("姓名") || "支付宝账户持有人");
  const identifier = meta.get("支付宝账户") || `${holder}:${digest}`;
  const sourceAccountKey = accountKey("alipay", identifier);
  const records = [];
  for (const row of rows.slice(hi + 1)) {
    if (!normalizeText(row[0])) continue;
    const item = rowObject(headers, row);
    const date = dateOnly(item["交易时间"]);
    if (!inMonth(date, year, month)) continue;
    const directionRaw = normalizeText(item["收/支"]);
    const status = normalizeText(item["交易状态"]);
    const gross = Math.abs(parseMoney(item["金额"]));
    let signed = directionRaw === "收入" ? gross : directionRaw === "支出" ? -gross : 0;
    let effect = signed !== 0;
    let reviewReason = "";
    if (status === "交易关闭") {
      signed = 0;
      effect = false;
      reviewReason = "交易关闭，不计入净额";
    } else if (directionRaw === "不计收支") {
      signed = 0;
      effect = false;
      reviewReason = status === "还款成功" ? "信用账户还款，等待资金账户双边流水" : "平台标记为不计收支";
    }
    const external = normalizeText(item["交易订单号"]) || `${date}:${records.length + 1}`;
    records.push({
      recordId: `ZFB-${shortHash(`${digest}:${external}`)}`,
      date,
      source: "支付宝",
      sourceAccountKey,
      sourceAccountName: "支付宝账户",
      holder,
      direction: directionRaw || "不计收支",
      signedMinor: signed,
      effect,
      category: normalizeText(item["交易分类"]),
      counterparty: normalizeText(item["交易对方"]),
      counterpartyAccount: maskIdentifier(item["对方账号"]),
      counterpartyInstitution: "",
      description: normalizeText(item["商品说明"]),
      paymentMethod: normalizeText(item["收/付款方式"]),
      status,
      evidence: evidenceRef("ZFB", digest),
      reviewReason,
    });
  }
  return {
    records,
    account: {
      key: sourceAccountKey,
      displayName: "支付宝账户",
      institution: "支付宝",
      type: "电子钱包",
      holder,
      identifier: maskIdentifier(identifier),
      evidenceStatus: "已提供独立流水",
      evidence: evidenceRef("ZFB", digest),
    },
  };
}

async function parseBoc(filePath, year, month) {
  const bytes = await readBytes(filePath);
  const digest = sha256(bytes);
  const workbook = await importXlsx(filePath);
  const sheetName = `${String(year).slice(-2)}.${month}中行邮箱待复核`;
  const sheet = workbook.worksheets.getItem(sheetName);
  if (!sheet) throw new Error(`BOC sheet not found: ${sheetName}`);
  const rows = sheet.getUsedRange().values;
  const hi = headerIndex(rows, ["清单ID", "银行", "账户号", "交易时间", "金额(元)"]);
  const headers = rows[hi].map(normalizeText);
  const records = [];
  let account = null;
  for (const row of rows.slice(hi + 1)) {
    if (!normalizeText(row[0])) continue;
    const item = rowObject(headers, row);
    const date = dateOnly(item["交易时间"]);
    if (!inMonth(date, year, month)) continue;
    const holder = normalizeText(item["账户名"]);
    const identifier = normalizeText(item["账户号"]);
    const key = accountKey("boc", identifier);
    if (!account) {
      account = {
        key,
        displayName: `中国银行储蓄卡（${lastFour(identifier)}）`,
        institution: "中国银行",
        type: "银行账户",
        holder,
        identifier: `****${lastFour(identifier)}`,
        evidenceStatus: "已提供独立流水",
        evidence: evidenceRef("BOC", digest),
      };
    }
    const signed = parseMoney(item["金额(元)"]);
    records.push({
      recordId: `BOC-${shortHash(`${digest}:${normalizeText(item["清单ID"])}`)}`,
      date,
      source: "中国银行",
      sourceAccountKey: key,
      sourceAccountName: account?.displayName || "中国银行账户",
      holder,
      direction: signed > 0 ? "收入" : signed < 0 ? "支出" : "不计收支",
      signedMinor: signed,
      effect: signed !== 0,
      category: normalizeText(item["交易名称"]),
      counterparty: normalizeText(item["对方户名"]),
      counterpartyAccount: maskIdentifier(item["对方账号"]),
      counterpartyInstitution: normalizeText(item["对方开户行"]),
      description: [item["渠道"], item["网点/机构"], item["附言/备注"]].map(normalizeText).filter(Boolean).join("；"),
      paymentMethod: "",
      status: normalizeText(item["机器数值校验"]) || "待复核",
      evidence: evidenceRef("BOC", normalizeText(item["附件SHA256"]) || digest),
      reviewReason: normalizeText(item["机器数值校验"]) === "通过" ? "" : normalizeText(item["复核重点"]),
    });
  }
  if (!account) throw new Error("BOC account was not found in selected month");
  return { records, account };
}

function parseReferencedAccount(method, holder, managedAccounts) {
  const instrument = fundingInstrument(method);
  if (!instrument) return null;
  const institution = instrument.institution;
  const accountKind = instrument.kind;
  const suffix = instrument.suffix;
  const managed = managedAccounts.find((item) => item.identifier.endsWith(suffix));
  if (managed) return managed;
  const type = /信用|花呗/.test(accountKind) ? "信用账户" : "银行账户";
  return {
    key: accountKey(institution, `${holder}:${suffix}`),
    displayName: `${institution}${type === "信用账户" ? "信用卡" : "账户"}（${suffix}）`,
    institution,
    type,
    holder,
    identifier: `****${suffix}`,
    evidenceStatus: "待补独立流水",
    evidence: "仅在支付方式中出现",
  };
}

function addPlatformSubaccounts(records, accounts) {
  const known = new Set(accounts.map((item) => item.key));
  const subaccountPatterns = ["零钱", "零钱通", "余额宝", "账户余额", "花呗"];
  for (const record of records) {
    const method = normalizeText(record.paymentMethod);
    for (const name of subaccountPatterns) {
      if (!method.includes(name)) continue;
      const key = accountKey(record.source, `${record.holder}:${name}`);
      if (known.has(key)) continue;
      known.add(key);
      const requiresIndependentStatement = name === "花呗";
      accounts.push({
        key,
        displayName: `${record.source}${name}`,
        institution: record.source,
        type: name === "花呗" ? "信用账户" : "平台子账户",
        holder: record.holder,
        identifier: "平台内账户",
        evidenceStatus: requiresIndependentStatement ? "待补独立账单" : "已在平台流水中出现",
        evidence: requiresIndependentStatement ? "仅在支付宝支付方式中出现" : record.evidence,
      });
    }
  }
}

function sameHolder(left, right) {
  const holder = normalizeText(left).replace(/\s+/g, "");
  const party = normalizeText(right).replace(/\s+/g, "");
  return Boolean(holder) && (party === holder || (party.startsWith(holder) && party.length <= holder.length + 2));
}

function addSameHolderCounterpartyAccounts(records, accounts) {
  const known = new Set(accounts.map((item) => item.key));
  for (const record of records) {
    if (record.source !== "中国银行" || !sameHolder(record.holder, record.counterparty)) continue;
    if (!record.counterpartyAccount || record.counterpartyAccount === "未提供") continue;
    const suffix = lastFour(record.counterpartyAccount);
    if (!/^\d{4}$/.test(suffix)) continue;
    const institutionRaw = normalizeText(record.counterpartyInstitution);
    if (!/银行|支行|信用社|农信/.test(institutionRaw)) continue;
    const institution = institutionRaw.length <= 24 ? institutionRaw : "待识别银行";
    const key = accountKey("same-holder-counterparty", `${record.holder}:${suffix}`);
    if (!known.has(key)) {
      known.add(key);
      accounts.push({
        key,
        displayName: `${institution}账户（${suffix}）`,
        institution,
        type: "银行账户",
        holder: record.holder,
        identifier: `****${suffix}`,
        evidenceStatus: "待补独立流水",
        evidence: `仅在${record.recordId}的对方账户中出现`,
      });
    }
    record.reviewReason = `疑似本人账户间转账；${institution}（${suffix}）尚缺独立流水`;
  }
}

function dayDistance(left, right) {
  return Math.abs(new Date(`${left}T00:00:00Z`) - new Date(`${right}T00:00:00Z`)) / DAY_MS;
}

function platformRailMatches(bank, platform) {
  const bankText = `${bank.category} ${bank.description} ${bank.counterparty}`;
  if (platform.source === "微信") return /财付通|微信|二维码付款/.test(bankText);
  if (platform.source === "支付宝") return /支付宝/.test(bankText);
  return false;
}

function linkCrossSource(records, statementAccounts, accounts) {
  const links = [];
  const usedBank = new Set();
  const statementByKey = new Map(statementAccounts.map((account) => [account.key, account]));
  const bankRecords = records.filter((item) => statementByKey.has(item.sourceAccountKey));
  const platformRecords = records.filter((item) => item.source === "微信" || item.source === "支付宝");
  for (const platform of platformRecords.sort((left, right) => left.date.localeCompare(right.date) || left.recordId.localeCompare(right.recordId))) {
    if (!platform.effect || platform.signedMinor >= 0) continue;
    const instrument = fundingInstrument(platform.paymentMethod);
    if (!instrument) continue;
    const candidates = bankRecords
      .filter((bank) => {
        const statement = statementByKey.get(bank.sourceAccountKey);
        return !usedBank.has(bank.recordId)
          && bank.effect
          && bank.signedMinor === platform.signedMinor
          && dayDistance(bank.date, platform.date) <= 2
          && statement
          && canonicalInstitution(statement.institution) === instrument.institutionKey
          && platformRailMatches(bank, platform);
      })
      .sort((left, right) => dayDistance(left.date, platform.date) - dayDistance(right.date, platform.date)
        || left.date.localeCompare(right.date)
        || left.recordId.localeCompare(right.recordId));
    const match = candidates[0];
    if (!match) continue;
    usedBank.add(match.recordId);
    match.effect = false;
    match.reviewReason = `与${platform.source}记录${platform.recordId}为同一笔消费凭证`;
    const statement = statementByKey.get(match.sourceAccountKey);
    links.push({
      groupId: `LINK-${shortHash(`${match.recordId}:${platform.recordId}`)}`,
      date: platform.date,
      amountMinor: Math.abs(platform.signedMinor),
      left: platform.recordId,
      right: match.recordId,
      type: "同一交易双重凭证",
      status: "已匹配",
      note: `${platform.paymentMethod}已与${statement?.displayName || "银行流水"}逐笔匹配；平台记录计入，银行记录仅作佐证`,
      fundingInstrumentKey: `${instrument.institutionKey}:${instrument.suffix}`,
      statementAccount: statement?.displayName || "银行账户",
    });
  }

  const linkedPlatformIds = new Set(links.map((link) => link.left));
  for (const referenced of accounts.filter((account) => account.evidenceStatus === "待补独立流水")) {
    const instrumentKey = `${canonicalInstitution(referenced.institution)}:${referenced.identifier.slice(-4)}`;
    const relatedExpenses = platformRecords.filter((platform) => {
      const instrument = fundingInstrument(platform.paymentMethod);
      return platform.effect
        && platform.signedMinor < 0
        && instrument
        && `${instrument.institutionKey}:${instrument.suffix}` === instrumentKey;
    });
    if (!relatedExpenses.length || !relatedExpenses.every((platform) => linkedPlatformIds.has(platform.recordId))) continue;
    const instrumentLinks = links.filter((link) => link.fundingInstrumentKey === instrumentKey);
    const statementNames = [...new Set(instrumentLinks.map((link) => link.statementAccount))];
    referenced.evidenceStatus = "已由银行流水佐证";
    referenced.evidence = `${instrumentLinks.length}笔支付已逐笔归并至${statementNames.join("、")}`;
    referenced.statementAccount = statementNames.join("、");
  }
  for (const bank of bankRecords) {
    if (!bank.effect || usedBank.has(bank.recordId) || bank.signedMinor === 0) continue;
    const bankText = `${bank.category} ${bank.description} ${bank.counterparty}`;
    if (!/支付宝|财付通|微信/.test(bankText)) continue;
    const match = platformRecords.find((platform) =>
      platform.effect &&
      /转账|提现|信用卡还款|投资理财|余额互转/.test(`${platform.category} ${platform.description}`) &&
      platform.signedMinor === -bank.signedMinor &&
      dayDistance(platform.date, bank.date) <= 2 &&
      !links.some((link) => link.left === platform.recordId || link.right === platform.recordId)
    );
    if (!match) continue;
    bank.effect = false;
    match.effect = false;
    bank.reviewReason = `与${match.source}记录${match.recordId}构成内部转入/转出`;
    match.reviewReason = `与中国银行记录${bank.recordId}构成内部转入/转出`;
    links.push({
      groupId: `MOVE-${shortHash(`${bank.recordId}:${match.recordId}`)}`,
      date: match.date,
      amountMinor: Math.abs(match.signedMinor),
      left: match.recordId,
      right: bank.recordId,
      type: "内部转账",
      status: "双边流水已匹配",
      note: "不计收入、不计支出",
    });
  }
  return links;
}

function buildAccounts(records, sourceAccounts) {
  const accounts = [...sourceAccounts];
  const known = new Set(accounts.map((item) => item.key));
  for (const record of records) {
    const referenced = parseReferencedAccount(record.paymentMethod, record.holder, accounts);
    if (!referenced || known.has(referenced.key)) continue;
    known.add(referenced.key);
    accounts.push(referenced);
  }
  addPlatformSubaccounts(records, accounts);
  addSameHolderCounterpartyAccounts(records, accounts);
  return accounts;
}

function buildPending(accounts, records) {
  const pending = [];
  for (const account of accounts.filter((item) => item.evidenceStatus === "待补独立流水" || item.evidenceStatus === "待补独立账单")) {
    const suffix = account.identifier.slice(-4);
    const related = records.filter((item) => account.displayName.endsWith("花呗")
      ? item.paymentMethod.includes("花呗")
      : item.paymentMethod.includes(suffix) || item.counterpartyAccount.endsWith(suffix));
    const count = related.length;
    const amountMinor = related.filter((item) => item.effect && item.signedMinor < 0)
      .reduce((sum, item) => sum - item.signedMinor, 0);
    const credit = account.type === "信用账户";
    pending.push({
      item: account.displayName,
      type: credit ? "信用账单与还款流水" : "账户流水",
      period: "2026-05",
      count,
      amountMinor,
      reason: `2026年5月交易中出现 ${count} 次${amountMinor > 0 ? `，涉及支出${formatMoney(amountMinor).toFixed(2)}元` : ""}，但未提供该账户自己的完整明细`,
      action: credit
        ? "提供覆盖2026年5月交易的完整账单，以及对应还款账户流水"
        : "提供2026-05-01至2026-05-31完整流水（优先CSV/XLSX原件，也可银行PDF）",
      priority: "高",
      status: "待提供",
    });
  }
  return pending;
}

function styleTitle(sheet, range) {
  const target = sheet.getRange(range);
  target.format.fill = "#17365D";
  target.format.font = { bold: true, color: "#FFFFFF", size: 18 };
  target.format.rowHeight = 32;
  target.format.verticalAlignment = "center";
}

function styleHeader(range) {
  range.format.fill = "#D9EAF7";
  range.format.font = { bold: true, color: "#17365D" };
  range.format.borders = { preset: "all", style: "thin", color: "#B4C7E7" };
  range.format.wrapText = true;
}

function styleBody(range) {
  range.format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
  range.format.verticalAlignment = "top";
}

function writeSummary(workbook, records, accounts, links, pending, year, month) {
  const sheet = workbook.worksheets.getItem("五月对账") || workbook.worksheets.add("五月对账");
  sheet.showGridLines = false;
  sheet.getRange("A1:H1").merge();
  sheet.getRange("A1").values = [[`${year}年${month}月财务对账`]];
  styleTitle(sheet, "A1:H1");
  sheet.getRange("A3:B3").values = [["对账结论", "待人工确认"]];
  sheet.getRange("A3:B3").format.fill = "#FFF2CC";
  sheet.getRange("A3:B3").format.font = { bold: true, color: "#7F6000" };
  sheet.getRange("A5:B5").values = [["核心指标", "结果"]];
  styleHeader(sheet.getRange("A5:B5"));
  const last = records.length + 1;
  sheet.getRange("A6:A12").values = [
    ["导入流水"], ["计入流水"], ["收入"], ["支出"], ["净额"], ["内部/重复匹配"], ["待补佐证"],
  ];
  sheet.getRange("B6:B12").formulas = [
    [`=COUNTA('五月流水'!$A$2:$A$${last})`],
    [`=COUNTIF('五月流水'!$G$2:$G$${last},"计入")`],
    [`=ROUND(SUMIFS('五月流水'!$F$2:$F$${last},'五月流水'!$G$2:$G$${last},"计入",'五月流水'!$F$2:$F$${last},">0"),2)`],
    [`=ROUND(-SUMIFS('五月流水'!$F$2:$F$${last},'五月流水'!$G$2:$G$${last},"计入",'五月流水'!$F$2:$F$${last},"<0"),2)`],
    ["=ROUND(B8-B9,2)"],
    [`=COUNTA('内部转账'!$A$2:$A$${Math.max(2, links.length + 1)})`],
    [`=COUNTA('待补佐证'!$A$2:$A$${Math.max(2, pending.length + 1)})`],
  ];
  sheet.getRange("B8:B10").setNumberFormat("¥#,##0.00;[Red]-¥#,##0.00");
  sheet.getRange("B8:B10").format.numberFormat = [
    ["¥#,##0.00;[Red]-¥#,##0.00"],
    ["¥#,##0.00;[Red]-¥#,##0.00"],
    ["¥#,##0.00;[Red]-¥#,##0.00"],
  ];
  styleBody(sheet.getRange("A6:B12"));
  sheet.getRange("D5:H5").values = [["来源", "流水数", "计入数", "收入", "支出"]];
  styleHeader(sheet.getRange("D5:H5"));
  const sources = ["微信", "支付宝", "中国银行"];
  sheet.getRange("D6:D8").values = sources.map((source) => [source]);
  for (let row = 6; row <= 8; row += 1) {
    sheet.getRange(`E${row}`).formulas = [[`=COUNTIF('五月流水'!$C$2:$C$${last},D${row})`]];
    sheet.getRange(`F${row}`).formulas = [[`=COUNTIFS('五月流水'!$C$2:$C$${last},D${row},'五月流水'!$G$2:$G$${last},"计入")`]];
    sheet.getRange(`G${row}`).formulas = [[`=ROUND(SUMIFS('五月流水'!$F$2:$F$${last},'五月流水'!$C$2:$C$${last},D${row},'五月流水'!$G$2:$G$${last},"计入",'五月流水'!$F$2:$F$${last},">0"),2)`]];
    sheet.getRange(`H${row}`).formulas = [[`=ROUND(-SUMIFS('五月流水'!$F$2:$F$${last},'五月流水'!$C$2:$C$${last},D${row},'五月流水'!$G$2:$G$${last},"计入",'五月流水'!$F$2:$F$${last},"<0"),2)`]];
  }
  sheet.getRange("G6:H8").setNumberFormat("¥#,##0.00;[Red]-¥#,##0.00");
  sheet.getRange("G6:H8").format.numberFormat = Array.from({ length: 3 }, () => [
    "¥#,##0.00;[Red]-¥#,##0.00",
    "¥#,##0.00;[Red]-¥#,##0.00",
  ]);
  styleBody(sheet.getRange("D6:H8"));
  sheet.getRange("D10:H10").merge();
  sheet.getRange("D10").values = [["规则说明"]];
  styleHeader(sheet.getRange("D10:H10"));
  sheet.getRange("D11:H14").merge();
  sheet.getRange("D11").values = [[
    "同一笔微信/支付宝银行卡消费，只计平台记录，银行记录作为佐证；内部转账双方均不计收入或支出；未提供独立流水的账户只记录、不自动核销。",
  ]];
  sheet.getRange("D11:H14").format.wrapText = true;
  sheet.getRange("D11:H14").format.fill = "#F3F6F8";
  styleBody(sheet.getRange("D11:H14"));
  sheet.getRange("A15:H15").merge();
  sheet.getRange("A15").values = [[`账户台账：${accounts.length} 个；所有判断保留证据编号，最终确认在网页完成。`]];
  sheet.getRange("A15:H15").format.font = { italic: true, color: "#5B6573" };
  sheet.getRange("A:H").format.columnWidth = 16;
  sheet.getRange("D:D").format.columnWidth = 20;
  sheet.freezePanes.freezeRows(1);
}

function writeTransactions(workbook, records) {
  const sheet = workbook.worksheets.add("五月流水");
  sheet.showGridLines = false;
  const headers = ["记录ID", "日期", "来源", "账户", "方向", "金额(元)", "是否计入", "交易类型", "对方", "支付/渠道", "状态", "凭证编号", "复核提示"];
  sheet.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
  const values = records.map((record) => [
    record.recordId,
    record.date,
    record.source,
    record.sourceAccountName,
    record.direction,
    formatMoney(record.signedMinor),
    record.effect ? "计入" : "不计",
    record.category,
    record.counterparty,
    record.paymentMethod || record.description,
    record.status,
    record.evidence,
    record.reviewReason,
  ]);
  if (values.length) sheet.getRangeByIndexes(1, 0, values.length, headers.length).values = values;
  styleHeader(sheet.getRangeByIndexes(0, 0, 1, headers.length));
  if (values.length) styleBody(sheet.getRangeByIndexes(1, 0, values.length, headers.length));
  sheet.getRange(`F2:F${values.length + 1}`).setNumberFormat("¥#,##0.00;[Red]-¥#,##0.00");
  sheet.getRange(`F2:F${values.length + 1}`).format.numberFormat = Array.from(
    { length: values.length },
    () => ["¥#,##0.00;[Red]-¥#,##0.00"],
  );
  sheet.getRange(`A1:M${values.length + 1}`).format.wrapText = true;
  sheet.getRange("A:M").format.columnWidth = 14;
  sheet.getRange("B:B").format.columnWidth = 12;
  sheet.getRange("D:D").format.columnWidth = 22;
  sheet.getRange("H:J").format.columnWidth = 24;
  sheet.getRange("M:M").format.columnWidth = 38;
  sheet.freezePanes.freezeRows(1);
  if (values.length) {
    const table = sheet.tables.add(`A1:M${values.length + 1}`, true, "MayTransactionsTable");
    table.style = "TableStyleMedium2";
    table.showFilterButton = true;
  }
}

function writeAccounts(workbook, accounts, records) {
  const sheet = workbook.worksheets.add("账户台账");
  sheet.showGridLines = false;
  const headers = ["账户", "机构", "类型", "户名", "标识", "流水状态", "五月出现次数", "证据编号", "处理规则"];
  sheet.getRange("A1:I1").values = [headers];
  const rows = accounts.map((account) => [
    account.displayName,
    account.institution,
    account.type,
    account.holder,
    account.identifier,
    account.evidenceStatus,
    records.filter((record) => record.sourceAccountKey === account.key || record.paymentMethod.includes(account.identifier.slice(-4)) || record.counterpartyAccount.endsWith(account.identifier.slice(-4))).length,
    account.evidence,
    account.evidenceStatus === "已提供独立流水"
      ? "作为内部账户参与双边匹配"
      : account.evidenceStatus === "已由银行流水佐证"
        ? `支付工具已归并至${account.statementAccount || "对应银行账户"}`
        : account.evidenceStatus.startsWith("待补")
          ? "记录账户，但不自动核销"
          : "作为平台内部子账户记录",
  ]);
  if (rows.length) sheet.getRangeByIndexes(1, 0, rows.length, headers.length).values = rows;
  styleHeader(sheet.getRange("A1:I1"));
  if (rows.length) styleBody(sheet.getRangeByIndexes(1, 0, rows.length, headers.length));
  sheet.getRange("A:I").format.columnWidth = 18;
  sheet.getRange("A:A").format.columnWidth = 26;
  sheet.getRange("H:I").format.columnWidth = 34;
  sheet.getRange(`A1:I${rows.length + 1}`).format.wrapText = true;
  sheet.freezePanes.freezeRows(1);
  if (rows.length) {
    const table = sheet.tables.add(`A1:I${rows.length + 1}`, true, "ManagedAccountsTable");
    table.style = "TableStyleMedium4";
    table.showFilterButton = true;
  }
}

function writeLinks(workbook, links) {
  const sheet = workbook.worksheets.add("内部转账");
  sheet.showGridLines = false;
  const headers = ["匹配组", "日期", "金额(元)", "第一条流水", "第二条流水", "类型", "状态", "说明"];
  sheet.getRange("A1:H1").values = [headers];
  const rows = links.map((link) => [link.groupId, link.date, formatMoney(link.amountMinor), link.left, link.right, link.type, link.status, link.note]);
  if (rows.length) sheet.getRangeByIndexes(1, 0, rows.length, headers.length).values = rows;
  else sheet.getRange("A2:H2").values = [["", "", 0, "", "", "", "无自动匹配", "后续新增账户流水后重新运行即可"]];
  styleHeader(sheet.getRange("A1:H1"));
  styleBody(sheet.getRange(`A2:H${Math.max(2, rows.length + 1)}`));
  sheet.getRange(`C2:C${Math.max(2, rows.length + 1)}`).setNumberFormat("¥#,##0.00;[Red]-¥#,##0.00");
  sheet.getRange(`C2:C${Math.max(2, rows.length + 1)}`).format.numberFormat = Array.from(
    { length: Math.max(1, rows.length) },
    () => ["¥#,##0.00;[Red]-¥#,##0.00"],
  );
  sheet.getRange("A:H").format.columnWidth = 18;
  sheet.getRange("H:H").format.columnWidth = 42;
  sheet.freezePanes.freezeRows(1);
}

function writePending(workbook, pending) {
  const sheet = workbook.worksheets.add("待补佐证");
  sheet.showGridLines = false;
  const headers = ["项目", "类型", "期间", "涉及交易", "涉及支出(元)", "原因", "需要做什么", "优先级", "状态"];
  sheet.getRange("A1:I1").values = [headers];
  const rows = pending.map((item) => [
    item.item,
    item.type,
    item.period,
    item.count,
    formatMoney(item.amountMinor),
    item.reason,
    item.action,
    item.priority,
    item.status,
  ]);
  if (rows.length) sheet.getRangeByIndexes(1, 0, rows.length, headers.length).values = rows;
  else sheet.getRange("A2:I2").values = [["无", "", "", 0, 0, "当前未发现缺失佐证", "", "", ""]];
  styleHeader(sheet.getRange("A1:I1"));
  styleBody(sheet.getRange(`A2:I${Math.max(2, rows.length + 1)}`));
  sheet.getRange(`E2:E${Math.max(2, rows.length + 1)}`).setNumberFormat("¥#,##0.00;[Red]-¥#,##0.00");
  sheet.getRange("A:I").format.columnWidth = 18;
  sheet.getRange("A:A").format.columnWidth = 30;
  sheet.getRange("F:G").format.columnWidth = 44;
  sheet.getRange(`A1:I${Math.max(2, rows.length + 1)}`).format.wrapText = true;
  sheet.freezePanes.freezeRows(1);
  if (rows.length) {
    const table = sheet.tables.add(`A1:I${rows.length + 1}`, true, "MissingMaterialsTable");
    table.style = "TableStyleMedium9";
    table.showFilterButton = true;
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const wechatParsed = await Promise.all(args.wechat.map((filePath) => parseWechat(filePath, args.year, args.month)));
  const alipayParsed = await Promise.all(args.alipay.map((filePath) => parseAlipay(filePath, args.year, args.month)));
  const bocParsed = await parseBoc(args.bocWorkbook, args.year, args.month);
  const records = [
    ...wechatParsed.flatMap((item) => item.records),
    ...alipayParsed.flatMap((item) => item.records),
    ...bocParsed.records,
  ].sort((left, right) => left.date.localeCompare(right.date) || left.recordId.localeCompare(right.recordId));
  const sourceAccounts = [];
  for (const item of [...wechatParsed, ...alipayParsed].map((entry) => entry.account).concat(bocParsed.account)) {
    if (!sourceAccounts.some((account) => account.key === item.key)) sourceAccounts.push(item);
  }
  const accounts = buildAccounts(records, sourceAccounts);
  const links = linkCrossSource(records, [bocParsed.account], accounts);
  const pending = buildPending(accounts, records);

  const workbook = Workbook.create();
  workbook.worksheets.add("五月对账");
  writeTransactions(workbook, records);
  writeAccounts(workbook, accounts, records);
  writeLinks(workbook, links);
  writePending(workbook, pending);
  writeSummary(workbook, records, accounts, links, pending, args.year, args.month);

  await fs.mkdir(path.dirname(args.output), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(args.output);

  const manifest = {
    period: `${args.year}-${String(args.month).padStart(2, "0")}`,
    counts: {
      records: records.length,
      included: records.filter((item) => item.effect).length,
      wechat: records.filter((item) => item.source === "微信").length,
      alipay: records.filter((item) => item.source === "支付宝").length,
      boc: records.filter((item) => item.source === "中国银行").length,
      accounts: accounts.length,
      links: links.length,
      pending: pending.length,
    },
    checks: {
      duplicateRecordIds: records.length - new Set(records.map((item) => item.recordId)).size,
      missingEvidence: records.filter((item) => !item.evidence).length,
      unbalancedInternalLinks: links.filter((item) => item.type === "内部转账" && item.amountMinor <= 0).length,
    },
    totalsMinor: {
      income: records.filter((item) => item.effect && item.signedMinor > 0).reduce((sum, item) => sum + item.signedMinor, 0),
      expense: -records.filter((item) => item.effect && item.signedMinor < 0).reduce((sum, item) => sum + item.signedMinor, 0),
      net: records.filter((item) => item.effect).reduce((sum, item) => sum + item.signedMinor, 0),
    },
    materialsNeeded: pending.map((item) => ({
      item: item.item,
      type: item.type,
      period: item.period,
      transactionCount: item.count,
      expenseMinor: item.amountMinor,
      action: item.action,
      priority: item.priority,
      status: item.status,
    })),
    generatedAt: new Date().toISOString(),
  };
  const manifestPath = args.manifest || args.output.replace(/\.xlsx$/i, ".manifest.json");
  await fs.writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  if (args.normalizedOutput) {
    const normalized = {
      schemaVersion: "ledgerbridge.financial-foundation-normalized.v1",
      period: manifest.period,
      records: records
        .filter((item) => item.source === "微信" || item.source === "支付宝")
        .map((item) => ({
          recordId: item.recordId,
          date: item.date,
          source: item.source,
          amountMinor: item.signedMinor,
          effect: item.effect,
          direction: item.direction,
          category: item.category,
          counterparty: item.counterparty,
          description: item.description,
          paymentMethod: item.paymentMethod,
          status: item.status,
          evidenceAlias: item.source === "微信" ? "wechat" : "alipay",
        })),
    };
    await fs.mkdir(path.dirname(args.normalizedOutput), { recursive: true });
    await fs.writeFile(args.normalizedOutput, `${JSON.stringify(normalized)}\n`, { encoding: "utf8", mode: 0o600 });
  }
  process.stdout.write(`${JSON.stringify(manifest)}\n`);
}

await main();
