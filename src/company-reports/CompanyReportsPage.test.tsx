import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { api } from '../api'
import type { CompanyReportsResponse } from '../types'
import { CompanyReportsPage } from './CompanyReportsPage'

const reports: CompanyReportsResponse = {
  contract_version: 'ledgerbridge.company-reports-bff.v2',
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
        confirmed_positive_minor: 10000,
        confirmed_negative_minor: -3000,
        confirmed_net_minor: 7000,
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
  compositions: [{
    contract_version: 'ledgerbridge.company-report-composition.v1',
    basis: 'CONFIRMED_CANDIDATE',
    from_month: '2026-05',
    to_month: '2026-05',
    items: [{
      company_ref: '10000000-0000-4000-8000-000000000001',
      company_name: 'LedgerBridge controlled reconciliation',
      currency: 'CNY',
      basis: 'CONFIRMED_CANDIDATE',
      positive: {
        total_minor: 10000,
        fact_count: 2,
        items: [
          { category_code: 'ROOM', category_label: '客房收入', amount_minor: 7500, fact_count: 1 },
          { category_code: 'OTHER', category_label: '其他收入', amount_minor: 2500, fact_count: 1 },
        ],
      },
      negative: {
        total_minor: 3000,
        fact_count: 1,
        items: [
          { category_code: 'SUPPLY', category_label: '经营物料', amount_minor: 3000, fact_count: 1 },
        ],
      },
    }],
  }],
}

