import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Theme } from '@radix-ui/themes'
import App from './App'

function renderApp() {
  return render(<Theme><App /></Theme>)
}

describe('LedgerBridge Web prototype', () => {
  it('shows the synthetic-data boundary and pending count', () => {
    renderApp()
    expect(screen.getByText('原型环境 · 合成数据')).toBeInTheDocument()
    expect(screen.getByText('4 条')).toBeInTheDocument()
  })

  it('confirms a complete candidate and removes it from the review queue', () => {
    renderApp()
    fireEvent.click(screen.getAllByText('待审核')[0])
    expect(screen.getByText('城南店 8 月布草清洗费用，供应商月结单')).toBeInTheDocument()
    const confirmButtons = screen.getAllByRole('button', { name: '确认' })
    fireEvent.click(confirmButtons[0])
    expect(screen.queryByText('城南店 8 月布草清洗费用，供应商月结单')).not.toBeInTheDocument()
    expect(screen.getByText(/已确认并进入本月草稿数据/)).toBeInTheDocument()
  })

  it('keeps conflict and incomplete candidates blocked', () => {
    renderApp()
    fireEvent.click(screen.getAllByText('待审核')[0])
    const confirmButtons = screen.getAllByRole('button', { name: '确认' })
    const disabledButtons = confirmButtons.filter((button) => button.hasAttribute('disabled'))
    expect(disabledButtons).toHaveLength(2)
  })

  it('shows the reconciliation blocker before draft generation', () => {
    renderApp()
    fireEvent.click(screen.getAllByText('月度对账')[0])
    expect(screen.getByText('本月草稿尚不可生成')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /生成对账草稿/ })).toBeDisabled()
  })
})
