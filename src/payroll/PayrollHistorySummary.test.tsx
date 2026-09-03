import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api'
import type {
  PayrollSummaryAuthoritativePreviewResponse,
  PayrollTestWorkspaceReadResponse,
} from '../types'
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
    routing_counts: { auto_test: 2, review_required: 0, date_unknown: 1 },
    materials: [
      {
        company_id: 'company_live_hotel',
        material_id: 'material_authoritative_summary',
        routing_status: 'DATE_UNKNOWN',
        period: null,
        material_type: 'PAYROLL_SUMMARY',
        payable: false,
        submission_supported: false,
      },
      {
        company_id: 'company_live_hotel',
        material_id: 'material_experiment_2026_07',
        routing_status: 'AUTO_TEST',
        period: '2026-07',
        material_type: 'PAYROLL_SHEET',
        payable: false,
        submission_supported: false,
      },
      {
        company_id: 'company_live_hotel',
        material_id: 'material_experiment_2026_08',
        routing_status: 'AUTO_TEST',
        period: '2026-08',
        material_type: 'PAYROLL_SHEET',
        payable: false,
        submission_supported: false,
      },
    ],
  },
}

const summaryResponse: PayrollSummaryAuthoritativePreviewResponse = {
  contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1',
  entity_ref: workspace.entity_ref,
  company_id: workspace.company_id,
  material_id: 'material_authoritative_summary',
  data: {
    schema_version: 'payroll-summary-authoritative-preview/v1',
    data_scope: 'TEST_ONLY',
    test_batch_id: workspace.data.test_batch_id,
    company_id: workspace.company_id,
    material_id: 'material_authoritative_summary',
    routing_status: 'DATE_UNKNOWN',
    source_of_truth: 'PAYROLL_SUMMARY',
    authoritative: true,
    period_count: 2,
    latest_period: '2026-07',
    periods: [
      {
        period: '2026-07',
        store_count: 2,
        stores: [
          { store_name: '青居客', net_pay_cents: 3_242_000 },
          { store_name: '同富', net_pay_cents: 14_019_198 },
        ],
        total_net_pay_cents: 17_261_198,
        total_source: 'SUMMARY_TOTAL_ROW',
        total_matches_stores: true,
      },
      {
        period: '2026-06',
        store_count: 2,
        stores: [
          { store_name: '青居客', net_pay_cents: 3_401_200 },
          { store_name: '同富', net_pay_cents: 13_632_798 },
        ],
        total_net_pay_cents: 17_033_998,
        total_source: 'SUMMARY_TOTAL_ROW',
        total_matches_stores: true,
      },
    ],
    payment_submission_supported: false,
    payable: false,
    submission_supported: false,
  },
}

afterEach(() => vi.restoreAllMocks())

describe('PayrollHistorySummary', () => {
  it('shows authoritative monthly store totals and ignores July/August experiment sheets', async () => {
    vi.spyOn(api, 'previewPayrollSummaryMaterial').mockResolvedValue(summaryResponse)

    render(<PayrollHistorySummary workspace={workspace} />)

    expect(await screen.findByRole('heading', { name: '2026-07 工资汇总' })).toBeInTheDocument()
    expect(api.previewPayrollSummaryMaterial).toHaveBeenCalledTimes(1)
    expect(api.previewPayrollSummaryMaterial).toHaveBeenCalledWith('material_authoritative_summary')
    expect(screen.getByRole('complementary', { name: '账期与版本' })).toBeInTheDocument()
    expect(screen.getByText('只读权威数据')).toBeInTheDocument()
    expect(screen.getByLabelText('对账月份')).toHaveValue('2026-07')
    expect(screen.getAllByText('¥172,611.98')).toHaveLength(2)
    const rows = screen.getByRole('table', { name: '各店当月工资汇总' })
    expect(within(rows).getByText('青居客')).toBeInTheDocument()
    expect(within(rows).getByText('同富')).toBeInTheDocument()
    expect(screen.getByText('七、八月工资素材保留在实验区，不参与这里的历史金额计算。')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('对账月份'), { target: { value: '2026-06' } })
    expect(screen.getByRole('heading', { name: '2026-06 工资汇总' })).toBeInTheDocument()
    expect(screen.getAllByText('¥170,339.98')).toHaveLength(2)
  })

  it('reports a controlled empty state when no payroll summary can be read', async () => {
    vi.spyOn(api, 'previewPayrollSummaryMaterial').mockRejectedValue(new Error('parse failed'))

    render(<PayrollHistorySummary workspace={workspace} />)

    await waitFor(() => expect(api.previewPayrollSummaryMaterial).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('工资统计总表暂时无法读取')).toBeInTheDocument()
    expect(screen.queryByText('¥172,611.98')).not.toBeInTheDocument()
  })
})
