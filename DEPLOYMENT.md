# Hermes 合成数据预览部署

本部署只提供前端合成数据原型，不连接 Hermes 消息数据库、LedgerBridge、OneDrive 或任何真实凭据。

## 启动

```bash
docker compose up -d --build
```

默认映射到主机 `8780` 端口。需要调整时，在命令环境中设置 `LEDGERBRIDGE_WEB_PORT`，不要为此创建包含凭据的环境文件。

## 验证

```bash
docker compose ps
curl -fsS http://127.0.0.1:8780/healthz
curl -I http://127.0.0.1:8780/
```

容器以非特权用户运行，根文件系统只读，丢弃全部 Linux capabilities，并启用 `no-new-privileges`。Nginx 添加了 CSP、禁止嵌入、MIME 嗅探防护和权限策略响应头。

如果 Hermes 暂时无法访问镜像仓库，可先在可信构建机执行 `npm ci && npm run build`，将 `dist` 完整同步到部署目录，再使用 Hermes 已缓存的 Python 运行时：

```bash
docker compose -f compose.offline.yaml up -d
```

离线预览服务器提供相同的安全响应头，仍以非特权用户、只读根文件系统和只读静态文件卷运行。它只用于合成数据预览；恢复镜像访问后应回到 Nginx 镜像方案。

## 更新

先同步已提交的仓库内容，再执行：

```bash
docker compose up -d --build
```

## 停止

```bash
docker compose down
```

在接入真实数据前，必须增加身份认证、服务端授权、审计记录、CSRF 防护和速率限制，并完成独立安全复核。
