import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { LegacyReconciliationMonth, LegacyReconciliationSourceList } from '../types'

const cellAddress = /^([A-Z]{1,3})([1-9][0-9]{0,4})$/

function columnIndex(letters: string): number {
  let result = 0
  for (const letter of letters) result = result * 26 + letter.charCodeAt(0) - 64
  return result - 1
}

export function LegacyReconciliationPanel({ month }: { month: string }) {
  const [result, setResult] = useState<{
    month: string
    sources: LegacyReconciliationSourceList | null
    snapshot: LegacyReconciliationMonth | null
    error: string | null
  } | null>(null)
  const loading = result?.month !== month
  const sources = loading ? null : result.sources
  const snapshot = loading ? null : result.snapshot
  const error = loading ? null : result.error

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const inventory = await api.getLegacyReconciliationSources()
        if (!active) return
        const source = inventory.sources.find((item) => item.periods.includes(month))
        const sheet = source
          ? await api.getLegacyReconciliationMonth(source.source_ref, month)
          : null
        if (sheet && (sheet.period !== month || sheet.source_ref !== source?.source_ref)) throw new Error('历史原表月份与来源不一致')
        if (active) setResult({ month, sources: inventory, snapshot: sheet, error: null })
      } catch (cause) {
        if (active) setResult({ month, sources: null, snapshot: null, error: cause instanceof Error ? cause.message : '历史原表暂不可读取' })
      }
    })()
    return () => { active = false }
  }, [month])

  const grid = useMemo(() => {
    if (!snapshot) return null
    const cells = new Map<string, LegacyReconciliationMonth['cells'][number]>()
    let maxColumn = 0
    for (const cell of snapshot.cells) {
      const match = cellAddress.exec(cell.address)
      if (!match) continue
      maxColumn = Math.max(maxColumn, columnIndex(match[1]) + 1)
      cells.set(cell.address, cell)
    }
    return { cells, maxColumn }
  }, [snapshot])

  return (
    <details className="panel legacy-reconciliation-panel" aria-label="历史对账原表">
      <summary className="panel-heading"><div><h2>历史对账原表</h2><p>数据库保存的原表值，不按新规则重算</p></div></summary>
      {error ? <p role="status">{error}</p> : null}
      {loading ? <p role="status">正在读取历史原表…</p> : null}
      {!loading && !error && sources && !snapshot ? <p>所选月份尚无已入库的历史原表。</p> : null}
      {snapshot && grid ? (
        <>
          <p>原工作表 {snapshot.sheet_name} · {snapshot.cell_count} 个有值单元格。公式显示文件中保存的结果；没有保存值时不推算。</p>
          <div className="legacy-reconciliation-grid" role="region" aria-label={`${month} 历史原表单元格`} tabIndex={0}>
            <table>
              <thead><tr><th scope="col">行</th>{Array.from({ length: grid.maxColumn }, (_, index) => <th scope="col" key={index}>{index + 1}</th>)}</tr></thead>
              <tbody>
                {Array.from({ length: snapshot.row_count }, (_, index) => {
                  const rowNumber = index + 1
                  return <tr key={rowNumber}><th scope="row">{rowNumber}</th>{Array.from({ length: grid.maxColumn }, (_, column) => {
                    let number = column + 1
                    let letters = ''
                    while (number > 0) { number -= 1; letters = String.fromCharCode(65 + number % 26) + letters; number = Math.floor(number / 26) }
                    const cell = grid.cells.get(`${letters}${rowNumber}`)
                    const display = cell?.type === 'formula' ? cell.cached_value ?? '未保存计算值' : cell?.value ?? ''
                    return <td key={column} title={cell?.type === 'formula' ? `${cell.address}: ${cell.value}` : cell?.address} className={cell?.type === 'number' ? 'legacy-number' : undefined}>{display}</td>
                  })}</tr>
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </details>
  )
}
