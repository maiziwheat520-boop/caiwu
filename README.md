# LedgerBridge Web

酒店财务工作台的独立 Web 原型。它用于验证从 Hermes 消息候选到人工审核、月度对账草稿和文件计算验证的交互，不连接真实消息、真实财务数据库或真实 OneDrive 账户。

## 当前原型

- 概览：本月候选、已确认金额、营业单元和对账就绪度。
- 待审核：按消息来源筛选，查看原始消息与附件证据，确认或忽略候选。
- 月度对账：按营业单元汇总水费、税费、布草、瓶装水和银行收款。
- 文件与连接：展示 OneDrive App Folder、Hermes 消息入口和 LibreOffice 计算服务的预期边界。
- 移动端：支持审核和摘要；完整科目网格保留给平板和电脑。

所有示例数据均为合成数据。页面中的连接状态不代表真实服务已经配置。

## 本地运行

```bash
npm install
npm run dev
```

默认端口为 `4173`，开发服务器监听所有接口，便于后续在 Hermes 内网环境验证。

Hermes 合成数据预览的容器部署方式见 [DEPLOYMENT.md](./DEPLOYMENT.md)。

后续真实接入的运行边界见 [集成架构](./docs/ARCHITECTURE.md)，草拟接口见 [OpenAPI 合同](./contracts/openapi.yaml)。

旧 Tkinter 对账规则的渐进提取方案见 [工作簿适配边界](./docs/WORKBOOK_ADAPTER.md)。

## 验证

```bash
npm run lint
npm run test
npm run build
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

原型通过后再设计 BFF/API，连接 LedgerBridge 的候选记录与只读汇总接口。Hermes 消息附件必须在消息入口即时摄取，避免依赖临时附件路径做历史轮询。真实消息启用前必须完成合成数据测试和独立安全复核。
