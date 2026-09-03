import { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button } from '@radix-ui/themes'
import {
  ArrowDown,
  ArrowUp,
  ArrowsLeftRight,
  ClockCounterClockwise,
  Info,
  ListChecks,
  Receipt,
  Warning,
} from '@phosphor-icons/react'
import { api, minorToMajor } from '../api'
import type { CashReconciliation, OriginalReconciliation, Page } from '../types'
import { PageHeader } from '../shared/PagePrimitives'
import {
  currentAccountCounterpartyNote,
  historicalClassificationCorrection,
} from './statementSourceRegistry'

type FlowKind = 'income' | 'expense' | 'current'

const currency = new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' })
const flowLabels: Record<FlowKind, string> = {
  income: '收入',
  expense: '支出',
  current: '往来款',
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

function gapLabel(gapCode: OriginalReconciliation['rows'][number]['cells'][number]['gap_code']) {
  switch (gapCode) {
    case 'MISSING_BALANCE_MAPPING': return '待补余额映射'
    case 'MISSING_ECONOMIC_EFFECT': return '待补业务性质'
    case 'POSTED_LEDGER_UNAVAILABLE': return '待接正式账簿'
    default: return '待补历史口径映射'
  }
}

export function OriginalReconciliationPage({ onNavigate }: {
  onNavigate: (page: Page) => void
}) {
  const [selectedMonthOverride, setSelectedMonthOverride] = useState<string | null>(null)
  const [selectedFlow, setSelectedFlow] = useState<FlowKind>('income')
  const [data, setData] = useState<OriginalReconciliation | null>(null)
  const [cashData, setCashData] = useState<CashReconciliation | null>(null)
  const [loading, setLoading] = useState(false)
  const [cashError, setCashError] = useState<string | null>(null)
  const [projectionWarning, setProjectionWarning] = useState<string | null>(null)
  const requestRef = useRef(0)
  const scopeRef = useRef<OriginalReconciliation['scope'] | null>(null)
  const selectedMonth = selectedMonthOverride ?? currentMonth()

  const load = useCallback(async () => {
    const requestId = ++requestRef.current
    setLoading(true)
    setCashError(null)
    setProjectionWarning(null)
    const scope = scopeRef.current
    const [projectionResult, cashResult] = await Promise.allSettled([
        api.getOriginalReconciliation({
          accountingMonth: selectedMonth,
          entityRef: scope?.entity_ref,
          businessUnitRef: scope?.business_unit_ref,
        }),
        api.getCashReconciliation(selectedMonth),
      ] as const)
    if (requestId !== requestRef.current) return
    if (projectionResult.status === 'fulfilled') {
      scopeRef.current = projectionResult.value.scope
      setData(projectionResult.value)
    } else {
      setData(null)
      setProjectionWarning('旧口径补充待办暂不可用')
    }
    if (cashResult.status === 'fulfilled') {
      setCashData(cashResult.value)
    } else {
      setCashData(null)
      setCashError(
        cashResult.reason instanceof Error
          ? cashResult.reason.message
          : '无法读取规则生成的月度对账',
      )
    }
    setLoading(false)
  }, [selectedMonth])

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0)
    return () => window.clearTimeout(timer)
  }, [load])

  const selectedMonthLabel = monthLabel(selectedMonth)
  const selectedCashRows = cashData?.rows.filter((row) => (
    row.flow_kind === selectedFlow.toUpperCase() && row.transaction_count > 0
  )) ?? []
  const laneDefinitions = [
    { kind: 'income' as const, label: '收入', detail: '经营流入', icon: <ArrowDown size={19} /> },
    { kind: 'expense' as const, label: '支出', detail: '经营流出', icon: <ArrowUp size={19} /> },
    { kind: 'current' as const, label: '往来款', detail: '不计损益', icon: <ArrowsLeftRight size={19} /> },
  ]
  const incomeSourceRuleCount = cashData?.rules.filter((rule) => rule.flow_kind === 'INCOME').length ?? 0
  const expenseSourceRuleCount = cashData?.rules.filter((rule) => rule.flow_kind === 'EXPENSE').length ?? 0
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
                setCashData(null)
                setSelectedFlow('income')
                setSelectedMonthOverride(event.target.value)
              }}
            />
            <span className="scope-chip">
              {cashData
                ? '授权范围：全部已授权主体'
                : loading
                  ? '正在确认授权范围'
                  : '授权范围待确认'}
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
            <p>{cashData?.matched_fact_count ?? 0} 笔流水唯一命中旧表项目规则</p>
          </div>
            <Button onClick={() => onNavigate('review')}><ListChecks size={16} />查看未识别流水</Button>
        </div>

        <div className="statement-flow-tabs" role="tablist" aria-label="业务性质">
          {laneDefinitions.map((lane) => {
            const cashRows = cashData?.rows.filter((row) => row.flow_kind === lane.kind.toUpperCase()) ?? []
            const amountMinor = cashRows.reduce((total, row) => total + row.amount_minor, 0)
            const itemCount = cashRows.reduce((total, row) => total + row.transaction_count, 0)
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

        <div
          id="statement-flow-panel"
          className="statement-flow-panel"
          role="tabpanel"
          aria-labelledby={`statement-flow-${selectedFlow}`}
        >
          <div className="statement-flow-panel-heading">
            <div><h3>{flowLabels[selectedFlow]}</h3><p>{selectedFlow === 'current' ? '核对往来双方、资金性质和对应账单' : `核对${flowLabels[selectedFlow]}来源、归属和金额`}</p></div>
            <Badge color={selectedFlow === 'income' ? 'green' : selectedFlow === 'expense' ? 'red' : 'blue'}>{selectedCashRows.reduce((total, row) => total + row.transaction_count, 0)} 笔</Badge>
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
            {selectedCashRows.length === 0 ? (
              <div className="empty-state compact-empty statement-empty">
                <Receipt size={30} />
                <h3>{cashData ? `本月没有${flowLabels[selectedFlow]}事项` : '规则生成结果暂不可用'}</h3>
                <p>{cashData ? '切换上方业务性质查看本月其他事项。' : '系统不会使用旧候选分类代替正式流水规则结果。'}</p>
              </div>
            ) : null}
          </div>
        </div>
      </section>

      {cashError ? (
        <section className="projection-state-alert" role="alert">
          <Warning size={18} />
          <div><strong>规则生成结果暂不可用</strong><span>{cashError}。为避免错用旧口径，当前不显示替代金额。</span></div>
          <Button size="1" variant="soft" color="gray" onClick={() => void load()}>重试</Button>
        </section>
      ) : null}

      {cashData && cashData.issue_count > 0 ? (
        <section className="panel reconciliation-issues" aria-label="规则缺口与冲突">
          <div className="panel-heading">
            <div><h2>规则缺口与冲突</h2><p>以下流水不会进入收入、支出或往来款合计</p></div>
            <Badge color={cashData.conflicted_fact_count > 0 ? 'red' : 'amber'}>
              未命中 {cashData.unmatched_fact_count} · 冲突 {cashData.conflicted_fact_count}
            </Badge>
          </div>
          <div className="reconciliation-issue-list">
            {cashData.issues.map((issue) => (
              <article key={`${issue.source_kind}:${issue.fact_ref}`} className={issue.issue_kind === 'MULTIPLE_RULES' ? 'conflict' : ''}>
                <div>
                  <Badge color={issue.issue_kind === 'MULTIPLE_RULES' ? 'red' : 'amber'}>
                    {issue.issue_kind === 'MULTIPLE_RULES' ? '多规则冲突' : '未命中规则'}
                  </Badge>
                  <strong>{issue.occurred_on}</strong>
                </div>
                <span>{issue.source_kind === 'BANK_TRANSACTION' ? '银行流水' : '微信流水'} · <code>{issue.fact_ref}</code></span>
                <strong>{currency.format(minorToMajor(issue.amount_minor))}</strong>
                {issue.matched_rule_keys.length > 0 ? <small>{issue.matched_rule_keys.join('、')}</small> : null}
              </article>
            ))}
          </div>
          {cashData.issues_truncated ? <p className="projection-note">仅显示前 500 条；总计 {cashData.issue_count} 条。</p> : null}
          <p className="projection-note">银行流水请按完整标识复核；微信流水可进入待审核继续处理。</p>
          <Button variant="outline" color="gray" onClick={() => onNavigate('review')}>查看微信待审核</Button>
        </section>
      ) : null}

      {projectionWarning ? (
        <section className="projection-state-alert" role="status">
          <Info size={18} />
          <div><strong>{projectionWarning}</strong><span>规则生成金额不受影响，补充材料和历史映射待办暂不显示。</span></div>
        </section>
      ) : null}

      <section className="panel statement-source-registry" aria-label="旧表项目取数来源">
        <div className="panel-heading">
          <div><h2>自动取数规则</h2><p>规则保存在系统中，只把明确命中的流水写入旧表项目</p></div>
          <Badge color="green">收入 {incomeSourceRuleCount} · 支出 {expenseSourceRuleCount}</Badge>
        </div>
        <div className="statement-source-registry-list">
          <div className="statement-source-registry-labels" aria-hidden="true"><span>主体</span><span>旧表项目</span><span>取数或权威来源</span></div>
          {cashData?.rules.map((rule) => (
            <article key={rule.rule_key}>
              <strong>{rule.business_unit_label}</strong>
              <span>{flowLabels[rule.flow_kind.toLowerCase() as FlowKind]} · {rule.item_label}</span>
              <span>
                {rule.source_kind === 'BANK_TRANSACTION' ? '银行' : '微信'} · {rule.source_ref} · {rule.amount_direction}
                <small>匹配：{rule.match_pattern} · {rule.effective_from} 起</small>
              </span>
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
            <Button variant="outline" color="gray" onClick={() => onNavigate('review')}>查看待审核</Button>
          </div>
        </section>
      ) : null}
    </>
  )
}
