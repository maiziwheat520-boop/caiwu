import type { CashReconciliation } from '../types'

type Row = CashReconciliation['rows'][number]
export type GroupedCashRow = Omit<Row, 'facts'> & {
  sourceKinds: Row['source_kind'][]
  sourceKeys: string[]
  facts: Array<Row['facts'][number] & {
    source_kind: Row['source_kind']; source_ref: string; rule_key: string
  }>
}

// Presentation grouping only: never reclassify, shift dates, net flows or edit facts.
export function groupCashRows(rows: Row[]): GroupedCashRow[] {
  const groups = new Map<string, GroupedCashRow>()
  for (const row of rows) {
    const key = JSON.stringify([row.flow_kind, row.business_unit_label, row.item_label])
    let group = groups.get(key)
    if (!group) {
      group = { ...row, rule_key: key, amount_minor: 0, transaction_count: 0,
        facts: [], sourceKinds: [], sourceKeys: [] }
      groups.set(key, group)
    }
    group.amount_minor += row.amount_minor
    group.transaction_count += row.transaction_count
    if (!Number.isSafeInteger(group.amount_minor)) throw new Error('分类汇总金额超出安全范围')
    if (!group.sourceKinds.includes(row.source_kind)) group.sourceKinds.push(row.source_kind)
    group.sourceKeys.push(row.rule_key)
    group.facts.push(...row.facts.map(fact => ({ ...fact, source_kind: row.source_kind,
      source_ref: row.source_ref, rule_key: row.rule_key })))
  }
  return [...groups.values()]
}

export const cashSourceLabel = (kind: Row['source_kind']) => (
  kind === 'BANK_TRANSACTION' ? '银行流水' : kind === 'CANDIDATE' ? '微信流水' : '已登记调整'
)
