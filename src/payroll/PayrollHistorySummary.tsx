import { useEffect, useMemo, useState } from 'react'

import { api } from '../api'
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

  const selectSummary = (materialId: string) => {
    setSelectedMaterialId(materialId)
    const selected = summaries.find((summary) => summary.material_id === materialId)
    setSelectedPeriod(selected?.data.latest_period ?? '')
  }

  return (
    <section className="panel payroll-history-summary" aria-labelledby="payroll-history-summary-heading">
      <header>
        <div>
          <span>工资统计总表 · 历史权威口径</span>
          <h2 id="payroll-history-summary-heading">各店工资与总汇总</h2>
          <p>按月份直接读取原工资统计总表；历史金额不再从员工明细或实验素材重新计算。</p>
        </div>
        {selectedSummary ? (
          <div className="payroll-history-summary-controls">
            {summaries.length > 1 ? (
              <label>
                汇总表版本
                <select
                  aria-label="汇总表版本"
                  value={selectedSummary.material_id}
                  onChange={(event) => selectSummary(event.target.value)}
                >
                  {summaries.map((summary, index) => (
                    <option key={summary.material_id} value={summary.material_id}>
                      版本 {index + 1} · 至 {summary.data.latest_period} · {summary.data.period_count} 个月
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <label>
              对账月份
              <select
                aria-label="对账月份"
                value={selectedMonth?.period ?? ''}
                onChange={(event) => setSelectedPeriod(event.target.value)}
              >
                {selectedSummary.data.periods.map((item) => (
                  <option key={item.period} value={item.period}>{item.period}</option>
                ))}
              </select>
            </label>
          </div>
        ) : null}
      </header>

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
          <div className="payroll-summary-store-table" role="table" aria-label="各店当月工资汇总">
            <div className="payroll-summary-store-row header" role="row">
              <span role="columnheader">门店</span>
              <span role="columnheader">当月工资</span>
            </div>
            {selectedMonth.stores.map((store) => (
              <div className="payroll-summary-store-row" role="row" key={store.store_name}>
                <strong role="cell">{store.store_name}</strong>
                <span role="cell">{formatMoney(store.net_pay_cents)}</span>
              </div>
            ))}
            <div className="payroll-summary-store-row total" role="row">
              <strong role="cell">总计</strong>
              <strong role="cell">{formatMoney(selectedMonth.total_net_pay_cents)}</strong>
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
