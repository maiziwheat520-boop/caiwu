import { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import {
  ArrowDown,
  ArrowUp,
  ArrowsLeftRight,
  CaretRight,
  ClockCounterClockwise,
  Info,
  ListChecks,
  Receipt,
  Warning,
} from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type { Candidate, CashReconciliation, OriginalReconciliation, Page } from '../types'
import { PageHeader } from '../shared/PagePrimitives'
import {
  currentAccountCounterpartyNote,
  historicalClassificationCorrection,
  legacyItemSourceRules,
  ORIGINAL_RECONCILIATION_SOURCE_SYSTEM,
} from './statementSourceRegistry'

type FlowKind = 'income' | 'expense' | 'current' | 'unclassified'

type ClassifiedCandidate = {
  candidate: Candidate
  flowKind: FlowKind
  signedAmountMinor: number
}

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const currentAccountCodes = new Set([
  'TRANSFER',
  'INTERNAL_TRANSFER',
  'CURRENT_ACCOUNT',
  'RELATED_PARTY',
  'RELATED_PARTY_CURRENT_ACCOUNT',
  'LOAN',
  'BORROWING',
  'REPAYMENT',
  'CAPITAL_ADVANCE',
])
const flowLabels: Record<FlowKind, string> = {
  income: '收入',
  expense: '支出',
  current: '往来款',
  unclassified: '待归类',
}

function currentMonth() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    timeZone: 'Asia/Shanghai',
  }).formatToParts(new Date())
  const year = parts.find((part) => part.type === 'year')?.value ?? '2026'
  const month = parts.find((part) => part.type === 'month')?.value ?? '01'
  return `${year}-${month}`
}

function monthLabel(month: string) {
  const [year, value] = month.split('-')
  return `${year} 年 ${Number(value)} 月`
}

function initialMonth(candidates: Candidate[]) {
  return candidates
    .filter(isOriginalReconciliationCandidate)
    .map((candidate) => candidate.accountingMonth)
    .filter((month): month is string => Boolean(month))
    .sort()
    .at(-1) ?? currentMonth()
}

function isOriginalReconciliationCandidate(candidate: Candidate) {
  return candidate.raw.source_system === ORIGINAL_RECONCILIATION_SOURCE_SYSTEM
}

// Exported for the finance-rule regression test; the page remains the only runtime consumer.
// eslint-disable-next-line react-refresh/only-export-components
export function classifyCandidate(candidate: Candidate): ClassifiedCandidate {
  const categoryCode = candidate.categoryCode.toUpperCase()
  if (currentAccountCodes.has(categoryCode)) {
    return { candidate, flowKind: 'current', signedAmountMinor: candidate.amountMinor }
  }
  if (/(^|_)INCOME($|_)/.test(categoryCode)) {
    return { candidate, flowKind: 'income', signedAmountMinor: Math.abs(candidate.amountMinor) }
  }
  if (/(^|_)EXPENSE($|_)/.test(categoryCode)) {
    return { candidate, flowKind: 'expense', signedAmountMinor: -Math.abs(candidate.amountMinor) }
  }
  return { candidate, flowKind: 'unclassified', signedAmountMinor: candidate.amountMinor }
}

function candidateStatus(candidate: Candidate) {
  switch (candidate.status) {
    case 'CONFIRMED': return { color: 'green' as const, label: '已确认' }
    case 'CONFLICTED': return { color: 'red' as const, label: '有冲突' }
    case 'INCOMPLETE': return { color: 'amber' as const, label: '待补录' }
    default: return { color: 'blue' as const, label: '待审核' }
  }
}

function candidateDate(candidate: Candidate) {
  return candidate.accountingMonth ?? '日期待补'
}

function sourceLabel(candidate: Candidate) {
  return candidate.raw.source_system || candidate.source
}

