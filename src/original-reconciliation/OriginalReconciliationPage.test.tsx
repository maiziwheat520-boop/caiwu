import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as monthPolicy from '../shared/monthPolicy'
import { api } from '../api'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import type { CashReconciliation } from '../types'
import { OriginalReconciliationPage } from './OriginalReconciliationPage'

const cashReconciliation: CashReconciliation = {
  contract_version: 'ledgerbridge.cash-reconciliation.v2',
  accounting_month: '2026-09',
  rules: [
    { rule_key: 'income.hotel-a', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.hotel-a', flow_kind: 'INCOME', business_unit_label: '示例门店 A', item_label: '平台实收', match_pattern: 'synthetic-income', amount_direction: 'CREDIT', effective_from: '2026-01-01', effective_to: null },
    { rule_key: 'expense.hotel-a', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.hotel-a', flow_kind: 'EXPENSE', business_unit_label: '示例门店 A', item_label: '布草', match_pattern: 'synthetic-expense', amount_direction: 'DEBIT', effective_from: '2026-01-01', effective_to: null },
    { rule_key: 'current.family', source_kind: 'CANDIDATE', source_ref: 'wechat.synthetic', flow_kind: 'CURRENT', business_unit_label: '示例门店 A', item_label: '往来款', match_pattern: 'synthetic-current', amount_direction: 'ANY', effective_from: '2026-01-01', effective_to: null },
  ],
  rows: [
    { rule_key: 'income.hotel-a', flow_kind: 'INCOME', business_unit_label: '示例门店 A', item_label: '平台实收', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.hotel-a', transaction_count: 2, amount_minor: 12_000, facts: [
      { fact_ref: 'fact-income-1', occurred_on: '2026-09-01', amount_minor: 5_000 },
      { fact_ref: 'fact-income-2', occurred_on: '2026-09-02', amount_minor: 7_000 },
    ] },
    { rule_key: 'expense.hotel-a', flow_kind: 'EXPENSE', business_unit_label: '示例门店 A', item_label: '布草', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.hotel-a', transaction_count: 1, amount_minor: 3_000, facts: [{ fact_ref: 'fact-expense-1', occurred_on: '2026-09-02', amount_minor: -3_000 }] },
    { rule_key: 'current.family', flow_kind: 'CURRENT', business_unit_label: '示例门店 A', item_label: '往来款', source_kind: 'CANDIDATE', source_ref: 'wechat.synthetic', transaction_count: 1, amount_minor: 2_000, facts: [{ fact_ref: 'fact-current-1', occurred_on: '2026-09-01', amount_minor: 2_000 }] },
  ],
  issues: [
    { issue_kind: 'UNMATCHED', source_kind: 'BANK_TRANSACTION', fact_ref: 'BANK_TRANSACTION:fact-unmatched', occurred_on: '2026-09-02', amount_minor: -500, matched_rule_keys: [] },
    { issue_kind: 'MULTIPLE_RULES', source_kind: 'BANK_TRANSACTION', fact_ref: 'BANK_TRANSACTION:fact-conflict', occurred_on: '2026-09-03', amount_minor: 800, matched_rule_keys: ['income.hotel-a', 'income.hotel-b'] },
  ],
  eligible_fact_count: 6,
  matched_fact_count: 4,
  unmatched_fact_count: 1,
  conflicted_fact_count: 1,
  issue_count: 2,
  issues_truncated: false,
  totals: { income_minor: 12_000, expense_minor: 3_000, current_minor: 2_000 },
}

function installSuccessfulReads() {
  vi.spyOn(api, 'getOriginalReconciliation').mockImplementation(async ({ accountingMonth }) => ({ ...originalReconciliationFixture, month: accountingMonth }))
  vi.spyOn(api, 'getCashReconciliation').mockResolvedValue(cashReconciliation)
}

describe('OriginalReconciliationPage', () => {
  beforeEach(() => {
    vi.spyOn(monthPolicy, 'previousBusinessMonth').mockReturnValue('2026-09')
    vi.spyOn(api, 'getMonthlyReview').mockRejectedValue(new Error('Synthetic report not loaded'))
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders only the rule-generated monthly reconciliation totals', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    expect(await screen.findByText('4 笔流水已归入对账项目')).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: '本月对账概览' })
    expect(within(overview).getByText('存在规则冲突')).toBeInTheDocument()
    expect(within(overview).getByText('4 / 6 笔已进入对账项目')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '收入分类汇总' })).toHaveTextContent('¥120.00')
    expect(screen.getByRole('region', { name: '支出分类汇总' })).toHaveTextContent('¥30.00')
    expect(screen.getByRole('region', { name: '往来款分类汇总' })).toHaveTextContent('净流入 ¥20.00')
    expect(screen.queryByRole('tablist', { name: '业务性质' })).not.toBeInTheDocument()
    expect(screen.getByText('平台实收')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '查看 2 笔流水明细' })).toBeInTheDocument()
  })

  it('expands a monthly item into its exact source facts', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    const disclosure = await screen.findByRole('button', { name: '查看 2 笔流水明细' })
    expect(screen.queryByRole('region', { name: '平台实收流水明细' })).not.toBeInTheDocument()

    fireEvent.click(disclosure)

    const details = screen.getByRole('region', { name: '平台实收流水明细' })
    expect(within(details).getByText('2026-09-01')).toBeInTheDocument()
    expect(within(details).getByText('2026-09-02')).toBeInTheDocument()
    expect(within(details).getByText('¥50.00')).toBeInTheDocument()
    expect(within(details).getByText('¥70.00')).toBeInTheDocument()
    expect(within(details).getByText('fact-income-1')).toBeInTheDocument()
    expect(within(details).getByText('fact-income-2')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('选择对账月份'), { target: { value: '2026-08' } })
    expect(screen.queryByRole('region', { name: '平台实收流水明细' })).not.toBeInTheDocument()
  })

  it('shows business categories first and keeps counterparties and gross current amounts out of the summary', async () => {
    installSuccessfulReads()
    const base = cashReconciliation.rows[2]
    vi.mocked(api.getCashReconciliation).mockResolvedValue({ ...cashReconciliation,
      rows: [...cashReconciliation.rows.slice(0, 2),
        { ...base, rule_key: 'person-a', item_label: '示例对方甲', source_kind: 'BANK_TRANSACTION',
          source_ref: 'company_transaction_classification:RELATED_PARTY_CURRENT:OTHER',
          amount_minor: 10_000, facts: [{ fact_ref: 'current-a', occurred_on: '2026-09-01', amount_minor: 10_000 }] },
        { ...base, rule_key: 'person-b', item_label: '示例对方乙', source_kind: 'BANK_TRANSACTION',
          source_ref: 'company_transaction_classification:RELATED_PARTY_CURRENT:OTHER',
          amount_minor: 7_000, facts: [{ fact_ref: 'current-b', occurred_on: '2026-09-02', amount_minor: -7_000 }] }],
    })
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    const current = await screen.findByRole('region', { name: '往来款分类汇总' })
    expect(within(current).getByText('关联往来')).toBeVisible()
    expect(current).toHaveTextContent('净流入 ¥30.00')
    expect(within(current).queryByText('¥170.00')).not.toBeInTheDocument()
    expect(screen.queryByText('示例对方甲')).not.toBeInTheDocument()
    expect(screen.queryByText('示例对方乙')).not.toBeInTheDocument()
    fireEvent.click(within(current).getByRole('button', { name: '查看 2 笔流水明细' }))
    const details = within(current).getByRole('region', { name: '关联往来流水明细' })
    expect(details).toHaveTextContent('示例对方甲')
    expect(details).toHaveTextContent('示例对方乙')
    expect(within(details).getByText('-¥70.00')).toBeInTheDocument()
    expect(within(details).getAllByText('current-a')).toHaveLength(1)
    expect(screen.getByRole('region', { name: '支出分类汇总' })).toBeVisible()
    expect(screen.getByRole('region', { name: '收入分类汇总' })).toBeVisible()
  })

  it('does not present a month-granularity candidate as an exact payment date', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    const current = await screen.findByRole('region', { name: '往来款分类汇总' })
    fireEvent.click(within(current).getByRole('button', { name: '查看 1 笔流水明细' }))

    const details = screen.getByRole('region', { name: '往来款流水明细' })
    expect(within(details).getByText('2026-09（月粒度）')).toBeInTheDocument()
    expect(within(details).queryByText('2026-09-01')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '查看 2 笔流水明细' }))
    expect(screen.queryByRole('region', { name: '往来款流水明细' })).not.toBeInTheDocument()
  })

  it('shows excluded unmatched facts and multi-rule conflicts', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    const issues = await screen.findByRole('region', { name: '规则缺口与冲突' })
    expect(within(issues).getByText('未命中 1 · 冲突 1')).toBeInTheDocument()
    expect(within(issues).getByText('多规则冲突')).toBeInTheDocument()
    expect(within(issues).getByText('未命中规则')).toBeInTheDocument()
    expect(within(issues).getByText('income.hotel-a、income.hotel-b')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '收入分类汇总' })).toHaveTextContent('2 笔')
  })

  it('does not equate all returned facts being classified with complete monthly coverage', async () => {
    installSuccessfulReads()
    vi.mocked(api.getCashReconciliation).mockResolvedValue({ ...cashReconciliation,
      eligible_fact_count: 4, matched_fact_count: 4, unmatched_fact_count: 0,
      conflicted_fact_count: 0, issue_count: 0, issues: [],
    })
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    expect(await screen.findByText('分类已返回 · 覆盖待核验')).toBeVisible()
    expect(screen.getByText(/分类金额不等于整月已结清/)).toBeVisible()
    expect(screen.queryByText('已导入流水均已归类')).not.toBeInTheDocument()
  })

  it('lists scoped Core rules instead of a hard-coded registry', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    const registry = await screen.findByRole('region', { name: '旧表项目取数来源' })
    expect(await within(registry).findByText(/银行 · bank\.hotel-a · CREDIT/)).toBeInTheDocument()
    expect(within(registry).getByText((_content, element) => (
      element?.classList.contains('statement-source-registry-summary-meta') ?? false
    ))).toHaveTextContent(/收入 1 条 · 支出 1 条/)
    expect(within(registry).getByText(/匹配：synthetic-income/)).toBeInTheDocument()
    expect(within(registry).getByText(/微信 · wechat\.synthetic · ANY/)).toBeInTheDocument()
  })

  it('does not replace a failed cash projection with candidate heuristics', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
    vi.spyOn(api, 'getCashReconciliation').mockRejectedValue(new Error('Core 规则读取失败'))
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    expect(await screen.findByText('规则生成结果暂不可用')).toBeInTheDocument()
    expect(screen.getByText('数据尚未读取成功，请稍后重试。')).toBeInTheDocument()
    expect(screen.queryByText('平台实收')).not.toBeInTheDocument()
  })

  it('keeps cash totals visible when only legacy projection todos fail', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockRejectedValue(new Error('legacy unavailable'))
    vi.spyOn(api, 'getCashReconciliation').mockResolvedValue(cashReconciliation)
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    expect(await screen.findByText('旧口径补充待办暂不可用')).toBeInTheDocument()
    expect(screen.getByText('平台实收')).toBeInTheDocument()
  })

  it('reloads both projections for the selected natural month', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    const month = await screen.findByLabelText('选择对账月份')
    expect(screen.getByText('对账月份')).toBeVisible()
    fireEvent.change(month, { target: { value: '2026-08' } })

    await vi.waitFor(() => expect(api.getCashReconciliation).toHaveBeenLastCalledWith('2026-08'))
    expect(api.getOriginalReconciliation).toHaveBeenLastCalledWith(expect.objectContaining({ accountingMonth: '2026-08' }))
  })
  it('uses the common workbench month instead of the current calendar month', async () => {
    vi.mocked(monthPolicy.previousBusinessMonth).mockReturnValue('2026-08')
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    await vi.waitFor(() => expect(api.getCashReconciliation).toHaveBeenCalledWith('2026-08'))
    expect(screen.getByLabelText('选择对账月份')).toHaveValue('2026-08')
  })
  it('does not refill old cash when its request finishes after changing month', async () => {
    let finishOld!: (value: CashReconciliation) => void
    vi.spyOn(api, 'getCashReconciliation').mockImplementation(() => new Promise(resolve => { finishOld = resolve }))
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue({ ...originalReconciliationFixture, month: '2026-09' })
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    await vi.waitFor(() => expect(api.getCashReconciliation).toHaveBeenCalled())
    await act(async () => {
      fireEvent.change(screen.getByLabelText('选择对账月份'), { target: { value: '2026-08' } })
      finishOld(cashReconciliation)
      await Promise.resolve()
    })
    expect(screen.queryByText('平台实收')).not.toBeInTheDocument()
  })
  it('rejects a cash response whose month differs from the selected month', async () => {
    installSuccessfulReads()
    vi.mocked(api.getCashReconciliation).mockResolvedValue({ ...cashReconciliation, accounting_month: '2026-07' })
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)
    expect(await screen.findByText('规则生成结果暂不可用')).toBeInTheDocument()
    expect(screen.queryByText('平台实收')).not.toBeInTheDocument()
  })
})
