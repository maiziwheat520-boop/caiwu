import { describe, expect, it } from 'vitest'
import type { CashReconciliation } from '../types'
import { groupCashRows, cashSourceLabel } from './groupCashRows'

const row = (key: string, kind: 'BANK_TRANSACTION' | 'CANDIDATE' = 'BANK_TRANSACTION'): CashReconciliation['rows'][number] => ({
  rule_key: key, source_kind: kind, source_ref: `source-${key}`, flow_kind: 'EXPENSE',
  business_unit_label: '合成门店', item_label: '银行收费', amount_minor: 150,
  transaction_count: 1, facts: [{ fact_ref: `fact-${key}`, occurred_on: '2026-09-02', amount_minor: -150 }],
})
describe('monthly company/category grouping', () => {
  it('merges categories across sources, preserving exact fact provenance and input', () => {
    const input = [row('one'), row('two', 'CANDIDATE')]
    const before = JSON.stringify(input)
    const output = groupCashRows(input)
    expect(output).toHaveLength(1)
    expect(output[0].amount_minor).toBe(300)
    expect(output[0].transaction_count).toBe(2)
    expect(output[0].sourceKinds).toEqual(['BANK_TRANSACTION', 'CANDIDATE'])
    expect(output[0].facts[1]).toMatchObject({ source_ref: 'source-two', rule_key: 'two', amount_minor: -150 })
    expect(JSON.stringify(input)).toBe(before)
  })
  it('keeps company, direction and detailed category separate', () => {
    expect(groupCashRows([row('one'), { ...row('two'), item_label: '税款' },
      { ...row('three'), business_unit_label: '其他门店' }, { ...row('four'), flow_kind: 'CURRENT' }])).toHaveLength(4)
  })
  it('does not call adjustments WeChat records', () => {
    expect(cashSourceLabel('ADJUSTMENT')).toBe('已登记调整')
  })
})
