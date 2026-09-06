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
    expect(screen.getByText('本月没有收入事项')).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: '本月对账概览' })
    expect(within(overview).getAllByText('0')).toHaveLength(3)
    expect(within(overview).getByText('待核验')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '收入分类汇总' })).toHaveTextContent('¥0.00')
    expect(screen.getByRole('region', { name: '支出分类汇总' })).toHaveTextContent('¥0.00')
    const current = screen.getByRole('region', { name: '往来款分类汇总' })
    expect(current).toHaveTextContent('流入 ¥0.00')
    expect(current).toHaveTextContent('流出 ¥0.00')
    expect(current).toHaveTextContent('净流入 ¥0.00')
  })

  it('shows unknown values rather than zero when the read is unavailable', async () => {
    prepare().mockRejectedValue(new Error('测试读取暂不可用'))
    render(<Theme><OriginalReconciliationPage onNavigate={() => undefined} /></Theme>)
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('测试读取暂不可用'))
    const overview = screen.getByRole('region', { name: '本月对账概览' })
    expect(within(overview).getAllByText('—')).toHaveLength(3)
    expect(within(overview).getByText('待核验')).toBeInTheDocument()
    expect(within(overview).queryByText('0')).not.toBeInTheDocument()
    expect(screen.queryByText('¥0.00')).not.toBeInTheDocument()
  })
})
