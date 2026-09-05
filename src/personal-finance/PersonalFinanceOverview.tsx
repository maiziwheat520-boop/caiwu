import { Badge, Button } from '@radix-ui/themes'
import {
  ArrowsClockwise,
  Bank,
  CaretRight,
  CheckCircle,
  CloudArrowUp,
  FolderOpen,
  ListChecks,
} from '@phosphor-icons/react'

import { minorToMajor } from '../api'
import type { Candidate, Page } from '../types'
import { ErrorState, LoadingState, Metric, PageHeader } from '../shared/PagePrimitives'
import { currency } from '../shared/format'
import { toCandidate } from '../candidates/candidateMapping'
import { accountingMonthLabel } from '../candidates/candidateLabels'
import { PersonalBankTransactionsPanel } from './PersonalBankTransactionsPanel'
import {
  candidateCashflowMinor,
  selectPersonalFinanceEntries,
} from './personalFinanceRules'
import { useCompleteCandidatePages } from './useCompleteCandidatePages'

export function PersonalFinanceOverview({ onNavigate, onOpenCandidate, csrfToken }: {
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
  csrfToken: string
}) {
  const completeCandidatePages = useCompleteCandidatePages()
  const candidates = completeCandidatePages.candidates?.map(toCandidate) ?? []
  const completeCandidatesAvailable = completeCandidatePages.candidates !== null

  const pending = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const personalSelection = selectPersonalFinanceEntries(candidates)
  const financialEntries = personalSelection.entries
  const unassignedEntries = personalSelection.unassignedEntries
  const testEntries = [...financialEntries, ...unassignedEntries]
  const confirmedPendingPostingCount = testEntries.length
  const financialCandidates = testEntries.map((entry) => entry.candidate)
  const incomeMinor = testEntries.reduce((total, entry) => total + Math.max(entry.cashflowMinor, 0), 0)
  const expenseMinor = testEntries.reduce((total, entry) => total + Math.abs(Math.min(entry.cashflowMinor, 0)), 0)
  const netMinor = incomeMinor - expenseMinor
  const evidenceCount = new Set(financialCandidates.flatMap((candidate) => candidate.evidence.map((evidence) => evidence.id))).size

  const categoryTotals = testEntries.reduce((totals, entry) => {
    const amountMinor = Math.abs(entry.cashflowMinor)
    if (amountMinor === 0) return totals
    const category = entry.candidate.category.trim() || '待分类'
    totals.set(category, (totals.get(category) ?? 0) + amountMinor)
    return totals
  }, new Map<string, number>())
  const categorizedTotalMinor = [...categoryTotals.values()].reduce((total, amountMinor) => total + amountMinor, 0)
  const categoryShares = [...categoryTotals.entries()]
    .map(([category, amountMinor]) => ({
      category,
      amountMinor,
      percentage: categorizedTotalMinor > 0 ? (amountMinor / categorizedTotalMinor) * 100 : 0,
    }))
    .sort((left, right) => right.amountMinor - left.amountMinor || left.category.localeCompare(right.category, 'zh-CN'))

  const monthlyTotals = testEntries.reduce((totals, entry) => {
    if (!entry.candidate.accountingMonth) return totals
    const current = totals.get(entry.candidate.accountingMonth) ?? { incomeMinor: 0, expenseMinor: 0 }
    if (entry.cashflowMinor >= 0) current.incomeMinor += entry.cashflowMinor
    else current.expenseMinor += Math.abs(entry.cashflowMinor)
    totals.set(entry.candidate.accountingMonth, current)
    return totals
  }, new Map<string, { incomeMinor: number; expenseMinor: number }>())
  const monthlyTrend = [...monthlyTotals.entries()]
    .map(([month, totals]) => ({ ...totals, month, netMinor: totals.incomeMinor - totals.expenseMinor }))
    .sort((left, right) => right.month.localeCompare(left.month))
  const latestTrend = monthlyTrend[0]
  const leadingCategory = categoryShares[0]
  return (
    <>
      <PageHeader
        eyebrow="个人财务"
        title="个人对账"
        description="正式银行流水与测试候选分层展示；归属待校准与会计过账仍严格分开。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />处理待审核</Button>}
      />

      {!completeCandidatesAvailable ? (
        completeCandidatePages.error ? (
          <ErrorState
            message={`${completeCandidatePages.error}；未显示不完整的收支合计。`}
            onRetry={() => { void completeCandidatePages.reload() }}
          />
        ) : (
          <LoadingState
            title="正在核对完整个人财务范围"
            description="正在逐页读取已授权候选；全部读取完成前不会显示收支合计。"
          />
        )
      ) : <>
        {completeCandidatePages.error ? (
          <div className="personal-finance-boundary" role="alert">
            <span>完整汇总刷新失败，已保留上一次成功读取的结果：{completeCandidatePages.error}</span>
            <Button size="1" variant="soft" disabled={completeCandidatePages.loading} onClick={() => { void completeCandidatePages.reload() }}>
              <ArrowsClockwise className={completeCandidatePages.loading ? 'state-spinner' : undefined} size={14} />
              重新读取
            </Button>
          </div>
        ) : completeCandidatePages.loading ? (
          <div className="personal-finance-boundary" role="status">正在刷新完整候选分页，当前仍显示上一次完整结果。</div>
        ) : null}

      <section className="panel personal-posting-status" aria-label="个人财务入账状态">
        <div>
          <span>入账链路</span>
          <h2>{confirmedPendingPostingCount} 条已确认、尚未过账</h2>
          <p>“确认”只代表审核完成并进入对账草稿，不等于已生成会计分录。系统会在主体和账户映射校准后生成平衡草稿，再由你明确确认过账。</p>
        </div>
        <Badge color="amber">正式过账未启用</Badge>
      </section>

      <section className="panel personal-review-priority" aria-label="个人财务待审核">
        <div className="panel-heading">
          <div><h2>待审核</h2><p>先处理仍可能改变收支归类的事项</p></div>
          <div className="personal-review-actions"><Badge color={pending.length > 0 ? 'amber' : 'green'}>{pending.length} 条</Badge><Button size="1" variant="soft" onClick={() => onNavigate('review')}>查看全部<CaretRight size={14} /></Button></div>
        </div>
        {pending.length > 0 ? (
          <div className="personal-review-list">
            {pending.slice(0, 4).map((candidate) => (
              <button key={candidate.id} onClick={() => onOpenCandidate(candidate)} type="button">
                <span><strong>{candidate.shortId}</strong><small>{candidate.businessUnit} · {candidate.category} · {accountingMonthLabel(candidate.accountingMonth)}</small></span>
                <span>{candidate.summary}</span>
                <strong>{currency.format(minorToMajor(candidateCashflowMinor(candidate)))}</strong>
                <CaretRight size={16} />
              </button>
            ))}
          </div>
        ) : <div className="empty-state compact-empty personal-review-empty"><CheckCircle size={30} /><h3>当前没有待审核事项</h3><p>新导入的风险或信息不完整事项会出现在这里。</p></div>}
      </section>

      <section className="metric-grid personal-finance-metrics" aria-label="个人财务收支概览">
        <Metric primary label="测试收入" value={currency.format(minorToMajor(incomeMinor))} detail={`${testEntries.filter((entry) => entry.cashflowMinor >= 0).length} 条已确认收入，含归属待校准`} icon={<CloudArrowUp size={20} />} />
        <Metric label="测试支出" value={currency.format(minorToMajor(expenseMinor))} detail={`${testEntries.filter((entry) => entry.cashflowMinor < 0).length} 条已确认支出，含归属待校准`} icon={<Bank size={20} />} />
        <Metric label="测试净额" value={currency.format(minorToMajor(netMinor))} detail="全量测试试算，尚未过账" icon={<ArrowsClockwise size={20} />} />
        <Metric label="原始材料" value={`${evidenceCount} 份`} detail="只计入本次汇总所依据的材料" icon={<FolderOpen size={20} />} />
      </section>

      <div className="personal-finance-boundary" role="status">
        <span>{personalSelection.excludedCount} 条不属于个人范围或状态未确认，未计入汇总</span>
        <span>{unassignedEntries.length} 条已确认记录归属待校准，单独列示</span>
        <span>{personalSelection.deduplicatedCount} 条跨来源重复记录已合并</span>
      </div>

      {unassignedEntries.length > 0 ? (
        <section className="panel personal-unassigned-panel" aria-label="个人财务归属待校准">
          <div className="panel-heading">
            <div><h2>归属待校准</h2><p>以下记录全部展开并进入上方测试试算，但归属确认前不会进入个人正式账簿。</p></div>
            <Badge color="amber">{unassignedEntries.length} 条归属待校准</Badge>
          </div>
          <div className="personal-review-list">
            {unassignedEntries.map((entry) => (
              <button key={entry.candidate.id} onClick={() => onOpenCandidate(entry.candidate)} type="button">
                <span><strong>{entry.candidate.shortId}</strong><small>{entry.candidate.businessUnit || '主体待校准'} · {entry.candidate.category} · {accountingMonthLabel(entry.candidate.accountingMonth)}</small></span>
                <span>{entry.candidate.summary}</span>
                <strong>{currency.format(minorToMajor(entry.cashflowMinor))}</strong>
                <CaretRight size={16} />
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div className="personal-insight-grid">
        <section className="panel personal-category-panel">
          <div className="panel-heading"><div><h2>测试分类占比</h2><p>按全部试算收入与支出的绝对金额计算</p></div></div>
          {categoryShares.length > 0 ? (
            <div className="personal-category-list">
              {categoryShares.map((item) => (
                <article key={item.category}>
                  <div><strong>{item.category}</strong><span>{currency.format(minorToMajor(item.amountMinor))}</span><b>{item.percentage.toFixed(1)}%</b></div>
                  <div aria-label={`${item.category}占比 ${item.percentage.toFixed(1)}%`} aria-valuemax={100} aria-valuemin={0} aria-valuenow={Number(item.percentage.toFixed(1))} className="personal-category-bar" role="progressbar"><span style={{ width: `${item.percentage}%` }} /></div>
                </article>
              ))}
            </div>
          ) : <p className="personal-finance-empty">暂无可统计的收支分类。</p>}
        </section>

        <section className="panel personal-trend-panel">
          <div className="panel-heading"><div><h2>测试月度趋势</h2><p>按全部已确认试算记录的归属月份汇总</p></div></div>
          {latestTrend ? (
            <p className="personal-trend-summary">
              {accountingMonthLabel(latestTrend.month)}净额 {currency.format(minorToMajor(latestTrend.netMinor))}
              {leadingCategory ? `；当前金额占比最高的分类是${leadingCategory.category} ${leadingCategory.percentage.toFixed(1)}%` : ''}。
            </p>
          ) : null}
          {monthlyTrend.length > 0 ? (
            <div className="personal-trend-list">
              {monthlyTrend.map((item) => (
                <article key={item.month}>
                  <strong>{accountingMonthLabel(item.month)}</strong>
                  <dl><div><dt>收入</dt><dd>{currency.format(minorToMajor(item.incomeMinor))}</dd></div><div><dt>支出</dt><dd>{currency.format(minorToMajor(item.expenseMinor))}</dd></div><div><dt>净额</dt><dd>{currency.format(minorToMajor(item.netMinor))}</dd></div></dl>
                </article>
              ))}
            </div>
          ) : <p className="personal-finance-empty">暂无已确认归属月份的收支数据。</p>}
        </section>
      </div>
      </>}

      <PersonalBankTransactionsPanel csrfToken={csrfToken} />

      <section className="panel report-entry-panel personal-finance-actions">
        <div><h2>材料与对账去向</h2><p>查看原始账单、凭证、待补材料和月度对账。</p></div>
        <div className="review-header-actions">
          <Button variant="outline" color="gray" onClick={() => onNavigate('files')}>查看材料总览</Button>
          <Button variant="outline" color="gray" onClick={() => onNavigate('reconciliation')}>查看月度对账</Button>
        </div>
      </section>
    </>
  )
}
