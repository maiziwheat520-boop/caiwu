# Phase 6 synthetic Connector fixtures

日期：2026-08-24  
结论：完成测试/隔离夹具；默认 manifest 仍为空

## 范围

`src/ledgerbridge/synthetic_connector.py` 提供一个无凭据、无网络、无副作用的
`SyntheticBankConnector` 和显式 `SyntheticBankFactory`。它只接受
`synthetic_upload` + `application/json`，识别固定 schema
`ledgerbridge.synthetic.bank.v1`，输出稳定的 `ParsedSourceRecord`，并沿用现有
JSON、CNY 分币整数、外部交易 ID 和大小边界校验。

固定样本位于 `tests/fixtures/synthetic_bank_statement.json`。它只包含演示数据，
不代表真实银行格式、账户或财务证据。`ConnectorRegistry()` 默认仍返回空集合；
只有测试显式提供 `SyntheticBankFactory` 时才构造该 Connector，生产 manifest、
邮箱 provider、runner 和真实数据均未改变。

## 验证

`tests/test_synthetic_connector.py` 覆盖稳定检测/解析、registry 显式构造、错误
来源不认领、截断 prefix、非法金额/币种、坏 schema、流大小和记录形状。下一步
可以把该夹具接到 importer 的合成端到端回放和 Phase 5 候选匹配，但必须继续保持
与生产 Connector/manifest 的隔离。
