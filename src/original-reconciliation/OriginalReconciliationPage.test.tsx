import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { api } from '../api'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import type { Candidate } from '../types'
import { classifyCandidate, OriginalReconciliationPage } from './OriginalReconciliationPage'

function candidate(overrides: Partial<Candidate>): Candidate {
  return {
    id: '10000000-0000-4000-8000-000000000006',
    shortId: 'C-JUNE',
    revision: 1,
    source: '中行账单（复核材料）',
    sourceChannel: 'outlook',
    receivedAt: '6月30日 12:00',
    status: 'PENDING',
    accountingMonth: '2026-06',
    summary: '建设银行 | 2026-06-30 | 收入 | 平台结算 | 携程 | 对公账户 | 交易成功',
    category: '建设银行收入',
    categoryCode: 'CCB_BANK_INCOME',
    businessUnit: '示例酒店',
    businessUnitRef: 'hotel-example',
    amount: 123.45,
    amountMinor: 12_345,
    evidence: [],
    confidence: 0.95,
    blockers: [],
    reviewRisks: [],
    reviewEvents: [],
    incomplete: false,
    conflict: false,
    raw: {
      id: '10000000-0000-4000-8000-000000000006',
      short_id: 'C-JUNE',
      revision: 1,
      status: 'PENDING',
      source_channel: 'outlook',
      source_system: 'ccb_statement_export',
      source_message_id: 'statement-june',
      received_at: '2026-06-30T04:00:00Z',
      business_unit: '示例酒店',
      business_unit_ref: 'hotel-example',
      category: '建设银行收入',
      category_code: 'CCB_BANK_INCOME',
      amount_minor: 12_345,
      currency: 'CNY',
      accounting_month: '2026-06',
      summary: '建设银行 | 2026-06-30 | 收入 | 平台结算 | 携程 | 对公账户 | 交易成功',
      confidence_basis_points: 9500,
      evidence: [],
      blockers: [],
      review_risks: [],
    },
    ...overrides,
  }
}

const juneCandidates = [
  candidate({ shortId: 'C-INCOME', summary: '建设银行 | 2026-06-30 | 收入 | 平台结算 | 携程 | 对公账户 | 交易成功' }),
  candidate({
    id: '10000000-0000-4000-8000-000000000007',
    shortId: 'C-EXPENSE',
    category: '建设银行支出',
    categoryCode: 'CCB_BANK_EXPENSE',
    amountMinor: -6_800,
    summary: '建设银行 | 2026-06-29 | 支出 | 商户消费 | 布草供应商 | 对公账户 | 交易成功',
  }),
  candidate({
    id: '10000000-0000-4000-8000-000000000008',
    shortId: 'C-CURRENT',
    category: '内部往来',
    categoryCode: 'TRANSFER',
    amountMinor: -20_000,
    summary: '建设银行 | 2026-06-28 | 支出 | 转账 | 关联公司 | 对公账户 | 交易成功',
  }),
]

