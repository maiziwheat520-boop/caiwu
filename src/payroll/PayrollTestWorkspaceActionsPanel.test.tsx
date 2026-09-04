import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type {
  PayrollTestBatchValidationResult,
  PayrollTestMaterialOrganizeResult,
  PayrollTestWorkspaceReadResponse,
} from '../types'
import { PayrollTestWorkspaceActionsPanel } from './PayrollTestWorkspaceActionsPanel'

const companyId = 'company_live_hotel'
const material = (
  materialId: string,
  period: '2026-07' | '2026-08',
  materialType: string,
) => ({
  company_id: companyId,
  material_id: materialId,
  routing_status: 'AUTO_TEST' as const,
  period,
  material_type: materialType,
  payable: false as const,
  submission_supported: false as const,
})

const workspace: PayrollTestWorkspaceReadResponse = {
  contract_version: 'ledgerbridge.payroll-test-workspace-read.v1',
  entity_ref: '10000000-0000-4000-8000-000000000001',
  company_id: companyId,
  data: {
    contract_version: '1.0.0',
    schema_version: 'payroll-ledgerbridge-test-projection/v1',
    data_scope: 'TEST_ONLY',
    test_batch_id: 'payroll_history_through_2026_08',
    company_id: companyId,
    cutoff_date: '2026-08-31',
    workspace_revision: 1,
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-09-01T00:00:00.000Z',
    auto_test_ready: true,
    payment_submission_supported: false,
    payable: false,
    submission_supported: false,
    routing_counts: { auto_test: 6, review_required: 0, date_unknown: 0 },
    materials: [
      material('material_main_2026_07', '2026-07', 'PAYROLL_SHEET'),
      material('material_attendance_2026_07', '2026-07', 'ATTENDANCE_SHEET'),
      material('material_aunt_2026_07', '2026-07', 'AUNT_ATTENDANCE_SHEET'),
      material('material_review_2026_08', '2026-08', 'REVIEW_STATISTICS'),
      material('material_legacy_review_z8legacy', '2026-08', 'ADJUSTMENT_SOURCE'),
      material('material_release_2026_08', '2026-08', 'RELEASE_LIST'),
    ],
  },
}

