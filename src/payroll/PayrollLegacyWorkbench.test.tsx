import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type {
  PayrollLegacyCommandResult,
  PayrollDisbursementRecordPage,
  PayrollLegacyEmployeeRule,
  PayrollLegacyWorkspace,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import { PayrollLegacyWorkbench } from './PayrollLegacyWorkbench'

const testWorkspace: PayrollTestWorkspaceReadResponse = {
  contract_version: 'ledgerbridge.payroll-test-workspace-read.v1',
  entity_ref: '10000000-0000-4000-8000-000000000001',
  company_id: 'company_live_hotel',
  data: {
    contract_version: '1.0.0', schema_version: 'payroll-ledgerbridge-test-projection/v1',
    data_scope: 'TEST_ONLY', test_batch_id: 'payroll_history_through_2026_08',
    company_id: 'company_live_hotel', cutoff_date: '2026-08-31', workspace_revision: 1,
    projection_revision: 'a'.repeat(64), etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-09-01T00:00:00.000Z', auto_test_ready: true,
    payment_submission_supported: false, payable: false, submission_supported: false,
    routing_counts: { auto_test: 3, review_required: 0, date_unknown: 0 }, materials: [],
  },
}

const employeeRule: PayrollLegacyEmployeeRule = {
  employee_id: 'emp_preview_001', employee_name: '示例员工甲',
  account_id: 'acct_preview_001', account_masked: '****0138',
  disbursement_company: '测试公司一', fixed_base_salary_cents: 500000,
  fixed_allowance_cents: 30000, night_shift_rate_cents: 0, rest_days: 4,
  payment_channel: 'MYBANK', payment_kind: 'NORMAL', job_group: '客房', location: '主楼',
}

const legacyWorkspace: PayrollLegacyWorkspace = {
  schema_version: 'payroll-legacy-feature-workspace/v1', data_scope: 'TEST_ONLY',
  company_id: 'company_live_hotel', test_batch_id: testWorkspace.data.test_batch_id,
  revision: 1, active_period: '2026-08',
  rules: {
    revision: 1, employees: [employeeRule], review_rules: [{
      rule_id: 'review_payment_channel', name: '发放渠道必须确认',
      rule_type: 'PAYMENT_CHANNEL_REQUIRED', enabled: true, severity: 'BLOCKING', threshold_cents: 0,
    }],
  },
  batches: [{
    batch_id: 'payroll_history_through_2026_08_2026_08', period: '2026-08', revision: 1,
    supporting_material_ids: {
      attendance: 'material_attendance_2026_08', aunt_attendance: 'material_aunt_2026_08',
      review_statistics: 'material_review_2026_08',
    },
    lines: [{
      source_row: 1, company_id: 'company_live_hotel', employee_id: 'emp_preview_001',
      employee_name: '示例员工甲', account_id: 'acct_preview_001', account_masked: '****0138',
      payment_channel: 'MYBANK', base_salary_cents: 500000, allowance_cents: 30000,
      bonus_cents: 0, deduction_cents: 0, social_insurance_cents: 0, housing_fund_cents: 0,
      individual_income_tax_cents: 0, gross_pay_cents: 530000, net_pay_cents: 530000,
      notes: '系统生成', disbursement_company: '测试公司一',
    }],
    adjustments: [], source_exceptions: [],
    drafts: Array.from({ length: 5 }, (_, index) => ({
      schema_version: 'payroll-bank-draft/v1' as const, draft_id: `draft_company_${index + 1}`,
      draft_type: 'normal_bank_payroll' as const, company_id: 'company_live_hotel',
      batch_id: 'payroll_history_through_2026_08_2026_08', pay_period: '2026-08', version: 1,
      disbursement_company: `测试公司${index + 1}`,
      lines: index === 0 ? [{ employee_id: 'emp_preview_001', account_id: 'acct_preview_001',
        account_masked: '****0138', amount_cents: 530000, payment_channel: 'MYBANK', memo: '' }] : [],
      total_amount_cents: index === 0 ? 530000 : 0, warning: '仅预览',
      payable: false as const, submission_supported: false as const,
    })),
    summary: null, verification: null, pending_items: [], checks: null,
  }],
  audit_events: [{ sequence: 1, action: 'payroll.monthly_generated', period: '2026-08',
    occurred_at: '2026-09-01T02:00:00.000Z', reason: '已生成当月工资' }],
  payment_submission_supported: false, payable: false, submission_supported: false,
}

const disbursementRecords: PayrollDisbursementRecordPage = {
  schema_version: 'ledgerbridge.payroll-disbursement-records.v1',
  pay_period: '2026-08', source_artifact_count: 1, record_count: 1, unmatched_count: 1,
  records: [{
    record_ref: '20000000-0000-4000-8000-000000000001',
    entity_ref: '20000000-0000-4000-8000-000000000002', company_name: '测试公司一',
    pay_period: '2026-08', occurred_at: '2026-09-20T08:00:00+08:00',
    actual_amount_minor: 530000, direction: 'OUTFLOW', currency: 'CNY',
    source_channel: 'MYBANK', source_system: 'mybank_statement_v1',
    source_artifact_ref: '30000000-0000-4000-8000-000000000001',
    source_statement_ref: '40000000-0000-4000-8000-000000000001', source_row_number: 7,
    ingested_at: '2026-09-21T08:00:00+08:00',
    managed_account_ref: '50000000-0000-4000-8000-000000000001',
    disbursement_account_masked: '****1234', counterparty_name: '批量代发',
    counterparty_account_masked: null, transaction_name: '批量代发',
    classification_revision: 1, classification_source: 'AUTO_RULE',
    classification_rule_version: 'company-payroll-autoclassifier.2026-09.v1',
    period_assignment_source: 'NEXT_MONTH_RULE',
    period_assignment_rule_version: 'payroll-next-month-disbursement.2026-09.v1',
    parse_status: 'PARSED', link_status: 'UNMATCHED', payable: false, submission_supported: false,
  }],
  payable: false, submission_supported: false,
}

const commandResult = (
  action: PayrollLegacyCommandResult['data']['action'],
  workspace = legacyWorkspace,
): PayrollLegacyCommandResult => ({
  contract_version: 'ledgerbridge.payroll-legacy-feature-command-result.v1',
  entity_ref: testWorkspace.entity_ref, company_id: testWorkspace.company_id,
  action: 'payroll.test_workspace.legacy.command', resource_ref: testWorkspace.data.test_batch_id,
  replayed: false, data: { action, replayed: false, workspace },
})

function mockRead(workspace = legacyWorkspace) {
  vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
    contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
    entity_ref: testWorkspace.entity_ref, company_id: testWorkspace.company_id, data: workspace,
  })
  vi.spyOn(api, 'getPayrollDisbursementRecords').mockResolvedValue({
    contract_version: 'ledgerbridge.payroll-read.v1',
    entity_ref: testWorkspace.entity_ref, company_id: testWorkspace.company_id,
    data: disbursementRecords,
  })
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollLegacyWorkbench', () => {
  it('separates global payroll rules from employee payroll parameters', async () => {
    mockRead()
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '工资概览' })
    const navigation = screen.getByRole('navigation', { name: '工资工作流程' })
    expect(within(navigation).getAllByRole('button')).toHaveLength(5)
    for (const name of ['生成当月工资', '查看代发表与发放表',
      '复核本月已发并更新汇总', '管理工资规则', '管理员工工资参数']) {
      expect(within(navigation).getByRole('button', { name })).toBeInTheDocument()
    }
    expect(within(navigation).queryByRole('button', { name: '生成补发代发表' })).not.toBeInTheDocument()
    expect(within(navigation).queryByRole('button', { name: '检查规则与历史' })).not.toBeInTheDocument()
    expect(screen.getByText('月度工资账本')).toBeInTheDocument()
    expect(screen.getByText('2026-08 工资表 · 版本 1')).toBeInTheDocument()
    expect(screen.getByText('实际发放（统计口径）')).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: '工资概览' })
    expect(overview).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '本月工资处理进度' })).toBeInTheDocument()
    expect(within(overview).getByText('员工参数')).toBeInTheDocument()
    expect(within(overview).getByText('1 人')).toBeInTheDocument()
  })

  it('keeps the initial payroll task inside the compact workflow instead of repeating a full form', async () => {
    mockRead({ ...legacyWorkspace, active_period: '', batches: [] })
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test"
      materialsPanel={<section aria-label="工资素材整理">素材确认内容</section>} />)
    await screen.findByRole('region', { name: '工资概览' })
    const flow = await screen.findByRole('region', { name: '本月工资处理进度' })
    expect(within(flow).getByText('物料确认')).toBeInTheDocument()
    expect(within(flow).getByText('生成工资')).toBeInTheDocument()
    expect(within(flow).getByText('发放对账')).toBeInTheDocument()
    expect(within(flow).getByRole('button', { name: '查看并确认素材' })).toBeInTheDocument()
    const workbench = screen.getByRole('region', { name: '本月工资处理' })
    expect(within(workbench).getByRole('region', { name: '工资素材整理' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '生成当月工资' })).not.toBeInTheDocument()
  })

  it('generates monthly payroll from the three explicitly confirmed materials', async () => {
    mockRead()
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue(commandResult('GENERATE_MONTHLY_PAYROLL'))
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" confirmedMaterials={{
      period: '2026-08', material_ids: {
        attendance: 'material_attendance_2026_08', aunt_attendance: 'material_aunt_2026_08',
        review_statistics: 'material_review_2026_08',
      },
    }} />)
    await screen.findByText('2026-08 三类素材已唯一确认')
    fireEvent.click(screen.getByRole('button', { name: '确认并生成当月工资表' }))
    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'GENERATE_MONTHLY_PAYROLL', expectedRevision: 1,
      payload: { period: '2026-08', supporting_material_ids: {
        attendance: 'material_attendance_2026_08', aunt_attendance: 'material_aunt_2026_08',
        review_statistics: 'material_review_2026_08',
      }, adjustments: [], pending_resolutions: [] }, csrfToken: 'csrf-test',
    }))
  })

  it('can generate a newly selected period when an older payroll batch already exists', async () => {
    const previousPeriodWorkspace = structuredClone(legacyWorkspace)
    previousPeriodWorkspace.active_period = '2026-07'
    previousPeriodWorkspace.batches[0].period = '2026-07'
    previousPeriodWorkspace.batches[0].batch_id = 'payroll_history_through_2026_08_2026_07'
    previousPeriodWorkspace.batches[0].supporting_material_ids = {
      attendance: 'material_attendance_2026_07',
      aunt_attendance: 'material_aunt_2026_07',
      review_statistics: 'material_review_2026_07',
    }
    mockRead(previousPeriodWorkspace)
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue(commandResult(
      'GENERATE_MONTHLY_PAYROLL',
      previousPeriodWorkspace,
    ))

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" confirmedMaterials={{
      period: '2026-08', material_ids: {
        attendance: 'material_attendance_2026_08', aunt_attendance: 'material_aunt_2026_08',
        review_statistics: 'material_review_2026_08',
      },
    }} />)

    const overview = await screen.findByRole('region', { name: '工资概览' })
    expect(within(overview).getByText('待生成')).toBeInTheDocument()
    expect(within(overview).getByText('1/3')).toBeInTheDocument()
    await screen.findByRole('button', { name: '确认并生成当月工资表' })
    fireEvent.click(screen.getByRole('button', { name: '确认并生成当月工资表' }))
    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'GENERATE_MONTHLY_PAYROLL', expectedRevision: 1,
      payload: { period: '2026-08', supporting_material_ids: {
        attendance: 'material_attendance_2026_08', aunt_attendance: 'material_aunt_2026_08',
        review_statistics: 'material_review_2026_08',
      }, adjustments: [], pending_resolutions: [] }, csrfToken: 'csrf-test',
    }))
  })

  it('restores the selected generated period instead of opening an older active batch', async () => {
    const restoredWorkspace = structuredClone(legacyWorkspace)
    const julyBatch = structuredClone(legacyWorkspace.batches[0])
    julyBatch.period = '2026-07'
    julyBatch.batch_id = 'payroll_history_through_2026_08_2026_07'
    julyBatch.lines[0].employee_id = 'emp_july_001'
    julyBatch.lines[0].employee_name = '七月示例员工'
    julyBatch.lines[0].account_id = 'acct_july_001'
    restoredWorkspace.active_period = '2026-07'
    restoredWorkspace.batches = [julyBatch, restoredWorkspace.batches[0]]
    mockRead(restoredWorkspace)

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    const flow = await screen.findByRole('region', { name: '本月工资处理进度' })
    expect(within(flow).getByText('物料确认').closest('div')).toHaveClass('done')
    fireEvent.click(within(flow).getByRole('button', { name: '进入发放复核' }))
    const review = await screen.findByRole('region', { name: '发放复核分类' })
    expect(within(review).getByText('示例员工甲')).toBeInTheDocument()
    expect(within(review).queryByText('七月示例员工')).not.toBeInTheDocument()
  })

  it('shows five payroll-list previews and the wage-disbursement table', async () => {
    mockRead()
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '工资概览' })
    fireEvent.click(screen.getByRole('button', { name: '查看代发表与发放表' }))
    expect(screen.getByRole('heading', { name: '五家公司代发表预览' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '工资发放表' })).toBeInTheDocument()
    expect(screen.getByText('测试公司1')).toBeInTheDocument()
    expect(screen.getByText(/示例员工甲 · MYBANK · 测试公司一/)).toBeInTheDocument()
  })

  it('classifies disbursement review from stored payroll facts without manual re-entry', async () => {
    mockRead()
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    await screen.findByRole('region', { name: '工资概览' })
    fireEvent.click(screen.getByRole('button', { name: '复核本月已发并更新汇总' }))

    const review = screen.getByRole('region', { name: '发放复核分类' })
    expect(await within(review).findByRole('region', { name: '已入库工资流水' })).toBeInTheDocument()
    expect(within(review).getByText('源文件只在入库时解析一次')).toBeInTheDocument()
    expect(within(review).getByText('待关联')).toBeInTheDocument()
    expect(within(review).getAllByText('测试公司一 · 网商银行')).toHaveLength(2)
    expect(within(review).getAllByText('证据缺失')).toHaveLength(2)
    expect(within(review).getByText('示例员工甲')).toBeInTheDocument()
    expect(within(review).getByText(/主楼 · 网商银行 · \*{4}0138/)).toBeInTheDocument()
    expect(within(review).getAllByText('理论应发')).toHaveLength(2)
    expect(within(review).getAllByText('实际到账')).toHaveLength(2)
    expect(screen.queryByLabelText('emp_preview_001实际到账金额')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('网商银行发放流水1')).not.toBeInTheDocument()
  })

  it('reads stored evidence and matched results without issuing a write command', async () => {
    const verifiedWorkspace = structuredClone(legacyWorkspace)
    verifiedWorkspace.batches[0].verification = {
      schema_version: 'payroll-current-paid-verification/v2', company_id: 'company_live_hotel',
      batch_id: 'payroll_history_through_2026_08_2026_08', period: '2026-08',
      evidence_documents: [
        ...Array.from({ length: 5 }, (_, index) => ({
          evidence_type: 'MYBANK_STATEMENT' as const,
          evidence_ref: `mybank_company_${index + 1}_2026_08`,
        })),
        { evidence_type: 'BOC_RECEIPT', evidence_ref: 'boc_cash_2026_08' },
        { evidence_type: 'WECHAT_RECEIPT', evidence_ref: 'wechat_separate_2026_08' },
      ],
      evidence_summary: [
        { evidence_type: 'MYBANK_STATEMENT', required_count: 5, received_count: 5 },
        { evidence_type: 'BOC_RECEIPT', required_count: 1, received_count: 1 },
        { evidence_type: 'WECHAT_RECEIPT', required_count: 1, received_count: 1 },
      ],
      theoretical_total_cents: 530000, actual_total_cents: 530000,
      difference_cents: 0, totals_match: true,
      by_payment_channel: [{ payment_channel: 'MYBANK', expected_amount_cents: 530000,
        actual_amount_cents: 530000, difference_cents: 0, totals_match: true }],
      overall_status: 'MATCHED',
      results: [{ employee_id: 'emp_preview_001', account_id: 'acct_preview_001',
        payment_channel: 'MYBANK', expected_amount_cents: 530000, actual_amount_cents: 530000,
        difference_cents: 0, status: 'MATCHED' }],
      verified_at: '2026-09-01T03:00:00.000Z', payable: false, submission_supported: false,
    }
    verifiedWorkspace.batches[0].summary = {
      schema_version: 'payroll-monthly-summary/v1', company_id: 'company_live_hotel',
      batch_id: 'payroll_history_through_2026_08_2026_08', period: '2026-08',
      employee_count: 1, gross_pay_cents: 530000, net_pay_cents: 530000,
      by_payment_channel: [{ payment_channel: 'MYBANK', amount_cents: 530000 }],
      by_location: [{ location: '主楼', employee_count: 1, gross_pay_cents: 530000, net_pay_cents: 530000 }],
      payable: false, submission_supported: false,
    }
    mockRead(verifiedWorkspace)
    const command = vi.spyOn(api, 'runPayrollLegacyCommand')
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '工资概览' })
    fireEvent.click(screen.getByRole('button', { name: '复核本月已发并更新汇总' }))
    const review = screen.getByRole('region', { name: '发放复核分类' })
    expect(await within(review).findByText(/已直接读取入库时归类的 1 笔工资流水/)).toBeInTheDocument()
    expect(within(review).getByText('源文件只在入库时解析一次')).toBeInTheDocument()
    expect(within(review).getAllByText('完全一致')).toHaveLength(2)
    expect(within(review).getAllByText('¥5,300.00').length).toBeGreaterThanOrEqual(2)
    expect(command).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('emp_preview_001实际到账金额')).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: '各店当月工资汇总' })).toBeInTheDocument()
    expect(screen.getByText('主楼 · 1 人')).toBeInTheDocument()
  })

  it('shows only global review rules in the payroll-rules task', async () => {
    const rulesOnly = { ...legacyWorkspace, batches: [] }
    mockRead(rulesOnly)
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '工资概览' })
    fireEvent.click(screen.getByRole('button', { name: '管理工资规则' }))
    expect(screen.getByRole('heading', { name: '全局工资规则' })).toBeInTheDocument()
    expect(screen.getByText('发放渠道完整性')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: '员工工资参数' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('示例员工甲固定工资')).not.toBeInTheDocument()
  })

  it('manages employee payroll parameters separately when no monthly payroll exists', async () => {
    const rulesOnly = { ...legacyWorkspace, batches: [] }
    mockRead(rulesOnly)
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue(commandResult('SAVE_RULES', rulesOnly))
    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByRole('region', { name: '工资概览' })
    fireEvent.click(screen.getByRole('button', { name: '管理员工工资参数' }))
    expect(screen.getByRole('heading', { name: '员工工资参数' })).toBeInTheDocument()
    expect(screen.queryByText('发放渠道完整性')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('示例员工甲固定工资'), { target: { value: '5200.00' } })
    fireEvent.click(screen.getByRole('button', { name: '保存员工工资参数' }))
    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith(
      expect.objectContaining({ action: 'SAVE_RULES', payload: expect.objectContaining({
        employee_rules: [expect.objectContaining({ employee_name: '示例员工甲',
          disbursement_company: '测试公司一', fixed_base_salary_cents: 520000 })],
      }) }),
    ))
  })
})
