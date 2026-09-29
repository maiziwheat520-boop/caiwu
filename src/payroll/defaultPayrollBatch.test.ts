import { describe, expect, it } from 'vitest'
import { defaultPayrollBatch } from './defaultPayrollBatch'

describe('defaultPayrollBatch', () => {
  it('only defaults a unique business-month batch, not a sole historical batch', () => {
    const historical = { pay_period: '2026-07', id: 'old' }
    const current = { pay_period: '2026-08', id: 'current' }
    expect(defaultPayrollBatch([historical], '2026-08')).toBeNull()
    expect(defaultPayrollBatch([historical, current], '2026-08')).toBe(current)
    expect(defaultPayrollBatch([], '2026-08')).toBeNull()
    expect(defaultPayrollBatch([current, { ...current, id: 'another' }], '2026-08')).toBeNull()
  })
})
