# S1 online encryption foundation

日期：2026-08-24
结论：合成应用实现完成；运维 gate 未通过，Hermes 在线卷迁移与真实密钥托管未授权

## 已确认范围

- 本轮实现应用密码格式、合成 KeyProvider、加密 artifact/state primitives、轮换与恢复
  合同、宿主存储加密 preflight 和明文泄漏测试。
- 大文件使用 libsodium XChaCha20-Poly1305 secretstream；每个对象使用独立随机 DEK，
  以版本化 purpose/AAD 绑定上下文并要求认证 FINAL 帧。
- 生产 KEK 不进入仓库、数据库、`.env`、Compose build context、日志或测试；本轮只提供
  KeyProvider 协议和合成 provider。
- 真实 ingest 在配置层无条件拒绝。S1 代码完成不等于 S1 运维 gate 完成，也不授权 R1、
  R2、I1、D1、Hermes/Outlook/OneDrive、生产迁移或真实数据。

## 宿主事实与未完成 gate

- 2026-08-24 只读检查显示 Hermes VM103 只有一块 64 GiB 系统盘；根目录与
  `/var/lib/docker` 位于同一 ext4 `/dev/sda1`，PostgreSQL 和 ArtifactStore 使用普通
  Docker named volume；未发现 swap。
- data checksums、内部 Docker network、文件权限和 GPG 加密备份都不等于在线 DB/WAL/
  volume 加密。
- 用户确认本轮不迁移 Hermes。推荐的后续运维切片是在 PVE 为 VM103 增加独立虚拟盘，
  使用 LUKS/dm-crypt 后迁移 PostgreSQL/WAL/artifact；该操作需要单独的停机、备份、root
  权限、恢复演练与回滚授权。

## 验收边界

- header、wrapped DEK、purpose/AAD、frame、FINAL、key generation 任一损坏或不匹配均
  fail closed，且不得产生业务副作用或明文 fallback。
- 同一明文重复加密产生不同密文；artifact staging、发布文件、短期状态文件与失败残留
  不出现合成 canary 明文。
- 新 generation 只用于新写，旧 generation 可在有界窗口读；旧 key 仍有引用时拒绝退役，
  rewrap 必须幂等且不解密 payload。
- 宿主 preflight 对 unknown、过期、mount/device 不一致、未加密 PGDATA/WAL/temp/
  tablespace/artifact、未保护 swap 或启用 core dump 一律拒绝。
- 最终记录定向/全量测试、Ruff、strict mypy、Bandit、依赖审计、独立安全复核和精确提交
  范围；未完成的运维 gate 保持显式阻断。

## 独立安全复核与剩余门槛

- 独立复核结论为代码级 BLOCKER/HIGH 0。复核发现的 spool 认证失败后仍可读取前缀、
  小写入帧膨胀两项 MEDIUM 已修复：验证失败会永久关闭 spool，小写入会合并为固定上限
  帧。
- `EncryptedStateStore` 内部 generation/TTL/tombstone 随密文一起存储，不能阻止旧的合法
  密文被回放；其跨平台合成锁也不证明生产目录 owner/mode、锁 inode 或崩溃恢复安全。
  这两项 MEDIUM 保留为真实数据启用前硬 gate，需要外部单调锚和宿主专用权限/锁适配器。
- `rewrap` 只证明 DEK 可换代和 framing 可解析，不证明 payload tag/FINAL 健康；生产轮换
  必须结合完整认证读取或可信 receipt。
- 宿主 preflight 只是纯 JSON 判定器，尚无可信采集、签名或独立 attestation channel，
  不能为当前 Hermes 签发上线许可。

## 验证结果

- `cryptography` 已按确认范围升级到 `>=50,<51`；锁文件解析到 50.0.0，完整回归通过后
  `pip-audit --strict` 报告无已知漏洞。
- 全量 pytest：402 passed、149 skipped、1 个既有 Starlette/httpx 弃用 warning；跳过项为
  Windows 不支持的 POSIX 行为及未配置的 PostgreSQL 集成环境。
- Ruff format/check：152 files、通过；strict mypy：86 source files、通过；Bandit、敏感路径
  扫描、Compose YAML 解析、`uv lock --check` 与 `git diff --check` 均通过。Bandit 仅显示
  `backup_restore.py` 中两个既有 `nosec` 注释警告，没有失败项。
- `pip-audit --strict` 对冻结的完整运行/开发依赖报告 `No known vulnerabilities found`。
- 完整回归还暴露并修复了既有签名清单在 Windows 上把读取引起的 `atime` 更新误判为
  TOCTOU 的问题；稳定指纹继续校验 dev/inode/mode/nlink/size/mtime/ctime，且 Ed25519
  验签保持不变。
- 本机没有 Docker CLI，因此没有声称本地 Compose render 通过；仅验证 YAML 和关键默认
  值。没有部署、迁移、真实凭据或真实财务数据。
