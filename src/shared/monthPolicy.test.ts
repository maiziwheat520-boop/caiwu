import { describe, expect, it } from 'vitest'
import { previousBusinessMonth } from './monthPolicy'

describe('previousBusinessMonth', () => {
  it.each([
    ['2026-09-06T01:00:00Z', '2026-08'],
    ['2026-08-31T15:59:59Z', '2026-07'],
    ['2026-08-31T16:00:00Z', '2026-08'],
    ['2026-12-31T15:59:59Z', '2026-11'],
    ['2026-12-31T16:00:00Z', '2026-12'],
    ['2024-03-01T00:00:00+08:00', '2024-02'],
  ])('uses Shanghai month at %s', (instant, expected) => {
    const sourceDate = new Date(instant)
    const before = sourceDate.getTime()
    expect(previousBusinessMonth(sourceDate)).toBe(expected)
    expect(sourceDate.getTime()).toBe(before)
  })
  it('rejects an invalid date instead of inventing a month', () => {
    expect(() => previousBusinessMonth(new Date('invalid'))).toThrow(RangeError)
  })
})
