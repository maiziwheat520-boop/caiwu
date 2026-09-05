import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MonthInput, PeriodSelect } from './TemporalControls'
import { formatMonthLabel } from './temporal-format'

describe('TemporalControls', () => {
  it('gives month inputs a visible accessible label and forwards changes', () => {
    const onChange = vi.fn()
    render(<MonthInput label="对账月份" value="2026-09" onChange={onChange} />)

    expect(screen.getByText('对账月份')).toBeVisible()
    fireEvent.change(screen.getByLabelText('对账月份'), { target: { value: '2026-08' } })
    expect(onChange).toHaveBeenCalledOnce()
  })

  it('keeps enumerated periods in a labelled select', () => {
    render(
      <PeriodSelect label="工资月份" value="2026-08" disabled>
        <option value="2026-08">{formatMonthLabel('2026-08')}</option>
      </PeriodSelect>,
    )

    expect(screen.getByLabelText('工资月份')).toHaveDisplayValue('2026 年 8 月')
  })

  it('formats valid months without changing unknown values', () => {
    expect(formatMonthLabel('2026-09')).toBe('2026 年 9 月')
    expect(formatMonthLabel('待确认')).toBe('待确认')
  })
})
