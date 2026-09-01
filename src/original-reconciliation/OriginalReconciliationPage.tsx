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
import type { Candidate, OriginalReconciliation, Page } from '../types'
import { PageHeader } from '../shared/PagePrimitives'
import {
  currentAccountCounterpartyNote,
  historicalClassificationCorrection,
  statementSourceRules,
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
const currentAccountTerms = /(内部往来|往来款|关联往来|股东往来|分红|利润分配|转账|余额互转|账户互转|资金调拨|借款|还款|垫付款|充值|提现|陈展武|林素美|老爸|老妈)/
const explicitIncomeTerms = /(文杰房租)/
const explicitExpenseTerms = /(消杀|工资|薪资|薪酬)/
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
    .map((candidate) => candidate.accountingMonth)
    .filter((month): month is string => Boolean(month))
    .sort()
    .at(-1) ?? currentMonth()
}

function summaryFields(candidate: Candidate) {
  return candidate.summary.split('|').map((value) => value.trim())
}

// Exported for the finance-rule regression test; the page remains the only runtime consumer.
// eslint-disable-next-line react-refresh/only-export-components
export function classifyCandidate(candidate: Candidate): ClassifiedCandidate {
  const fields = summaryFields(candidate)
  const direction = fields[2]
  const transactionType = fields[3] ?? ''
  const categoryCode = candidate.categoryCode.toUpperCase()
  const classificationText = `${candidate.category} ${categoryCode} ${transactionType} ${candidate.summary}`
  const riskCodes = new Set(candidate.reviewRisks.map((risk) => risk.code))
  const isCurrentAccount = currentAccountCodes.has(categoryCode)
    || currentAccountTerms.test(classificationText)
    || riskCodes.has('RELATED_ACCOUNT_STATEMENT_REQUIRED')
    || riskCodes.has('TRANSFER_REVIEW_REQUIRED')

  if (explicitIncomeTerms.test(classificationText)) {
    return { candidate, flowKind: 'income', signedAmountMinor: Math.abs(candidate.amountMinor) }
  }
  if (explicitExpenseTerms.test(classificationText)) {
    return { candidate, flowKind: 'expense', signedAmountMinor: -Math.abs(candidate.amountMinor) }
  }
  if (isCurrentAccount) {
    const signedAmountMinor = direction === '收入'
      ? Math.abs(candidate.amountMinor)
      : direction === '支出'
        ? -Math.abs(candidate.amountMinor)
        : candidate.amountMinor
    return { candidate, flowKind: 'current', signedAmountMinor }
  }
  if (direction === '收入' || /(^|_)INCOME($|_)/.test(categoryCode)) {
    return { candidate, flowKind: 'income', signedAmountMinor: Math.abs(candidate.amountMinor) }
  }
  if (direction === '支出' || /(^|_)EXPENSE($|_)/.test(categoryCode)) {
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
  const date = summaryFields(candidate)[1]
  return /^\d{4}-\d{2}-\d{2}$/.test(date ?? '') ? date : candidate.accountingMonth ?? '日期待补'
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
    candidate.accountingMonth === selectedMonth
    && candidate.status !== 'IGNORED'
    && candidate.status !== 'SUPERSEDED'
  ))
  const classifiedCandidates = monthCandidates.map(classifyCandidate)
  const grouped = classifiedCandidates.reduce<Record<FlowKind, ClassifiedCandidate[]>>((result, item) => {
    result[item.flowKind].push(item)
    return result
  }, { income: [], expense: [], current: [], unclassified: [] })
  const selectedItems = grouped[selectedFlow]
  const laneDefinitions = [
    { kind: 'income' as const, label: '收入', detail: '经营流入', icon: <ArrowDown size={19} /> },
    { kind: 'expense' as const, label: '支出', detail: '经营流出', icon: <ArrowUp size={19} /> },
    { kind: 'current' as const, label: '往来款', detail: '不计损益', icon: <ArrowsLeftRight size={19} /> },
  ]
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
        description="按业务性质核对每一笔账单事项。收入、支出影响经营结果，往来款单独核对且不计损益。"
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
          <strong>当前数据入口：银行与平台导出的账单</strong>
          <span>旧截图和历史表格提交已暂停。账单来源与业务性质分开记录。</span>
        </div>
      </section>

      <section className="panel statement-workbench" aria-label="收支与往来事项">
        <div className="panel-heading statement-workbench-heading">
          <div>
            <h2>{selectedMonthLabel}</h2>
            <p>{monthCandidates.length} 笔有效账单事项，选择业务性质后逐笔核对</p>
          </div>
          <Button onClick={() => onNavigate('review')}><ListChecks size={16} />前往待审核</Button>
        </div>

        <div className="statement-flow-tabs" role="tablist" aria-label="业务性质">
          {laneDefinitions.map((lane) => {
            const laneItems = grouped[lane.kind]
            const amountMinor = laneItems.reduce((total, item) => total + Math.abs(item.signedAmountMinor), 0)
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
                <span className="statement-flow-value"><strong>{currency.format(minorToMajor(amountMinor))}</strong><small>{laneItems.length} 笔</small></span>
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
            {selectedItems.map((item) => {
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
            })}
            {selectedItems.length === 0 ? (
              <div className="empty-state compact-empty statement-empty">
                <Receipt size={30} />
                <h3>{monthCandidates.length === 0 ? '本月还没有可核对的账单记录' : `本月没有${flowLabels[selectedFlow]}事项`}</h3>
                <p>{monthCandidates.length === 0 ? '后续接入银行与平台导出的账单后，记录会按业务性质显示在这里。' : '切换上方业务性质查看本月其他事项。'}</p>
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

      <section className="panel statement-source-registry" aria-label="已确认账单来源">
        <div className="panel-heading">
          <div><h2>已确认账单来源</h2><p>账单账户只说明去哪里取数，业务性质仍按每笔交易单独判断</p></div>
          <Badge color="green">收入 {statementSourceRules.length} 条</Badge>
        </div>
        <div className="statement-source-registry-list">
          <div className="statement-source-registry-labels" aria-hidden="true"><span>主体</span><span>收入来源</span><span>对应账单</span></div>
          {statementSourceRules.map((rule) => (
            <article key={`${rule.businessUnit}:${rule.businessSource}:${rule.statementAccount}`}>
              <strong>{rule.businessUnit}</strong>
              <span>{rule.businessSource}</span>
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
          <Badge color="blue">导入时强制采用</Badge>
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
