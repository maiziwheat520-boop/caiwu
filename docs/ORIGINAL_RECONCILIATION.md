# 原口径业务窗口

## 模块边界

`/reconciliation` 是统一的月度对账入口；旧路径 `/original-reconciliation` 只做兼容跳转。它不是 Excel 预览器，也不替代以下模块：

- `/personal-finance`：完整个人财务材料与审核概览。
- `/company-reports`：按公司主体汇总的报表入口。
- `/payroll`：工资发布契约与连接状态。

Web 不在本地重算旧表业务规则。Core 的 `ledgerbridge.cash-reconciliation.v2` 是收入、支出、往来款金额和冲突/缺口的权威投影；`ledgerbridge.original-reconciliation.v1` 只补充旧表待办。来源系统精确等于 `original_reconciliation_xlsx` 的 Candidate 仍只负责受控导入计划映射的逐项工作流。Web 只把这些 Core 事实转换为以下业务窗口：

- 当月收入、支出和利润合计；
- 已导入的业务事项，可返回原始材料；
- 待补录、待归属和待审核清单；
- 可按完整事实标识定位的冲突/缺口，以及文件和待审核入口。

逐项卡片按 Candidate 的稳定 `source_system` 做 fail-closed 准入：只接受 `original_reconciliation_xlsx`，其他银行、平台、采购、实际报销和工资 Candidate 即使摘要或金额相似也不显示。卡片业务性质只读取 Core 复核后的类别代码；摘要文案、银行正负号、风险提示和附件文件名都不能改写分类。无法由稳定类别代码确定的项目进入“待归类”，不进入收入、支出或往来款合计。

正式银行流水和已确认微信 Candidate 是本页的规则匹配事实源。银行按 Asia/Shanghai 的实际交易日归属自然月；微信 Candidate 当前只有月粒度，因此按 Core 已确认的 `accounting_month` 归属，不能声称具备日级实际收付款时间。旧截图与历史表格提交入口继续暂停。

点击“打开事项”进入现有 Candidate 详情、证据和更正/审核表单。提交决定后，Web 必须再次读取同一 Candidate，并核对 ID、revision 和 status；重读失败时只能提示“已保存、需刷新确认”，不能声称闭环成功。

## 旧表项目取数来源

- 收入：薇旭携程/美团取个人中国银行；薇旭飞猪及文杰房租取薇旭网商银行企业账户；薇旭银行收款取个人农业银行；景怡美团/银行收款取个人建设银行；逸豪飞猪取逸豪网商银行企业账户。
- 支出：瓶装水按旧表对应主体取相应网商银行，景怡取景怡农行，一品取微信转账；布草取逸豪网商银行；雅朵和景怡税费分别取各自农行。
- 代发工资：只读取相邻工资表已核对的最终数据，工资表是权威源；不从银行代发金额反推。
- 往来款：重点对象为陈展武（老爸）和林素美（老妈）；分红、投资等非经营资金不得计入经营收入或支出。

26.6、26.7 消杀均由 Core 原表映射记为景怡公账支出，修正后期末分别为 `-161,330.34` 和 `-319,401.10`；Web 不按摘要或银行正负号覆盖该口径。26.8 历史差错调整只在 Core 有正式数据时显示，不在 Web 伪造。

用户不在网页中面对 A–M 列或 40 行网格；这些只是 Core 与 Web 之间的兼容传输结构。

每份投影携带共享分类 `taxonomy_version`、固定布局 `layout_version` 和栏位规则 `mapping_version`；Web 只展示这些追溯版本，不维护版本 allowlist，也不据此推断业务分类。

## 授权范围

浏览器读取 `GET /api/v1/original-reconciliations/{month}`。首次读取可以省略 scope；BFF 使用部署时已授权的 `entity_ref` 和 `business_unit_ref` 调用 Core。浏览器后续回传 scope 时，只允许与该授权范围完全相同，否则返回 `403 SCOPE_NOT_AUTHORIZED`。

BFF 不接受浏览器选择任意公司或门店，也不从候选显示名称推断实体归属。

当前部署仍只有一个固定业务单元授权范围。要完整替代包含多个公司/门店的历史工作簿，后续必须把会话授权扩展为经 Core 验证的范围集合；不能继续把月份切换建立在固定 May 范围上。

## 金额与完整性

- 所有金额均为 CNY 整数分。历史 AMOUNT 单元格的正负号由 Core 给出，Web 只负责格式化。
- `posted_income_minor`、`posted_expense_minor` 和 `posted_profit_minor` 只描述正式入账（POSTED_LEDGER）账簿分类；退款可能令净支出为负，Web 不施加正负号业务含义。
- `posted_ledger_complete=false` 时，所有 POSTED 合计和 `posted_amount_minor` 必须为 `null`，Web 显示“待接正式账簿”，禁止当作 0；只有完整接入且确无事实才显示 0。
- `confirmed_candidate_amount_minor` 是已确认、待入账事实的有符号审计量，不显示为正式损益。
- `confirmed_pending_posting_count` 单独显示“已确认待入账”；大于 0 时投影不完整，且不进入 POSTED 合计。
- 待审核事实只显示 `pending_review_count`，不进入单元格或正式合计。
- 账户或上月映射缺失时，期初/期末余额为 `null`，Web 显示“待补账户映射”，禁止自行推算。
- `is_complete` 由 Core 决定。GAP、待审核、待补材料、未映射已确认事实或余额缺口存在时必须为 `false`。
- `projection_gaps` 单独保留不属于某个单元格的维度缺口：当前月度候选使用 `MISSING_TIME_GRANULARITY`，不能解析摘要猜周次；未来公司合计有效但历史营业单元快照缺失时使用 `MISSING_BUSINESS_UNIT_ATTRIBUTION`，只缺拆分，不把公司合计改成未知。

## 兼容传输结构与缺口

为兼容旧口径，合同内部仍固定返回 A–M 共 13 列、1–40 共 40 行，每行固定 13 个 cell。F、G 是 SPACER 且始终为 BLANK。该结构不直接渲染为网页表格。cell kind 含义：

- `BLANK`：保留原表空白。
- `LABEL`：原表文字标签。
- `AMOUNT`：CNY 整数分；直接事实格带来源引用，派生合计、零值或余额格可以没有来源引用。
- `GAP`：栏位、余额映射、经济影响或正式账簿缺失；允许 `MISSING_LEGACY_SLOT_MAPPING`、`MISSING_BALANCE_MAPPING`、`MISSING_ECONOMIC_EFFECT`、`POSTED_LEDGER_UNAVAILABLE`，`label` 固定为 `null`，不伪造金额，也可以没有来源引用。Web 只把 `gap_code` 翻译成缺口提示。

来源按 `POSTED_LEDGER`、`CONFIRMED_CANDIDATE`、`ACCOUNT_STATEMENT` 区分，并保留可空的脱敏 `source_label`。`mapped_cell_count` 只统计直接事实格，不等于所有 AMOUNT 格数量。

BFF 对精确字段集、版本、scope、A–E/F–G/H–M 角色列序、坐标、cell kind、POSTED 合计关系和完整性进行 fail-closed 校验；Core 返回不合约数据时对浏览器返回 503。
