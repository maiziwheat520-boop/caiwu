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
import type { Page } from '../types'
import { ErrorState, LoadingState, Metric, PageHeader } from '../shared/PagePrimitives'
import { currency } from '../shared/format'
import { accountingMonthLabel } from '../candidates/candidateLabels'
import { PersonalBankTransactionsPanel } from './PersonalBankTransactionsPanel'
import { usePersonalFinanceSummary } from './usePersonalFinanceSummary'

const percentage = (basisPoints: number) => (basisPoints / 100).toFixed(1)

/**
 * Personal reconciliation, rendered from the summary Core builds.
 *
 * The rules that decide which confirmed candidates are personal cash movement,
 * collapse the same movement seen through both a bank and a platform, and total
 * what is left used to run here, so the page paged the entire candidate
 * collection into the browser before it could show a single number. They live
 * beside the facts now; this reads one summary and renders it.
 */
export function PersonalFinanceOverview({ onNavigate, onOpenCandidateRef, csrfToken }: {
  onNavigate: (page: Page) => void
  onOpenCandidateRef: (candidateRef: string) => void
  csrfToken: string
}) {
  const { summary, loading, error, reload } = usePersonalFinanceSummary()
  const leadingCategory = summary?.category_shares[0]
  const latestTrend = summary?.monthly_totals[0]

  return (
    <>
      <PageHeader
        eyebrow="个人财务"
        title="个人对账"
        description="正式银行流水与测试候选分层展示；归属待校准与会计过账仍严格分开。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />处理待审核</Button>}
      />

      {summary === null ? (
        error ? (
          <ErrorState message={`${error}；未显示不完整的收支合计。`} onRetry={() => { void reload() }} />
        ) : (
          <LoadingState
            title="正在读取个人财务汇总"
            description="正在向账本读取已授权范围的收支合计。"
          />
        )
      ) : <>
        {error ? (
          <div className="personal-finance-boundary" role="alert">
            <span>汇总刷新失败，已保留上一次成功读取的结果：{error}</span>
            <Button size="1" variant="soft" disabled={loading} onClick={() => { void reload() }}>
              <ArrowsClockwise className={loading ? 'state-spinner' : undefined} size={14} />
              重新读取
            </Button>
          </div>
        ) : loading ? (
          <div className="personal-finance-boundary" role="status">正在刷新汇总，当前仍显示上一次结果。</div>
        ) : null}

      <section className="panel personal-posting-status" aria-label="个人财务入账状态">
        <div>
          <span>入账链路</span>
          <h2>{summary.entry_total} 条已确认、尚未过账</h2>
          <p>“确认”只代表审核完成并进入对账草稿，不等于已生成会计分录。系统会在主体和账户映射校准后生成平衡草稿，再由你明确确认过账。</p>
        </div>
        <Badge color="amber">正式过账未启用</Badge>
      </section>

      <section className="panel personal-review-priority" aria-label="个人财务待审核">
        <div className="panel-heading">
          <div><h2>待审核</h2><p>先处理仍可能改变收支归类的事项</p></div>
          <div className="personal-review-actions"><Badge color={summary.pending_total > 0 ? 'amber' : 'green'}>{summary.pending_total} 条</Badge><Button size="1" variant="soft" onClick={() => onNavigate('review')}>查看全部<CaretRight size={14} /></Button></div>
        </div>
        {summary.pending_preview.length > 0 ? (
          <div className="personal-review-list">
            {summary.pending_preview.map((candidate) => (
              <button key={candidate.candidate_ref} onClick={() => onOpenCandidateRef(candidate.candidate_ref)} type="button">
                <span><strong>{candidate.short_id}</strong><small>{candidate.business_unit_label} · {candidate.category_label} · {accountingMonthLabel(candidate.accounting_month)}</small></span>
                <span>{candidate.summary}</span>
                <CaretRight size={16} />
              </button>
            ))}
          </div>
        ) : <div className="empty-state compact-empty personal-review-empty"><CheckCircle size={30} /><h3>当前没有待审核事项</h3><p>新导入的风险或信息不完整事项会出现在这里。</p></div>}
      </section>

      <section className="metric-grid personal-finance-metrics" aria-label="个人财务收支概览">
        <Metric primary label="测试收入" value={currency.format(minorToMajor(summary.income_minor))} detail={`${summary.income_entry_count} 条已确认收入，含归属待校准`} icon={<CloudArrowUp size={20} />} />
        <Metric label="测试支出" value={currency.format(minorToMajor(summary.expense_minor))} detail={`${summary.expense_entry_count} 条已确认支出，含归属待校准`} icon={<Bank size={20} />} />
        <Metric label="测试净额" value={currency.format(minorToMajor(summary.net_minor))} detail="全量测试试算，尚未过账" icon={<ArrowsClockwise size={20} />} />
        <Metric label="原始材料" value={`${summary.evidence_count} 份`} detail="只计入本次汇总所依据的材料" icon={<FolderOpen size={20} />} />
      </section>

      <div className="personal-finance-boundary" role="status">
        <span>{summary.excluded_count} 条不属于个人范围或状态未确认，未计入汇总</span>
        <span>{summary.unassigned_entries.length} 条已确认记录归属待校准，单独列示</span>
        <span>{summary.deduplicated_count} 条跨来源重复记录已合并</span>
      </div>

      {summary.unassigned_entries.length > 0 ? (
        <section className="panel personal-unassigned-panel" aria-label="个人财务归属待校准">
          <div className="panel-heading">
            <div><h2>归属待校准</h2><p>以下记录全部展开并进入上方测试试算，但归属确认前不会进入个人正式账簿。</p></div>
            <Badge color="amber">{summary.unassigned_entries.length} 条归属待校准</Badge>
          </div>
          <div className="personal-review-list">
            {summary.unassigned_entries.map((entry) => (
              <button key={entry.candidate_ref} onClick={() => onOpenCandidateRef(entry.candidate_ref)} type="button">
                <span><strong>{entry.short_id}</strong><small>{entry.business_unit_label || '主体待校准'} · {entry.category_label} · {accountingMonthLabel(entry.accounting_month)}</small></span>
                <span>{entry.summary}</span>
                <strong>{currency.format(minorToMajor(entry.cashflow_minor))}</strong>
                <CaretRight size={16} />
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div className="personal-insight-grid">
        <section className="panel personal-category-panel">
          <div className="panel-heading"><div><h2>测试分类占比</h2><p>按全部试算收入与支出的绝对金额计算</p></div></div>
          {summary.category_shares.length > 0 ? (
            <div className="personal-category-list">
              {summary.category_shares.map((item) => (
                <article key={item.category}>
                  <div><strong>{item.category}</strong><span>{currency.format(minorToMajor(item.amount_minor))}</span><b>{percentage(item.basis_points)}%</b></div>
                  <div aria-label={`${item.category}占比 ${percentage(item.basis_points)}%`} aria-valuemax={100} aria-valuemin={0} aria-valuenow={Number(percentage(item.basis_points))} className="personal-category-bar" role="progressbar"><span style={{ width: `${item.basis_points / 100}%` }} /></div>
                </article>
              ))}
            </div>
          ) : <p className="personal-finance-empty">暂无可统计的收支分类。</p>}
        </section>

        <section className="panel personal-trend-panel">
          <div className="panel-heading"><div><h2>测试月度趋势</h2><p>按全部已确认试算记录的归属月份汇总</p></div></div>
          {latestTrend ? (
            <p className="personal-trend-summary">
              {accountingMonthLabel(latestTrend.month)}净额 {currency.format(minorToMajor(latestTrend.net_minor))}
              {leadingCategory ? `；当前金额占比最高的分类是${leadingCategory.category} ${percentage(leadingCategory.basis_points)}%` : ''}。
            </p>
          ) : null}
          {summary.monthly_totals.length > 0 ? (
            <div className="personal-trend-list">
              {summary.monthly_totals.map((item) => (
                <article key={item.month}>
                  <strong>{accountingMonthLabel(item.month)}</strong>
                  <dl><div><dt>收入</dt><dd>{currency.format(minorToMajor(item.income_minor))}</dd></div><div><dt>支出</dt><dd>{currency.format(minorToMajor(item.expense_minor))}</dd></div><div><dt>净额</dt><dd>{currency.format(minorToMajor(item.net_minor))}</dd></div></dl>
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
