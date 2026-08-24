# Hermes 合成数据预览部署

本部署提供前端与同源合成 BFF API，不连接 Hermes 消息数据库、LedgerBridge、OneDrive 或任何真实凭据。无认证合成模式只用于交互预览；Passkey 模式使用独立 SQLite 状态目录并必须位于受信 HTTPS origin。

## 启动

```bash
docker compose up -d --build
```

默认只映射到主机回环地址 `127.0.0.1:8780`。需要调整端口时设置 `LEDGERBRIDGE_WEB_PORT`；不要为认证版增加 LAN 监听开关，也不要为此创建包含凭据的环境文件。

例如在 Hermes 内网地址发布合成预览：

```bash
docker compose up -d --build
```

## 验证

```bash
docker compose ps
curl -fsS http://127.0.0.1:8780/healthz
curl -I http://127.0.0.1:8780/
curl -i http://127.0.0.1:8780/api/v1/session
```

容器以非特权用户运行，根文件系统只读，丢弃全部 Linux capabilities，并启用 `no-new-privileges`。预览服务添加了 CSP、禁止嵌入、MIME 嗅探防护和权限策略响应头。

如果 Hermes 暂时无法访问镜像仓库，可先在可信构建机执行 `npm ci && npm run build`，将 `dist` 完整同步到部署目录，再使用 Hermes 已缓存的 Python 运行时：

```bash
docker compose -f compose.offline.yaml up -d
```

离线方式使用同一个合成 BFF 服务和安全响应头，仍以非特权用户、只读根文件系统和只读代码、静态文件卷运行。它依赖部署主机已经缓存 `python:3.12-slim`。

## 更新

先同步已提交的仓库内容，再执行：

```bash
docker compose up -d --build
```

## 停止

```bash
docker compose down
```

无认证合成模式的自动会话不属于生产认证。下述 Passkey 模式增加了真实 WebAuthn 验证和 SQLite 持久化，但仍只承载合成财务数据；接入真实数据前还必须完成服务端授权细化、持久化限流、受控附件解析、备份恢复演练与独立安全复核。

## Passkey + 持久化预览

WebAuthn 不能在普通局域网 IP 的 HTTP 页面工作。Passkey 模式必须通过固定域名的 HTTPS 反向代理访问，并把 RP ID 和 exact origin 固定为部署配置，不能从请求头动态推导。

1. 将 linux/amd64 wheelhouse 同步到 `wheelhouse/`，离线安装到只读挂载的 `vendor/`：

   ```bash
   docker run --rm --user "$(id -u):$(id -g)" \
     -v "$PWD/wheelhouse:/wheelhouse:ro" -v "$PWD/vendor:/vendor" \
     python:3.12-slim python -m pip install --no-index --require-hashes \
     --find-links=/wheelhouse --target=/vendor -r /wheelhouse/requirements.lock
   ```

2. 创建本机状态、依赖和安装标记目录并限制权限；不要放在 OneDrive、NFS 或共享盘：

   ```bash
   install -d -m 700 state vendor config
   ```

3. 用 `python deploy/generate_setup_code.py --ttl 600` 生成十分钟有效的首次设置码。原文只显示一次；只把摘要和到期时间传给 Compose，不写入仓库或环境文件。

4. 必须使用 LedgerBridge 独占的稳定 DNS 主机名；不能复用 Home Assistant、Grafana 等服务的主机名后只换端口，因为 Cookie 不按端口隔离。HTTPS 反向代理只允许 tailnet 访问且不启用 Funnel。

5. 首次创建数据库时，在当前 shell 临时设置 `ALLOW_INITIAL_BOOTSTRAP=1`，并设置 `WEBAUTHN_RP_ID`、`WEBAUTHN_EXPECTED_ORIGIN`、`SETUP_CODE_SHA256`、`SETUP_CODE_EXPIRES_AT`、容器 UID/GID，然后启动：

   ```bash
   docker compose -f compose.authenticated.yaml up -d --force-recreate
   ```

6. Passkey 登记并保存恢复码后，由主机管理员创建 root-owned 的 `config/enrolled-v1`，内容必须精确为 `ledgerbridge-enrolled-v1` 加换行；随后立即去掉 `ALLOW_INITIAL_BOOTSTRAP=1` 并重建容器。标记存在但数据库缺失、标记缺失但数据库已登记，都会拒绝启动，不能静默重新开放首次设置。

7. 只通过最终 HTTPS origin 输入设置码并登记 Passkey。首次登记返回 10 个高熵恢复码，仅显示一次。每个恢复码使用后立即失效；恢复会话不能读取业务数据，必须先登记新 Passkey并保存轮换后的恢复码。恢复登记同时撤销旧 Passkey、旧恢复码和旧会话。

数据库目录应备份整个 SQLite DB/WAL/SHM 一致状态，在线备份优先使用 SQLite Backup API。恢复旧备份后必须撤销会话并复核 RP ID/origin，不能用复制活跃主文件代替一致备份。
