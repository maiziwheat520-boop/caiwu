# Phase 4 mailbox and Connector framework

日期：2026-08-24
结论：框架完成；真实邮箱、OAuth、签名 manifest 和生产启用仍关闭

## 目标

为后续真实证据连接器建立一个可审计的边界：邮箱 provider 只负责读取
经过授权的 Microsoft Graph 响应，Connector registry 只负责从部署侧显式
注入的 factory 构建并校验 Connector。两者都不自行发现模块、读取环境令牌、
写入 ArtifactStore 或选择生产 manifest。

## 已实现

- `src/ledgerbridge/mail_collector.py`
  - 注入式 `AccessTokenProvider` 和 `GraphTransport`；没有令牌字段、缓存或日志输出。
  - mailbox、folder、页数、消息数、附件数、文件名、媒体类型和附件字节上限。
  - 只接受 HTTPS `graph.microsoft.com/v1.0` 分页链接，拒绝跨主机跳转。
  - Graph 错误、认证异常、响应形状和 Base64/大小不一致均映射为稳定错误码。
  - `MailCollector.collect()` 流式产生 `CollectedAttachment`，避免把一批附件全部留在内存。
  - 默认入口仍明确返回 `MAIL_PROVIDER_DISABLED`；没有真实 OAuth 或网络客户端。
- `src/ledgerbridge/connector_registry.py`
  - 显式 factory tuple，空注册表是默认状态；不做动态 import、entry-point discovery 或 manifest 读取。
  - factory ID 去重，Connector identity 去重，二次执行模式/元数据校验。
  - `production=True` 时沿用 SDK 规则拒绝进程内 Connector，只接受 runner 模式。
- `src/ledgerbridge/config.py`
  - 增加邮箱 provider、mailbox、页大小、消息上限和超时配置。
  - provider 默认 `disabled`；生产环境即使配置 Graph 也 fail-closed，直到认证与签名 manifest gate 单独批准。

## 未包含

本阶段没有 OAuth client、secret-store 读取、refresh token、真实 Graph 请求、
EML/PDF/CSV parser、Connector manifest 签名/密钥保管、ArtifactStore 发布、
数据库写入或生产 Compose 开关。后续实现必须保留这些独立审批和审计口。

## 验证

- 邮箱 provider、registry、Settings 回归测试覆盖禁用状态、令牌异常脱敏、URL
  编码、跨主机分页、危险文件名、Base64/size 校验、factory/identity 去重和
  生产 runner-only 规则。
- Windows 完整回归：`260 passed / 147 skipped`；ruff、mypy、Bandit 和敏感路径
  检查通过。Hosted CI push `32680886553` 与 PR `32680884286` 的 `quality`、
  `secrets`、`compose` 全部成功。Windows 本地 pip-audit 受系统编码/外部漏洞源
  网络限制，未将该环境异常宣称为通过；Hosted CI 仍是依赖审计门禁。