function amountLabel(item: ClassifiedCandidate) {
  const amount = currency.format(minorToMajor(Math.abs(item.signedAmountMinor)))
  if (item.flowKind === 'income') return `+${amount}`
  if (item.flowKind === 'expense') return `-${amount}`
  if (item.signedAmountMinor > 0) return `+${amount}`
  if (item.signedAmountMinor < 0) return `-${amount}`
  return amount
}

function gapLabel(gapCode: OriginalReconciliation['rows'][number]['cells'][number]['gap_code']) {
  switch (gapCode) {
    case 'MISSING_BALANCE_MAPPING': return '待补余额映射'
    case 'MISSING_ECONOMIC_EFFECT': return '待补业务性质'
    case 'POSTED_LEDGER_UNAVAILABLE': return '待接正式账簿'
    default: return '待补历史口径映射'
  }
}

export function OriginalReconciliationPage({ candidates, onNavigate, onOpenCandidate }: {
  candidates: Candidate[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const [selectedMonthOverride, setSelectedMonthOverride] = useState<string | null>(null)
  const [selectedFlow, setSelectedFlow] = useState<FlowKind>('income')
  const [data, setData] = useState<OriginalReconciliation | null>(null)
  const [cashData, setCashData] = useState<CashReconciliation | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestRef = useRef(0)
  const scopeRef = useRef<OriginalReconciliation['scope'] | null>(null)
  const selectedMonth = selectedMonthOverride ?? initialMonth(candidates)

  const load = useCallback(async () => {
    const requestId = ++requestRef.current
    setLoading(true)
    setError(null)
    try {
      const scope = scopeRef.current
      const [projection, cashProjection] = await Promise.all([
        api.getOriginalReconciliation({
          accountingMonth: selectedMonth,
          entityRef: scope?.entity_ref,
          businessUnitRef: scope?.business_unit_ref,
        }),
        api.getCashReconciliation(selectedMonth).catch(() => null),
      ])
      if (requestId !== requestRef.current) return
      scopeRef.current = projection.scope
      setData(projection)
      setCashData(cashProjection)
    } catch (loadError) {
      if (requestId !== requestRef.current) return
      setData(null)
      setCashData(null)
      setError(loadError instanceof Error ? loadError.message : '无法读取月度对账状态')
    } finally {
      if (requestId === requestRef.current) setLoading(false)
    }
  }, [selectedMonth])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const selectedMonthLabel = monthLabel(selectedMonth)
  const monthCandidates = candidates.filter((candidate) => (
    isOriginalReconciliationCandidate(candidate)
    && candidate.accountingMonth === selectedMonth
    && candidate.status !== 'IGNORED'
    && candidate.status !== 'SUPERSEDED'
  ))
  const classifiedCandidates = monthCandidates.map(classifyCandidate)
  const grouped = classifiedCandidates.reduce<Record<FlowKind, ClassifiedCandidate[]>>((result, item) => {
    result[item.flowKind].push(item)
    return result
  }, { income: [], expense: [], current: [], unclassified: [] })
  const selectedItems = grouped[selectedFlow]
  const selectedCashRows = cashData?.rows.filter((row) => row.flow_kind === selectedFlow.toUpperCase()) ?? []
  const laneDefinitions = [
    { kind: 'income' as const, label: '收入', detail: '经营流入', icon: <ArrowDown size={19} /> },
    { kind: 'expense' as const, label: '支出', detail: '经营流出', icon: <ArrowUp size={19} /> },
    { kind: 'current' as const, label: '往来款', detail: '不计损益', icon: <ArrowsLeftRight size={19} /> },
  ]
  const incomeSourceRuleCount = legacyItemSourceRules.filter((rule) => rule.flowKind === 'income').length
  const expenseSourceRuleCount = legacyItemSourceRules.filter((rule) => rule.flowKind === 'expense').length
  const gapLabels = data
    ? Array.from(new Set(data.rows.flatMap((row) => row.cells
      .filter((cell) => cell.kind === 'GAP')
      .map((cell) => gapLabel(cell.gap_code)))))
    : []
  const hasProjectionTodos = Boolean(data && (
    data.pending_review_count
    || data.confirmed_pending_posting_count
    || data.missing_material_count
    || data.unmapped_confirmed_count
    || gapLabels.length
    || data.projection_gaps.length
  ))

  return (
    <>
      <PageHeader
        eyebrow="月度对账"
        title="收支与往来对账"
        description="导入银行和微信流水后，系统按固定规则直接生成旧对账表已有项目。"
        action={(
          <div className="original-reconciliation-filters">
            <input
              aria-label="选择对账月份"
              className="original-month-input"
              max="9999-12"
              min="2000-01"
              type="month"
              value={selectedMonth}
              onChange={(event) => {
                if (!event.target.value) return
                setData(null)
                setSelectedFlow('income')
                setSelectedMonthOverride(event.target.value)
              }}
            />
            <span className="scope-chip" title={data?.scope.entity_ref ?? undefined}>
              {data
                ? `授权范围：${data.scope.business_unit_ref}`
                : loading
                  ? '正在确认公司 / 门店范围'
                  : '公司 / 门店范围待确认'}
            </span>
          </div>
        )}
      />

      <section className="statement-source-notice" aria-label="账单数据入口状态">
        <Receipt size={20} />
        <div>
          <strong>流水是对账表的直接取数来源</strong>
          <span>按实际到账的自然月归类；不再上传平台账单、不重建七天账期，也不与平台金额比较。</span>
        </div>
      </section>

      <section className="panel statement-workbench" aria-label="收支与往来事项">
        <div className="panel-heading statement-workbench-heading">
          <div>
            <h2>{selectedMonthLabel}</h2>
            <p>{monthCandidates.length} 笔已按落库规则归类的旧表项目</p>
          </div>
            <Button onClick={() => onNavigate('review')}><ListChecks size={16} />查看未识别流水</Button>
        </div>

        <div className="statement-flow-tabs" role="tablist" aria-label="业务性质">
          {laneDefinitions.map((lane) => {
            const laneItems = grouped[lane.kind]
            const cashRows = cashData?.rows.filter((row) => row.flow_kind === lane.kind.toUpperCase()) ?? []
            const amountMinor = cashRows.length
              ? cashRows.reduce((total, row) => total + row.amount_minor, 0)
              : laneItems.reduce((total, item) => total + Math.abs(item.signedAmountMinor), 0)
            const itemCount = cashRows.length
              ? cashRows.reduce((total, row) => total + row.transaction_count, 0)
              : laneItems.length
            const active = selectedFlow === lane.kind
            return (
              <button
                key={lane.kind}
                id={`statement-flow-${lane.kind}`}
                aria-controls="statement-flow-panel"
                aria-selected={active}
                className={`${active ? 'active' : ''} ${lane.kind}`}
                role="tab"
                type="button"
                onClick={() => setSelectedFlow(lane.kind)}
              >
                <span className="statement-flow-icon">{lane.icon}</span>
                <span className="statement-flow-copy"><strong>{lane.label}</strong><small>{lane.detail}</small></span>
                <span className="statement-flow-value"><strong>{currency.format(minorToMajor(amountMinor))}</strong><small>{itemCount} 笔</small></span>
              </button>
            )
          })}
        </div>

        <div className="current-account-rule"><Info size={17} /><span>往来款不计入收入或支出</span></div>

        {grouped.unclassified.length > 0 ? (
          <div className="unclassified-alert">
            <Warning size={18} />
            <div><strong>{grouped.unclassified.length} 笔事项无法确认业务性质</strong><span>这些金额不会进入收入、支出或往来款合计。</span></div>
            <button type="button" aria-label={`查看 ${grouped.unclassified.length} 笔待归类事项`} onClick={() => setSelectedFlow('unclassified')}>查看待归类<CaretRight size={14} /></button>
          </div>
        ) : null}

        <div
          id="statement-flow-panel"
          className="statement-flow-panel"
          role="tabpanel"
          aria-label={selectedFlow === 'unclassified' ? '待归类事项' : undefined}
          aria-labelledby={selectedFlow === 'unclassified' ? undefined : `statement-flow-${selectedFlow}`}
        >
          <div className="statement-flow-panel-heading">
            <div><h3>{flowLabels[selectedFlow]}</h3><p>{selectedFlow === 'current' ? '核对往来双方、资金性质和对应账单' : selectedFlow === 'unclassified' ? '补充业务性质后再进入财务合计' : `核对${flowLabels[selectedFlow]}来源、归属和金额`}</p></div>
            <Badge color={selectedFlow === 'unclassified' ? 'amber' : selectedFlow === 'income' ? 'green' : selectedFlow === 'expense' ? 'red' : 'blue'}>{selectedItems.length} 笔</Badge>
          </div>

          <div className="statement-item-list">
            {selectedCashRows.map((row) => (
              <article key={row.rule_key} className={`statement-item ${selectedFlow}`}>
                <div className="statement-item-status"><Badge color="green">自动生成</Badge><span>{selectedMonth}</span></div>
                <div className="statement-item-main"><strong>{row.item_label}</strong><span>{row.transaction_count} 笔实收流水</span></div>
                <div className="statement-item-context"><span>{row.business_unit_label}</span><small>{row.source_kind === 'BANK_TRANSACTION' ? '银行流水' : '微信流水'}</small></div>
                <div className="statement-item-amount"><strong>{selectedFlow === 'expense' ? '-' : selectedFlow === 'income' ? '+' : ''}{currency.format(minorToMajor(row.amount_minor))}</strong><span>{row.rule_key}</span></div>
              </article>
            ))}
            {selectedCashRows.length === 0 ? selectedItems.map((item) => {
              const status = candidateStatus(item.candidate)
              return (
                <article key={item.candidate.id} className={`statement-item ${item.flowKind}`}>
                  <div className="statement-item-status">
                    <Badge color={status.color}>{status.label}</Badge>
                    <span>{candidateDate(item.candidate)}</span>
                  </div>
                  <div className="statement-item-main">
                    <strong>{item.candidate.summary}</strong>
                    <span>{item.candidate.category || '种类待补'}</span>
                    {item.flowKind === 'unclassified' ? <small>无法确认业务性质</small> : null}
                  </div>
                  <div className="statement-item-context">
                    <span>{item.candidate.businessUnit || '公司 / 门店待补'}</span>
                    <small>{sourceLabel(item.candidate)}</small>
                  </div>
                  <div className="statement-item-amount">
                    <strong>{amountLabel(item)}</strong>
                    <span>{item.candidate.shortId}</span>
                  </div>
                  <Button
                    aria-label={`打开事项 ${item.candidate.shortId}`}
                    size="1"
                    variant="soft"
                    onClick={() => onOpenCandidate(item.candidate)}
                  >
                    打开<CaretRight size={14} />
                  </Button>
                </article>
              )
            }) : null}
            {selectedCashRows.length === 0 && selectedItems.length === 0 ? (
              <div className="empty-state compact-empty statement-empty">
                <Receipt size={30} />
                <h3>{monthCandidates.length === 0 ? '本月还没有已映射的旧表事项' : `本月没有${flowLabels[selectedFlow]}事项`}</h3>
                <p>{monthCandidates.length === 0 ? '本月尚未导入可命中规则的银行或微信流水。' : '切换上方业务性质查看本月其他事项。'}</p>
              </div>
            ) : null}
          </div>
        </div>
      </section>

      {error ? (
        <section className="projection-state-alert" role="alert">
          <Warning size={18} />
          <div><strong>月度对账状态暂不可用</strong><span>{error}。上方账单事项仍可继续查看。</span></div>
          <Button size="1" variant="soft" color="gray" onClick={() => void load()}>重试</Button>
        </section>
      ) : null}

      <section className="panel statement-source-registry" aria-label="旧表项目取数来源">
        <div className="panel-heading">
          <div><h2>自动取数规则</h2><p>规则保存在系统中，只把明确命中的流水写入旧表项目</p></div>
          <Badge color="green">收入 {incomeSourceRuleCount} · 支出 {expenseSourceRuleCount}</Badge>
        </div>
        <div className="statement-source-registry-list">
          <div className="statement-source-registry-labels" aria-hidden="true"><span>主体</span><span>旧表项目</span><span>取数或权威来源</span></div>
          {legacyItemSourceRules.map((rule) => (
            <article key={`${rule.businessUnit}:${rule.businessSource}:${rule.statementAccount}`}>
              <strong>{rule.businessUnit}</strong>
              <span>{flowLabels[rule.flowKind]} · {rule.businessSource}</span>
              <span>{rule.statementAccount}</span>
            </article>
          ))}
        </div>
        <div className="current-account-registry-note">
          <ArrowsLeftRight size={18} />
          <div><strong>往来款重点对象</strong><span>{currentAccountCounterpartyNote}</span></div>
          <Badge color="amber">账户与例外待确认</Badge>
        </div>
        <div className="current-account-registry-note">
          <Info size={18} />
          <div><strong>历史口径校正</strong><span>{historicalClassificationCorrection}</span></div>
          <Badge color="blue">网页核对口径</Badge>
        </div>
      </section>

      {data && hasProjectionTodos ? (
        <section className="panel original-workflow-todos statement-todos" aria-label="对账待办">
          <div className="original-workflow-subheading">
            <div><h3>对账待办</h3><p>只显示需要补充或复核的事项</p></div>
            <span title={`${data.taxonomy_version} | ${data.layout_version} | ${data.mapping_version}`}>规则版本可追溯</span>
          </div>
          <div className="original-todo-list">
            {data.pending_review_count > 0 ? <article><Warning size={18} /><div><strong>{data.pending_review_count} 条交易待审核</strong><span>核对公司 / 门店、业务性质、金额和说明</span></div></article> : null}
            {data.confirmed_pending_posting_count > 0 ? <article><ClockCounterClockwise size={18} /><div><strong>{data.confirmed_pending_posting_count} 条已确认待入账</strong><span>分类已完成，不需要重复审核</span></div></article> : null}
            {data.missing_material_count > 0 ? <article><Receipt size={18} /><div><strong>{data.missing_material_count} 份对应账单待补</strong><span>根据原账单来源补充银行或平台导出文件</span></div></article> : null}
            {data.unmapped_confirmed_count > 0 ? <article><Info size={18} /><div><strong>{data.unmapped_confirmed_count} 条已确认事项待归类</strong><span>确认业务性质和归属后进入对应合计</span></div></article> : null}
            {gapLabels.map((label) => <article key={label}><Warning size={18} /><div><strong>{label}</strong><span>需要在待审核事项中补充对应字段</span></div></article>)}
            {data.projection_gaps.includes('MISSING_TIME_GRANULARITY') ? <article><ClockCounterClockwise size={18} /><div><strong>月内日期待补</strong><span>核对具体日期，不从摘要推测</span></div></article> : null}
            {data.projection_gaps.includes('MISSING_BUSINESS_UNIT_ATTRIBUTION') ? <article><Warning size={18} /><div><strong>公司或门店归属待补</strong><span>确认归属后进入对应主体汇总</span></div></article> : null}
          </div>
          <div className="original-workflow-footer-actions">
            <Button variant="outline" color="gray" onClick={() => onNavigate('reconciliation')}>查看月度对账</Button>
          </div>
        </section>
      ) : null}
    </>
  )
}
