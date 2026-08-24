# LedgerBridge 真实数据接入边界

状态：设计已确认，尚未授权实现或启用真实数据
日期：2026-08-24

## 结论

真实数据模式下，LedgerBridge Core 是唯一业务事实源。LedgerBridge-Web 只负责 Passkey 会话、页面编排和 Core API 适配，不直连 PostgreSQL，也不在自己的 SQLite 中保存真实候选、审核事件、冲突、快照或工作簿草稿。现有认证预览中的合成业务投影只属于 synthetic 模式，两种模式必须互斥。

第一条实施路径是：先完成 Core 只读内部 API 和合成合同，再接 Hermes 的新私聊；Outlook.com、工作簿发布和任何审核写入分别位于后续闸门。当前服务继续只承载合成数据。

## 已确认决定

| 主题 | 决定 |
| --- | --- |
| 业务事实源 | 候选、证据、审核、冲突、快照和草稿全部归 LedgerBridge Core |
| 第一实施顺序 | Core 只读内部 API，然后 Hermes |
| Hermes 范围 | 当前主账号入口的所有新私聊；排除群聊、家庭 profile、助手、工具和系统消息 |
| 非财务内容 | 只允许瞬时处理，判别后立即删除正文和附件 |
| 财务内容 | 原文和附件作为不可变证据保存；模型只接收脱敏文本和结构化字段 |
| 不确定内容 | 建立歧义候选，等待人工审核，不自动采用 |
| Hermes 回执 | 只对命中或歧义消息回执；无关消息不回执 |
| 非财务去重 | 正文和附件立即逻辑删除，只保留 30 天不可逆 HMAC 去重墓碑 |
| 失败暂存 | 加密 retry/dead-letter 最长 72 小时；到期必须处置并暂停有缺口的来源 |
| Outlook | 个人 Outlook.com；监控收件箱；首次回扫最近 30 天，之后增量同步 |
| Outlook 原件 | 保存 RFC822/MIME 原件，并把允许的附件拆成独立不可变 artifact |
| 服务身份 | LedgerBridge-Web 到 Core 使用回环 mTLS 服务身份 |
| 模型执行 | 本地预筛后，由专用、无工具的提取 worker 调用模型 |
| 证据保留 | 遵循现有财务归档制度；制度年限未配置前禁止自动删除 |
| 首批报表 | 只复刻旧程序已有的月度对账规则；税费新规则和工资不进入首批 |

## 目标拓扑

```text
Telegram / 钉钉 / 微信新私聊              Outlook.com 收件箱
              |                                  |
              | 现有适配器鉴权后事件              | Graph delegated Mail.Read
              v                                  v
        Hermes durable outbox              Mail delta collector
              |                                  |
              +------------+---------------------+
                           |
                           v
                 本地预筛 / 不可信文件解析
                 - 无网络解析容器
                 - 非财务内容立即删除
                           |
                           | 疑似财务的脱敏片段
                           v
                 专用无工具模型提取 worker
                           |
                           | 结构化、强 schema 输出
                           v
                    LedgerBridge Core
                 - RawArtifact / SourceRecord
                 - Candidate / Review / Conflict
                 - Snapshot / WorkbookDraft
                           ^
                           | 回环 mTLS 内部 API
                           |
浏览器 -- Passkey + Tailnet HTTPS --> LedgerBridge-Web BFF
                           |
                           v
                 OneDrive Apps/LedgerBridge
                    新版本 + If-Match
```

浏览器永远不能直接访问 Core、PostgreSQL、ArtifactStore、Hermes 消息库或 Graph。Core 不接收浏览器 Cookie，不开放 CORS，不向 BFF 返回数据库键、artifact `storage_key`、完整 `raw_fields` 或任何服务凭据。

## 数据所有权

| 数据 | 权威位置 | Web 可见范围 |
| --- | --- | --- |
| Passkey、恢复码哈希、Web 会话 | Web SQLite | 认证状态本身 |
| 原始消息、邮件和附件 | Core 加密 ArtifactStore（尚待实现） | 经单独授权的流式证据下载 |
| 导入作业和解析来源记录 | Core PostgreSQL | 白名单状态与不透明引用 |
| 财务候选和当前投影 | Core PostgreSQL | 分页、月份和状态筛选后的投影 |
| 审核事件与冲突 | Core PostgreSQL，追加式 | 有限审阅字段，不暴露内部 payload |
| 对账快照和工作簿草稿 | Core PostgreSQL + ArtifactStore | 摘要、差异、状态和下载授权 |
| Graph deltaLink、平台游标 | 各 transport/collector 的加密受限状态存储 | 仅连接健康状态 |
| OAuth token、mTLS 私钥、模型凭据 | 工作区外的既定凭据存储 | 永不返回浏览器或写入日志 |

