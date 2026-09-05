import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
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
  vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
  vi.spyOn(api, 'getCashReconciliation').mockResolvedValue(cashReconciliation)
}

describe('OriginalReconciliationPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders only the rule-generated monthly reconciliation totals', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    expect(await screen.findByText('4 笔流水已归入对账项目')).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: '本月对账概览' })
    expect(within(overview).getByText('存在规则冲突')).toBeInTheDocument()
    expect(within(overview).getByText('4 / 6 笔已进入对账项目')).toBeInTheDocument()
    const lanes = screen.getByRole('tablist', { name: '业务性质' })
    expect(within(lanes).getByRole('tab', { name: /收入/ })).toHaveTextContent('¥120.00')
    expect(within(lanes).getByRole('tab', { name: /支出/ })).toHaveTextContent('¥30.00')
    expect(within(lanes).getByRole('tab', { name: /往来款/ })).toHaveTextContent('¥20.00')
    expect(screen.getByText('平台实收')).toBeInTheDocument()
    expect(screen.getByText('2 笔实收流水')).toBeInTheDocument()
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

  it('does not present a month-granularity candidate as an exact payment date', async () => {
    installSuccessfulReads()
    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    fireEvent.click(await screen.findByRole('tab', { name: /往来款/ }))
    fireEvent.click(screen.getByRole('button', { name: '查看 1 笔流水明细' }))

    const details = screen.getByRole('region', { name: '往来款流水明细' })
    expect(within(details).getByText('2026-09（月粒度）')).toBeInTheDocument()
    expect(within(details).queryByText('2026-09-01')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /收入/ }))
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
    expect(screen.getByRole('tab', { name: /收入/ })).toHaveTextContent('2 笔')
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
    fireEvent.change(month, { target: { value: '2026-08' } })

    await vi.waitFor(() => expect(api.getCashReconciliation).toHaveBeenLastCalledWith('2026-08'))
    expect(api.getOriginalReconciliation).toHaveBeenLastCalledWith(expect.objectContaining({ accountingMonth: '2026-08' }))
  })
})
