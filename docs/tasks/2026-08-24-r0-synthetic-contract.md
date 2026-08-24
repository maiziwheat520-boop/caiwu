# R0 synthetic Core contract

日期：2026-08-24  
结论：实现完成，待独立复核；未接线、未部署、未启用真实数据

## 交付物

- `candidate_contract.py` 冻结 `ledgerbridge.candidate.v1` 投影与
  `ledgerbridge.candidate-state.v1` 状态图。投影只包含白名单规范字段、内部不透明
  来源/证据引用、blocker 和审核摘要；不包含 `raw_fields`、`storage_key`、原生消息
  ID、原文件名、任意 Review payload 或原始身份。
- 状态机是无数据库、无网络、无文件副作用的纯合同：补全与解冲突只到
  `PENDING`，只有 `PENDING` 可确认；开放态可忽略；`CONFIRMED` 只能通过独立
  `SUPERSEDE` 变成 `SUPERSEDED` 并创建带双向来源链接的新 `PENDING` 候选。
  每个动作要求 `expected_revision`，只追加事件，并对 operation ID 重放做幂等校验。
- `internal_read_contract.py` 冻结六类只读路由的 capability、entity/营业单元范围和
  SAN 固定映射合同。`candidate:create`、`candidate:decide` 和
  `candidate:supersede` 分权；跨范围对象统一采用 not-visible 语义。
- `docs/contracts/internal-read-v1.openapi.yaml` 是独立的 Core 内部 OpenAPI 3.1
  合同。它只包含规定的六类 GET 和 `mutualTLS` security scheme，不含浏览器
  cookie、CORS、写方法或通用查询。
- `r0_contract_fixture.json` 和两份小型 evidence 文件全部为固定合成数据，覆盖六种
  candidate 状态、两组 entity/营业单元、Hermes 三平台、Outlook、整数分币边界、
  POSTED 与非 POSTED 账簿数据、普通文本及活动 MIME 降级。fixture 中摘要、长度和
  实际 bytes 一致。

## 关键边界

- R0 没有修改 `main.py`、`config.py`、数据库模型、Alembic、ArtifactStore、
  Connector manifest、worker 或部署文件。
- `/internal/v1` 未安装到 FastAPI；静态 OpenAPI 是后续 R1 实现必须遵守的合同，
  不是已上线接口。
- `SyntheticPeerEvidence` 仅模拟未来可信 mTLS verifier 的输出，不解析请求头、
  不生成证书，也不包含 CA、私钥或其他凭据。
- 当前金额 JSON 合同限制在 JavaScript safe-integer 范围内，同时仍为 CNY 整数
  分币；超界、float、bool、NaN/Infinity 均拒绝，避免 Core 到 BFF 时发生静默精度
  丢失。
- R0 不实现 S1 在线加密、R1 数据库查询/真实 mTLS、R2 Web adapter、I1 ingest、
  D1 命令 API、Hermes/Outlook/OneDrive/OAuth、真实 parser 或工作簿发布。

## 验证

- `tests/test_r0_candidate_contract.py`：合成 fixture、所有合法状态迁移、非法迁移、
  stale revision、幂等重放冲突、append-only revision、supersede 派生关系、金额与
  projection shape。
- `tests/test_r0_internal_read_contract.py`：只读 OpenAPI、capability 非传递、worker/
  reviewer/supervisor 分权、mTLS 测试身份 fail-closed、entity/营业单元对象范围、
  evidence digest/MIME 和 POSTED-only 合成账簿汇总。
- 定向测试、Ruff、strict mypy 与 Bandit 已通过；完整项目回归和独立审查结果在提交
  前补入本任务卡。

