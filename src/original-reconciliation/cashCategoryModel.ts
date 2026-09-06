import type { CashReconciliation } from '../types'

type Row = CashReconciliation['rows'][number]
export type CashCategory = {
  key: string
  hotel: string
  category: string
  flow: Row['flow_kind']
  amountMinor: number
  transactionCount: number
  inflowMinor: number
  outflowMinor: number
  directionComplete: boolean
  sources: Row[]
}

const categoryLabels: Record<string, string> = {
  RELATED_PARTY_CURRENT: '关联往来', FINANCING: '融资及还款', INTERNAL_TRANSFER: '内部转账',
}

// Use only a structured Core category; never infer a person's economic role from their name.
export function cashCategoryLabel(row: Row): string {
  const classification = row.source_ref.match(/^company_transaction_classification:([A-Z_]+)(?::|$)/)
  return classification && categoryLabels[classification[1]]
    ? categoryLabels[classification[1]]
    : row.item_label
}

function safeAdd(left: number, right: number): number {
  const value = left + right
  if (!Number.isSafeInteger(value)) throw new Error('分类汇总金额超出安全范围')
  return value
}

export function summarizeCashCategories(rows: Row[]): CashCategory[] {
  const groups = new Map<string, CashCategory>()
  for (const row of rows) {
    if (!row.transaction_count && !row.amount_minor) continue
    const category = cashCategoryLabel(row)
    const key = JSON.stringify([row.flow_kind, row.business_unit_label, category])
    let group = groups.get(key)
    if (!group) {
      group = { key, hotel: row.business_unit_label, category, flow: row.flow_kind,
        amountMinor: 0, transactionCount: 0, inflowMinor: 0, outflowMinor: 0,
        directionComplete: true, sources: [] }
      groups.set(key, group)
    }
    group.amountMinor = safeAdd(group.amountMinor, row.amount_minor)
    group.transactionCount += row.transaction_count
    group.sources.push(row)
    if (row.flow_kind === 'CURRENT') {
      const inflow = row.facts.reduce((sum, fact) => safeAdd(sum, Math.max(0, fact.amount_minor)), 0)
      const outflow = row.facts.reduce((sum, fact) => safeAdd(sum, Math.max(0, -fact.amount_minor)), 0)
      group.inflowMinor = safeAdd(group.inflowMinor, inflow)
      group.outflowMinor = safeAdd(group.outflowMinor, outflow)
      group.directionComplete &&= row.facts.length === row.transaction_count
        && safeAdd(inflow, outflow) === row.amount_minor
    }
  }
  return [...groups.values()].sort((a, b) => a.hotel.localeCompare(b.hotel, 'zh-CN')
    || a.category.localeCompare(b.category, 'zh-CN'))
}

export function cashFlowTotals(groups: CashCategory[], flow: Row['flow_kind']) {
  return groups.filter(group => group.flow === flow).reduce((total, group) => ({
    amountMinor: safeAdd(total.amountMinor, group.amountMinor),
    inflowMinor: safeAdd(total.inflowMinor, group.inflowMinor),
    outflowMinor: safeAdd(total.outflowMinor, group.outflowMinor),
    directionComplete: total.directionComplete && group.directionComplete,
  }), { amountMinor: 0, inflowMinor: 0, outflowMinor: 0, directionComplete: true })
}
