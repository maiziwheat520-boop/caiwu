# R1 synthetic Core read API foundation

日期：2026-08-24
结论：合成路由代码基础完成；不代表 R1 运维 gate 通过

## 已确认范围

- 在 S1 分支基础上安装 R0 冻结的六个 `/internal/v1` GET 路由，运行数据只来自
  随包发布且经过完整校验的合成 fixture；不读取 PostgreSQL、legacy ArtifactStore、
  Hermes、Outlook、OneDrive 或真实财务数据。
- 路由默认关闭，生产配置即使显式开启也拒绝。`enable_real_ingest=true` 继续无条件
  fail closed。
- 身份只能由受信 mTLS verifier 向 ASGI scope 注入精确 typed assertion；请求 header、
  cookie、bearer token、字符串 state 或直接传入的 principal 都没有身份权限。本轮不部署
  TLS verifier，也不把反向代理 header 冒充为 mTLS。断言必须为带时区的短时窗口，最长
  一小时，并同时匹配当前策略代次和 principal 自身策略代次。
- 每条路由使用独立 capability，并在读取对象/证据前执行 entity + business-unit scope。
  `business_unit_ref=null` 的候选仅在该 entity grant 明确设置“允许未分配候选”时可见，
  不能从任一普通营业单元授权推断；该权限也可单独授予而不附带普通营业单元权限。
- 证据在授权后完整复核大小与 SHA-256，再统一以 `application/octet-stream`、attachment、
  `no-store`、`nosniff` 和 `Content-Digest` 返回。成功响应必须先写入注入式 append-only
  audit sink；事件包含 principal、策略代次、evidence/entity/business-unit scope、大小和
  digest。默认 sink 不可用并 fail closed。本轮只实现接口和合成测试 sink，不宣称已有
  生产持久审计。

## 明确不做

- 不新建 Candidate/营业单元/加密证据/月度快照数据库 schema，不追加数据库 grants，
  不把 `ReviewItem.payload` 或 `SourceRecord.raw_fields` 拼成候选事实。
- 不完成生产 mTLS、游标持久密钥、生产审计、Hermes LUKS、生产 KeyProvider、备份恢复
  演练或 Web BFF 适配；因此只称“R1 合成代码基础”，不称完整 R1。
- 不实现 ingest/decision 写方法、真实 Connector/OAuth、Hermes/Outlook 采集、自动匹配、
  自动过账或工作簿发布。

## 验收

- 六路由响应严格符合 R0 Pydantic/OpenAPI 白名单；未知/重复/非法 query 返回固定
  `application/problem+json` 400，缺失身份为 401，缺 capability 为 403，不存在与越权
  对象同形 404。
- 候选集合先按 principal grant 过滤，再按 month/status/business unit 过滤，并稳定按
  `(created_at, candidate_ref)` 升序、最多 100 项。合成 fixture 不签发 cursor，任何非空
  cursor 统一拒绝。
- Evidence 拒绝未授权读取、digest/size 损坏、活动 MIME inline、安全文件名异常和审计
  sink 失败；拒绝路径不得读取证据 bytes 或留下成功审计。
- POST/PUT/PATCH/DELETE/HEAD/OPTIONS 不得形成业务能力；不得返回 CORS、cookie、主机、
  数据库、存储路径、证书或密钥信息。所有业务成功与问题响应统一 `Cache-Control:
  no-store`。
- 完成定向与全量 pytest、Ruff、strict mypy、Bandit、敏感路径和依赖审计，并进行独立
  安全复核；任何未完成的运维条件保持显式 gate。

## 独立安全复核与验证

- 首轮只读安全复核未发现 BLOCKER/HIGH；发现的 1 个 MEDIUM 和 4 个 LOW 已全部修复，
  窄范围复核确认无新增 BLOCKER/HIGH/MEDIUM。修复覆盖最小权限的 unassigned-only grant、
  全响应 `no-store`、参数校验先于 fixture 构造、对象查找时序收口，以及审计记录实际
  verifier SAN。
- R1/R0/config 定向测试：`64 passed`；全量测试：`461 passed, 149 skipped`。跳过项均为
  当前 Windows/POSIX 能力或未配置 PostgreSQL 集成环境的既有条件；另有 1 个上游
  Starlette/httpx 弃用警告。
- Ruff format/check、strict mypy、Bandit、敏感路径检查、`uv lock --check`、
  `git diff --check`、Compose feature-gate YAML 解析和 `pip-audit --strict` 均通过；依赖审计
  未发现已知漏洞。构建出的 wheel 也已验证包含 JSON fixture 与三份 evidence 资源。

## 未闭环运维 gate

生产 mTLS verifier、持久 append-only audit backend、真实 Candidate/营业单元/加密证据/
月度快照 schema 与最小数据库 grants、Hermes LUKS/KeyProvider/备份恢复演练均未实施。
在这些条件完成并单独复核前，生产启用继续被配置校验拒绝。
