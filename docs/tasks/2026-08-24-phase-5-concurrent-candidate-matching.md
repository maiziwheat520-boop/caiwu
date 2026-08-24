# Phase 5 concurrent candidate-matching boundary

日期：2026-08-24  
结论：完成进程内并发 admission 边界；数据库唯一约束仍是跨进程最终边界

## 范围

`ConcurrentDedupIndex` 为 importer/worker 候选匹配提供一个原子
`classify → register` 操作。相同外部身份且指纹一致的并发副本只允许一个成为
`NEW`，其余返回 `DUPLICATE`；同一外部身份但指纹冲突的副本全部返回
`NEEDS_REVIEW`，不会覆盖已登记记录，也不会自动删除证据。

该锁只保护同一进程内的 worker。跨进程部署仍必须依赖 PostgreSQL 的唯一索引、事务
和已有 immutable source-record 约束；本边界不创建自动 POST、自动审核决定或生产
Connector 注册。`admit_many` 保持输入顺序，便于 importer 将结果稳定地映射回批次。

## 验证

`tests/test_reconciliation.py` 使用 16 个并发线程验证等价候选只有一个 `NEW`、其余
均为 `DUPLICATE`；另用 8 个相同外部 ID、不同指纹的线程验证只有一个 `NEW`，其余
均为 `EXTERNAL_ID_CONFLICT` 的 `NEEDS_REVIEW`，登记计数始终为 1。

下一步可在持久化候选表/Review API 接线时，将该原子边界与数据库唯一约束、审核
事件和跨进程重试回放合并验证；真实 Connector 和生产开关继续关闭。
