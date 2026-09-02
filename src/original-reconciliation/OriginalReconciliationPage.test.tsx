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
    source: '照片凭证',
    sourceChannel: 'controlled_upload',
    receivedAt: '6月30日 12:00',
    status: 'PENDING',
    accountingMonth: '2026-06',
    summary: '六月平台收入 | 原表 26.6!B3',
    category: '房费收入',
    categoryCode: 'ROOM_INCOME',
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
      source_channel: 'controlled_upload',
      source_system: 'original_reconciliation_xlsx',
      source_message_id: 'statement-june',
      received_at: '2026-06-30T04:00:00Z',
      business_unit: '示例酒店',
      business_unit_ref: 'hotel-example',
      category: '房费收入',
      category_code: 'ROOM_INCOME',
      amount_minor: 12_345,
      currency: 'CNY',
      accounting_month: '2026-06',
      summary: '六月平台收入 | 原表 26.6!B3',
      confidence_basis_points: 9500,
      evidence: [],
      blockers: [],
      review_risks: [],
    },
    ...overrides,
  }
}

const juneCandidates = [
  candidate({ shortId: 'C-INCOME' }),
  candidate({
    id: '10000000-0000-4000-8000-000000000007',
    shortId: 'C-EXPENSE',
    category: '经营支出',
    categoryCode: 'OPERATING_EXPENSE',
    amountMinor: -6_800,
    summary: '六月经营支出 | 原表 26.6!D24',
  }),
  candidate({
    id: '10000000-0000-4000-8000-000000000008',
    shortId: 'C-CURRENT',
    category: '往来款',
    categoryCode: 'CURRENT_ACCOUNT',
    amountMinor: -20_000,
    summary: '六月往来款 | 原表 26.6!E24',
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
    expect(screen.queryByRole('button', { name: /上传|提交历史表格|截图导入/ })).not.toBeInTheDocument()
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
    expect(screen.getAllByText('往来款').length).toBeGreaterThan(0)
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

  it('fails closed for ordinary bank, platform, purchase, reimbursement and payroll candidates', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
    const externalCandidates = [
      candidate({
        id: '10000000-0000-4000-8000-000000000010',
        shortId: 'C-BANK',
        accountingMonth: '2026-07',
        categoryCode: 'BANK_INCOME',
        summary: '中行 | 2026-07-01 | 收入 | 普通银行流水',
        raw: { ...juneCandidates[0].raw, source_system: 'boc_transaction_statement', accounting_month: '2026-07' },
      }),
      candidate({
        id: '10000000-0000-4000-8000-000000000011',
        shortId: 'C-PLATFORM',
        categoryCode: 'PLATFORM_INCOME',
        summary: '支付宝 | 收入 | 旧表外平台流水',
        raw: { ...juneCandidates[0].raw, source_system: 'alipay_export' },
      }),
      candidate({
        id: '10000000-0000-4000-8000-000000000012',
        shortId: 'C-PURCHASE',
        categoryCode: 'PURCHASE_EXPENSE',
        summary: '网商银行 | 支出 | 采购',
        raw: { ...juneCandidates[0].raw, source_system: 'mybank_xlsx_export' },
      }),
      candidate({
        id: '10000000-0000-4000-8000-000000000013',
        shortId: 'C-REIMBURSEMENT',
        categoryCode: 'REIMBURSEMENT_EXPENSE',
        summary: '农行 | 支出 | 实际报销',
        raw: { ...juneCandidates[0].raw, source_system: 'abc_personal_pdf_export' },
      }),
      candidate({
        id: '10000000-0000-4000-8000-000000000014',
        shortId: 'C-PAYROLL',
        categoryCode: 'PAYROLL_EXPENSE',
        summary: '建行 | 支出 | 银行代发工资',
        raw: { ...juneCandidates[0].raw, source_system: 'ccb_personal_xls_export' },
      }),
    ]

    render(<OriginalReconciliationPage candidates={[...juneCandidates, ...externalCandidates]} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    expect(await screen.findByLabelText('选择对账月份')).toHaveValue('2026-06')
    expect(screen.getByText(/3 笔已按落库规则归类的旧表项目/)).toBeInTheDocument()
    expect(screen.queryByText('C-BANK')).not.toBeInTheDocument()
    expect(screen.queryByText('C-PLATFORM')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /支出/ }))
    expect(screen.queryByText('C-PURCHASE')).not.toBeInTheDocument()
    expect(screen.queryByText('C-REIMBURSEMENT')).not.toBeInTheDocument()
    expect(screen.queryByText('C-PAYROLL')).not.toBeInTheDocument()
  })

  it('shows income and expense source rules without treating whole statements as page input', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)

    render(<OriginalReconciliationPage candidates={juneCandidates} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    const sourceRegistry = await screen.findByRole('region', { name: '旧表项目取数来源' })
    expect(within(sourceRegistry).getByText('收入 · 携程')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('收入 · 美团')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('个人中国银行 · 赫程旅行社入账')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('个人中国银行 · 北京钱袋宝入账')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('收入 · 文杰房租')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('薇旭网商银行企业账户')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('支出 · 布草')).toBeInTheDocument()
    expect(within(sourceRegistry).getAllByText('景怡农业银行')).toHaveLength(2)
    expect(within(sourceRegistry).getByText('相邻工资表已核对最终数据（权威源）')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText('陈展武（老爸）、林素美（老妈）')).toBeInTheDocument()
    expect(within(sourceRegistry).getByText(/26\.6、26\.7 消杀均记景怡公账支出/)).toBeInTheDocument()
  })

  it('uses reviewed Core category codes and never infers classification from summaries', () => {
    const ambiguousPayroll = classifyCandidate(candidate({
      category: '工资',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: -500_000,
      summary: '建行 | 支出 | 林素美工资',
    }))
    const ambiguousDisinfection = classifyCandidate(candidate({
      category: '消杀',
      categoryCode: 'MANUAL_REVIEW',
      amountMinor: 430_000,
      summary: '景怡公账 | 收入 | 消杀',
    }))
    const income = classifyCandidate(candidate({
      categoryCode: 'ROOM_INCOME',
      summary: '摘要即使写支出也不覆盖 Core 类别',
    }))
    const expense = classifyCandidate(candidate({
      categoryCode: 'OPERATING_EXPENSE',
      amountMinor: 6_800,
      summary: '摘要即使写收入也不覆盖 Core 类别',
    }))
    const current = classifyCandidate(candidate({ categoryCode: 'CURRENT_ACCOUNT' }))

    expect(ambiguousPayroll.flowKind).toBe('unclassified')
    expect(ambiguousDisinfection.flowKind).toBe('unclassified')
    expect(income.flowKind).toBe('income')
    expect(expense.flowKind).toBe('expense')
    expect(expense.signedAmountMinor).toBe(-6_800)
    expect(current.flowKind).toBe('current')
  })
})
