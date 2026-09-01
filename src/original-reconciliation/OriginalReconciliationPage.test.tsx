import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { api } from '../api'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import { OriginalReconciliationPage } from './OriginalReconciliationPage'

describe('OriginalReconciliationPage', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders business tasks instead of the legacy spreadsheet grid', async () => {
    vi.spyOn(api, 'getOriginalReconciliation').mockResolvedValue(originalReconciliationFixture)

    render(<OriginalReconciliationPage onNavigate={vi.fn()} />)

    expect(await screen.findByText('已导入业务事项')).toBeInTheDocument()
    expect(screen.getByText('已确认候选（脱敏）')).toBeInTheDocument()
    expect(screen.getByText('待补录与审核')).toBeInTheDocument()
    expect(screen.queryByText('A')).not.toBeInTheDocument()
  })
})