Web 的现有持久化合成候选不能迁移为真实事实。启动时必须验证 `synthetic` 与 `core-backed` 模式互斥；真实模式拒绝包含业务 seed 的 Web SQLite，并由 Core 投影完全替换。

当前 Core ArtifactStore 只提供内容寻址、权限位和摘要验证，不提供在线加密；因此表中的“加密”是 H2/E2 前必须实现和验收的目标，不是现状声明。真实来源启用前至少完成：

- ArtifactStore、staging、outbox、retry、临时文件和 token 映射使用应用级 AEAD；密钥与数据分离，支持轮换和恢复演练；
- PostgreSQL、WAL、临时空间和在线卷由主机/卷级加密覆盖；备份继续独立加密；
- `SourceRecord.raw_fields` 只保存脱敏白名单结构，正文、原始身份、邮件头和 token 映射只进入加密 artifact；
- swap、崩溃转储、日志、模型缓存和普通备份不得包含明文 outbox/retry 内容；
- 任何加密自检、密钥加载或恢复验证失败都使真实 ingest fail closed。

## Core 内部 API

### 第一阶段：只读

| 接口 | 能力 | 合同 |
| --- | --- | --- |
| `GET /internal/v1/capabilities` | `system:read` | 契约版本、数据模式和已启用模块，不返回部署秘密 |
| `GET /internal/v1/candidates` | `candidate:read` | 游标分页；只允许月份、状态和营业单元筛选 |
| `GET /internal/v1/candidates/{id}` | `candidate:read` | 白名单 normalized 字段、blockers、revision、审核摘要和不透明 evidence refs |
| `GET /internal/v1/evidence/{id}/content` | `evidence:read` | 实体范围检查、摘要复核、流式响应、`Cache-Control: no-store` |
| `GET /internal/v1/reconciliations/{month}` | `reconciliation:read` | 提案、Suspense、阻断项、快照 revision 和仅 POSTED 的账簿汇总 |
| `GET /internal/v1/ledger-summary` | `ledger:read` | 明确 entity/期间，金额一律为整数分币，只统计 POSTED 分录 |

第一阶段不提供通用 SQL、通用 PATCH、Connector 选择、自动 POST、自动解决 Suspense 或删除去重记录。mTLS 只证明 workload 身份，不自动产生授权；Core 使用 deny-by-default 策略把证书 SAN 映射为固定 capability、entity 和营业单元范围，并在查询、请求体读取和证据流式打开之前完成检查。模型或请求体给出的营业单元只能是候选值，不能扩大授权范围。

真实 Hermes/Outlook 启用前还需单独实现受认证 ingest：

| 接口 | workload | 最小能力与限制 |
| --- | --- | --- |
| `POST /internal/v1/ingest/events` | `svc:hermes-ingress` | `evidence:write:hermes`；只接受注册的平台、主 profile、DM 和 entity 范围 |
| `POST /internal/v1/ingest/events` | `svc:outlook-collector` | `evidence:write:outlook`；只接受绑定 mailbox/Inbox 和 entity 范围 |
| `GET /internal/v1/ingest/receipts/{event_id}` | 对应 workload | 只查询自身命名空间的幂等 acceptance receipt |

两个 workload 使用独立 mTLS 证书、策略代次、撤销和 kill switch，不复用 BFF、Hermes API Server 或彼此的身份。`ingest_channel`、`source_system`、entity 和营业单元范围由 Core 注册表绑定，模型、manifest 和请求体都不能自行决定。Core 以 `connector_instance_id + source namespace + case-sensitive native id` 派生/验证 `event_id`，并在自己的事务中提交 inbox 幂等、artifact 引用、候选/冲突和审计 receipt。同一 `event_id + artifact digest` 重放返回原 receipt；同一 event ID 出现不同 digest 视为篡改或来源冲突，保全两份输入并进入 `CONFLICTED`，不得覆盖、静默接受或当作普通新事件。

### 后续阶段：命令式写入

审核写入只有在候选投影、revision、追加式审计服务和数据库权限全部就绪后才能开放：

