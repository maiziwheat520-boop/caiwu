# LedgerBridge Web

酒店财务工作台的独立 Web 原型。它用于验证从 Hermes 消息候选到人工审核、月度对账草稿和文件计算验证的交互。当前前端连接同源的合成 BFF API，不连接真实消息、真实财务数据库或真实 OneDrive 账户。

## 当前原型

- 概览：本月候选、已确认金额、营业单元和对账就绪度。
- 待审核：按消息来源筛选，查看原始消息与附件证据，确认或忽略候选。
- 审核操作记录：按时间倒序查看追加式确认、更正、冲突处置和忽略记录，支持筛选、搜索，并可下钻到只读候选详情、原始证据和单候选审核历史。
- 月度对账：按营业单元汇总水费、税费、布草、瓶装水和银行收款。
- 原口径对账表：将历史口径整理为可补录、可审核的业务事项窗口；不在网页中复制 Excel 网格，不复用月度草稿或公司报表。
- 文件与连接：展示 OneDrive App Folder、Hermes 消息入口和 LibreOffice 计算服务的预期边界。
- 移动端：支持审核和摘要；完整科目网格保留给平板和电脑。

所有示例数据均为合成数据。页面中的连接状态不代表真实服务已经配置。`synthetic-preview` 的状态只在内存中；`authenticated-preview` 使用本地 SQLite 保存审核事件、幂等响应、草稿、Passkey 公钥、恢复码哈希和会话哈希。

## 本地运行

```bash
npm install
npm run dev
```

默认端口为 `4173`，开发服务器监听所有接口，便于后续在 Hermes 内网环境验证。

开发服务器只提供前端资源。若要验证完整同源 API 流程，先构建前端，再运行预览服务：

```bash
npm run build
python deploy/server.py
```

默认监听 `127.0.0.1:8080`；只在受信内网预览时显式设置 `BIND_ADDRESS`。

## 本机单用户模式

`LEDGERBRIDGE_MODE=local-single-user` 让 BFF 用明文 HTTP 连接同一台电脑上的 Core 本机档案（默认 `http://127.0.0.1:8661`）。它复用 `core-backed` 的同一个 Core 客户端、同一个适配器和同一批 `/internal/v1` 读取路由，只去掉在一台电脑上无人可验证的部分：没有 mTLS 证书、没有 Passkey、没有受信代理、没有 Secure Cookie（回环上没有 TLS）。

唯一的写入是候选审核：单条确认、忽略、改分类或营业单元，以及按分类组确认。本机的决定请求不带用户断言签名（Core 本机档案对应的两条路由不验签），仍然要求本地会话 Cookie 和 CSRF 令牌。为防 DNS 重绑定，BFF 和 Core 都只接受以本机名字访问的请求（`127.0.0.1` 或 `localhost` 加各自端口），其他 Host 一律返回 `421 LOCAL_HOST_REJECTED`。

```bash
npm run build
LEDGERBRIDGE_MODE=local-single-user \
  BIND_ADDRESS=127.0.0.1 \
  CORE_ENTITY_REF=<entity uuid> \
  CORE_BUSINESS_UNIT_REF=<business unit> \
  python deploy/server.py
```

`CORE_ENTITY_REF` 和 `CORE_BUSINESS_UNIT_REF` 说明打开哪套账，不是凭据，因此仍然必填。注意两者形式不同：`CORE_ENTITY_REF` 是主体的 UUID，`CORE_BUSINESS_UNIT_REF` 是营业单元的**稳定 ref**（账本准入时的短名，例如 `book-08`），不是 UUID。填成 UUID 时每个读取都会成功并返回空，工作台看起来就像一套没有待办的账——所以启动时会先向 Core 查一次，对不上直接拒绝启动，并列出该主体实际有哪些 ref。

本模式不做任何身份验证，唯一的访问控制是监听地址，所以以下情况直接拒绝启动，而不是降级或告警：

- `BIND_ADDRESS` 不是回环地址（含留空，即监听全部接口）。
- `CORE_BASE_URL` 不是回环 `http://` origin。
- 出现只属于部署路径的设置：`CORE_CA_FILE`、`CORE_CERT_FILE`、`CORE_KEY_FILE`、`CORE_USER_ASSERTION_KEY`、`CORE_WORKLOAD_PRINCIPAL`、`CORE_POLICY_GENERATION`、`TRUSTED_PROXY_CIDRS`。设置了却被忽略，会让运维以为连接已经双向认证。
- `PAYROLL_COMMANDS_ENABLED=1`：工资命令需要经过校验的用户断言，本机 Core 不校验。

Hermes 合成数据预览的容器部署方式见 [DEPLOYMENT.md](./DEPLOYMENT.md)。

后续真实接入的运行边界见 [集成架构](./docs/ARCHITECTURE.md)，草拟接口见 [OpenAPI 合同](./contracts/openapi.yaml)。

原口径业务窗口的 scope、金额和缺口显示边界见 [原口径业务窗口](./docs/ORIGINAL_RECONCILIATION.md)。

旧 Tkinter 对账规则的渐进提取方案见 [工作簿适配边界](./docs/WORKBOOK_ADAPTER.md)。

已经确认的 Hermes、Outlook.com、LedgerBridge Core、模型提取和旧程序真实数据边界见 [真实数据接入边界](./docs/REAL_DATA_BOUNDARY.md)。该文档只冻结设计与授权闸门，不表示已经启用真实数据。

## 验证

```bash
npm run lint
npm run test
npm run build
python -m unittest discover -s server/tests
```

## 已锁定的业务边界

- 规则优先、模型补充，提取结果一律先成为待审核候选。
- 缺归属月份的候选标记为不完整，不进入报表。
- 冲突候选阻断草稿生成。
- 原始消息、单个附件和每次确认、更正、忽略均保留可追溯记录。
- 候选不能直接生成正式凭证或正式入账。
- OneDrive Personal 计划只申请 `Files.ReadWrite.AppFolder`，访问 `Apps/LedgerBridge`。
- LibreOffice 只处理临时副本，输出只能标记为“LibreOffice 已验证”，不能声称“Excel 已验证”。

## 视觉基线

信任优先、低视觉变化、克制动效、中高信息密度。唯一主强调色为蓝色，红、橙、绿只用于风险和状态语义；圆角和阴影保持统一且克制。

## 下一阶段接口边界

当前 BFF 已实现合成数据合同、单用户多设备 Passkey、一次性恢复码、持久化 SQLite 投影、CSRF、幂等键、乐观并发、证据下载和草稿状态轮询。已登录用户可在账户菜单通过一次现有 Passkey 二次确认，为当前设备追加独立 Passkey；已有设备、恢复码和会话不会因此失效，最多登记 10 个。恢复码登录仍是受限会话，必须登记新的 Passkey 并轮换恢复码后才能读取财务页面。下一阶段才连接 LedgerBridge 的候选记录与只读汇总接口。Hermes 消息附件必须在消息入口即时摄取，避免依赖临时附件路径做历史轮询；真实消息启用前仍须完成独立安全复核。
