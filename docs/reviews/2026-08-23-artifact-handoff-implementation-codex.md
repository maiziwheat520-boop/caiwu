# Slice C ArtifactStore handoff implementation report

日期：2026-08-23  
实现提交：`6300bf5`（`feat: add bounded artifact handoff sessions`）  
分支：`ai/chatgpt/phase-3-connector-runner`  
PR：[PR #18](https://github.com/maiziwheat520-boop/caiwu/pull/18)

## 结论

Option 2（ArtifactStore-owned transactional handoff session）已实现并通过
Windows、本地静态门禁、GitHub Linux/PostgreSQL CI，以及 Hermes 独立 Compose
回放。实现只增加存储交接和 multipart 完成信号；没有 HTTP 路由、认证、真实
Connector、数据库导入或生产部署。

## 实现边界

- `src/ledgerbridge/upload.py` 新增 `MultipartComplete`，只在 closing boundary
  被完整消费且没有尾随字节后产生；`MultipartFileEnd` 仍只表示文件字节结束。
- `src/ledgerbridge/artifacts.py` 新增 `ArtifactStore.begin_handoff()` 和
  `ArtifactHandoff.write/complete/abort`。临时 inode、描述符、摘要、大小和
  状态均由存储层拥有，调用方不能选择路径或 storage key。
- 每次写入在有界 quota lock 下重新扫描 staging，统一计入 512 MiB staging
  预算；提交前从持有的描述符 fsync/验证，使用描述符级私有权限，并采用安全
  hard-link、目录 fsync、已存在内容校验和去重。
- 临时路径带 `artifact-handoff-` 前缀并绑定创建时的 `(st_dev, st_ino)`；路径
  替换、非 regular inode、内容不匹配和失败清理均 fail closed，不删除未知替换
  文件。旧的阻塞式 `publish()` API 保持兼容。

## 验证证据

- Windows：`uv lock --offline`、Ruff format/check、严格 mypy、Bandit 通过；
  全量 pytest `160 passed / 119 skipped / 1 warning`。handoff/multipart 定向集
  `70 passed / 10 skipped`，覆盖提交前 completion gate、取消/abort、错误尾界、
  staging/published quota、去重和路径替换回归。
- GitHub Actions：提交 `6300bf5` 的 push run `32613520982` 与 pull-request
  run `32613522285` 的 `secrets`、`quality`、`compose` 全部 success；质量门禁
  仍保留 95% coverage threshold。
- Hermes disposable replay：使用唯一 Compose project
  `ledgerbridge-handoff-6300bf5` 构建当前提交，PostgreSQL migration
  `0001 -> 0002 -> 0003 -> 0004` 成功；worker handoff smoke 完成提交、读取和
  staging 清零，第二次相同内容返回 `created=False` 并成功去重。回放结束后
  `docker ps`、volume、network 对该 project 均为 0，生产 Compose/卷未触碰。

## 未闭合但有意保留的门

下一步才允许设计内部 route：认证主体必须服务端派生，路由必须先完成 parser
终态再调用 `complete()`，只有拿到 committed `PublishedArtifact` 后才能进入
`EvidenceImporter`；错误状态映射、幂等策略、无 manifest fail-closed 和零行/零
审计回归仍未实现。PR #18 保持 open、未 merge，Hermes 生产仍运行 Slice A
`e426b488b2abb02f10ef02a61aae7ebe24c3283f`。
