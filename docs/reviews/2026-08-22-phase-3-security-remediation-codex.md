# LedgerBridge Phase 3 Slice A security remediation

日期：2026-08-22
实施分支：`ai/chatgpt/phase-3-platform-controls`
基线：`b1792701fa20a55de6233206fbe29ce6ee427e28`
实现提交：`54cbd8d49f64ba4926db128ec23751780768b958`
修复提交：`b72b229363f60de71c19933c45a7ef8bc45ee346`

## 结论

本轮已完成用户授权的五项 Slice A 安全修复。生产环境未部署、未写入真实证据，Claude 的后续窄范围复核入口仍保留。原始差异扫描发现的同 UID 已打开 inode 原地修改问题不属于本轮可安全闭合的边界，按授权留给 Slice B 的身份隔离设计。

## 逐项回应初始差异扫描

| 初始候选 | 结论 | 修复证据 |
| --- | --- | --- |
| `f683c4b3347361f0` artifact archive 可伪造摘要、无逐文件配额/数据库绑定 | 已修复 | `scripts/backup_restore.py` 对每个已发布成员重算 SHA-256、核对 content-addressed storage key、核对 tar 实际字节数、执行单文件/总量配额，并将排序后的成员清单摘要与数据库 `raw_artifact` 清单双向绑定。 |
| `fef425cc0ceb844e` 运行时 SQL 可制造无 provenance/terminal audit 的 ImportJob | 已修复 | `ImportJob.source_system`、`terminal_audit_event_id`、组合 provenance 外键和 deferred constraint trigger；终态必须绑定同一 job/artifact/status 的 `import.complete` 审计事件。 |
| `e1b304eb94e38274` 同 UID 可在摘要后原地替换已打开 inode | Slice B | 这是文件身份隔离/不同 UID runner 的结构性边界，本轮不伪装成已解决；真实 Connector runner 必须在 Slice B 通过 hostile-container/IPC 验收。 |
| `619c7dff47b1e724` v2 restore 未验证触发器/运行时授权基线 | 已修复 | v2 恢复要求 Phase 1/2/3 全部 revision-owned triggers 启用、运行时表/列授权精确匹配、无 sequence 权限，且 `append_audit_event` 是唯一不可转授权的运行函数。 |
| `03a22831e6d0c3f2` connector 名称可碰撞内部身份 | 已修复 | `validate_connector()` 拒绝 `ledgerbridge.*` 保留命名空间。 |
| `0e31d651bf796751` 恢复执行归档内的 deployment manifest verifier | 已修复 | `_verify_deployment_manifest()` 始终从正在运行的 verifier project 调用脚本；归档树只作为待验证数据传入，不能提供可执行验证器。 |

## 验证证据

本地验证：

- `uv lock --offline` 成功解析 74 个包。
- Ruff format/check、严格 mypy、Python compileall 通过。
- 非 PostgreSQL 单元测试 `77 passed, 78 skipped`；连接器与备份恢复定向测试 `30 passed`。
- Bandit 未发现失败项；`pip-audit --offline` 报告 `No known vulnerabilities found`（本地包未发布到 PyPI，按工具提示跳过自身包）。

Hermes 隔离 PostgreSQL：

- 临时环境已迁移到 Alembic `20260822_0004`；重跑迁移幂等通过。
- 16 个 revision-owned public triggers 全部启用；`ledgerbridge_app` 的数据库 TEMP 权限为 false，实测 `CREATE TEMP TABLE` 被拒绝。
- 运行时授权检查显示仅保留规定的表/列权限，`append_audit_event` 为 `EXECUTE, grantable=NO`。
- 行为探针实测拒绝：创建 `pg_temp` 表、删除 POSTED posting、修改 POSTED posting 金额、修改已使用账户的 `account_class`。
- Hermes 测试容器曾尝试安装开发依赖，但该隔离网络 DNS 不可用且镜像缓存没有 `pytest`/`tomli`，因此本轮不宣称远端完整 pytest 结果；本地测试、迁移和直接数据库行为探针已完成。

所有临时 Hermes 容器、网络和测试数据均不属于生产。生产仍停留在 Phase 2 revision `c56b6ffdde9f723efe1792ae1312ec8795bba165` / Alembic `20260821_0003`，没有部署授权也没有执行部署。

## 后续门禁

1. 已完成 `b72b229363f60de71c19933c45a7ef8bc45ee346` 的最终固定 SHA 安全复扫；正式报告为 `docs/reviews/2026-08-22-phase-3-security-scan-final-codex.md`。结果为 0 个未闭合的 Slice A 报告项，保留 1 个低危同 UID inode 问题作为 Slice B deferred finding。
2. 用户另行授权后，才可发布/创建受保护 PR；合并和生产部署继续分别请求授权。
3. Slice B 单独实现 Unix-socket、无网络、不同 UID 的 Connector runner，并重新验证 `e1b304eb94e38274` 的 inode 身份边界。
