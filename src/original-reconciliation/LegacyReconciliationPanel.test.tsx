import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'
import type { LegacyReconciliationMonth } from '../types'
import { LegacyReconciliationPanel } from './LegacyReconciliationPanel'

const sourceRef = '99999999-9999-4999-8999-999999999999'

function sources() {
  return {
    contract_version: 'ledgerbridge.reconciliation-legacy-sources.v1' as const,
    sources: [{ source_ref: sourceRef, source_sha256: 'a'.repeat(64), imported_at: '2026-09-29T00:00:00Z', periods: ['2024-01'] }],
  }
}

function month(): LegacyReconciliationMonth {
  return {
    contract_version: 'ledgerbridge.reconciliation-legacy-month.v1',
    source_ref: sourceRef, source_sha256: 'a'.repeat(64), imported_at: '2026-09-29T00:00:00Z',
    period: '2024-01', sheet_name: '24.01', row_count: 2, cell_count: 2,
    content_sha256: 'b'.repeat(64),
    cells: [
      { address: 'A1', type: 'text', value: '历史收入', cached_type: null, cached_value: null },
      { address: 'B1', type: 'formula', value: '=100+20', cached_type: 'number', cached_value: '120' },
    ],
  }
}

describe('historical reconciliation archive', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows saved workbook values without recalculating the formula', async () => {
    vi.spyOn(api, 'getLegacyReconciliationSources').mockResolvedValue(sources())
    vi.spyOn(api, 'getLegacyReconciliationMonth').mockResolvedValue(month())
    render(<LegacyReconciliationPanel month="2024-01" />)
    fireEvent.click(screen.getByText('历史对账原表'))

    const grid = await screen.findByRole('region', { name: '2024-01 历史原表单元格' })
    expect(within(grid).getByText('历史收入')).toBeInTheDocument()
    expect(within(grid).getByText('120')).toBeInTheDocument()
    expect(within(grid).queryByText('=100+20')).not.toBeInTheDocument()
  })

  it('does not present an absent month as a zero reconciliation', async () => {
    vi.spyOn(api, 'getLegacyReconciliationSources').mockResolvedValue(sources())
    render(<LegacyReconciliationPanel month="2024-02" />)
    fireEvent.click(screen.getByText('历史对账原表'))

    expect(await screen.findByText('所选月份尚无已入库的历史原表。')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /历史原表单元格/ })).not.toBeInTheDocument()
  })
})
