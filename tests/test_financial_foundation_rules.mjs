import assert from "node:assert/strict";
import test from "node:test";

import {
  accountLifecycle,
  accountNeedsCollection,
  counterpartyProjection,
  matchWechatRefunds,
  platformRecordPreference,
  platformSubaccountEvidence,
} from "../scripts/financial_foundation_rules.mjs";

test("only ABC account suffix 2061 is closed and excluded from collection", () => {
  assert.deepEqual(
    accountLifecycle({
      institution: "中国农业银行成都某支行",
      identifier: "****2061",
    }),
    {
      lifecycleStatus: "closed",
      excludedFromCollection: true,
      lifecycleReason: "账户已注销；保留历史交易与证据，不再要求补充后续流水",
    },
  );
  assert.deepEqual(
    accountLifecycle({
      institution: "中国农业银行成都某支行",
      identifier: "****2062",
    }),
    {
      lifecycleStatus: "active",
      excludedFromCollection: false,
      lifecycleReason: "",
    },
  );
  assert.equal(
    accountLifecycle({
      institution: "中国建设银行",
      identifier: "****2061",
    }).excludedFromCollection,
    false,
  );
  assert.equal(
    accountLifecycle({
      institution: "非农业银行文本",
      identifier: "****2061",
    }).excludedFromCollection,
    false,
  );
  assert.equal(
    accountNeedsCollection({
      institution: "中国农业银行成都某支行",
      identifier: "****2061",
      evidenceStatus: "待补独立流水",
    }),
    false,
  );
  assert.equal(
    accountNeedsCollection({
      institution: "中国农业银行成都某支行",
      identifier: "****2062",
      evidenceStatus: "待补独立流水",
    }),
    true,
  );
});

test("counterparty output is stable and excludes platform-internal subaccounts", () => {
  const projected = counterpartyProjection({
    counterparty: " 同一　对象 ",
    category: "消费",
    paymentMethod: "花呗",
  });
  assert.deepEqual(projected, counterpartyProjection({
    counterparty: "同一 对象",
    category: "消费",
    paymentMethod: "余额宝",
  }));
  assert.equal(projected.counterpartyClass, "unknown");
  assert.match(projected.counterpartyRef, /^cp_[0-9a-f]{64}$/);
  assert.equal(
    counterpartyProjection({ counterparty: "余额宝", category: "余额互转", paymentMethod: "账户余额" }),
    null,
  );
});

test("platform statements cover every platform-internal subaccount", () => {
  for (const name of ["花呗", "余额宝", "账户余额", "零钱", "零钱通"]) {
    assert.deepEqual(platformSubaccountEvidence(name, "platform-evidence"), {
      status: "已由所属平台完整流水覆盖",
      evidence: "platform-evidence",
    });
  }
});

test("purchases use the platform row while transfers use the bank row", () => {
  assert.equal(platformRecordPreference({ category: "商户消费", description: "供电" }), "platform");
  assert.equal(platformRecordPreference({ category: "转账", description: "转给个人" }), "bank");
  assert.equal(platformRecordPreference({ category: "投资理财", description: "申购基金" }), "bank");
});

test("a unique WeChat partial refund keeps both rows and closes to the net expense", () => {
  const records = [
    {
      recordId: "WX-payment",
      date: "2026-05-01",
      source: "微信",
      sourceAccountKey: "wechat:one",
      holder: "测试用户",
      direction: "支出",
      signedMinor: -10000,
      effect: true,
      category: "商户消费",
      counterparty: "合成商户",
      description: "合成商品",
      paymentMethod: "零钱通",
      status: "已退款(￥49.95)",
      reviewReason: "",
    },
    {
      recordId: "WX-refund",
      date: "2026-05-03",
      source: "微信",
      sourceAccountKey: "wechat:one",
      holder: "测试用户",
      direction: "退款收入",
      signedMinor: 4995,
      effect: true,
      category: "商户消费-退款",
      counterparty: "合成商户",
      description: "合成商品",
      paymentMethod: "/",
      status: "已退款(￥49.95)",
      reviewReason: "",
    },
  ];

  const links = matchWechatRefunds(records);

  assert.equal(links.length, 1);
  assert.deepEqual(
    links[0],
    {
      groupId: "REFUND-e9a2b8b6410e",
      date: "2026-05-03",
      amountMinor: 4995,
      left: "WX-payment",
      right: "WX-refund",
      type: "部分退款",
      status: "原支付与退款已匹配",
      note: "原支出100.00元，退款49.95元，净支出50.05元；两条流水均保留",
    },
  );
  assert.equal(records[0].signedMinor + records[1].signedMinor, -5005);
  assert.match(records[0].reviewReason, /部分退款已匹配/);
  assert.match(records[1].reviewReason, /部分退款已匹配/);
});

test("ambiguous WeChat refunds remain unmatched", () => {
  const payment = {
    recordId: "WX-payment",
    date: "2026-05-01",
    source: "微信",
    sourceAccountKey: "wechat:one",
    holder: "测试用户",
    direction: "支出",
    signedMinor: -10000,
    effect: true,
    category: "商户消费",
    counterparty: "合成商户",
    description: "相同商品",
    paymentMethod: "零钱通",
    status: "已退款(￥49.95)",
    reviewReason: "",
  };
  const refund = (recordId) => ({
    ...payment,
    recordId,
    date: "2026-05-03",
    direction: "退款收入",
    signedMinor: 4995,
    category: "商户消费-退款",
    paymentMethod: "/",
  });
  const records = [payment, refund("WX-refund-one"), refund("WX-refund-two")];

  assert.deepEqual(matchWechatRefunds(records), []);
  assert.equal(records[0].reviewReason, "");
});
