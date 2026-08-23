# 旧程序工作簿适配边界

## 结论

`auto/app.py` 的月度对账规则可以复用，但 `build_reconciliation()` 目前不是可直接放进 Web worker 的纯函数。第一步应先做无行为变化的提取，再接 OneDrive 和 LibreOffice；不应在 Web 请求线程中直接导入整个 Tkinter 程序。

## 当前函数合同

```python
build_reconciliation(
    workbook_path,
    stat_file,
    folder,
    month,
    year,
    log,
    progress=None,
) -> tuple[Workbook, str, set[tuple[int, int]]] | None
```

它已经具备一个重要安全边界：生成内存预览，不直接保存。`save_reconciliation()` 在用户确认后才写入，并使用临时 XLSX、ZIP 完整性检查和 `os.replace()`。

## 不能原样搬到 Hermes 的原因

- 模块顶层导入 Tkinter、ttkbootstrap、拖放控件和工资模块，后台 worker 不需要也不应加载这些 UI 依赖。
- 预览阶段会在工作簿旁创建 `月度数据/<月份>.json`；这是隐藏写操作，不符合只读预览。
- OCR、文件枚举、配置回退、工作簿复制和公式求值耦合在一个函数内，难以重试和审计。
- 输入以目录为单位，无法直接表达“某条已确认候选对应某个单元格”的来源关系。
- `save_reconciliation()` 假定本地文件路径可原子替换；OneDrive App Folder 应上传新版本，而不是把云端同步目录当成本地原子文件系统。
- 保存后的“请用 Excel 打开核对”依赖桌面 Excel；Hermes 需要 LibreOffice 临时副本重算和明确的兼容性状态。

## 提取顺序

### 1. 隔离计算核心

在旧程序代码库中新增不导入 UI 的模块，例如：

```text
finance_core/
  reconciliation.py
  workbook_preview.py
  formula_preview.py
  material_extractors/
```

原 Tkinter 按钮和 Web worker 都调用同一套模块，避免复制规则。

### 2. 显式输入

计算入口改为接收不可变输入对象：

```python
@dataclass(frozen=True)
class ReconciliationInput:
    accounting_month: str
    workbook_copy: Path
    onedrive_item_id: str
    onedrive_etag: str
    onedrive_ctag: str
    workbook_sha256: str
    payroll_statistics_copy: Path | None
    materials: tuple[MaterialInput, ...]
    confirmed_candidate_snapshot_id: UUID
    candidate_snapshot_revision: int
    overrides: tuple[CellOverride, ...]
```

输入对象只引用 worker 临时目录中的副本。真实 OneDrive 文件和 LedgerBridge 证据区均只读。

`MaterialInput` 不是目录名，而是有序的不可变清单项，至少包含 evidence ID、SHA-256、实际 magic/MIME、受控文件名、source role、业务键和候选 revision。worker 只能 stage 清单中的文件，不得扫描整个上传目录。相同摘要、相同业务键或截图/表格跨格式重复必须形成冲突，不能累计两次；排序与归并规则属于有版本的计算规则。

`CellOverride` 必须包含 sheet、坐标、原值摘要、带类型的新值、原因、审核事件 ID 和 expected workbook revision。覆盖公式是单独的高风险操作，不能混在普通金额更正中。

### 3. 显式输出

```python
@dataclass(frozen=True)
class ReconciliationPreview:
    output_path: Path
    output_sha256: str
    target_sheet: str
    changed_cells: tuple[CellChange, ...]
    formula_values: Mapping[str, FormulaPreviewValue]
    warnings: tuple[PreviewWarning, ...]
    provenance: tuple[CandidateCellBinding, ...]
```

金额使用 `Decimal` 或分币整数，禁止在 API 边界使用浮点数。

`FormulaPreviewValue` 必须区分 `VALUE`、`UNKNOWN`、`CYCLE`、`MISSING_SHEET` 和 `UNSUPPORTED`，并记录 engine 与 error。旧 `_formula_value()` 对不支持函数、循环或缺失工作表的结果不能再被转换成 `0.0`；只有 LibreOffice 重算后的值可以成为验证值，旧预览值与 LibreOffice 值分别保存和展示。

### 4. 把隐藏写入变成 worker artifact

当前 `月度数据/<月份>.json` 的自动创建要改为显式 artifact：

- 读取已有 override 时记录输入摘要。
- 没有 override 时使用内存空配置，不在预览阶段创建文件。
- 用户更正后由独立写操作追加版本，不能静默覆盖。

### 5. 加入候选到单元格的来源绑定

每个写入单元格保存：

- candidate ID 与 revision；
- evidence ID 与 SHA-256；
- 提取字段名和营业单元；
- 原值、建议值、用户确认值；
- 规则版本和 worker 版本。

报表中的合计公式可以关联多个候选，但不能伪造一条原始消息作为合计的直接证据。

## Hermes worker 流程

```text
冻结已确认候选 revision
  -> 下载 OneDrive 工作簿到随机临时目录
  -> 校验 item id、eTag/cTag、SHA-256 和 XLSX package
  -> 校验每个素材的 magic/MIME、大小、页数、像素、解压比、加密/宏状态
  -> 在无网络且受 CPU/内存/pids/时限约束的解析 worker 处理素材
  -> 调用对账核心生成预览副本
  -> 在另一个无网络受限 worker 中执行 LibreOffice headless 重算
  -> 检查公式错误、关键单元格、sheet 名和 OOXML 结构差异
  -> 发布不可变 draft + 摘要 + 来源绑定
  -> 浏览器人工确认
  -> 使用原始 eTag 的 If-Match 上传 OneDrive 新版本
```

