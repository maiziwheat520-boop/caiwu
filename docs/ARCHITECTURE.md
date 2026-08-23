# LedgerBridge Web 集成架构

## 目标

LedgerBridge Web 是部署在 Hermes 上的单用户财务工作台。浏览器不直接访问数据库、Hermes 消息库、OneDrive 或旧程序文件，而是通过同源的 Web API 完成候选审核、对账草稿生成和文件版本管理。

当前 `8780` 端口只运行合成数据静态预览。真实数据路径尚未启用。

## 已验证的现状

- 旧程序 `auto/app.py` 已有成熟的 `build_reconciliation()` 内存预览和 `save_reconciliation()` 人工确认保存边界。
- Hermes 上 LedgerBridge API 和 PostgreSQL 健康。
- LedgerBridge 生产部署的 `/v1/evidence/imports` 与 `/v1/evidence/import-requests` 当前返回 404。这是预期的失败关闭行为，因为生产导入开关和已审核连接器清单尚未启用。
- LedgerBridge 当前负责不可变证据、导入任务、来源记录和双重记账核心，但还没有酒店消息候选审核与月度报表 API。
- Hermes 消息附件路径是临时路径，所以消息和附件必须在消息入口即时复制并计算摘要，不能依赖事后扫描历史数据库。

## 运行边界

```text
手机 / 电脑浏览器
        |
        | 同源 HTTPS，会话 Cookie + CSRF
        v
LedgerBridge Web API（Hermes）
        |
        +--> 候选审核与报表草稿存储
        |
        +--> LedgerBridge 内部 API（不可变证据与来源记录）
        |
        +--> 工作簿适配器（旧程序纯函数 + LibreOffice 临时副本校验）
        |
        +--> OneDrive App Folder（Apps/LedgerBridge）

Hermes 主账号私聊入口
        |
        +--> 即时证据封装 --> LedgerBridge 内部导入队列
```

浏览器端不保存服务凭据。LedgerBridge 内部 API 继续只监听回环或专用容器网络，Web API 是唯一面向浏览器的后端入口。

## 数据模型增量

LedgerBridge 核心现有表不应直接承担 UI 草稿状态。后续经独立评审的迁移应新增：

- `finance_candidate`：提取字段、营业单元、归属月份、置信度、当前审核投影和来源记录引用。
- `candidate_evidence_link`：候选与原始消息或附件的多对多引用。
- `candidate_review_event`：确认、更正、忽略和重开事件，只追加不覆盖。
- `candidate_conflict`：重复消息、附件摘要或业务键冲突及其处置状态。
- `reconciliation_snapshot`：按月份生成的不可变报表输入快照。
- `workbook_draft`：工作簿输入版本、输出摘要、计算器版本和验证结果。

金额在 API 和数据库中使用最小货币单位整数，例如 `638000` 表示人民币 `6380.00` 元，避免浮点误差。

## 候选状态机

```text
INCOMPLETE -> PENDING -> CONFIRMED
                    \-> IGNORED
          \-> CONFLICTED -> PENDING
CONFIRMED -> SUPERSEDED（通过新事件更正，不覆盖旧记录）
```

- 缺归属月份时为 `INCOMPLETE`，即使系统建议当前月份，也不能进入报表。
- 命中消息 ID、附件摘要或业务键冲突时为 `CONFLICTED`，必须人工处理。
- `CONFIRMED` 只代表允许进入报表草稿，不代表正式入账。
- Web API 不提供直接创建 `POSTED` 日记账分录的路由。

## 旧程序渐进迁移

第一阶段不重写 `auto/app.py` 的计算规则：

1. 将 `build_reconciliation()` 依赖的文件读取、规则计算和工作簿写入拆成无界面模块。
2. Web API 从已确认候选生成只读输入快照。
3. 工作簿适配器在临时目录调用旧逻辑生成预览副本。
4. LibreOffice 后台重算临时副本，检查公式错误和关键单元格。
5. Web 展示差异；用户确认后才生成新的 OneDrive 文件版本。
6. 在迁移完成前，旧 Tkinter 程序仍可打开同一类文件并执行最终确认保存。

工资模块保持在第二阶段，避免把两套高风险规则同时迁移。

## 身份和网络

- 单用户 Passkey 为主认证方式，一次性恢复码只显示一次并只存哈希。
- 会话使用 `Secure`、`HttpOnly`、`SameSite=Strict` Cookie。
- 所有状态变更要求同源检查、CSRF 令牌、幂等键和候选版本号。
- 内网通过 Hermes 地址访问；外部访问沿用家庭 Tailscale 子网，不直接开放公网端口。
- 当前合成数据预览没有认证，不得接入真实数据。

## 上线门槛

真实消息开关必须同时满足：

1. 合成消息和附件的端到端测试通过。
2. 候选、冲突、审核事件和报表快照迁移通过 LedgerBridge 质量门。
3. Hermes 入口只允许主账号 Telegram、钉钉、微信私聊，家庭账号和群聊保持排除。
4. 身份认证、CSRF、速率限制、审计和附件下载授权完成。
5. 独立安全复核通过。
6. 用户单独授权启用真实新消息；历史消息仍不扫描。
