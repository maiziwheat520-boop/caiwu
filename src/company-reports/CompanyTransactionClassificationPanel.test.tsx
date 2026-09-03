import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from '../api'
import type { CompanyTransactionClassificationsResponse } from '../types'
import { CompanyTransactionClassificationPanel } from './CompanyTransactionClassificationPanel'

const transactionRef = '90000000-0000-4000-8000-000000000009'
const page: CompanyTransactionClassificationsResponse = {
  contract_version: 'ledgerbridge.company-transaction-classifications-bff.v1',
  items: [{
    transaction_ref: transactionRef,
    entity_ref: '10000000-0000-4000-8000-000000000001',
    company_name: '演示公司',
    occurred_at: '2026-08-03T10:30:00+08:00',
    amount_minor: 120000,
    currency: 'CNY',
    counterparty_name: '待确认对方',
    transaction_name: '转账',
    status: 'PENDING',
    category_code: null,
    cashflow_role: null,
    revision: 1,
    source: 'AUTO_RULE',
    rule_version: 'company-transaction-rules.v1',
  }],
}

describe('CompanyTransactionClassificationPanel', () => {
  afterEach(() => vi.restoreAllMocks())

  it('requires an explicit per-transaction category and reason before approval', async () => {
    vi.spyOn(api, 'getCompanyTransactionClassifications').mockResolvedValue(page)
    const review = vi.spyOn(api, 'reviewCompanyTransactionClassification').mockResolvedValue({
      contract_version: 'ledgerbridge.company-transaction-classification-review.v1',
      transaction_ref: transactionRef,
      status: 'CONFIRMED',
      category_code: 'RELATED_PARTY_CURRENT',
      revision: 2,
      created: true,
    })

    render(<CompanyTransactionClassificationPanel csrfToken="csrf-test" />)

    expect(await screen.findByText('1 条待确认')).toBeInTheDocument()
    const approve = screen.getByRole('button', { name: '确认本笔分类' })
    expect(approve).toBeDisabled()
    fireEvent.change(screen.getByLabelText(`确认分类 ${transactionRef}`), {
      target: { value: 'RELATED_PARTY_CURRENT' },
    })
    expect(approve).toBeDisabled()
    fireEvent.change(screen.getByLabelText(`审批理由 ${transactionRef}`), {
      target: { value: '人工核对后确认为往来款' },
    })
    fireEvent.click(approve)

    await waitFor(() => expect(review).toHaveBeenCalledWith({
      transaction: page.items[0],
      categoryCode: 'RELATED_PARTY_CURRENT',
      reason: '人工核对后确认为往来款',
      csrfToken: 'csrf-test',
    }))
    expect(await screen.findByText('0 条待确认')).toBeInTheDocument()
    expect(screen.getByText('本笔流水已追加人工确认记录；未生成分录或过账。')).toBeInTheDocument()
  })
})
