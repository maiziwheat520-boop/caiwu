# Phase 5 Review API and worker persistence boundary

日期：2026-08-24  
结论：完成受控接线；默认关闭，未启用生产

## 已实现

- `src/ledgerbridge/review_service.py` 提供 Review 列表、显式决定、worker 创建
  Review 和原子创建 Suspense 的数据库边界。
- API 新增 `GET /v1/reviews` 与
  `POST /v1/reviews/{review_id}/decision`。接口只在
  `LEDGERBRIDGE_ENABLE_REVIEW_API=true` 且非 production 时开放，并始终要求
  已安装的 authenticated principal；API 不能创建、删除或直接 POST 日记账。
- Review 决定在同一事务写入独立审计事件；对账决定同步子表状态，Suspense 决定
  必须携带不同的目标账户。数据库触发器继续作为最终状态边界。
- worker 暴露 `build_review_service()`，使用 `ledgerbridge_worker` URL，供后续
  解析/候选流程创建 Review 或 Suspense；本轮没有把启发式匹配或自动 POST 接入。

## 验证与边界

`tests/test_review_api.py` 覆盖 API 默认关闭、principal、冲突映射、ReviewService
的列表/读取/决定、对账与 Suspense 分支、worker 创建校验。迁移仍由
`20260824_0010` 提供表级约束和权限；真实数据、真实 Connector、生产开关、自动
匹配和自动入账均未启用。下一步是用合成 SourceRecord 做并发候选匹配和审计回放。
