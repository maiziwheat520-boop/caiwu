import { useMemo, useState } from 'react'

import { api } from '../api'
import type {
  PayrollTestMaterialPreview,
  PayrollTestWorkspaceReadResponse,
} from '../types'

type Props = {
  workspace: PayrollTestWorkspaceReadResponse
}

type SummaryResult = {
  materialId: string
  preview: PayrollTestMaterialPreview | null
  error: boolean
}

const money = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
})

export function PayrollHistorySummary({ workspace }: Props) {
  const periods = useMemo(
    () => Array.from(new Set(workspace.data.materials
      .filter((material) => material.routing_status === 'AUTO_TEST'
        && material.material_type === 'PAYROLL_SHEET'
        && material.period !== null)
      .map((material) => material.period as string)))
      .sort((left, right) => right.localeCompare(left)),
    [workspace.data.materials],
  )
  const [selectedPeriod, setSelectedPeriod] = useState(periods[0] ?? '')
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState<SummaryResult[] | null>(null)

  const summarize = async () => {
    if (!selectedPeriod || busy) return
    const candidates = workspace.data.materials.filter((material) => (
      material.routing_status === 'AUTO_TEST'
      && material.material_type === 'PAYROLL_SHEET'
      && material.period === selectedPeriod
    ))
    setBusy(true)
    setResults(null)
    const next = await Promise.all(candidates.map(async (material): Promise<SummaryResult> => {
      try {
        const response = await api.previewPayrollTestMaterial(material.material_id)
        return { materialId: material.material_id, preview: response.data, error: false }
      } catch {
        return { materialId: material.material_id, preview: null, error: true }
      }
    }))
    setResults(next)
    setBusy(false)
  }

  return (
    <section className="payroll-history-summary" aria-labelledby="payroll-history-summary-heading">
      <header>
        <div>
          <span>汇总到工资总表 · 网页化切片</span>
          <h3 id="payroll-history-summary-heading">历史工资汇总预览</h3>
          <p>按账期读取已保存的工资表并显示人数、实发和校验状态；多个版本分开列示，不自动相加。</p>
        </div>
        <div className="payroll-history-summary-controls">
          <label>
            工资月份
            <select
              aria-label="工资汇总月份"
              value={selectedPeriod}
              onChange={(event) => {
                setSelectedPeriod(event.target.value)
                setResults(null)
              }}
            >
              {periods.map((period) => <option key={period} value={period}>{period}</option>)}
            </select>
          </label>
          <button type="button" disabled={!selectedPeriod || busy} onClick={() => void summarize()}>
            {busy ? '正在汇总…' : '生成网页汇总预览'}
          </button>
        </div>
      </header>

      {periods.length === 0 ? <p className="payroll-test-empty">暂无可汇总的历史工资表。</p> : null}
      {results ? (
        <div className="payroll-history-summary-results" aria-label="历史工资汇总结果">
          {results.length > 1 ? (
            <p className="payroll-history-summary-warning">
              本月存在 {results.length} 份不同工资表；为防重复计算，系统没有把它们合并为一个总数。
            </p>
          ) : null}
          {results.map((result) => (
            <article key={result.materialId}>
              <div>
                <strong>工资表 {result.materialId.slice(-8).toUpperCase()}</strong>
                <span>{selectedPeriod}</span>
              </div>
              {result.preview ? (
                <dl>
                  <div><dt>人数</dt><dd>{result.preview.line_count}</dd></div>
                  <div><dt>实发合计</dt><dd>{money.format(result.preview.total_net_pay_cents / 100)}</dd></div>
                  <div><dt>校验</dt><dd>{result.preview.exceptions.length === 0 ? '通过' : `${result.preview.exceptions.length} 项待核对`}</dd></div>
                </dl>
              ) : <strong className="payroll-history-summary-error">文件无法安全解析</strong>}
            </article>
          ))}
          <small>只读汇总 · 测试数据 · 不可付款 · 刷新后可从已保存材料重新生成</small>
        </div>
      ) : null}
    </section>
  )
}
