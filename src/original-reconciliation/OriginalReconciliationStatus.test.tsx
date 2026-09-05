import { render, screen, waitFor, within } from '@testing-library/react'
import { Theme } from '@radix-ui/themes'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { CashReconciliation } from '../types'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import { OriginalReconciliationPage } from './OriginalReconciliationPage'

function prepare() {
  vi.spyOn(api, 'getOriginalReconciliation').mockImplementation(async ({ accountingMonth }) => ({
    ...originalReconciliationFixture, month: accountingMonth,
  }))
  return vi.spyOn(api, 'getCashReconciliation').mockImplementation(async (accountingMonth): Promise<CashReconciliation> => ({
    contract_version: 'ledgerbridge.cash-reconciliation.v2', accounting_month: accountingMonth,
    rules: [], rows: [], issues: [], eligible_fact_count: 0, matched_fact_count: 0,
    unmatched_fact_count: 0, conflicted_fact_count: 0, issue_count: 0, issues_truncated: false,
    totals: { income_minor: 0, expense_minor: 0, current_minor: 0 },
  }))
}

afterEach(() => vi.restoreAllMocks())

describe('monthly reconciliation status presentation', () => {
  it('describes an empty month without claiming all transactions were reconciled', async () => {
    prepare()
    render(<Theme><OriginalReconciliationPage onNavigate={() => undefined} /></Theme>)
    expect(await screen.findByText('本月暂无可对账流水')).toBeInTheDocument()
    expect(screen.queryByText('全部流水已归类')).not.toBeInTheDocument()
    expect(screen.getByText('切换月份，或检查本月账单是否已导入。')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '本月对账概览' })).getAllByText('0')).toHaveLength(4)
    expect(screen.getAllByText('¥0.00')).toHaveLength(3)
  })

  it('shows unknown values rather than zero when the read is unavailable', async () => {
    prepare().mockRejectedValue(new Error('测试读取暂不可用'))
    render(<Theme><OriginalReconciliationPage onNavigate={() => undefined} /></Theme>)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('测试读取暂不可用'))
    const overview = screen.getByRole('region', { name: '本月对账概览' })
    expect(within(overview).getAllByText('—')).toHaveLength(4)
    expect(within(overview).queryByText('0')).not.toBeInTheDocument()
    expect(screen.queryByText('¥0.00')).not.toBeInTheDocument()
  })
})