- `POST /internal/v1/candidates/{id}/decisions`
- `POST /internal/v1/reconciliations/{id}/decisions`
- `POST /internal/v1/suspense-items/{id}/resolution`
- `POST /internal/v1/workbook-drafts`
- `POST /internal/v1/workbook-drafts/{id}/publish`

每个命令都必须具有 `Idempotency-Key`、`expected_revision` 或 `If-Match`、有限枚举动作、字段更正白名单和有界理由。Core 在一个事务内执行锁定、追加审计和条件状态迁移；重放返回原结果，不产生第二个事件。

请求体不能自报 actor。读阶段只使用 mTLS 服务主体；未来写阶段还要加入不超过 60 秒的 BFF 用户断言，规范绑定 issuer/audience、HTTP method、canonical path、body digest、resource ID、expected revision、mTLS 服务主体、策略代次、认证代次和一次性 `jti`。Core 同时记录人类主体和 mTLS 服务主体，并拒绝客户端伪造的身份头或把断言重放到另一命令。

### 候选状态合同

R0 必须冻结版本化状态图，不能把 Core `ReviewItem` 状态直接透传为 Web 状态：

```text
新提取 -> INCOMPLETE（缺必填）
       -> CONFLICTED（身份/摘要/业务键冲突）
       -> PENDING（可审核）

INCOMPLETE --补全--> PENDING
CONFLICTED --解决冲突--> PENDING
PENDING --确认--> CONFIRMED
INCOMPLETE/PENDING/CONFLICTED --忽略--> IGNORED
CONFIRMED --有更正--> SUPERSEDED + 新派生候选
```

`IGNORED` 和 `SUPERSEDED` 在 v1 中为终态；`CONFIRMED` 对普通审核动作封存，唯一例外是具有独立 `candidate:supersede` 能力的追加式 `SUPERSEDE`：原候选转为 `SUPERSEDED`，同时创建带来源链接的新派生候选，绝不覆写原值。其他“重开”也通过新候选完成。只有 `candidate:decide` 人类主体可以确认、补全、解决冲突或忽略；worker 只能创建候选。每次动作都要求 expected revision，并明确映射到 Core Review/Reconciliation/Suspense 状态和追加式审计事件。

## Core 数据增量

现有 `RawArtifact`、`ImportJob`、`SourceRecord`、`ReviewItem`、`ReconciliationGroup` 和 `SuspenseItem` 不能直接冒充 Web 候选。Core 需要拥有明确的防腐/翻译层：

- `finance_candidate`：来源记录、营业单元、归属月、提取字段、置信度、状态和 revision。
- `candidate_evidence_link`：候选到消息 envelope、邮件 envelope 和附件 artifact 的多对多绑定。
- `candidate_review_event`：确认、更正、忽略、解决冲突和重开，只追加不覆盖。
- `candidate_conflict`：消息 ID、附件摘要、外部业务键和跨格式重复冲突。
- `reconciliation_snapshot`：冻结已确认候选 revision 的不可变月度输入。
- `workbook_draft`：工作簿输入版本、输出摘要、规则/worker 版本和验证状态。

所有 API/数据库金额使用 CNY 整数分币。外部金额必须从原始字符串构造 `Decimal`，整数、一位或两位小数可精确转为分币；超过两位小数、NaN/Infinity、溢出或语义歧义进入审核。旧程序计算结果使用经金样本确认的版本化 Decimal 舍入规则；在该规则冻结前不允许自动写入，禁止从 binary float 直接构造 Decimal。

## Hermes 新私聊入口

### 接入点

不得创建第二个 Telegram、钉钉或微信消费者，也不得读取 Hermes `state.db`。应在现有平台适配器完成 allowlist 鉴权后、交给通用 Agent 改写前，新增窄事件接缝 `authorized_message_ingress`。

接缝再次强制以下条件，不能只依赖现有平台设置：

- `platform` 仅为 Telegram、钉钉或微信；
- `profile` 必须是当前主 profile；
- `chat_type` 必须精确为 `dm`；
- sender 必须映射到当前已确认主账号 allowlist；
- 仅处理入站人类消息，排除助手、工具、系统、群聊、线程共享内容和历史会话；
- 启用时从新 checkpoint 开始，不回扫 Hermes 历史。

### 耐久交接

每个原生 update 在平台解析完成后、文本 debounce 或媒体组聚合前，先进入 owner-only、加密的 durable outbox。跨 Hermes/Outlook 与 Core 不使用分布式事务，而使用 transactional outbox/inbox 和可重放 acceptance receipt。

