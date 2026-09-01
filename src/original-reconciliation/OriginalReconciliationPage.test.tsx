import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { api } from '../api'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import type { Candidate } from '../types'
import { OriginalReconciliationPage } from './OriginalReconciliationPage'

const juneWorkbookCandidate = {
  id: '10000000-0000-4000-8000-000000000006',
  shortId: 'C-JUNE',
  revision: 1,
  status: 'PENDING',
  accountingMonth: '2026-06',
  summary: '六月平台收入 | 原表 26.6!B4',
  category: '客房收入',
  businessUnit: '示例酒店',
  amountMinor: 12_345,
  evidence: [{
    id: '20000000-0000-4000-8000-000000000006',
    kind: 'attachment',
    media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    sha256: null,
    original_filename: 'original-reconciliation.xlsx',
  }],
  raw: { source_system: 'original_reconciliation_xlsx' },
} as Candidate

describe('OriginalReconciliationPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders business tasks instead of the legacy spreadsheet grid', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)

    render(<OriginalReconciliationPage candidates={[]} onNavigate={vi.fn()} onOpenCandidate={vi.fn()} />)

    expect(await screen.findByText('已导入业务事项')).toBeInTheDocument()
    expect(screen.getByText('已确认候选（脱敏）')).toBeInTheDocument()
    expect(screen.getByText('待补录与审核')).toBeInTheDocument()
    expect(screen.queryByText('A')).not.toBeInTheDocument()
  })

  it('opens an imported workbook item from the selected month', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)
    const onOpenCandidate = vi.fn()

    render(
      <OriginalReconciliationPage
        candidates={[juneWorkbookCandidate]}
        onNavigate={vi.fn()}
        onOpenCandidate={onOpenCandidate}
      />,
    )

    expect(await screen.findByText('六月平台收入 | 原表 26.6!B4')).toBeInTheDocument()
    expect(screen.getByText('客房收入')).toBeInTheDocument()
    expect(screen.getByText('示例酒店')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '打开事项 C-JUNE' }))
    expect(onOpenCandidate).toHaveBeenCalledWith(juneWorkbookCandidate)
  })
})
