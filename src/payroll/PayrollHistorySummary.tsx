import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { DownloadSimple } from '@phosphor-icons/react'

import { api } from '../api'
import { PeriodSelect } from '../shared/TemporalControls'
import { formatMonthLabel } from '../shared/temporal-format'
import type {
  PayrollSummaryAuthoritativePreviewResponse,
  PayrollTestWorkspaceReadResponse,
} from '../types'

type Props = {
  workspace: PayrollTestWorkspaceReadResponse
}

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
})

const formatMoney = (cents: number) => money.format(cents / 100)

const rankSummaries = (
  left: PayrollSummaryAuthoritativePreviewResponse,
  right: PayrollSummaryAuthoritativePreviewResponse,
) => right.data.period_count - left.data.period_count
  || right.data.latest_period.localeCompare(left.data.latest_period)
  || left.material_id.localeCompare(right.material_id)

export function PayrollHistorySummary({ workspace }: Props) {
  const summaryMaterialIds = useMemo(
    () => workspace.data.materials
      .filter((material) => material.material_type === 'PAYROLL_SUMMARY')
      .map((material) => material.material_id),
    [workspace.data.materials],
  )
  const [summaries, setSummaries] = useState<PayrollSummaryAuthoritativePreviewResponse[]>([])
  const [selectedMaterialId, setSelectedMaterialId] = useState('')
  const [selectedPeriod, setSelectedPeriod] = useState('')
  const [loading, setLoading] = useState(summaryMaterialIds.length > 0)
  const [failed, setFailed] = useState(false)
  const noSummaryMaterials = summaryMaterialIds.length === 0

  useEffect(() => {
    let current = true
    if (summaryMaterialIds.length === 0) {
      return () => { current = false }
    }
    void Promise.allSettled(summaryMaterialIds.map(
      (materialId) => api.previewPayrollSummaryMaterial(materialId),
    )).then((results) => {
      if (!current) return
      const valid = results.flatMap((result) => (
        result.status === 'fulfilled'
        && result.value.data.schema_version === 'payroll-summary-authoritative-preview/v1'
          ? [result.value]
          : []
      )).sort(rankSummaries)
      setSummaries(valid)
      setFailed(valid.length === 0)
      const preferred = valid[0]
      if (preferred) {
        setSelectedMaterialId(preferred.material_id)
        setSelectedPeriod(preferred.data.latest_period)
      }
      setLoading(false)
    })
    return () => { current = false }
  }, [summaryMaterialIds])

  const selectedSummary = summaries.find(
    (summary) => summary.material_id === selectedMaterialId,
  ) ?? summaries[0]
  const selectedMonth = selectedSummary?.data.periods.find(
    (item) => item.period === selectedPeriod,
  ) ?? selectedSummary?.data.periods[0]
  const comparisonPeriods = (selectedSummary?.data.periods ?? []).slice(0, 4)
  const storeNames = Array.from(new Set(
    comparisonPeriods.flatMap((period) => period.stores.map((store) => store.store_name)),
  ))

  const exportSummary = () => {
    if (!selectedSummary) return
    const headings = ['门店', ...comparisonPeriods.map((period) => `${period.period} 工资总额`)]
    const rows = storeNames.map((storeName) => [
      storeName,
      ...comparisonPeriods.map((period) => String(
        (period.stores.find((store) => store.store_name === storeName)?.net_pay_cents ?? 0) / 100,
      )),
    ])
    const csv = [headings, ...rows].map((row) => row.join(',')).join('\n')
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `工资历史汇总-${selectedMonth?.period ?? '全部'}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }
  const storeTrend = (storeName: string) => {
    const current = comparisonPeriods[0]?.stores.find((store) => store.store_name === storeName)?.net_pay_cents
    const previous = comparisonPeriods[1]?.stores.find((store) => store.store_name === storeName)?.net_pay_cents
    if (current === undefined || previous === undefined || previous === 0) return null
    return ((current - previous) / previous) * 100
  }
  const totalTrend = comparisonPeriods.length > 1 && comparisonPeriods[1].total_net_pay_cents !== 0
    ? ((comparisonPeriods[0].total_net_pay_cents - comparisonPeriods[1].total_net_pay_cents)
      / comparisonPeriods[1].total_net_pay_cents) * 100
    : null

  return (
    <section className="panel payroll-history-summary" aria-labelledby="payroll-history-summary-heading">
      <header>
        <div>
          <h2 id="payroll-history-summary-heading">各店历史工资汇总 <small>（仅展示已生成的期间）</small></h2>
        </div>
      </header>

      {selectedSummary ? (
        <aside className="payroll-history-summary-controls" aria-label="账期与版本">
          <div className="payroll-history-control-fields">
            <label>汇总维度<select aria-label="汇总维度" value="门店" disabled><option>门店</option></select></label>
            <PeriodSelect label="对账月份" value={selectedMonth?.period ?? ''} onChange={(event) => setSelectedPeriod(event.target.value)}>
                {selectedSummary.data.periods.map((item) => (
                  <option key={item.period} value={item.period}>{formatMonthLabel(item.period)}</option>
                ))}
            </PeriodSelect>
          </div>
          <button type="button" className="payroll-summary-export" onClick={exportSummary}><DownloadSimple size={16} />导出明细</button>
        </aside>
      ) : null}

      {loading ? <p className="payroll-summary-state">正在读取工资统计总表…</p> : null}
      {!loading && (failed || noSummaryMaterials) ? (
        <div className="payroll-summary-state payroll-summary-state-error" role="status">
          <strong>工资统计总表暂时无法读取</strong>
          <span>七、八月实验素材仍保留；它们不会被删除，也不会临时替代历史汇总金额。</span>
        </div>
      ) : null}

      {selectedMonth ? (
        <div className="payroll-history-summary-results">
          <div className="payroll-summary-title-row">
            <div>
              <span>选定账期</span>
              <h3>{selectedMonth.period} 工资汇总</h3>
            </div>
            <div className="payroll-summary-total-card">
              <span>总汇总</span>
              <strong>{formatMoney(selectedMonth.total_net_pay_cents)}</strong>
              <small>{selectedMonth.store_count} 个门店 · 来自工资统计总表</small>
            </div>
          </div>
          {!selectedMonth.total_matches_stores ? (
            <p className="payroll-history-summary-warning">
              总计行与各店相加不一致；本页保留显示总表“总计”行，需人工对账。
            </p>
          ) : null}
          <div
            className="payroll-summary-store-table payroll-summary-comparison-table"
            role="table"
            aria-label="各店当月工资汇总"
            style={{ '--payroll-period-count': comparisonPeriods.length } as CSSProperties}
          >
            <div className="payroll-summary-store-row header" role="row">
              <span role="columnheader">门店</span>
              <span role="columnheader">员工数</span>
              {comparisonPeriods.map((period) => (
                <span role="columnheader" className={period.period === selectedMonth.period ? 'selected' : ''} key={period.period}>{period.period} 工资总额</span>
              ))}
              <span role="columnheader">本期环比（06 → 07）</span>
              <span role="columnheader">状态</span>
            </div>
            {storeNames.map((storeName) => {
              const trend = storeTrend(storeName)
              return <div className="payroll-summary-store-row" role="row" key={storeName}>
                <strong role="cell">{storeName}</strong>
                <span role="cell">—</span>
                {comparisonPeriods.map((period) => (
                  <span role="cell" className={period.period === selectedMonth.period ? 'selected' : ''} key={period.period}>
                    {formatMoney(period.stores.find((store) => store.store_name === storeName)?.net_pay_cents ?? 0)}
                  </span>
                ))}
                <span role="cell" className={trend !== null && trend >= 0 ? 'trend-positive' : 'trend-negative'}>{trend === null ? '—' : `${trend >= 0 ? '+' : ''}${trend.toFixed(2)}%`}</span>
                <span role="cell"><b className="payroll-generated-badge">已生成</b></span>
              </div>
            })}
            <div className="payroll-summary-store-row total" role="row">
              <strong role="cell">总计（{selectedMonth.store_count} 个门店）</strong>
              <strong role="cell">—</strong>
              {comparisonPeriods.map((period) => (
                <strong role="cell" className={period.period === selectedMonth.period ? 'selected' : ''} key={period.period}>{formatMoney(period.total_net_pay_cents)}</strong>
              ))}
              <strong role="cell" className={totalTrend !== null && totalTrend >= 0 ? 'trend-positive' : 'trend-negative'}>{totalTrend === null ? '—' : `${totalTrend >= 0 ? '+' : ''}${totalTrend.toFixed(2)}%`}</strong>
              <strong role="cell">—</strong>
            </div>
          </div>
          <footer>
            <span>只读对账展示 · 权威数据源：工资统计总表 · 不可付款</span>
            <span>七、八月工资素材保留在实验区，不参与这里的历史金额计算。</span>
          </footer>
        </div>
      ) : null}
    </section>
  )
}
