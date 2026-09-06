import { describe, expect, it } from 'vitest'
import type { CashReconciliation } from '../types'
import { cashCategoryLabel, cashFlowTotals, summarizeCashCategories } from './cashCategoryModel'

type Row = CashReconciliation['rows'][number]
const row = (id: string, amount: number, overrides: Partial<Row> = {}): Row => ({
  rule_key: id, source_kind: 'BANK_TRANSACTION', source_ref: 'company_transaction_classification:RELATED_PARTY_CURRENT:OTHER',
  flow_kind: 'CURRENT', business_unit_label: '示例酒店', item_label: `对方${id}`,
  transaction_count: 1, amount_minor: Math.abs(amount),
  facts: [{ fact_ref: id, occurred_on: '2026-08-01', amount_minor: amount }], ...overrides,
})

describe('cash category summary', () => {
  it('groups structured categories rather than persons and keeps signed facts unchanged', () => {
    const input = [row('a', 200), row('b', -150), row('c', -50)]
    const copy = structuredClone(input)
    const result = summarizeCashCategories(input)
    expect(result).toHaveLength(1)
    expect(result[0]).toMatchObject({ category: '关联往来', amountMinor: 400, inflowMinor: 200, outflowMinor: 200, transactionCount: 3, directionComplete: true })
    expect(result[0].sources.flatMap(source => source.facts)).toHaveLength(3)
    expect(cashFlowTotals(result, 'CURRENT')).toMatchObject({ inflowMinor: 200, outflowMinor: 200 })
    expect(input).toEqual(copy)
  })

  it('preserves hotel, flow and business items across bank, WeChat and registered adjustments', () => {
    const input = [row('bank', -500, { flow_kind: 'EXPENSE', item_label: '布草', source_ref: 'bank' }),
      row('wechat', -300, { flow_kind: 'EXPENSE', source_kind: 'CANDIDATE', item_label: '布草', source_ref: 'wechat' }),
      row('income', 700, { flow_kind: 'INCOME', item_label: '布草', source_ref: 'income' }),
      row('other', -100, { flow_kind: 'EXPENSE', business_unit_label: '另一酒店', item_label: '布草', source_ref: 'other' }),
      row('adjustment', 200, { flow_kind: 'INCOME', source_kind: 'ADJUSTMENT', item_label: '历史调整', source_ref: 'adjustment', transaction_count: 0, facts: [] })]
    const result = summarizeCashCategories(input)
    expect(result).toHaveLength(4)
    expect(cashFlowTotals(result, 'EXPENSE').amountMinor).toBe(900)
    expect(cashFlowTotals(result, 'INCOME').amountMinor).toBe(900)
    expect(result.find(group => group.amountMinor === 800)?.transactionCount).toBe(2)
  })

  it('never derives category from names or an untrusted embedded code', () => {
    expect(cashCategoryLabel(row('a', 100, { source_ref: 'unknown', item_label: '父亲' }))).toBe('父亲')
    expect(cashCategoryLabel(row('a', 100, { source_ref: 'memo:company_transaction_classification:FINANCING' }))).toBe('对方a')
    expect(cashCategoryLabel(row('a', 100, { source_ref: 'company_transaction_classification:INTERNAL_TRANSFER' }))).toBe('内部转账')
    expect(cashCategoryLabel(row('a', 100, { source_ref: 'company_transaction_classification:PLATFORM_ROOM_REVENUE', item_label: '美团' }))).toBe('美团')
  })

  it('marks direction unavailable when the row cannot reconcile to complete signed facts', () => {
    expect(summarizeCashCategories([row('a', 100, { transaction_count: 2 })])[0].directionComplete).toBe(false)
    expect(summarizeCashCategories([row('a', 100, { amount_minor: 200 })])[0].directionComplete).toBe(false)
  })

  it('fails closed on integer overflow', () => {
    expect(() => summarizeCashCategories([row('a', Number.MAX_SAFE_INTEGER), row('b', 1)])).toThrow('安全范围')
  })
})
