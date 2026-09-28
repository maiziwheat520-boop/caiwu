import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { api, ApiError } from '../api'
import type { PayrollDatabaseWorkbench as Workbench } from '../types'
import { PayrollDatabaseWorkbench } from './PayrollDatabaseWorkbench'

const data: Workbench = {
  contract_version: 'ledgerbridge.payroll-workbench.v1',
  entity_ref: '10000000-0000-4000-8000-000000000001',
  batch_ref: '20000000-0000-4000-8000-000000000001',
  batch_version_ref: '30000000-0000-4000-8000-000000000001',
  pay_period: '2026-08',
  reconciliation_month: '2026-08',
  revision: 1,
  status: 'LOCKED',
  rules_version: 'legacy-v1',
  content_sha256: 'a'.repeat(64),
  line_count: 1,
  net_amount_minor: 600000,
  cash_amount_minor: 100000,
  supplemental_amount_minor: 15000,
  bank_amount_minor: 500000,
  lines: [{
    line_ref: '40000000-0000-4000-8000-000000000001',
    employee_ref: '50000000-0000-4000-8000-000000000001',
    employee_name: '测试员工',
    employee_type: 'REGULAR',
    location: '星汇',
    job_group: '前台',
    attendance_days: '31',
    payment_channel: 'MYBANK',
    payee_name: '测试收款人',
    account_masked: '****1234',
    memo: '工资',
    net_amount_minor: 600000,
    cash_amount_minor: 100000,
    supplemental_amount_minor: 15000,
    bank_amount_minor: 500000,
  }],
  issues: [],
}

describe('PayrollDatabaseWorkbench', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows database totals and masked lines without a raw account', async () => {
    vi.spyOn(api, 'getPayrollDatabaseWorkbench').mockResolvedValue(data)
    render(<PayrollDatabaseWorkbench />)

    expect(await screen.findByText('测试员工')).toBeInTheDocument()
    expect(screen.getByText('****1234')).toBeInTheDocument()
    expect(screen.queryByText(/6222000012345678/)).not.toBeInTheDocument()
    expect(screen.getByText(/含补发/)).toBeInTheDocument()
  })

  it('treats an unmigrated month as absent rather than zero pay', async () => {
    vi.spyOn(api, 'getPayrollDatabaseWorkbench').mockRejectedValue(
      new ApiError('missing', 404, 'PAYROLL_WORKBENCH_NOT_FOUND'),
    )
    render(<PayrollDatabaseWorkbench />)

    expect(await screen.findByText(/该月尚未迁入数据库/)).toBeInTheDocument()
    expect(screen.queryByText('¥0.00')).not.toBeInTheDocument()
  })
})