传输确认与业务处理是两个正交状态机，不能让聊天平台 ACK 等待模型或 Core：

```text
transport:
  RECEIVED -> LOCAL_DURABLE -> TRANSPORT_ACKED / CURSOR_COMMITTED

business:
  PREFILTERING -> DISCARDED -> TOMBSTONE
               \-> SUSPECTED -> EXTRACTING -> MATCHED/AMBIGUOUS
                                             -> CORE_DELIVERING
                                             -> CORE_ACCEPTED
                                             -> RECEIPT_PENDING
                                             -> RECEIPT_SENT/EXPIRED
  未完成财务判定 -> RETRY -> 原阶段
  未判定内容超过次数或时限 -> DEAD_LETTER -> 72 小时内人工处置，否则删除内容、记录 coverage gap 并暂停来源
  MATCHED/AMBIGUOUS/Core 投递失败 -> FINANCIAL_QUARANTINE（按财务归档制度保全，不适用 72 小时删除）
```

流程是：collector 先 fsync 本地事件与附件并完成 transport ACK/cursor CAS；Core 以 `event_id + artifact digest` 幂等接收并提交 receipt；collector 保存 receipt 后再清理本地原文。Core 已提交但响应丢失时，重复投递返回同一 receipt。transport ACK、Core acceptance 和用户回执是独立状态，任何崩溃窗口都靠重放收敛；`DISCARDED` 路径同样必须提交 transport 状态。

`DISCARDED` 后正文和附件立即逻辑删除或销毁其 job key，只保留 30 天不可逆 HMAC tombstone；tombstone 不含联系人、正文、附件、原生 ID或可逆标识。临时附件路径必须在入口即时复制、限额、识别 magic/MIME 并计算 SHA-256，不能事后依赖 Hermes 会话库。普通 unlink 不宣称物理擦除，备份/快照和密钥策略必须保证已删除明文不可恢复。

每条消息、附件、批次和全局 outbox 都有配额与 backpressure。尚未完成财务判定的 RETRY 使用有抖动退避、最多 8 次且不超过 72 小时；到期未处置就删除暂存内容、保留脱敏 coverage gap、暂停对应来源并告警，不能无限期留存私人消息。一旦判定为 `MATCHED` 或 `AMBIGUOUS`，内容即属于财务证据：Core 故障时转入长期加密 quarantine，按财务归档制度保全；达到配额则暂停来源，不得因 72 小时 TTL 自动删除。

Telegram 首次启用可以显式建立“从现在开始”的 cutoff，但之后普通重启不得再 `drop_pending_updates`；使用 `update_id` 作为传输游标，证据定位还要绑定 chat scope 与原生 message ID。钉钉 callback 只有在 outbox fsync 后才 ACK，并用 `msg_id` 幂等。微信一个 `getupdates` 批次中的全部事件先原子落盘，再保存新的 `sync_buf`，不能逐条异步处理后提前推进。

原生 provenance 不因后续聚合而丢失。受限聚合层用 `native_event_id`、`media_group_id` 或有界 batch key 关联快速连续文本/相册；默认窗口 5 秒、最多 20 个原生事件且总字节不超过既定批次上限。超时、缺成员、caption 分离或超限进入 `AMBIGUOUS`，不能把无文字图片当作非财务删除；Core 候选通过多对多 evidence link 指向每个原生消息和附件。

回执有独立幂等键、重试和短期加密 reply handle；钉钉 webhook 等 reply handle 过期时进入独立 `RECEIPT_EXPIRED` 终态，不重复发送“已进入审核”，也绝不回滚 `CORE_ACCEPTED` 证据。Web 待审核列表和连接健康页必须显示“已入库但平台回执失败”，避免对用户静默。所有平台都有独立启用开关和 kill switch；“财务：”或 `/finance` 是强制纳入通道，但没有标记的主账号私聊仍按用户决定进入自动判别。预筛采用“宁可 AMBIGUOUS、不可自信误删”的门槛，并在 H1 用去标识金样本同时验收召回率和误报率。

## Outlook.com 入口

Outlook 使用独立 LedgerBridge Connector，不复用 Hermes Email adapter。后者会跳过既有邮件、只读 `UNSEEN`、忽略 automated/noreply 发件人并进入自动回复流程，不适合财务证据采集。

### 授权

