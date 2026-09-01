import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { api, ApiError } from '../api'
import type { PayrollReadResponse, PayrollStatusData } from '../types'
import { PayrollWorkspacePage } from './PayrollWorkspacePage'

const status: PayrollReadResponse<PayrollStatusData> = {
  contract_version: 'ledgerbridge.payroll-read.v1',
  entity_ref: '30000000-0000-4000-8000-000000000001',
  company_id: 'company_hotel_001',
  data: {
    schema_version: 'ledgerbridge.payroll-status.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    live_data_ready: false,
    live_projection_schema: 'payroll-ledgerbridge-live-projection/v1',
    payment_operations_exposed: false,
    capabilities: { commands_enabled: false, allowed_actions: [] },
    setup_summary: {
      provider_connected: true,
      runtime_mode: 'live-provider',
      unassigned_material_count: 3,
      ready_material_count: 0,
      company_mapped_material_count: 1,
      blocking_reason_codes: ['UNASSIGNED_MATERIALS', 'PAYROLL_BATCH_REQUIRED'],
    },
  },
}

describe('PayrollWorkspacePage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('owns payroll loading and renders the provider blockers through its module interface', async () => {
    vi.spyOn(api, 'getSession').mockResolvedValue({
      principal: 'test-user',
      csrf_token: 'csrf-test',
      expires_at: '2026-09-01T12:00:00+08:00',
      runtime_mode: 'core-backed',
    })
    vi.spyOn(api, 'getPayrollStatus').mockResolvedValue(status)
    vi.spyOn(api, 'getPayrollTestWorkspace').mockRejectedValue(new ApiError('disabled', 404))

    render(<PayrollWorkspacePage />)

    expect(await screen.findByText('服务已接通，待归属材料 3 份')).toBeInTheDocument()
    expect(screen.getByText('工资材料仍有待归属项')).toBeInTheDocument()
    expect(screen.getByText('尚未生成可核对的工资批次')).toBeInTheDocument()
    expect(screen.getByText('八项工资功能独立恢复')).toBeInTheDocument()
    expect(screen.getByText('填入主表')).toBeInTheDocument()
    expect(screen.getByText('生成代发表')).toBeInTheDocument()
    expect(screen.getByText('管理工资规则')).toBeInTheDocument()
    expect(screen.getByText('0 / 8 已接回可见工作区')).toBeInTheDocument()
  })
})
