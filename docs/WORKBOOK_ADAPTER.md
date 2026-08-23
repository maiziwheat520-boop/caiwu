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
    payroll_statistics_copy: Path | None
    material_directory: Path
    confirmed_candidate_snapshot_id: UUID
    overrides: Mapping[str, object]
```

输入对象只引用 worker 临时目录中的副本。真实 OneDrive 文件和 LedgerBridge 证据区均只读。

### 3. 显式输出

```python
@dataclass(frozen=True)
class ReconciliationPreview:
    output_path: Path
    output_sha256: str
    target_sheet: str
    changed_cells: tuple[CellChange, ...]
    formula_values: Mapping[str, Decimal]
    warnings: tuple[PreviewWarning, ...]
    provenance: tuple[CandidateCellBinding, ...]
```

金额使用 `Decimal` 或分币整数，禁止在 API 边界使用浮点数。

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
  -> 校验输入 SHA-256 和 XLSX ZIP
  -> 调用对账核心生成预览副本
  -> LibreOffice headless 重算另一个临时副本
  -> 检查公式错误、关键单元格、sheet 名和结构
  -> 发布不可变 draft + 摘要 + 来源绑定
  -> 浏览器人工确认
  -> 上传 OneDrive 新版本
```

任何一步失败都保留源文件不变。临时目录必须使用操作系统安全随机目录，任务完成后删除；日志不得记录工作簿内容、消息全文或凭据。

## LibreOffice 验证状态

允许的状态只有：

- `QUEUED`
- `BUILDING`
- `NEEDS_REVIEW`
- `LIBREOFFICE_VERIFIED`
- `FAILED`

不能显示“Excel 已验证”。如果 LibreOffice 打开后产生结构变化、公式错误或关键值差异，状态必须是 `NEEDS_REVIEW`，并阻止自动上传新版本。

## 第一批特征测试

提取前先从现有行为固化测试：

1. 已存在月份只覆盖素材或人工修改单元格，保留用户公式。
2. 新月份从上月模板复制结构，但当月平台和银行数据不继承。
3. 非当月银行流水被清除，合计公式移动到正确行。
4. 缺少月份、冲突候选和空证据快照不能进入 worker。
5. 布草 3% 上浮、瓶装水、水费、税费和各店工资映射保持当前结果。
6. 临时 XLSX 不是合法 ZIP 时绝不替换源文件。
7. LibreOffice 重算差异会阻断发布。
8. 同一 snapshot + workbook digest 重试得到同一幂等结果。

这些测试通过后，才适合让旧 Tkinter 和 Web 共用提取后的核心模块。