- 账户类型固定为个人 Microsoft account，authority 使用 `consumers`。
- 邮件和 OneDrive 使用两个独立 OAuth app registration、redirect URI 和 token cache，保证连接、撤销和换账号互不连带。
- 使用 delegated authorization-code flow + PKCE；浏览器只完成授权，Graph token 不进入前端存储。一次性 `state`、OIDC nonce 和 PKCE verifier 保存在服务端，并绑定当前 Passkey session、精确 HTTPS redirect URI 和短时过期。
- callback 校验 issuer、audience、nonce、state、PKCE 和重放；把已验证 ID token 的稳定个人账户 subject 绑定为唯一允许账号。连接、断开、换账号和扩大 scope 都要求近期 Passkey re-auth、CSRF、幂等和追加式审计。
- 邮件只请求 `Mail.Read` 和维持授权所需的标准 OIDC/offline scope，不请求 `Mail.ReadWrite`、`Mail.Send` 或 application-wide mailbox 权限。
- OneDrive 发布使用独立能力 `Files.ReadWrite.AppFolder`，只访问 `Apps/LedgerBridge`；邮件和文件开关、审计与撤销必须彼此独立。
- 静态 app registration 材料放在工作区外的 Hermes systemd credentials/受限 secret store。两套动态 refresh-token cache 则使用各自的可变加密 vault，原子更新并以 account binding、token generation 和 CAS 防止旧实例覆盖新 token；覆盖 token refresh、进程崩溃、上一代恢复、撤销和换账号。静态材料与动态 token 不混存，也不使用同步盘、仓库环境文件或浏览器存储。

