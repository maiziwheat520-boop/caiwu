# Hermes 合成数据预览部署

本部署提供前端与同源合成 BFF API，不连接 Hermes 消息数据库、LedgerBridge、OneDrive 或任何真实凭据。审核决定和草稿状态只保存在进程内存中，容器重启后恢复初始状态。

## 启动

```bash
docker compose up -d --build
```

默认只映射到主机回环地址 `127.0.0.1:8780`。需要调整端口时设置 `LEDGERBRIDGE_WEB_PORT`；只在受信内网预览时显式设置 `LEDGERBRIDGE_WEB_BIND_ADDRESS`。不要为此创建包含凭据的环境文件。

例如在 Hermes 内网地址发布合成预览：

```bash
LEDGERBRIDGE_WEB_BIND_ADDRESS=192.168.1.39 docker compose up -d --build
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

当前合成 BFF 已实现预览会话、CSRF、幂等键、乐观并发与追加式审核事件，但这些不是生产身份认证或持久化审计。在接入真实数据前，必须增加 Passkey 身份认证、服务端授权、持久化审计、速率限制和受控附件解析，并完成独立安全复核。