const validation: PayrollTestBatchValidationResult = {
  schema_version: 'payroll-test-batch-validation-result/v1',
  data_scope: 'TEST_ONLY',
  test_batch_id: workspace.data.test_batch_id,
  company_id: workspace.company_id,
  workspace_revision: 1,
  ready_batch_count: 1,
  blocked_material_count: 0,
  batches: [{
    batch_id: 'payroll_history_through_2026_08_2026_07',
    period: '2026-07',
    material_count: 3,
    payroll_sheet_count: 1,
    supporting_material_count: 2,
    status: 'READY_FOR_TEST_REVIEW',
  }],
  payment_submission_supported: false,
  payable: false,
  submission_supported: false,
  replayed: false,
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollTestWorkspaceActionsPanel', () => {
  it('single-selects and explicitly confirms exactly one material for each monthly role', () => {
    const onConfirmedMaterials = vi.fn()
    const selectionWorkspace: PayrollTestWorkspaceReadResponse = {
      ...workspace,
      data: {
        ...workspace.data,
        materials: [
          ...workspace.data.materials,
          material('material_attendance_2026_08', '2026-08', 'ATTENDANCE_SHEET'),
          material('material_aunt_2026_08', '2026-08', 'AUNT_ATTENDANCE_SHEET'),
        ],
      },
    }
    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={selectionWorkspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
        onConfirmedMaterials={onConfirmedMaterials}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '2026 年 8 月 4' }))
    fireEvent.click(screen.getByLabelText('选择2026.8_考勤表'))
    fireEvent.click(screen.getByLabelText('选择2026.8_阿姨考勤表'))
    fireEvent.click(screen.getByLabelText('选择2026.8_好评统计（版本 2）'))
    fireEvent.click(screen.getByRole('button', { name: '确认这三份素材' }))

    expect(onConfirmedMaterials).toHaveBeenCalledWith({
      period: '2026-08',
      material_ids: {
        attendance: 'material_attendance_2026_08',
        aunt_attendance: 'material_aunt_2026_08',
        review_statistics: 'material_review_2026_08',
      },
    })
  })

  it('shows only July/August wage inputs with the original standard names', () => {
    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={workspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: '七、八月工资表素材' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '全部 4' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '2026 年 7 月 2' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '2026 年 8 月 2' })).toBeInTheDocument()
    expect(screen.getByText('2026.7_考勤表')).toBeInTheDocument()
    expect(screen.getByText('2026.7_阿姨考勤表')).toBeInTheDocument()
    expect(screen.getByText('2026.8_好评统计（版本 1）')).toBeInTheDocument()
    expect(screen.getByText('2026.8_好评统计（版本 2）')).toBeInTheDocument()
    expect(screen.queryByText(/material_main|material_release|发放名单/)).not.toBeInTheDocument()
    expect(screen.queryByText(/8 月及以前|9 月后待审核|日期待确认/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '2026 年 7 月 2' }))
    expect(screen.getByText('2026.7_考勤表')).toBeInTheDocument()
    expect(screen.queryByText(/2026\.8_好评统计/)).not.toBeInTheDocument()
  })

  it('renames a legacy wage input within the July/August boundary', async () => {
    const target = workspace.data.materials.find(
      (item) => item.material_id === 'material_legacy_review_z8legacy',
    )!
    const organized: PayrollTestMaterialOrganizeResult = {
      schema_version: 'payroll-test-material-organize-result/v1',
      data_scope: 'TEST_ONLY',
      test_batch_id: workspace.data.test_batch_id,
      company_id: workspace.company_id,
      workspace_revision: 2,
      projection_revision: 'b'.repeat(64),
      material: { ...target, material_type: 'REVIEW_STATISTICS' },
      payment_submission_supported: false,
      payable: false,
      submission_supported: false,
      replayed: false,
    }
    vi.spyOn(api, 'organizePayrollTestMaterial').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-workspace-command-result.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      action: 'payroll.test_workspace.organize',
      resource_ref: target.material_id,
      replayed: false,
      data: organized,
    })
    vi.spyOn(api, 'getPayrollTestWorkspace').mockResolvedValue(workspace)

    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={workspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    const targetCard = screen.getByText(/Z8LEGACY/).closest('article')!
    fireEvent.click(within(targetCard).getByRole('button', { name: '调整名称' }))
    expect(screen.getByLabelText('归属月份')).toHaveAttribute('min', '2026-07')
    expect(screen.getByLabelText('归属月份')).toHaveAttribute('max', '2026-08')
    fireEvent.change(screen.getByLabelText('材料类型'), { target: { value: 'REVIEW_STATISTICS' } })
    fireEvent.click(screen.getByRole('button', { name: '确认归类' }))

    await waitFor(() => expect(api.organizePayrollTestMaterial).toHaveBeenCalledWith({
      materialId: target.material_id,
      expectedWorkspaceRevision: 1,
      period: '2026-08',
      materialType: 'REVIEW_STATISTICS',
      csrfToken: 'csrf-test-token',
    }))
  })

  it('opens a safe in-page preview so the imported material can be identified', async () => {
    vi.spyOn(api, 'previewPayrollInputMaterial').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      material_id: 'material_attendance_2026_07',
      data: {
        schema_version: 'payroll-input-material-preview/v1',
        data_scope: 'TEST_ONLY',
        test_batch_id: workspace.data.test_batch_id,
        company_id: workspace.company_id,
        material_id: 'material_attendance_2026_07',
        period: '2026-07',
        material_type: 'ATTENDANCE_SHEET',
        detected_material_type: 'ATTENDANCE_SHEET',
        canonical_name: '2026.7_考勤表',
        selected_sheet: '考勤表',
        sheet_names: ['考勤表'],
        columns: ['姓名', '考勤天数', '夜班'],
        record_count: 2,
        preview_rows: [
          { source_row: 2, values: ['员工甲', '26', '4'] },
          { source_row: 3, values: ['员工乙', '25', '3'] },
        ],
        status: 'READY_FOR_REVIEW',
        payment_submission_supported: false,
        payable: false,
        submission_supported: false,
      },
    })
    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={workspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    const card = screen.getByText('2026.7_考勤表').closest('article')!
    fireEvent.click(within(card).getByRole('button', { name: '查看内容' }))

    expect(await screen.findByRole('table', { name: '素材前八行' })).toBeInTheDocument()
    expect(screen.getByText('考勤表 · 共 2 条记录')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '员工甲' })).toBeInTheDocument()
    expect(screen.getByText('只读内容预览 · 不可付款')).toBeInTheDocument()
  })

  it('checks the selected test months without exposing payment actions', async () => {
    vi.spyOn(api, 'validatePayrollTestWorkspace').mockResolvedValue({
      contract_version: 'ledgerbridge.payroll-test-workspace-command-result.v1',
      entity_ref: workspace.entity_ref,
      company_id: workspace.company_id,
      action: 'payroll.test_workspace.validate',
      resource_ref: workspace.data.test_batch_id,
      replayed: false,
      data: validation,
    })
    render(
      <PayrollTestWorkspaceActionsPanel
        workspace={workspace}
        csrfToken="csrf-test-token"
        onWorkspaceChange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '检查七八月素材' }))
    expect(await screen.findByText('1 个可测试，0 份材料仍需整理')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|提交银行/ })).not.toBeInTheDocument()
  })
})
