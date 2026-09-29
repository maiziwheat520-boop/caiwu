import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { MonthlyReview } from '../types'
import { MonthlyReviewPanel } from './MonthlyReviewPanel'

const fixture = (month = '2026-09'): MonthlyReview => ({
  contract_version: 'ledgerbridge.monthly-review.v1', accounting_month: month,
  authority: 'NON_AUTHORITATIVE_REFERENCE',
  revision: 'synthetic-review-v1', confirmed_on: '2026-09-06', production_posted: false,
  policy: { expense_basis: 'ACTUAL_PAYMENT_MONTH', income_basis: 'SCREENSHOT_SETTLEMENT_PERIOD', legacy_payroll_through: '2026-07', workbench_payroll_from: '2026-08', workbench_status: 'NOT_CONNECTED' },
  adjustments: [{ id: 'synthetic-adjustment', label: '合成历史补记', month, amount_minor: 500, balance_effect_minor: -500, cash_effect_minor: 0, status: 'PENDING_ENTRY', note: '已扣除旧表重复项' }],
  bridges: [], pending: [], backtest: [], accepted_exceptions: [{ id: 'accepted', label: '合成例外', note: '接受上线，保留差异', status: 'ACCEPTED_OPEN' }],
  historical_payroll: [{ period: '2026-07', total_minor: 1000, stores: [{ label: '合成门店', amount_minor: 1000 }, { label: '合成空项', amount_minor: null }] }],
})

describe('monthly review evidence panel', () => {
  afterEach(() => vi.restoreAllMocks())
  it('keeps historical adjustments separate, displays unavailable live payroll and nullable sources', async () => {
    vi.spyOn(api, 'getMonthlyReview').mockResolvedValue(fixture())
    render(<MonthlyReviewPanel month="2026-09" />)
    expect(await screen.findByText('合成历史补记')).toBeInTheDocument()
    expect(screen.getByText(/未计入实时收支合计/)).toBeInTheDocument()
    expect(screen.getByText(/工作台结果尚未接入此页/)).toBeInTheDocument()
    expect(screen.getByText(/源表空白（不是零）/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/历史回测与已接受例外/))
    expect(screen.getByText(/此月尚无整表历史回测记录/)).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '历史核对参考报告' })).getByText(/合成例外/)).toBeInTheDocument()
  })
  it('does not show the previous month while a new request is pending', async () => {
    const read = vi.spyOn(api, 'getMonthlyReview').mockResolvedValueOnce(fixture())
    const { rerender } = render(<MonthlyReviewPanel month="2026-09" />)
    await screen.findByText('合成历史补记')
    read.mockImplementationOnce(() => new Promise(() => {}))
    rerender(<MonthlyReviewPanel month="2026-08" />)
    expect(screen.queryByText('合成历史补记')).not.toBeInTheDocument()
    expect(screen.getByText('正在读取历史核对参考报告…')).toBeInTheDocument()
  })
  it('reports source failure instead of inventing zero adjustments or completed reconciliation', async () => {
    vi.spyOn(api, 'getMonthlyReview').mockRejectedValue(new Error('unavailable'))
    render(<MonthlyReviewPanel month="2026-09" />)
    expect(await screen.findByText(/历史核对参考报告暂不可用/)).toBeInTheDocument()
    expect(screen.queryByText('¥0.00')).not.toBeInTheDocument()
  })
  it('rejects evidence returned for a different month', async () => {
    vi.spyOn(api, 'getMonthlyReview').mockResolvedValue(fixture('2026-08'))
    render(<MonthlyReviewPanel month="2026-09" />)
    expect(await screen.findByText(/历史核对参考报告暂不可用/)).toBeInTheDocument()
    expect(screen.queryByText('合成历史补记')).not.toBeInTheDocument()
  })
})
