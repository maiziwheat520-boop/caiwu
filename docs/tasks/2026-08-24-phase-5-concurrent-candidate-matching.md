# Phase 5 concurrent candidate-matching boundary

日期：2026-08-24  
结论：完成进程内 admission 与持久化 DEDUP Review 收敛；数据库唯一约束仍是跨进程最终边界

## 范围

`ConcurrentDedupIndex` 为 importer/worker 候选匹配提供一个原子
`classify → register` 操作。相同外部身份且指纹一致的并发副本只允许一个成为
`NEW`，其余返回 `DUPLICATE`；同一外部身份但指纹冲突的副本全部返回
`NEEDS_REVIEW`，不会覆盖已登记记录，也不会自动删除证据。

该锁只保护同一进程内的 worker。`20260824_0011` 在 `review_item` 增加可选的
64 位 SHA-256 `candidate_key`、格式约束和部分唯一索引；`ReviewService` 使用该 key
在唯一索引冲突时重新读取已提交的 ReviewItem，使并发 worker 收敛到同一条 DEDUP
审核记录。跨进程部署仍必须依赖 PostgreSQL 的唯一索引、事务
和已有 immutable source-record 约束；本边界不创建自动 POST、自动审核决定或生产
Connector 注册。`admit_many` 保持输入顺序，便于 importer 将结果稳定地映射回批次。

## 验证

`tests/test_reconciliation.py` 使用 16 个并发线程验证等价候选只有一个 `NEW`、其余
均为 `DUPLICATE`；另用 8 个相同外部 ID、不同指纹的线程验证只有一个 `NEW`，其余
均为 `EXTERNAL_ID_CONFLICT` 的 `NEEDS_REVIEW`，登记计数始终为 1。
`tests/test_candidate_review_persistence.py` 在 Hermes 隔离 PostgreSQL 中以 worker
角色并发创建同一 `candidate_key`，结果为同一 UUID 且数据库只有 1 条 ReviewItem；
临时数据库、卷、网络和隧道已清理。

Review API 仍默认关闭，但已返回 `candidate_key` 供后续人工审核关联；真实 Connector
和生产开关继续关闭。