任何一步失败都保留源文件不变。临时目录必须使用操作系统安全随机目录，任务完成后删除；日志不得记录工作簿内容、消息全文或凭据。

上传前必须再次核对 OneDrive item id、eTag/cTag 和下载摘要。只要桌面 Excel、旧 Tkinter 或其他客户端在 draft 期间改过文件，If-Match 就应失败并把 draft 标为过期；系统必须基于最新版本重新生成，不能自动 rebase 或覆盖。

## 不可信素材解析

Hermes 自动收到的 PDF、Excel 和图片均视为不可信输入。解析不能在 Web API、LedgerBridge API 或 Hermes gateway 进程内运行。

- 每个任务使用独立非特权进程或容器，根文件系统只读、无网络、临时目录独享。
- 设置文件大小、解压后大小/压缩比、PDF 页数、图片像素、CPU、内存、pids 和墙钟时限。
- 拒绝加密文件、宏、外部链接和不允许的 OOXML 部件。
- pypdf、openpyxl、xlrd、Pillow、RapidOCR/onnxruntime 和 LibreOffice 分开限定权限；LibreOffice 不与素材解析器共用长期进程。
- OCR 引擎和 token cache 为 job scoped，任务结束清空；引擎初始化失败只能通过受控 worker 重启恢复。

每个 extractor 必须返回以下状态之一：`MATCHED`、`NO_MATCH`、`PARSE_FAILED`、`DEPENDENCY_UNAVAILABLE`、`AMBIGUOUS`。后三种必须进入 `NEEDS_REVIEW` 并阻断发布，绝不能被当成没有素材后回退到旧值、模板值或 0。结果要逐个记录输入 digest、解析器版本和处置状态。

## 工作簿结构保真

ZIP 可读只证明 XLSX 容器没有损坏，不证明业务结构完整。迁移前必须建立模板 feature inventory，并比较 pre-openpyxl、pre-LibreOffice、post-LibreOffice 三份 artifact 的 OOXML package 差异。

允许变化清单至少覆盖：sheet XML、公式与缓存、calc chain、defined names、print area/page setup、freeze panes、conditional formatting、data validation、hyperlinks、protection、drawings/images/charts/tables。未在 allowlist 中的部件删除或结构变化一律阻断。

月份工作表必须通过统一 resolver 兼容 `YY.M` 和 `YY.MM`。两种别名并存时视为冲突；输出命名由模板版本决定，并测试 1 月、跨年和历史补零月份，不能直接拼接后创建重复 sheet。

## Draft 生命周期与 LibreOffice 验证

Draft 生命周期状态为：

- `QUEUED`
- `BUILDING`
- `NEEDS_REVIEW`
- `VERIFIED`
- `FAILED`

验证结果单独为 `LIBREOFFICE_VERIFIED` 或空。不能显示“Excel 已验证”。如果 LibreOffice 打开后产生结构变化、公式错误或关键值差异，生命周期状态必须是 `NEEDS_REVIEW`，并阻止自动上传新版本。

## 可重复运行环境

Hermes worker 需要固定 Linux image digest、Python lock、pypdf/openpyxl/xlrd/Pillow/onnxruntime 版本、OCR 模型 checksum、字体、locale、时区和 LibreOffice build。启动自检必须用不含真实财务数据的金样本覆盖 PDF、XLS、XLSX 和图片，并把所有版本写入 provenance；Windows Excel/pywin32 行为不能作为 Hermes 的隐含依赖。

## 第一批特征测试

提取前先从现有行为固化测试：

1. 已存在月份只覆盖素材或人工修改单元格，保留用户公式。
2. 新月份从上月模板复制结构，但当月平台和银行数据不继承。
3. 非当月银行流水被清除，合计公式移动到正确行。
4. 缺少月份、冲突候选和空证据快照不能进入 worker。
5. 布草 3% 上浮、瓶装水、水费和各店工资映射保持当前结果；税费是旧核心尚不存在的新增规则，必须先定义营业单元、科目、来源和 legacy cell 映射，再建立独立金样本。
6. 临时 XLSX 不是合法 ZIP 时绝不替换源文件。
7. LibreOffice 重算差异会阻断发布。
8. 同一 snapshot + workbook digest 重试得到同一幂等结果。
9. 水费完整性检查读取各营业单元叶子输入格或候选覆盖，不能因聚合公式存在就判定已填。
10. `PARSE_FAILED`、`DEPENDENCY_UNAVAILABLE`、`AMBIGUOUS` 均阻断，不能回退旧月份或默认值。
11. 重复摘要、相同业务键、截图与 Excel 双来源不会重复计数，且目录顺序不影响输出。
12. OneDrive eTag 变化导致 draft 过期，上传使用 If-Match 并拒绝覆盖。
13. UNKNOWN/CYCLE/MISSING_SHEET 公式绝不显示为 0。
14. OOXML 结构差异只允许命中明确 allowlist 的变更。
15. `YY.M`、`YY.MM`、1 月和跨年模板解析不会创建重复月份。

这些测试通过后，才适合让旧 Tkinter 和 Web 共用提取后的核心模块。
