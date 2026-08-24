# Phase 6 synthetic Connector fixtures

日期：2026-08-24  
结论：完成测试/隔离夹具及 importer 隔离端到端回放；默认 manifest 仍为空

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
来源不认领、截断 prefix、非法金额/币种、坏 schema、流大小和记录形状。
`tests/test_synthetic_import_replay.py` 在隔离 PostgreSQL 中通过真实 `EvidenceImporter`
回放固定夹具：首次导入成功并创建 2 条 source record，重复同一文件保持幂等且不重复
写入。Hermes 回放使用一次性 Compose 项目和临时凭据，验证后已销毁卷、网络、容器及
隧道；没有连接生产数据库或启用默认 manifest。

下一步进入并发候选匹配/导入边界审计；真实 Connector、OAuth 和自动 POST 仍保持关闭。
