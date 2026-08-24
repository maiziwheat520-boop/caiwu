# Phase 5 deduplication, reconciliation, and Suspense framework

日期：2026-08-24
结论：纯契约框架完成；持久化服务、真实 parser 和自动入账仍未启用

## 边界

Phase 5 先冻结“如何提出候选”和“如何进入人工决定”的契约，不把启发式
匹配伪装成事实。`src/ledgerbridge/reconciliation.py` 是无副作用的纯逻辑层：
它不删除 `SourceRecord`，不修改 `JournalEntry`，不自动 POST，也不直接写数据库。

## 去重

- 外部交易身份为 `(source_system, account_key, external_transaction_id)`。
- 相同外部身份且指纹一致时返回 `DUPLICATE`；身份冲突返回 `NEEDS_REVIEW`。
- 日期、金额、对手方、描述和余额形成 SHA-256 辅助指纹；指纹相同只返回
  `NEEDS_REVIEW`，绝不自动删除或合并。
- `DedupIndex.register()` 只接受 `NEW`，没有删除/覆盖接口，保留原始证据。

## 对账

- `ReconciliationProposal` 只接受明确的 1:1、1:N、N:1 legs，并强制所有金额
  为 CNY、记录定位符唯一、合计为零。
- 提案初始为 `PROPOSED`；只能由显式 actor/reason 转为 `CONFIRMED` 或
  `REJECTED`。确认本身不产生 JournalEntry，后续服务必须在现有审计事务边界内执行。

## Suspense

- `SuspenseItem` 表达未知对手方、未匹配转账、余额差异和贷款拆分等待处理项。
- 项目保持 `OPEN`，只有提供目标账户、操作人和理由才能 `RESOLVED`；金额不可改变，
  不能把 Suspense 解析为同一个 Suspense 账户。

## 未包含

本阶段没有迁移、数据库表、自动转账发现、规则引擎、真实财务数据、自动 POST、
或生产配置开关。持久化实现必须新增 append-only 审计、并发唯一约束、人工 Review
边界和回滚/恢复演练后才能进入下一次独立授权。

## 验证

`tests/test_reconciliation.py` 覆盖外部 ID 冲突、辅助指纹、三种对账关系、零和/重复
定位符、显式确认/拒绝、Suspense 金额守恒和输入安全边界。
