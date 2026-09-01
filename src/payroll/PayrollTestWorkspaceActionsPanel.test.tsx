import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type {
  PayrollTestBatchValidationResult,
  PayrollTestMaterialOrganizeResult,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import { PayrollTestWorkspaceActionsPanel } from './PayrollTestWorkspaceActionsPanel'

const workspace: PayrollTestWorkspaceReadResponse = {
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
    auto_test_ready: false,
    payment_submission_supported: false,
    payable: false,
    submission_supported: false,
    routing_counts: { auto_test: 0, review_required: 0, date_unknown: 1 },
    materials: [{
      company_id: 'company_live_hotel',
      material_id: 'material_date_unknown',
      routing_status: 'DATE_UNKNOWN',
      period: null,
      material_type: null,
      payable: false,
      submission_supported: false,
    }],
  },
}

const organized: PayrollTestMaterialOrganizeResult = {
  schema_version: 'payroll-test-material-organize-result/v1',
  data_scope: 'TEST_ONLY',
  test_batch_id: workspace.data.test_batch_id,
  company_id: workspace.company_id,
  workspace_revision: 2,
  projection_revision: 'b'.repeat(64),
  material: {
    ...workspace.data.materials[0],
    routing_status: 'AUTO_TEST',
    period: '2026-08',
    material_type: 'PAYROLL_SHEET',
  },
  payment_submission_supported: false,
  payable: false,
  submission_supported: false,
  replayed: false,
}

