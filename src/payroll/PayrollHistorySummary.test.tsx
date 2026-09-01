import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type { PayrollTestWorkspaceReadResponse } from '../types'
import { PayrollHistorySummary } from './PayrollHistorySummary'

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
    auto_test_ready: true,
    payment_submission_supported: false,
    payable: false,
    submission_supported: false,
    routing_counts: { auto_test: 2, review_required: 0, date_unknown: 0 },
    materials: [
      {
        company_id: 'company_live_hotel',
        material_id: 'material_payroll_version_001',
        routing_status: 'AUTO_TEST',
        period: '2026-08',
        material_type: 'PAYROLL_SHEET',
        payable: false,
        submission_supported: false,
      },
      {
        company_id: 'company_live_hotel',
        material_id: 'material_payroll_version_002',
        routing_status: 'AUTO_TEST',
        period: '2026-08',
        material_type: 'PAYROLL_SHEET',
        payable: false,
        submission_supported: false,
      },
    ],
  },
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollHistorySummary', () => {
  it('lists competing saved payroll sheets separately instead of double counting them', async () => {
    vi.spyOn(api, 'previewPayrollTestMaterial')
      .mockResolvedValueOnce({
        contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1',
        entity_ref: workspace.entity_ref,
        company_id: workspace.company_id,
        material_id: workspace.data.materials[0].material_id,
        data: {
          schema_version: 'payroll-test-material-preview/v1',
          data_scope: 'TEST_ONLY',
          test_batch_id: workspace.data.test_batch_id,
          company_id: workspace.company_id,
          material_id: workspace.data.materials[0].material_id,
          period: '2026-08',
          status: 'READY_FOR_REVIEW',
          line_count: 2,
          total_net_pay_cents: 820000,
          lines: [],
          exceptions: [],
          payment_submission_supported: false,
          payable: false,
          submission_supported: false,
        },
      })
      .mockRejectedValueOnce(new Error('controlled parse failure'))

    render(<PayrollHistorySummary workspace={workspace} />)
    fireEvent.click(screen.getByRole('button', { name: '生成网页汇总预览' }))

    await waitFor(() => expect(api.previewPayrollTestMaterial).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('本月存在 2 份不同工资表；为防重复计算，系统没有把它们合并为一个总数。')).toBeInTheDocument()
    expect(screen.getByText('¥8,200.00')).toBeInTheDocument()
    expect(screen.getByText('文件无法安全解析')).toBeInTheDocument()
    expect(screen.getByText('只读汇总 · 测试数据 · 不可付款 · 刷新后可从已保存材料重新生成')).toBeInTheDocument()
  })
})
