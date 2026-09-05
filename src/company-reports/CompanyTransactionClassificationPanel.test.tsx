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

  it.each([['社保', 'SOCIAL_SECURITY'], ['税款', 'TAX']])('suggests %s for treasury debits but submits only after explicit confirmation', async (label, code) => {
    const treasuryPage = { ...page, items: [{ ...page.items[0], amount_minor: -120000, counterparty_name: '国家金库某支库' }] }
    vi.spyOn(api, 'getCompanyTransactionClassifications').mockResolvedValue(treasuryPage)
    const review = vi.spyOn(api, 'reviewCompanyTransactionClassification').mockResolvedValue({
      contract_version: 'ledgerbridge.company-transaction-classification-review.v1',
      transaction_ref: transactionRef, status: 'CONFIRMED', category_code: 'OPERATING_FEE',
      reporting_item_code: code, reporting_item_revision: 1, revision: 2, created: true,
    })
    render(<CompanyTransactionClassificationPanel csrfToken="csrf-test" />)
    const choice = await screen.findByRole('button', { name: label })
    expect(screen.getByLabelText(`确认分类 ${transactionRef}`)).toHaveValue('')
    expect(review).not.toHaveBeenCalled()
    fireEvent.click(choice)
    expect(screen.getByLabelText(`确认分类 ${transactionRef}`)).toHaveValue('OPERATING_FEE')
    expect(screen.getByLabelText(`营运费明细 ${transactionRef}`)).toHaveValue(code)
    expect(review).not.toHaveBeenCalled()
    const approve = screen.getByRole('button', { name: '确认本笔分类' })
    expect(approve).toBeDisabled()
    fireEvent.change(screen.getByLabelText(`审批理由 ${transactionRef}`), { target: { value: '已核对本笔缴款凭证' } })
    fireEvent.click(approve)
    await waitFor(() => expect(review).toHaveBeenCalledWith({ transaction: treasuryPage.items[0],
      categoryCode: 'OPERATING_FEE', reportingItemCode: code, reason: '已核对本笔缴款凭证', csrfToken: 'csrf-test' }))
  })

  it.each([[120000, '国家金库'], [0, '国库'], [-120000, '普通供应商']])('does not suggest treasury choices for amount %s and counterparty %s', async (amount, counterparty) => {
    vi.spyOn(api, 'getCompanyTransactionClassifications').mockResolvedValue({ ...page,
      items: [{ ...page.items[0], amount_minor: Number(amount), counterparty_name: String(counterparty) }] })
    render(<CompanyTransactionClassificationPanel csrfToken="csrf-test" />)
    await screen.findByText('1 条待确认')
    expect(screen.queryByRole('button', { name: '社保' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '税款' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(`确认分类 ${transactionRef}`), { target: { value: 'OPERATING_FEE' } })
    expect(screen.getByRole('option', { name: '社保' })).toHaveValue('SOCIAL_SECURITY')
  })

  it('requires an explicit per-transaction category and reason before approval', async () => {
    vi.spyOn(api, 'getCompanyTransactionClassifications').mockResolvedValue(page)
    const review = vi.spyOn(api, 'reviewCompanyTransactionClassification').mockResolvedValue({
      contract_version: 'ledgerbridge.company-transaction-classification-review.v1',
      transaction_ref: transactionRef,
      status: 'CONFIRMED',
      category_code: 'RELATED_PARTY_CURRENT',
      reporting_item_code: 'NON_OPERATING.RELATED_PARTY_CURRENT',
      reporting_item_revision: 1,
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
      reportingItemCode: null,
      reason: '人工核对后确认为往来款',
      csrfToken: 'csrf-test',
    }))
    expect(await screen.findByText('0 条待确认')).toBeInTheDocument()
    expect(screen.getByText('本笔流水已追加人工确认记录；未生成分录或过账。')).toBeInTheDocument()
  })

  it('requires the shared operating-fee detail for every company', async () => {
    const anotherCompanyPage: CompanyTransactionClassificationsResponse = {
      ...page,
      items: [{ ...page.items[0], company_name: '另一家公司' }],
    }
    vi.spyOn(api, 'getCompanyTransactionClassifications').mockResolvedValue(anotherCompanyPage)
    const review = vi.spyOn(api, 'reviewCompanyTransactionClassification').mockResolvedValue({
      contract_version: 'ledgerbridge.company-transaction-classification-review.v1',
      transaction_ref: transactionRef,
      status: 'CONFIRMED',
      category_code: 'OPERATING_FEE',
      reporting_item_code: 'TAX',
      reporting_item_revision: 1,
      revision: 2,
      created: true,
    })

    render(<CompanyTransactionClassificationPanel csrfToken="csrf-test" />)

    expect(await screen.findByText('另一家公司')).toBeInTheDocument()
    const approve = screen.getByRole('button', { name: '确认本笔分类' })
    fireEvent.change(screen.getByLabelText(`确认分类 ${transactionRef}`), {
      target: { value: 'OPERATING_FEE' },
    })
    fireEvent.change(screen.getByLabelText(`审批理由 ${transactionRef}`), {
      target: { value: '人工核对税费缴款' },
    })
    expect(approve).toBeDisabled()
    expect(screen.getByRole('option', { name: '银行手续费' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '税费' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: '其他营运费' })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(`营运费明细 ${transactionRef}`), {
      target: { value: 'TAX' },
    })
    fireEvent.click(approve)

    await waitFor(() => expect(review).toHaveBeenCalledWith({
      transaction: anotherCompanyPage.items[0],
      categoryCode: 'OPERATING_FEE',
      reportingItemCode: 'TAX',
      reason: '人工核对税费缴款',
      csrfToken: 'csrf-test',
    }))
  })
})