const validation: PayrollTestBatchValidationResult = {
  schema_version: 'payroll-test-batch-validation-result/v1',
  data_scope: 'TEST_ONLY',
  test_batch_id: workspace.data.test_batch_id,
  company_id: workspace.company_id,
  workspace_revision: 2,
  ready_batch_count: 1,
  blocked_material_count: 0,
  batches: [{
    batch_id: 'payroll_history_through_2026_08_2026_08',
    period: '2026-08',
    material_count: 1,
    payroll_sheet_count: 1,
    supporting_material_count: 0,
    status: 'READY_FOR_TEST_REVIEW',
  }],
  payment_submission_supported: false,
  payable: false,
  submission_supported: false,
  replayed: false,
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollTestWorkspaceActionsPanel', () => {
  it('organizes a historical material and validates a nonpayable test batch', async () => {
    const updated = {
      ...workspace,
      data: {
        ...workspace.data,
        workspace_revision: 2,
        auto_test_ready: true,
        routing_counts: { auto_test: 1, review_required: 0, date_unknown: 0 },
        materials: [organized.material],
      },
    }
    const onWorkspaceChange = vi.fn()
    vi.spyOn(api, 'organizePayrollTestMaterial').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-workspace-command-result.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      action: 'payroll.test_workspace.organize',
      resource_ref: organized.material.material_id,
      replayed: false,
      data: organized,
    })
    vi.spyOn(api, 'getPayrollTestWorkspace').mockResolvedValue(updated)
    vi.spyOn(api, 'validatePayrollTestWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-workspace-command-result.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      action: 'payroll.test_workspace.validate',
      resource_ref: workspace.data.test_batch_id,
      replayed: false,
      data: validation,
    })

    const { rerender } = render(
      <PayrollTestWorkspaceActionsPanel
        workspace={workspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={onWorkspaceChange}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '归类' }))
    expect(screen.getByLabelText('归属月份')).toHaveValue('')
    expect(screen.getByRole('button', { name: '确认归类' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('归属月份'), { target: { value: '2026-08' } })
    fireEvent.click(screen.getByRole('button', { name: '确认归类' }))

    await waitFor(() => expect(api.organizePayrollTestMaterial).toHaveBeenCalledWith({
      materialId: 'material_date_unknown',
      expectedWorkspaceRevision: 1,
      period: '2026-08',
      materialType: 'PAYROLL_SHEET',
      csrfToken: 'csrf-test-token',
    }))
    await waitFor(() => expect(onWorkspaceChange).toHaveBeenCalledWith(updated))

    rerender(
      <PayrollTestWorkspaceActionsPanel
        workspace={updated}
        csrfToken="csrf-test-token"
        onWorkspaceChange={onWorkspaceChange}
      />,
    )
    expect(screen.getByRole('button', { name: '查看工资明细' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '生成并验证测试批次' }))
    await waitFor(() => expect(api.validatePayrollTestWorkspace).toHaveBeenCalledWith({
      expectedWorkspaceRevision: 2,
      csrfToken: 'csrf-test-token',
    }))
    expect(await screen.findByText('1 个可测试，0 份材料仍需整理')).toBeInTheDocument()
    expect(screen.getByText('测试数据，不会发薪')).toBeInTheDocument()
  })

  it('shows parsed XLS/XLSX payroll lines instead of metadata-only controls', async () => {
    const material = organized.material
    vi.spyOn(api, 'previewPayrollTestMaterial').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      material_id: material.material_id,
      data: {
        schema_version: 'payroll-test-material-preview/v1',
        data_scope: 'TEST_ONLY',
        test_batch_id: workspace.data.test_batch_id,
        company_id: workspace.company_id,
        material_id: material.material_id,
        period: '2026-08',
        routing_status: 'AUTO_TEST',
        auto_batch_eligible: true,
        status: 'READY_FOR_REVIEW',
        line_count: 1,
        total_net_pay_cents: 512000,
        lines: [{
          source_row: 4,
          company_id: workspace.company_id,
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
          net_pay_cents: 512000,
          notes: '脱敏测试材料',
        }],
        exceptions: [],
        payment_submission_supported: false,
        payable: false,
        submission_supported: false,
      },
    })
    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={{
          ...workspace,
          data: {
            ...workspace.data,
            auto_test_ready: true,
            routing_counts: { auto_test: 1, review_required: 0, date_unknown: 0 },
            materials: [material],
          },
        }}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看工资明细' }))
    await waitFor(() => expect(api.previewPayrollTestMaterial).toHaveBeenCalledWith(
      material.material_id,
    ))
    expect(await screen.findByText('示例员工甲')).toBeInTheDocument()
    expect(screen.getByText('****0138')).toBeInTheDocument()
    expect(screen.getByText('¥5,120.00')).toBeInTheDocument()
    expect(screen.getByText('表内金额校验通过')).toBeInTheDocument()
    expect(screen.getByText('只读预览 · 不可付款')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|提交银行/ })).not.toBeInTheDocument()
  })

  it('allows a review-required payroll sheet to be previewed without exposing payment actions', async () => {
    const material = {
      ...organized.material,
      routing_status: 'REVIEW_REQUIRED' as const,
      period: '2026-09',
    }
    vi.spyOn(api, 'previewPayrollTestMaterial').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      material_id: material.material_id,
      data: {
        schema_version: 'payroll-test-material-preview/v1',
        data_scope: 'TEST_ONLY',
        test_batch_id: workspace.data.test_batch_id,
        company_id: workspace.company_id,
        material_id: material.material_id,
        period: '2026-09',
        routing_status: 'REVIEW_REQUIRED',
        auto_batch_eligible: false,
        status: 'NEEDS_HUMAN_REVIEW',
        line_count: 0,
        total_net_pay_cents: 0,
        lines: [],
        exceptions: [{ code: 'REVIEW_REQUIRED', severity: 'WARNING', row: 1 }],
        payment_submission_supported: false,
        payable: false,
        submission_supported: false,
      },
    })

    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={{
          ...workspace,
          data: {
            ...workspace.data,
            routing_counts: { auto_test: 0, review_required: 1, date_unknown: 0 },
            materials: [material],
          },
        }}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '查看工资明细' }))
    await waitFor(() => expect(api.previewPayrollTestMaterial).toHaveBeenCalledWith(material.material_id))
    expect(await screen.findByText('只读预览 · 不可付款')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|提交银行/ })).not.toBeInTheDocument()
  })
})
