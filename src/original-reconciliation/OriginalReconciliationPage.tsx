import { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import {
  ArrowsClockwise,
  Bank,
  CaretRight,
  ClockCounterClockwise,
  CloudArrowUp,
  Database,
  Info,
  ListChecks,
  Paperclip,
  Table,
  Warning,
} from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type { Candidate, OriginalReconciliation, Page } from '../types'
import { ErrorState, LoadingState, Metric, PageHeader } from '../shared/PagePrimitives'

const DEFAULT_MONTH = '2026-08'
const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })

function monthLabel(month: string) {
  const [year, value] = month.split('-')
  return `${year} 年 ${Number(value)} 月`
}

function isImportedWorkbookItem(candidate: Candidate) {
  return candidate.raw.source_system === 'original_reconciliation_xlsx'
}

function initialMonth(candidates: Candidate[]) {
  return candidates
    .filter(isImportedWorkbookItem)
    .map((candidate) => candidate.accountingMonth)
    .filter((month): month is string => Boolean(month))
    .sort()
    .at(-1) ?? DEFAULT_MONTH
}

function candidateStatus(candidate: Candidate) {
  switch (candidate.status) {
    case 'CONFIRMED': return { color: 'green' as const, label: '已确认' }
    case 'IGNORED': return { color: 'gray' as const, label: '已忽略' }
    case 'SUPERSEDED': return { color: 'gray' as const, label: '已替代' }
    case 'CONFLICTED': return { color: 'red' as const, label: '有冲突' }
    case 'INCOMPLETE': return { color: 'amber' as const, label: '待补录' }
    default: return { color: 'blue' as const, label: '待审核' }
  }
}

