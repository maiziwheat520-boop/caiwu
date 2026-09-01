import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { api } from '../api'
import type { CompanyReportsResponse } from '../types'
import { CompanyReportsPage } from './CompanyReportsPage'

const reports: CompanyReportsResponse = {
  contract_version: 'ledgerbridge.company-reports-bff.v1',
  from_month: '2026-05',
  to_month: '2026-05',
  posted_ledger_status: 'AVAILABLE',
  layers: [{
    contract_version: 'ledgerbridge.company-report.v1',
    basis: 'CONFIRMED_CANDIDATE',
    from_month: '2026-05',
    to_month: '2026-05',
    items: [{
      company_ref: '10000000-0000-4000-8000-000000000001',
      company_name: 'LedgerBridge controlled reconciliation',
      currency: 'CNY',
      business_unit_breakdown_status: 'UNAVAILABLE_ATTRIBUTION_PENDING',
      metrics: {
        basis: 'CONFIRMED_CANDIDATE',
        confirmed_positive_minor: 0,
        confirmed_negative_minor: 0,
        confirmed_net_minor: 0,
        confirmed_count: 216,
        source_count: 6,
      },
      pending_review_count: 0,
      attribution_pending_count: 216,
      missing_material_count: null,
      taxonomy_version: null,
      balance: {
        balance_basis: 'UNAVAILABLE',
        opening_balance_minor: null,
        closing_balance_minor: null,
        gap: 'AUTHORITATIVE_BALANCE_UNAVAILABLE',
      },
      months: [],
    }],
  }],
}

describe('CompanyReportsPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('makes missing company attribution explicit instead of presenting a complete report', async () => {
    vi.spyOn(api, 'getCompanyReports').mockResolvedValue(reports)

    render(<CompanyReportsPage />)

    expect(await screen.findByText('待完成公司归属')).toBeInTheDocument()
    expect(screen.getByText(/216 条已确认来源待账户或经济性质归属/)).toBeInTheDocument()
    expect(screen.getByText('正式账簿尚未接入')).toBeInTheDocument()
  })
})