describe('CompanyReportsPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('makes missing company attribution explicit instead of presenting a complete report', async () => {
    vi.spyOn(api, 'getCompanyReports').mockResolvedValue(reports)

    render(<CompanyReportsPage csrfToken="csrf-test" />)

    expect(await screen.findByText('待完成公司归属')).toBeInTheDocument()
    expect(screen.getByText(/216 条已确认来源待账户或经济性质归属/)).toBeInTheDocument()
    expect(screen.getByText('会计账簿尚未接入')).toBeInTheDocument()
  })

  it('shows totals and ranked category shares for the selected test company', async () => {
    vi.spyOn(api, 'getCompanyReports').mockResolvedValue(reports)

    render(<CompanyReportsPage csrfToken="csrf-test" />)

    const dashboard = await screen.findByRole('region', {
      name: 'LedgerBridge controlled reconciliation 财务汇总',
    })
    expect(within(dashboard).getByText('总收入')).toBeInTheDocument()
    expect(within(dashboard).getByText('¥100.00')).toBeInTheDocument()
    expect(within(dashboard).getAllByText('¥30.00')).toHaveLength(2)
    expect(within(dashboard).getByRole('img', { name: '客房收入 75.0%' })).toBeInTheDocument()
    expect(within(dashboard).getByRole('img', { name: '其他收入 25.0%' })).toBeInTheDocument()
  })

  it('defaults to confirmed account cash flow when statement facts are available', async () => {
    const response = structuredClone(reports)
    const statement = structuredClone(response.layers[0].items[0])
    statement.metrics = {
      basis: 'ACCOUNT_STATEMENT',
      cash_inflow_minor: 616021531,
      cash_outflow_minor: 593078786,
      net_cash_flow_minor: 22942745,
      confirmed_transaction_count: 1042,
      statement_count: 6,
    }
    statement.pending_review_count = 0
    statement.attribution_pending_count = 0
    response.layers.push({
      contract_version: 'ledgerbridge.company-report.v1',
      basis: 'ACCOUNT_STATEMENT',
      from_month: response.from_month,
      to_month: response.to_month,
      items: [statement],
    })
    vi.spyOn(api, 'getCompanyReports').mockResolvedValue(response)

    render(<CompanyReportsPage csrfToken="csrf-test" />)

    const dashboard = await screen.findByRole('region', {
      name: 'LedgerBridge controlled reconciliation 财务汇总',
    })
    expect(screen.getByRole('button', { name: '正式银行流水' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(dashboard).getByText('¥6,160,215.31')).toBeInTheDocument()
    expect(within(dashboard).getByText('¥5,930,787.86')).toBeInTheDocument()
    expect(within(dashboard).getByText('¥229,427.45')).toBeInTheDocument()
    expect(within(dashboard).getAllByText('账户流水尚未完成收支类型分类，当前仅展示现金流总额。')).toHaveLength(2)
    const statementSummary = screen.getByRole('region', {
      name: 'LedgerBridge controlled reconciliation 账户流水汇总',
    })
    expect(within(statementSummary).getByText('正式银行流水')).toBeInTheDocument()
    expect(within(statementSummary).getByText('正式数据')).toBeInTheDocument()
    expect(within(statementSummary).getByText('账户流入')).toBeInTheDocument()
    expect(within(statementSummary).getByText('账户流出')).toBeInTheDocument()
    expect(within(statementSummary).getByText('净现金流')).toBeInTheDocument()
    expect(screen.queryByText('测试汇总收入')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', {
      name: 'LedgerBridge controlled reconciliation 正式财务总额',
    })).not.toBeInTheDocument()
  })

  it('switches companies and requests an applied month range', async () => {
    const response = structuredClone(reports)
    response.layers[0].items[0].company_name = '薇旭公司'
    response.compositions![0].items[0].company_name = '薇旭公司'
    const secondReport = structuredClone(response.layers[0].items[0])
    secondReport.company_ref = '20000000-0000-4000-8000-000000000002'
    secondReport.company_name = '景怡公司'
    if (secondReport.metrics.basis === 'CONFIRMED_CANDIDATE') {
      secondReport.metrics.confirmed_positive_minor = 22000
      secondReport.metrics.confirmed_negative_minor = -5000
      secondReport.metrics.confirmed_net_minor = 17000
    }
    response.layers[0].items.push(secondReport)
    const secondComposition = structuredClone(response.compositions![0].items[0])
    secondComposition.company_ref = secondReport.company_ref
    secondComposition.company_name = secondReport.company_name
    if (secondComposition.basis === 'CONFIRMED_CANDIDATE') {
      secondComposition.positive = {
        total_minor: 22000,
        fact_count: 1,
        items: [{ category_code: 'SERVICE', category_label: '服务收入', amount_minor: 22000, fact_count: 1 }],
      }
      secondComposition.negative = {
        total_minor: 5000,
        fact_count: 1,
        items: [{ category_code: null, category_label: null, amount_minor: 5000, fact_count: 1 }],
      }
    }
    response.compositions![0].items.push(secondComposition)
    const getReports = vi.spyOn(api, 'getCompanyReports').mockResolvedValue(response)

    render(<CompanyReportsPage csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '薇旭公司 财务汇总' })
    fireEvent.change(screen.getByRole('combobox', { name: '选择公司' }), {
      target: { value: secondReport.company_ref },
    })

    const dashboard = screen.getByRole('region', { name: '景怡公司 财务汇总' })
    expect(within(dashboard).getAllByText('¥220.00')).toHaveLength(2)
    expect(within(dashboard).getByText('未分类')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('开始月份'), { target: { value: '2026-03' } })
    fireEvent.change(screen.getByLabelText('结束月份'), { target: { value: '2026-05' } })
    fireEvent.click(screen.getByRole('button', { name: '应用期间' }))
    await waitFor(() => expect(getReports).toHaveBeenLastCalledWith({
      fromMonth: '2026-03',
      toMonth: '2026-05',
    }))
  })

  it('lists all five server-authorized companies without browser scope requests', async () => {
    const response = structuredClone(reports)
    response.layers[0].items[0].company_name = '公司一'
    response.compositions![0].items[0].company_name = '公司一'
    for (let ordinal = 2; ordinal <= 5; ordinal += 1) {
      const company = structuredClone(response.layers[0].items[0])
      company.company_ref = `${ordinal}0000000-0000-4000-8000-00000000000${ordinal}`
      company.company_name = `公司${['零', '一', '二', '三', '四', '五'][ordinal]}`
      response.layers[0].items.push(company)
      const composition = structuredClone(response.compositions![0].items[0])
      composition.company_ref = company.company_ref
      composition.company_name = company.company_name
      response.compositions![0].items.push(composition)
    }
    const getReports = vi.spyOn(api, 'getCompanyReports').mockResolvedValue(response)

    render(<CompanyReportsPage csrfToken="csrf-test" />)

    const selector = await screen.findByRole('combobox', { name: '选择公司' })
    expect(within(selector).getAllByRole('option')).toHaveLength(5)
    expect(within(selector).getByRole('option', { name: '公司五' })).toBeInTheDocument()
    fireEvent.change(selector, {
      target: { value: '50000000-0000-4000-8000-000000000005' },
    })
    expect(screen.getByRole('region', { name: '公司五 财务汇总' })).toBeInTheDocument()
    expect(getReports).toHaveBeenCalledTimes(1)
    expect(getReports).toHaveBeenCalledWith({})
  })

  it('uses posted ledger totals and categories after switching to the formal basis', async () => {
    const response = structuredClone(reports)
    const postedReport = structuredClone(response.layers[0].items[0])
    postedReport.metrics = {
      basis: 'POSTED_LEDGER',
      revenue_minor: 12000,
      expense_minor: 4500,
      profit_minor: 7500,
      posted_entry_count: 3,
      source_count: 2,
    }
    postedReport.pending_review_count = 0
    postedReport.attribution_pending_count = 0
    response.layers.push({
      contract_version: 'ledgerbridge.company-report.v1',
      basis: 'POSTED_LEDGER',
      from_month: response.from_month,
      to_month: response.to_month,
      items: [postedReport],
    })
    response.compositions!.push({
      contract_version: 'ledgerbridge.company-report-composition.v1',
      basis: 'POSTED_LEDGER',
      from_month: response.from_month,
      to_month: response.to_month,
      items: [{
        company_ref: postedReport.company_ref,
        company_name: postedReport.company_name,
        currency: postedReport.currency,
        basis: 'POSTED_LEDGER',
        revenue: {
          total_minor: 12000,
          fact_count: 2,
          items: [{ category_code: 'ROOM', category_label: '客房收入', amount_minor: 12000, fact_count: 2 }],
        },
        expense: {
          total_minor: 4500,
          fact_count: 1,
          items: [{ category_code: 'UTILITY', category_label: '水电费', amount_minor: 4500, fact_count: 1 }],
        },
      }],
    })
    vi.spyOn(api, 'getCompanyReports').mockResolvedValue(response)

    render(<CompanyReportsPage csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: 'LedgerBridge controlled reconciliation 财务汇总' })
    fireEvent.click(screen.getByRole('button', { name: '正式账簿' }))

    const dashboard = screen.getByRole('region', {
      name: 'LedgerBridge controlled reconciliation 财务汇总',
    })
    expect(within(dashboard).getAllByText('¥120.00')).toHaveLength(2)
    expect(within(dashboard).getAllByText('¥45.00')).toHaveLength(2)
    expect(within(dashboard).getByText('¥75.00')).toBeInTheDocument()
    expect(within(dashboard).getByRole('img', { name: '水电费 100.0%' })).toBeInTheDocument()
  })
})
