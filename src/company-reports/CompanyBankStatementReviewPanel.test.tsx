import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { api } from '../api'
import type { CompanyBankStatementsResponse } from '../types'
import { CompanyBankStatementReviewPanel } from './CompanyBankStatementReviewPanel'

const statements: CompanyBankStatementsResponse = {
  contract_version: 'ledgerbridge.company-bank-statements-bff.v1',
  statements: Array.from({ length: 8 }, (_, index) => ({
    statement_ref: `${index + 1}0000000-0000-4000-8000-00000000000${index + 1}`,
    managed_account_ref: `account-${index + 1}`,
    institution_code: 'MYBANK',
    account_suffix: `${2000 + index}`,
    period_start: '2025-09-01',
    period_end: '2026-09-01',
    transaction_count: index + 1,
    review_status: 'CONFIRMED',
    review_revision: 1,
    company_name: `公司 ${index + 1}`,
  })),
}

describe('CompanyBankStatementReviewPanel', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps a fully confirmed statement list collapsed until requested', async () => {
    vi.spyOn(api, 'getCompanyBankStatements').mockResolvedValue(statements)

    render(<CompanyBankStatementReviewPanel csrfToken="csrf-test" />)

    expect(await screen.findByText('全部已确认')).toBeInTheDocument()
    expect(screen.queryByText('公司 1')).not.toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: '查看 8 份账单明细' }))
    expect(screen.getByText('公司 1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起账单明细' })).toBeInTheDocument()
  })

  it('replaces backend implementation details with an actionable Chinese read error', async () => {
    vi.spyOn(api, 'getCompanyBankStatements').mockRejectedValue(
      new Error('LedgerBridge Core request failed'),
    )

    render(<CompanyBankStatementReviewPanel csrfToken="csrf-test" />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('8 份公司账单中至少 1 份暂时无法读取，请重试')
    expect(alert).not.toHaveTextContent('LedgerBridge Core request failed')
    expect(screen.getByRole('button', { name: '重试公司流水' })).toBeInTheDocument()
  })
})