Microsoft 官方权限参考确认 `Mail.Read` 和 `Files.ReadWrite.AppFolder` 的 delegated 权限可供个人 Microsoft 账户同意；App Folder delegated scope 当前标注为 preview，因此真实文件发布前必须做兼容性和撤销测试。[Graph permissions](https://learn.microsoft.com/en-us/graph/permissions-reference) [OneDrive App Folder](https://learn.microsoft.com/en-us/graph/onedrive-sharepoint-appfolder)

### 30 天首轮回扫与增量

E2a 激活时固定时间 `T`，用两条流避免 30 天历史与未来增量之间出现竞态：

1. 先对 Inbox 建立 `changeType=created` 且 `$filter=receivedDateTime ge T` 的 delta baseline，完成到可持续使用的 deltaLink；
2. 再用普通 list-messages API 对 `[T-30 天, T)` 做有上下界的历史分页回扫，并以 `$orderby=receivedDateTime desc` 和 ImmutableId 处理；
3. 历史回扫结束后立即从 baseline deltaLink catch up；两条流按 immutable ID 幂等合并。

Graph list-messages 支持 OData filter/orderby 和完整 nextLink 分页。[list messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0) 所有请求只选择需要的字段，限制页数、消息数、附件数和总字节。若 delta 的时间过滤触达 5,000 封或历史列表触达任一本地上限，都标为“完整性未知”，不得建立成功覆盖报告；人工缩短/分段只应用于有上下界的历史 list 流，不能伪造 delta 上界。

每次 delta、message、MIME 和 attachment 请求都带 `Prefer: IdType="ImmutableId"`；ID 在同一 mailbox 内按大小写精确比较。Collector 加密保存完整 `@odata.nextLink` / `@odata.deltaLink`，禁止写日志，并把它绑定 connection、mailbox、Inbox、query contract、policy generation 和单活 lease。lease 带单调 fencing epoch；每轮与每次 checkpoint CAS 都验证当前 epoch，旧实例恢复后不能提交 deltaLink，续期、过期、时钟偏差和强制接管必须故障注入测试。

跟随 URL 时要求 HTTPS、无 userinfo、`graph.microsoft.com`、规范默认端口、`/v1.0/` 下预期资源路径且不得跨 connection 置换。HTTP 客户端禁用自动重定向；若未来允许跳转，每一跳都重复完整校验并限制跳数，规则同样覆盖 MIME、message 和 attachment 请求。只有整轮分页和本地 outbox 提交成功后才用 fencing CAS 推进 deltaLink。Graph 官方说明 message delta 是按文件夹跟踪，支持 `receivedDateTime ge/gt` 过滤，并要求后续复用完整 deltaLink；不可变 ID 头也适用于 delta 响应。[message delta](https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0) [immutable Outlook IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)

Collector 持久化 `last_successful_sync_at`、`oldest_uncovered_received_at`、round ID、上一个 deltaLink 摘要及 mailbox/folder/query/policy generation。delta 中的删除、移动、已读变化、`@removed` 和重复 change 只更新连接覆盖记录，不重抓或改写已归档证据；新建以 `connection_id + mailbox + case-sensitive immutable id` 的首次出现判定。

delta token 返回 410/`syncStateNotFound` 时立即暂停。重新同步下界必须覆盖上次成功点到当前的全部未覆盖区间；若超过已授权 30 天或 5,000 封边界，明确生成 coverage gap 并等人工授权，不能自动缩成最近 30 天或全邮箱回扫。

邮件使用两阶段预筛：先检查 envelope、正文和附件名称；只有在能够安全排除时才删除。正文空泛、只有“见附件”或仍疑似财务时，把带硬上限的 MIME/附件流式写入短期加密 staging，在无网络解析器检查后再决定 `DISCARDED`、`MATCHED` 或 `AMBIGUOUS`。MIME 和附件边读边执行单项/总量限额，禁止先完整缓冲后检查。

这次 `GET /me/messages/{id}/$value` 得到并已完成摘要校验的 staging 快照就是候选分类和永久证据的共同输入。命中或歧义时原子提升同一份密文 staging artifact，禁止为“保存原件”再次下载可能已变化的邮件；再从该 MIME 快照拆出允许的独立附件 artifact。只有 MIME 未包含且必须调用 Graph attachment endpoint 时，才允许补抓，并同时绑定 ImmutableId、message `changeKey`/ETag 和响应摘要；任何漂移都进入 `AMBIGUOUS`/coverage gap。Microsoft Graph v1.0 明确支持个人账户在 `Mail.Read` 下读取 MIME。[get message MIME](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0)

`fileAttachment` 和允许的 inline CID 图片在限额内拆分；`itemAttachment` 保留于 MIME，并仅在独立受限解析器支持时拆分；`referenceAttachment`、加密附件、宏、外部链接或未知类型进入 `NEEDS_REVIEW`，不能静默跳过后声称附件完整。delta 枚举后到 MIME/附件抓取前若发生 404/410，记录耐久 coverage gap 并继续受控同步。自动化/noreply 发件人不能默认排除。

## 本地预筛与模型提取

处理分成三个权限域：

1. **本地预筛器**：规则、发件域类别、文件类型、金额/日期模式和旧程序已有业务词典；不联网。
2. **不可信文件解析器**：PDF、XLS、XLSX 和图片分别在无网络、非特权、只读根、独立临时目录的受限 worker 中处理；限制文件、解压、页数、像素、CPU、内存、pids 和墙钟。
3. **模型提取 worker**：没有工具、文件系统、数据库或通用 Hermes Agent 权限；只获得脱敏文本片段、job-scoped 占位符和结构化字段，并通过受限模型出口调用固定 allowlist 模型。

模型输入中的姓名、邮箱、电话、账号和卡号默认使用 job-scoped token 替换；跨 job 稳定 token 默认禁止，只有单独业务理由和授权后才可启用。金额、日期、营业单元候选和文档类型按提取需要保留。token 到原值的映射只在 Core 本地后处理内存中存在，不写模型日志。

模型出口 gate 必须固定 provider、区域、endpoint、模型版本、prompt/schema/脱敏器版本，确认 API 数据不用于训练、provider 日志关闭且保留期符合批准策略；记录请求摘要和来源引用，不记录内容。provider 故障时禁止自动切换模型或区域。模型输出必须通过版本化 JSON Schema、整数金额、枚举、长度和来源引用复核；消息正文中的任何“指令”都只是数据，不能触发工具或改变系统提示。

模型永远只能输出候选。`AMBIGUOUS`、`PARSE_FAILED`、`DEPENDENCY_UNAVAILABLE`、字段缺失、月份缺失和冲突都进入待审核或阻断状态，绝不能回退旧月份值、模板值、配置值或 0。

## 旧程序与工作簿

首批只迁移 `auto/app.py` 的月度对账已有行为；工资、代发表、税费新规则、Excel COM 和 GUI 不进入本期。

实施前必须先把不含真实文件和 sidecar 的旧程序代码、依赖锁和合成特征测试纳入版本化基线。后台 worker 不能直接 import Tkinter 巨石，而应按 `WORKBOOK_ADAPTER.md` 抽取 `finance_core`。旧 Tkinter 入口随后也必须调用同一核心，禁止复制 Web 专用规则形成两套实现。

预览不得创建 `月度数据/<月份>.json` 或任何隐藏 sidecar。worker 只接收有序、不可变的 `MaterialInput` 清单，不能扫描整个目录；每项绑定 evidence ID、摘要、magic/MIME、业务键、候选 revision 和来源角色。重复摘要、相同业务键、截图与表格跨格式重复必须形成冲突，且目录顺序不能改变结果。

固定 Linux image digest、Python lock、pypdf/openpyxl/xlrd/Pillow/onnxruntime、OCR 模型 checksum、字体、locale、时区和 LibreOffice build；启动时运行去标识金样本，并把全部版本写入 provenance：

```text
已确认候选快照
  -> 下载 OneDrive App Folder 工作簿临时副本
  -> 绑定 item id、eTag/cTag、SHA-256
  -> 调用无 UI 月度对账纯核心
  -> LibreOffice 在独立无网络容器重算临时副本
  -> OOXML 结构 allowlist 与关键值差异检查
  -> 发布不可变 draft 和候选到单元格 provenance
  -> 人工确认
  -> 发布前再次核对 item id、eTag/cTag 和下载 SHA-256
  -> 使用原 eTag 的 If-Match 上传新版本
```

任何版本漂移都使 draft 过期，必须基于新工作簿重建；不能自动 rebase、覆盖或把本地同步目录当原子文件系统。公式结果必须区分 `VALUE`、`UNKNOWN`、`CYCLE`、`MISSING_SHEET` 和 `UNSUPPORTED`，未知结果不得伪装成 0；旧预览值与 LibreOffice 值分别保存。LibreOffice 结果只能标为“LibreOffice 已验证”，不能声称“Excel 已验证”。

W1 金样本显式覆盖 `YY.M/YY.MM` 并存冲突、1 月/跨年、目录顺序、跨格式重复、模板公式和旧人工覆盖。现有普通 OneDrive 文件必须经过一次人工选择/上传或复制，才能成为 App Folder 中首个 canonical item；记录源摘要、item id、eTag/cTag，保留原文件不变。

## 失败与隐私语义

- 非财务内容只有在成功完成判别后才能删除；判别失败进入最长 72 小时的加密 retry，不得当作无关内容丢弃。
- 财务或歧义证据使用 outbox/inbox receipt 协议协调发布和 checkpoint，不宣称跨服务原子事务；孤立 blob 可回收，但已引用证据禁止覆盖或删除。
- 原始身份和正文只存在于加密证据层；候选投影、日志、模型输入和普通列表使用内部别名或脱敏值。
- 证据下载单独审计，响应 `no-store`、`nosniff`、安全文件名和固定 `Content-Disposition: attachment`；活动类型统一为 `application/octet-stream`，Core 元数据不能直接控制响应头，不允许 inline HTML/SVG/脚本或长期公开 URL。
- 归档年限未配置前没有自动删除任务。以后启用处置也必须导出清单、经过人工批准并保留处置审计，不通过迁移或 downgrade 偷删证据。
- Connector、worker、BFF 或模型不可创建 POSTED 日记账；只有现有账簿审计事务边界内的后续人工流程可以过账。

## 实施切片与授权闸门

1. **R0 合成合同**：Core 候选 projection、状态图、只读 OpenAPI、授权矩阵和合成 fixtures；无生产部署。
2. **S1 在线加密基础**：ArtifactStore/staging/outbox/retry AEAD、DB/WAL/volume 加密、密钥托管/轮换/恢复和明文泄漏测试。
3. **R1 Core 只读 API**：完成 candidate 防腐层、实体范围、mTLS principal、最小数据库 grants、备份/恢复演练和独立安全复核；生产数据仍为空。
4. **R2 Web 只读适配**：认证 BFF 切换到 Core 空/合成投影；证明浏览器无法访问 Core、数据库或 artifact 路径。
5. **I1 Core 受认证 ingest**：Hermes/Outlook 独立 workload、`evidence:write`、注册来源范围、inbox receipt、生产 feature flag、候选/冲突写入和 worker dispatch；仍只用合成数据。
6. **D1 审核闭环**：Core 命令 API、BFF 用户断言、Web 确认/更正/忽略/冲突处置和追加式审计；合成 E2E 通过后才允许真实 capture。
7. **H1 Hermes 合成 ingress**：逐平台验证 DM 二次限制、outbox/inbox、幂等、回执、召回率和丢弃语义。
8. **H2a/H2b/H2c Hermes 真实新私聊**：Telegram、钉钉、微信分别授权、分别从新 checkpoint 启用并可独立回滚；不回扫历史。
9. **E1 Outlook 合成 transport**：Graph 响应 fixtures、delta token、ImmutableId、30 天/5,000 封上限、MIME、附件、OAuth account binding 和撤销测试。
10. **E2a Outlook 真实回扫**：单独授权 OAuth 和最近 30 天回扫，完成覆盖报告后停止。
11. **E2b Outlook 持续增量**：再次授权后启用 delta；token 失效和 coverage gap 均 fail closed。
12. **W1 旧规则纯函数**：只用去标识金样本固化旧月度对账行为，不接真实工作簿。
13. **W2a OneDrive 只读 draft**：独立 Files App Folder OAuth、人工引入 canonical item、真实工作簿只读下载和 draft 生成。由于 `Files.ReadWrite.AppFolder` token 本身可写，token 只由 broker 持有；W2a 对 worker 只开放 GET/下载的 method+path allowlist，publisher 不部署、不得取得 token 或写网络能力。
14. **W2b OneDrive 发布**：单独授权后才部署 publisher，并以独立 workload、method+path allowlist 和 feature flag 允许人工确认的 If-Match 新版本上传；工资与税费继续关闭。

依赖关系为 `R0 -> S1 -> R1 -> R2 -> I1 -> D1 -> H1 -> 各 H2`，以及 `I1 + D1 -> E1 -> E2a -> E2b`；`R1 + D1 + W1 -> W2a -> W2b`。任何前置 gate 未完成都 fail closed。

每个平台、每个 OAuth client、Connector/collector 注册、Core 生产迁移、真实回扫、持续采集、OneDrive 读取和 OneDrive 发布都是独立授权，不能由前一步自动推定。Outlook 是有网络的 transport/collector；附件解析才进入 D-011 要求的签名、无网络 runner，两者不得合并成一个有网络 Connector。

## 上线前验收

- Phase 5 与 candidate 增量迁移经过 PR/CI、独立审计、加密备份和隔离恢复演练。
- BFF、Hermes ingress、Outlook collector 到 Core 的 mTLS 分别验证证书链、SAN 到 capability/entity 映射、轮换、过期、撤销、直连绕过和策略代次。
- Connector manifest 有签名验证和工作区外密钥托管；真实 parser 位于可终止的进程/容器隔离中。
- 合成 E2E 覆盖 Telegram 首启/普通重启/重连，钉钉 ACK 前后重放，微信多消息批次与 `sync_buf` 崩溃窗口，Core receipt/回执各 crash window，30 天 HMAC tombstone 和 72 小时 TTL。
- Graph 覆盖 5,000 封边界、无序页、重复 change、`@removed`、移动、410/token 失效超过 30 天、MIME/附件抓取 404、跨 mailbox deltaLink 置换和单活 lease。
- OAuth 覆盖 state/nonce/PKCE/callback 重放、错误个人账号、近期 Passkey re-auth、邮件与文件独立撤销；模型覆盖区域/版本锁定、无训练/日志保留证明、无 failover 和 schema/prompt injection 失败。
- 候选更正、冲突、快照、draft 和发布均覆盖幂等、并发 revision、审计绑定和失败恢复。
- 旧程序 15 项特征测试、OOXML feature inventory、LibreOffice 差异和 OneDrive If-Match 全部通过。
- 证据归档制度的具体保留年限、模型 provider/区域、营业单元词典和首批来源映射在真实启用前得到书面确认。

## 明确不做

- 不扫描 Hermes 历史会话、群聊、家庭 profile 或联系人列表。
- 不让 LedgerBridge 读取 Hermes `state.db`，不共享 Hermes 广权限 API key。
- 不启动第二个平台消费者，不把临时附件路径当证据。
- 不把 Graph/OAuth、mTLS、模型或 OneDrive 凭据写入仓库、共享记录或浏览器。
- 不让 Web 直连 PostgreSQL，不在 Web SQLite 建第二套业务事实。
- 不自动 POST、自动删除去重记录、自动解决 Suspense 或覆盖 OneDrive 文件。
- 不在首批迁移工资、代发表或旧程序没有的税费规则。
- 不把有网络的 Outlook transport 与无网络附件解析 runner 合并。