export function OriginalReconciliationPage({ candidates, onNavigate, onOpenCandidate }: {
  candidates: Candidate[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const [selectedMonth, setSelectedMonth] = useState(() => initialMonth(candidates))
  const [data, setData] = useState<OriginalReconciliation | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestRef = useRef(0)
  const scopeRef = useRef<OriginalReconciliation['scope'] | null>(null)

  const load = useCallback(async () => {
    const requestId = ++requestRef.current
    setLoading(true)
    setError(null)
    try {
      const scope = scopeRef.current
      const projection = await api.getOriginalReconciliation({
        accountingMonth: selectedMonth,
        entityRef: scope?.entity_ref,
        businessUnitRef: scope?.business_unit_ref,
      })
      if (requestId !== requestRef.current) return
      scopeRef.current = projection.scope
      setData(projection)
    } catch (loadError) {
      if (requestId !== requestRef.current) return
      setData(null)
      setError(loadError instanceof Error ? loadError.message : '无法读取原口径对账投影')
    } finally {
      if (requestId === requestRef.current) setLoading(false)
    }
  }, [selectedMonth])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const selectedMonthLabel = monthLabel(selectedMonth)
  const formatBalance = (amountMinor: number | null) => amountMinor === null
    ? '待补账户映射'
    : currency.format(minorToMajor(amountMinor))
  const formatPosted = (amountMinor: number | null) => amountMinor === null
    ? '待接正式账簿'
    : currency.format(minorToMajor(amountMinor))
  const gapLabel = (gapCode: OriginalReconciliation['rows'][number]['cells'][number]['gap_code']) => {
    switch (gapCode) {
      case 'MISSING_BALANCE_MAPPING': return '待补余额映射'
      case 'MISSING_ECONOMIC_EFFECT': return '待补经济影响'
      case 'POSTED_LEDGER_UNAVAILABLE': return '待接正式账簿'
      default: return '待补原表栏位映射'
    }
  }
  const gapLabels = data
    ? Array.from(new Set(data.rows.flatMap((row) => row.cells
      .filter((cell) => cell.kind === 'GAP')
      .map((cell) => gapLabel(cell.gap_code)))))
    : []
  const workbookItems = candidates.filter((candidate) => (
    isImportedWorkbookItem(candidate) && candidate.accountingMonth === selectedMonth
  ))
  const empty = Boolean(
    data
    && data.totals.mapped_cell_count === 0
    && data.unmapped_confirmed_count === 0
    && data.confirmed_pending_posting_count === 0,
  )

  return (
    <>
      <PageHeader
        eyebrow="历史口径"
        title="原口径对账表"
        description="按原有口径整理为业务事项；已导入数据自动带入，缺口统一进入补录和审核流程。"
        action={(
          <div className="original-reconciliation-filters">
            <input
              aria-label="选择原口径对账月份"
              className="original-month-input"
              max="9999-12"
              min="2000-01"
              type="month"
              value={selectedMonth}
              onChange={(event) => {
                if (!event.target.value) return
                setData(null)
                setSelectedMonth(event.target.value)
              }}
            />
            <span className="scope-chip" title={data?.scope.entity_ref ?? undefined}>
              {data ? `Core 授权范围 · ${data.scope.business_unit_ref}` : '正在确认公司 / 门店范围'}
            </span>
          </div>
        )}
      />

      {loading && !data ? <LoadingState title="正在读取原口径对账表" description={`正在读取 ${selectedMonthLabel} 的只读投影。`} /> : null}
      {error && !data ? <ErrorState message={error} onRetry={() => void load()} /> : null}

      {data ? (
        <>
          <section className="metric-grid original-reconciliation-metrics" aria-label="原口径合计">
            <Metric label="正式入账收入" value={formatPosted(data.totals.posted_income_minor)} detail="只统计 POSTED_LEDGER 账簿分类" icon={<CloudArrowUp size={20} />} />
            <Metric label="正式入账支出" value={formatPosted(data.totals.posted_expense_minor)} detail="Core 账簿分类，退款可能形成负数" icon={<Bank size={20} />} />
            <Metric primary label="正式入账利润" value={formatPosted(data.totals.posted_profit_minor)} detail={data.posted_ledger_complete ? '由 Core 的正式账簿分类计算' : '正式账簿尚未完整接入'} icon={<ArrowsClockwise size={20} />} />
            <Metric label="期初余额" value={formatBalance(data.totals.opening_balance_minor)} detail="缺少账户映射时不推算" icon={<Database size={20} />} />
            <Metric label="期末余额" value={formatBalance(data.totals.closing_balance_minor)} detail="缺少账户映射时不推算" icon={<Database size={20} />} />
          </section>

          <section className="panel original-reconciliation-workflow" aria-label="原口径事项录入与审核">
            <div className="panel-heading original-workflow-heading">
              <div><h2>已导入业务事项</h2><p>{selectedMonthLabel}的平台结算、账户流水与正式入账事实，不再展示 Excel 网格。</p></div>
              <div className="original-workflow-actions">
                <Badge color={data.is_complete ? 'green' : 'amber'}>{data.is_complete ? '已完成对账' : '仍有待办'}</Badge>
                <Button onClick={() => onNavigate('review')}><ListChecks size={16} />前往待审核</Button>
              </div>
            </div>

            <div className="original-business-item-list">
              {workbookItems.map((candidate) => {
                const status = candidateStatus(candidate)
                return (
                  <article key={candidate.id} className="original-workbook-item">
                    <div className="original-business-item-title">
                      <Badge color={status.color}>{status.label}</Badge>
                      <strong>{candidate.summary}</strong>
                      <small>{candidate.shortId} · 第 {candidate.revision} 版</small>
                    </div>
                    <dl>
                      <div><dt>公司 / 门店</dt><dd>{candidate.businessUnit || '待补录'}</dd></div>
                      <div><dt>种类</dt><dd>{candidate.category || '待补录'}</dd></div>
                      <div><dt>金额</dt><dd>{currency.format(minorToMajor(candidate.amountMinor))}</dd></div>
                      <div><dt>关联证据</dt><dd>{candidate.evidence.length} 份</dd></div>
                    </dl>
                    <Button
                      aria-label={`打开事项 ${candidate.shortId}`}
                      size="1"
                      variant="soft"
                      onClick={() => onOpenCandidate(candidate)}
                    >
                      打开事项<CaretRight size={14} />
                    </Button>
                  </article>
                )
              })}
              {workbookItems.length === 0 ? (
                <div className="empty-state compact-empty original-reconciliation-empty">
                  <ListChecks size={30} />
                  <h3>本月 Excel 尚未迁入 Core</h3>
                  <p>工作簿里有数据并不等于系统已经导入；完成复核映射和受控导入后，事项才会出现在这里。</p>
                </div>
              ) : null}
              {data.sources.map((source) => (
                <article key={`${source.source_kind}:${source.source_system}`}>
                  <div className="original-business-item-title">
                    <Badge color={source.source_kind === 'POSTED_LEDGER' ? 'green' : source.source_kind === 'ACCOUNT_STATEMENT' ? 'gray' : 'blue'}>
                      {source.source_kind === 'POSTED_LEDGER' ? '正式入账' : source.source_kind === 'ACCOUNT_STATEMENT' ? '账户材料' : '已确认候选'}
                    </Badge>
                    <strong>{source.source_label ?? source.source_system}</strong>
                    {source.source_label ? <small>{source.source_system}</small> : null}
                  </div>
                  <dl>
                    <div><dt>事项数量</dt><dd>{source.fact_count} 条</dd></div>
                    <div><dt>已归类</dt><dd>{source.mapped_fact_count} 条</dd></div>
                    <div><dt>合计金额</dt><dd>{currency.format(minorToMajor(source.amount_minor))}</dd></div>
                  </dl>
                  <Button
                    size="1"
                    variant="soft"
                    color="gray"
                    onClick={() => onNavigate(source.source_kind === 'POSTED_LEDGER' ? 'company-reports' : source.source_kind === 'ACCOUNT_STATEMENT' ? 'files' : 'review')}
                  >
                    {source.source_kind === 'POSTED_LEDGER' ? '查看正式报表' : source.source_kind === 'ACCOUNT_STATEMENT' ? '查看关联材料' : '审核与归类'}<CaretRight size={14} />
                  </Button>
                </article>
              ))}
              {empty && workbookItems.length === 0 ? (
                <div className="empty-state compact-empty original-reconciliation-empty">
                  <ListChecks size={30} />
                  <h3>Core 也没有本月对账事实</h3>
                  <p>候选事项、账户流水和正式入账均为空，不能据此生成月度投影。</p>
                </div>
              ) : null}
            </div>

            <div className="original-workflow-todos">
              <div className="original-workflow-subheading">
                <div><h3>待补录与审核</h3><p>只展示真正需要处理的缺口；已导入数据不需要重复填写。</p></div>
                <span title={`${data.taxonomy_version} · ${data.layout_version} · ${data.mapping_version}`}>规则版本可追溯</span>
              </div>
              <div className="original-todo-list">
                {data.pending_review_count > 0 ? <article><Warning size={18} /><div><strong>{data.pending_review_count} 条交易待审核</strong><span>补入营业单元、种类、金额和说明后确认</span></div></article> : null}
                {data.confirmed_pending_posting_count > 0 ? <article><ClockCounterClockwise size={18} /><div><strong>{data.confirmed_pending_posting_count} 条已确认待入账</strong><span>已完成分类，不需要重复审核</span></div></article> : null}
                {data.missing_material_count > 0 ? <article><Paperclip size={18} /><div><strong>{data.missing_material_count} 份关联材料待补</strong><span>从文件与连接页导入账单或凭证</span></div></article> : null}
                {data.unmapped_confirmed_count > 0 ? <article><Info size={18} /><div><strong>{data.unmapped_confirmed_count} 条已确认事项待归类</strong><span>完成经济种类和归属后自动进入汇总</span></div></article> : null}
                {gapLabels.map((label) => <article key={label}><Warning size={18} /><div><strong>{label}</strong><span>需在待审核事项中补入对应字段</span></div></article>)}
                {data.projection_gaps.includes('MISSING_TIME_GRANULARITY') ? <article><ClockCounterClockwise size={18} /><div><strong>月内日期待补</strong><span>需核对具体日期，不从摘要猜测</span></div></article> : null}
                {data.projection_gaps.includes('MISSING_BUSINESS_UNIT_ATTRIBUTION') ? <article><Bank size={18} /><div><strong>公司或门店归属待补</strong><span>补入历史归属后进入对应公司报表</span></div></article> : null}
              </div>
              <div className="original-workflow-footer-actions">
                <Button variant="outline" color="gray" onClick={() => onNavigate('files')}><Paperclip size={16} />查看文件与连接</Button>
                <Button variant="outline" color="gray" onClick={() => onNavigate('reconciliation')}><Table size={16} />查看月度对账</Button>
              </div>
            </div>
          </section>
        </>
      ) : null}
    </>
  )
}
