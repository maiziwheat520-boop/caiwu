import { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import {
  ArrowDown,
  ArrowUp,
  ArrowsLeftRight,
  CaretRight,
  CheckCircle,
  ClockCounterClockwise,
  DownloadSimple,
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
  legacyItemSourceRules,
  ORIGINAL_RECONCILIATION_SOURCE_SYSTEM,
} from './statementSourceRegistry'
import {
  buildOriginalReviewCsv,
  buildOriginalWorkflowSummary,
  type OriginalWorkflowItem,
} from './workflowState'
import './OriginalReconciliationPage.css'

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
    isOriginalReconciliationCandidate(candidate)
    && candidate.accountingMonth === selectedMonth
    && candidate.status !== 'IGNORED'
    && candidate.status !== 'SUPERSEDED'
  ))
  const classifiedCandidates = monthCandidates.map(classifyCandidate)
  const workflowItems: OriginalWorkflowItem[] = classifiedCandidates
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
  const workflow = buildOriginalWorkflowSummary(workflowItems, data)

  const exportReviewList = () => {
    const csv = buildOriginalReviewCsv(selectedMonth, workflowItems)
    const blob = new Blob(['\uFEFF', csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `原口径对账-${selectedMonth}-审核清单.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <>
      <PageHeader
        eyebrow="月度对账"
        title="收支与往来对账"
        description="只核对旧 Excel 原有项目。收入、支出影响经营结果，往来款单独核对且不计损益。"
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
          <strong>账单只用于旧表项目取数和复核</strong>
          <span>普通收支、采购、实际报销和银行工资候选不进入本页；旧截图和历史表格提交仍暂停。</span>
        </div>
      </section>

      <section className="panel original-workflow-progress" aria-label="本月对账流程">
        <div className="original-workflow-progress-heading">
          <div>
            <h2>本月对账流程</h2>
            <p>逐项显示事项来源、业务归类、账户匹配、凭证、审核和月度闭环状态。</p>
          </div>
          <Badge color={workflow.closeReady ? 'green' : 'amber'}>
            {workflow.closeReady ? '已闭环' : `${workflow.blockers.length} 项阻断`}
          </Badge>
        </div>
        <div className="original-workflow-stage-list">
          {workflow.stages.map((stage) => (
            <article className={`original-workflow-stage ${stage.state.toLowerCase()}`} key={stage.id}>
              <header>
                <strong>{stage.label}</strong>
                <Badge color={stage.state === 'COMPLETE' ? 'green' : stage.state === 'PAUSED' ? 'gray' : 'amber'}>
                  {stage.state === 'COMPLETE' ? '完成' : stage.state === 'PAUSED' ? '已暂停' : '待处理'}
                </Badge>
              </header>
              <p>{stage.detail}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="panel statement-workbench" aria-label="收支与往来事项">
        <div className="panel-heading statement-workbench-heading">
          <div>
            <h2>{selectedMonthLabel}</h2>
            <p>{monthCandidates.length} 笔由 Core 识别的旧表事项，选择业务性质后逐笔核对</p>
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
                    <div className="statement-item-evidence">
                      <Receipt size={13} />
                      <span>{item.candidate.evidence.length > 0 ? `已关联 ${item.candidate.evidence.length} 份凭证` : '待关联凭证'}</span>
                      {item.candidate.evidence.length > 0 ? (
                        <small title={item.candidate.evidence.map((evidence) => evidence.original_filename ?? evidence.id).join('、')}>
                          {item.candidate.evidence.map((evidence) => evidence.original_filename ?? evidence.id).join('、')}
                        </small>
                      ) : null}
                    </div>
                  </div>
                  <div className="statement-item-amount">
                    <strong>{amountLabel(item)}</strong>
                    <span>{item.candidate.shortId}</span>
                  </div>
                  <Button
                    aria-label={`核对事项与凭证 ${item.candidate.shortId}`}
                    size="1"
                    variant="soft"
                    onClick={() => onOpenCandidate(item.candidate)}
                  >
                    核对事项与凭证<CaretRight size={14} />
                  </Button>
                </article>
              )
            })}
            {selectedItems.length === 0 ? (
              <div className="empty-state compact-empty statement-empty">
                <Receipt size={30} />
                <h3>{monthCandidates.length === 0 ? '本月还没有已映射的旧表事项' : `本月没有${flowLabels[selectedFlow]}事项`}</h3>
                <p>{monthCandidates.length === 0 ? '只有 Core 受控来源中识别为旧表事项的项目会显示；整份银行或平台账单不会自动进入。' : '切换上方业务性质查看本月其他事项。'}</p>
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
          <div><h2>旧表项目取数来源</h2><p>这些来源只给旧表已有项目取数，不代表该账户其他交易也进入本页</p></div>
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
        <div className="statement-account-contract-note" role="status">
          <Warning size={18} />
          <div>
            <strong>取数规则已登记，逐笔账户尚未绑定</strong>
            <span>当前事项只有 Core 类别，没有稳定旧表项目编号和账户引用；本页不会按摘要、金额或文件名猜测账户。</span>
          </div>
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

      <section className="panel original-close-panel" aria-label="月度闭环">
        <div className="original-close-heading">
          <div>
            <h2>{selectedMonthLabel}闭环检查</h2>
            <p>{workflow.closeReady ? '本月事项、凭证、账户匹配与 Core 完整性均已验证。' : '阻断项清零前不显示已关账；本页也不会用导出文件代替正式月结。'}</p>
          </div>
          <div className="original-close-actions">
            <Button variant="outline" color="gray" disabled={workflow.itemCount === 0} onClick={exportReviewList}>
              <DownloadSimple size={16} />导出本月审核清单
            </Button>
            <Button variant="soft" color="gray" onClick={() => onNavigate('review')}>
              <ListChecks size={16} />处理待审核事项
            </Button>
          </div>
        </div>
        {workflow.closeReady ? (
          <div className="current-account-rule"><CheckCircle size={17} /><span>本月已完成正式闭环</span></div>
        ) : (
          <ul className="original-close-blockers">
            {workflow.blockers.map((blocker) => <li key={blocker}><Warning size={15} /><span>{blocker}</span></li>)}
          </ul>
        )}
      </section>
    </>
  )
}
