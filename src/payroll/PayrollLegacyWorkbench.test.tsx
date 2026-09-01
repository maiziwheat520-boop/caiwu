import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '../api'
import type {
  PayrollLegacyCommandResult,
  PayrollLegacyWorkspace,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import { PayrollLegacyWorkbench } from './PayrollLegacyWorkbench'

const testWorkspace: PayrollTestWorkspaceReadResponse = {
  contract_version: 'ledgerbridge.payroll-test-workspace-read.v1',
  entity_ref: '10000000-0000-4000-8000-000000000001',
  company_id: 'company_live_hotel',
  data: {
    contract_version: '1.0.0',
    schema_version: 'payroll-ledgerbridge-test-projection/v1',
    data_scope: 'TEST_ONLY',
    test_batch_id: 'payroll_history_through_2026_08',
    company_id: 'company_live_hotel',
    cutoff_date: '2026-08-31',
    workspace_revision: 1,
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-09-01T00:00:00.000Z',
    auto_test_ready: true,
    payment_submission_supported: false,
    payable: false,
    submission_supported: false,
    routing_counts: { auto_test: 2, review_required: 0, date_unknown: 0 },
    materials: [{
      company_id: 'company_live_hotel',
      material_id: 'material_history_2026_08',
      routing_status: 'AUTO_TEST',
      period: '2026-08',
      material_type: 'PAYROLL_SHEET',
      payable: false,
      submission_supported: false,
    }, {
      company_id: 'company_live_hotel',
      material_id: 'material_history_2026_07',
      routing_status: 'AUTO_TEST',
      period: '2026-07',
      material_type: 'PAYROLL_SHEET',
      payable: false,
      submission_supported: false,
    }],
  },
}

const legacyWorkspace: PayrollLegacyWorkspace = {
  schema_version: 'payroll-legacy-feature-workspace/v1',
  data_scope: 'TEST_ONLY',
  company_id: 'company_live_hotel',
  test_batch_id: testWorkspace.data.test_batch_id,
  revision: 1,
  active_period: '2026-08',
  rules: {
    revision: 0,
    employees: [],
    review_rules: [{
      rule_id: 'review_payment_channel',
      name: '发放渠道必须确认',
      rule_type: 'PAYMENT_CHANNEL_REQUIRED',
      enabled: true,
      severity: 'BLOCKING',
      threshold_cents: 0,
    }, {
      rule_id: 'review_supporting_materials',
      name: '三类工资素材必须齐全',
      rule_type: 'SUPPORTING_MATERIAL_REQUIRED',
      enabled: true,
      severity: 'REVIEW',
      threshold_cents: 0,
    }, {
      rule_id: 'review_history_change',
      name: '相邻月份人员与工资变化',
      rule_type: 'HISTORY_CHANGE_REVIEW',
      enabled: true,
      severity: 'REVIEW',
      threshold_cents: 1,
    }],
  },
  batches: [{
    batch_id: 'payroll_history_through_2026_08_2026_08',
    period: '2026-08',
    revision: 1,
    main_material_id: 'material_history_2026_08',
    supporting_material_ids: {},
    lines: [{
      source_row: 4,
      company_id: 'company_live_hotel',
      employee_id: 'emp_preview_001',
      employee_name: '示例员工甲',
      account_id: 'acct_preview_001',
      account_masked: '****0138',
      payment_channel: 'MYBANK',
      base_salary_cents: 500000,
      allowance_cents: 30000,
      bonus_cents: 20000,
      deduction_cents: 5000,
      social_insurance_cents: 18000,
      housing_fund_cents: 12000,
      individual_income_tax_cents: 15000,
      gross_pay_cents: 550000,
      net_pay_cents: 500000,
      notes: '',
    }],
    adjustments: [],
    source_exceptions: [],
    drafts: [],
    summary: null,
    verification: null,
    pending_items: [],
    checks: null,
  }],
  audit_events: [{
    sequence: 1,
    action: 'payroll.main_filled',
    period: '2026-08',
    occurred_at: '2026-09-01T02:00:00.000Z',
    reason: '受信工资表已进入网页测试主表',
  }],
  payment_submission_supported: false,
  payable: false,
  submission_supported: false,
}

const fillResult: PayrollLegacyCommandResult = {
  contract_version: 'ledgerbridge.payroll-legacy-feature-command-result.v1',
  entity_ref: testWorkspace.entity_ref,
  company_id: testWorkspace.company_id,
  action: 'payroll.test_workspace.legacy.command',
  resource_ref: testWorkspace.data.test_batch_id,
  replayed: false,
  data: { action: 'FILL_MAIN', replayed: false, workspace: legacyWorkspace },
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollLegacyWorkbench', () => {
  it('imports a real payroll sheet into a calculated persistent main table', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockRejectedValue(new ApiError('missing', 404))
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue(fillResult)

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    const monthSelect = await screen.findByLabelText('工资月份')
    expect(monthSelect).toHaveValue('2026-08')
    expect(screen.getByLabelText('选择原工资表版本')).toHaveValue('material_history_2026_08')
    fireEvent.click(screen.getByRole('button', { name: '导入主表并计算' }))

    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'FILL_MAIN',
      expectedRevision: 0,
      payload: {
        main_material_id: 'material_history_2026_08',
        supporting_material_ids: {},
        adjustments: [],
      },
      csrfToken: 'csrf-test',
    }))
    expect(await screen.findByRole('cell', { name: '示例员工甲' })).toBeInTheDocument()
    expect(screen.getByText('工资主表已保存 · 版本 1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '管理工资规则' }))
    expect(screen.getByRole('button', { name: '保存全部工资规则' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|提交银行/ })).not.toBeInTheDocument()
  })

  it('selects a source month before choosing that month payroll version', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockRejectedValue(new ApiError('missing', 404))
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue(fillResult)

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    const monthSelect = await screen.findByLabelText('工资月份')
    fireEvent.change(monthSelect, { target: { value: '2026-07' } })
    expect(screen.getByLabelText('选择原工资表版本')).toHaveValue('material_history_2026_07')
    fireEvent.click(screen.getByRole('button', { name: '导入主表并计算' }))

    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'FILL_MAIN',
      expectedRevision: 0,
      payload: {
        main_material_id: 'material_history_2026_07',
        supporting_material_ids: {},
        adjustments: [],
      },
      csrfToken: 'csrf-test',
    }))
  })

  it('reloads the saved main table and exposes all eight real feature actions', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
      entity_ref: testWorkspace.entity_ref,
      company_id: testWorkspace.company_id,
      data: legacyWorkspace,
    })

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    expect(await screen.findByText('工资主表已保存 · 版本 1')).toBeInTheDocument()
    for (const name of [
      '填入主表',
      '生成网商银行代发表',
      '生成补发代发草稿',
      '更新工资汇总',
      '检查上月待办',
      '核对本月已发',
      '管理工资规则',
      '检查规则与历史',
    ]) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
  })

  it('requires five MYBANK, one BOC, and one WeChat bill before reconciling to payroll', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
      entity_ref: testWorkspace.entity_ref,
      company_id: testWorkspace.company_id,
      data: legacyWorkspace,
    })
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue({
      ...fillResult,
      data: { action: 'VERIFY_CURRENT_PAID', replayed: false, workspace: legacyWorkspace },
    })

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    await screen.findByText('工资主表已保存 · 版本 1')
    fireEvent.click(screen.getByRole('button', { name: '核对本月已发' }))
    expect(screen.getByText('账单收集完整度')).toBeInTheDocument()
    expect(screen.getByText('网商银行实际发放流水 0/5')).toBeInTheDocument()
    expect(screen.getByText('中国银行实际发放流水 0/1')).toBeInTheDocument()
    expect(screen.getByText('李勇微信实际转账记录 0/1')).toBeInTheDocument()
    expect(screen.getByText('工资表理论总额：¥5,000.00')).toBeInTheDocument()

    for (let index = 1; index <= 5; index += 1) {
      fireEvent.change(screen.getByLabelText(`网商银行发放流水${index}`), {
        target: { value: `mybank_company_${index}_2026_08` },
      })
    }
    fireEvent.change(screen.getByLabelText('中国银行实际发放流水'), {
      target: { value: 'boc_cash_2026_08' },
    })
    fireEvent.change(screen.getByLabelText('李勇微信实际转账记录'), {
      target: { value: 'wechat_separate_2026_08' },
    })
    fireEvent.change(screen.getByLabelText('emp_preview_001实际到账金额'), {
      target: { value: '5000.00' },
    })
    fireEvent.change(screen.getByLabelText('emp_preview_001回单状态'), {
      target: { value: 'SUCCEEDED' },
    })
    fireEvent.click(screen.getByRole('button', { name: '保存本月已发核对' }))

    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'VERIFY_CURRENT_PAID',
      expectedRevision: 1,
      payload: {
        period: '2026-08',
        evidence_documents: [
          ...Array.from({ length: 5 }, (_, index) => ({
            evidence_type: 'MYBANK_STATEMENT',
            evidence_ref: `mybank_company_${index + 1}_2026_08`,
          })),
          { evidence_type: 'BOC_RECEIPT', evidence_ref: 'boc_cash_2026_08' },
          { evidence_type: 'WECHAT_RECEIPT', evidence_ref: 'wechat_separate_2026_08' },
        ],
        receipts: [{
          employee_id: 'emp_preview_001',
          account_id: 'acct_preview_001',
          payment_channel: 'MYBANK',
          amount_cents: 500000,
          status: 'SUCCEEDED',
        }],
      },
      csrfToken: 'csrf-test',
    }))
  })

  it('edits rules, recalculates, saves, and reloads the new workspace revision', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
      entity_ref: testWorkspace.entity_ref,
      company_id: testWorkspace.company_id,
      data: legacyWorkspace,
    })
    const savedWorkspace: PayrollLegacyWorkspace = {
      ...legacyWorkspace,
      revision: 2,
      rules: {
        revision: 1,
        review_rules: legacyWorkspace.rules.review_rules,
        employees: [{
          employee_id: 'emp_preview_001',
          fixed_base_salary_cents: 520000,
          fixed_allowance_cents: 30000,
          night_shift_rate_cents: 0,
          rest_days: 4,
          payment_channel: 'MYBANK',
          payment_kind: 'NORMAL',
          job_group: '客房',
          location: '主楼',
        }],
      },
      batches: legacyWorkspace.batches.map((batch) => ({ ...batch, revision: 2 })),
    }
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue({
      ...fillResult,
      data: { action: 'SAVE_RULES', replayed: false, workspace: savedWorkspace },
    })

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)

    await screen.findByText('工资主表已保存 · 版本 1')
    fireEvent.click(screen.getByRole('button', { name: '管理工资规则' }))
    fireEvent.change(screen.getByLabelText('示例员工甲固定工资'), { target: { value: '5200.00' } })
    fireEvent.change(screen.getByLabelText('示例员工甲可休天数'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('示例员工甲工种'), { target: { value: '客房' } })
    fireEvent.change(screen.getByLabelText('示例员工甲地点'), { target: { value: '主楼' } })
    fireEvent.click(screen.getByRole('button', { name: '保存全部工资规则' }))

    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith({
      action: 'SAVE_RULES',
      expectedRevision: 1,
      payload: {
        period: '2026-08',
        employee_rules: [{
          employee_id: 'emp_preview_001',
          fixed_base_salary_cents: 520000,
          fixed_allowance_cents: 30000,
          night_shift_rate_cents: 0,
          rest_days: 4,
          payment_channel: 'MYBANK',
          payment_kind: 'NORMAL',
          job_group: '客房',
          location: '主楼',
        }],
        review_rules: legacyWorkspace.rules.review_rules,
      },
      csrfToken: 'csrf-test',
    }))
    expect(await screen.findByText('工资主表已保存 · 版本 2')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('已保存并重新读取最新工资工作区')
  })

  it('lists, edits, toggles, deletes, saves, and reloads payroll review rules', async () => {
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
      entity_ref: testWorkspace.entity_ref,
      company_id: testWorkspace.company_id,
      data: legacyWorkspace,
    })
    const savedReviewRules = [
      legacyWorkspace.rules.review_rules![0],
      {
        ...legacyWorkspace.rules.review_rules![1],
        name: '工资素材完整性',
        enabled: false,
      },
    ]
    vi.spyOn(api, 'runPayrollLegacyCommand').mockResolvedValue({
      ...fillResult,
      data: {
        action: 'SAVE_RULES',
        replayed: false,
        workspace: {
          ...legacyWorkspace,
          revision: 2,
          rules: { ...legacyWorkspace.rules, revision: 1, review_rules: savedReviewRules },
        },
      },
    })

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByText('工资主表已保存 · 版本 1')
    fireEvent.click(screen.getByRole('button', { name: '管理工资规则' }))

    expect(screen.getByDisplayValue('三类工资素材必须齐全')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('示例员工甲工种'), { target: { value: '前台' } })
    fireEvent.change(screen.getByLabelText('示例员工甲地点'), { target: { value: '测试酒店' } })
    fireEvent.click(screen.getByRole('button', { name: '停用 三类工资素材必须齐全' }))
    fireEvent.change(screen.getByLabelText('辅助材料完整性规则名称'), {
      target: { value: '工资素材完整性' },
    })
    fireEvent.click(screen.getByRole('button', { name: '删除 相邻月份人员与工资变化' }))
    fireEvent.click(screen.getByRole('button', { name: '保存全部工资规则' }))

    await waitFor(() => expect(api.runPayrollLegacyCommand).toHaveBeenCalledWith(expect.objectContaining({
      action: 'SAVE_RULES',
      expectedRevision: 1,
      payload: expect.objectContaining({ review_rules: savedReviewRules }),
      csrfToken: 'csrf-test',
    })))
    expect(screen.getByRole('button', { name: '启用 工资素材完整性' })).toBeInTheDocument()
    expect(screen.queryByDisplayValue('相邻月份人员与工资变化')).not.toBeInTheDocument()
  })

  it('adds a missing review rule and shows the real save error', async () => {
    const withoutHistory: PayrollLegacyWorkspace = {
      ...legacyWorkspace,
      rules: {
        ...legacyWorkspace.rules,
        review_rules: legacyWorkspace.rules.review_rules!.slice(0, 2),
      },
    }
    vi.spyOn(api, 'getPayrollLegacyWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1',
      entity_ref: testWorkspace.entity_ref,
      company_id: testWorkspace.company_id,
      data: withoutHistory,
    })
    vi.spyOn(api, 'runPayrollLegacyCommand').mockRejectedValue(
      new ApiError('审查规则名称不能为空', 422, 'LEGACY_PAYROLL_REVIEW_RULES_INVALID'),
    )

    render(<PayrollLegacyWorkbench testWorkspace={testWorkspace} csrfToken="csrf-test" />)
    await screen.findByText('工资主表已保存 · 版本 1')
    fireEvent.click(screen.getByRole('button', { name: '管理工资规则' }))
    fireEvent.change(screen.getByLabelText('示例员工甲工种'), { target: { value: '前台' } })
    fireEvent.change(screen.getByLabelText('示例员工甲地点'), { target: { value: '测试酒店' } })
    fireEvent.click(screen.getByRole('button', { name: '新增审查规则' }))
    expect(screen.getByDisplayValue('相邻月份人员与工资变化')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存全部工资规则' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('审查规则名称不能为空')
  })
})