describe('OriginalReconciliationPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('presents statement data as income, expense and current-account work lanes', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)

    render(<OriginalReconciliationPage candidates={juneCandidates} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    expect(await screen.findByRole('heading', { name: '收支与往来对账' })).toBeInTheDocument()
    const lanes = screen.getByRole('tablist', { name: '业务性质' })
    expect(within(lanes).getByRole('tab', { name: /收入/ })).toHaveAttribute('aria-selected', 'true')
    expect(within(lanes).getByRole('tab', { name: /支出/ })).toBeInTheDocument()
    expect(within(lanes).getByRole('tab', { name: /往来款/ })).toBeInTheDocument()
    expect(screen.getByText('往来款不计入收入或支出')).toBeInTheDocument()
    expect(screen.queryByText(/Excel|截图导入|受控导入/)).not.toBeInTheDocument()
  })

  it('switches lanes and opens a statement-export candidate', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
    const onOpenCandidate = vi.fn()

    render(
      <OriginalReconciliationPage
        candidates={juneCandidates}
        onNavigate={vi.fn()}
        onOpenCandidate={onOpenCandidate}
      />,
    )

    expect(await screen.findByText('C-INCOME')).toBeInTheDocument()
    expect(screen.queryByText('C-EXPENSE')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /支出/ }))
    expect(screen.getByText('C-EXPENSE')).toBeInTheDocument()
    expect(screen.queryByText('C-INCOME')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /往来款/ }))
    expect(screen.getByText('C-CURRENT')).toBeInTheDocument()
    expect(screen.getByText('内部往来')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开事项 C-CURRENT' }))
    expect(onOpenCandidate).toHaveBeenCalledWith(juneCandidates[2])
  })

  it('keeps ambiguous statement rows out of the three financial totals', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
    const ambiguous = candidate({
      id: '10000000-0000-4000-8000-000000000009',
      shortId: 'C-UNCLASSIFIED',
      category: '支付宝交易复核',
      categoryCode: 'ALIPAY_TRANSACTION_REVIEW',
      amountMinor: 5_000,
      summary: '支付宝账单待核对',
    })

    render(<OriginalReconciliationPage candidates={[...juneCandidates, ambiguous]} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    expect(await screen.findByRole('button', { name: '查看 1 笔待归类事项' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看 1 笔待归类事项' }))
    expect(screen.getByText('C-UNCLASSIFIED')).toBeInTheDocument()
    expect(screen.getByText('无法确认业务性质')).toBeInTheDocument()
  })

  it('shows the confirmed statement-source mapping separately from financial classification', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)

    render(<OriginalReconciliationPage candidates={juneCandidates} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    const sourceRegistry = await screen.findByRole('region', { name: '已确认账单来源' })
    expect(within(sourceRegistry).getByText('携程、美团')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('个人中国银行')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('文杰房租')).toBeInTheDocument()
    expect(within(sourceRegistry).getAllByText('薇旭网商银行企业账户')).toHaveLength(2)
    expect(within(sourceRegistry).getByText('陈展武（老爸）、林素美（老妈）')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText(/消杀 4,300 元记景怡公账支出/)).toBeInTheDocument()
  })

  it('applies the corrected historical rules before legacy signs and generic transfer terms', () => {
    const disinfection = classifyCandidate(candidate({
      category: '结余滚动',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: 430_000,
      summary: '景怡公账 | 2026-06-30 | 收入 | 消杀 | 原表正号待校正',
    }))
    const dividend = classifyCandidate(candidate({
      category: '股东分红',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: -1_000_000,
      summary: '网商银行 | 2026-07-31 | 支出 | 分红 | 股东资金流出',
    }))
    const parentPayroll = classifyCandidate(candidate({
      category: '工资',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: -500_000,
      summary: '建设银行 | 2026-07-31 | 支出 | 林素美工资 | 实际工资',
    }))
    const ordinaryPayrollIncome = classifyCandidate(candidate({
      category: '个人工资收入',
      categoryCode: 'PAYROLL_INCOME',
      amountMinor: 800_000,
      summary: '建设银行 | 2026-07-31 | 收入 | 工资 | 本人工资收入',
    }))
    const otherMonthDisinfection = classifyCandidate(candidate({
      accountingMonth: '2026-07',
      category: '结余滚动',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: 430_000,
      summary: '景怡公账 | 2026-07-31 | 收入 | 消杀 | 另一月份待核对',
    }))
    const parentNonTransferIncome = classifyCandidate(candidate({
      category: '其他收入',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: 200_000,
      summary: '建设银行 | 2026-07-31 | 收入 | 林素美退款 | 业务退款',
    }))

    expect(disinfection.flowKind).toBe('expense')
    expect(disinfection.signedAmountMinor).toBe(-430_000)
    expect(dividend.flowKind).toBe('current')
    expect(parentPayroll.flowKind).toBe('expense')
    expect(ordinaryPayrollIncome.flowKind).toBe('income')
    expect(otherMonthDisinfection.flowKind).toBe('income')
    expect(parentNonTransferIncome.flowKind).toBe('income')
  })
})
