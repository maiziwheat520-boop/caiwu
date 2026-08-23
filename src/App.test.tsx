import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Theme } from '@radix-ui/themes'
import App from './App'
import type { ApiCandidate } from './types'

const session = {
  principal: 'finance-admin',
  csrf_token: 'csrf-token-with-at-least-thirty-two-characters',
  expires_at: '2026-08-24T18:00:00+08:00',
}

const evidence = [{
  id: 'evidence-1', kind: 'message' as const, media_type: 'text/plain', sha256: 'a'.repeat(64), original_filename: null,
}]

const candidates: ApiCandidate[] = [
  {
    id: 'candidate-1', short_id: 'C-8F21', revision: 3, status: 'PENDING', source_channel: 'telegram',
    source_message_id: 'message-1', received_at: '2026-08-24T09:42:00+08:00', business_unit: '城南店',
    category: '布草', amount_minor: 638000, currency: 'CNY', accounting_month: '2026-08',
    summary: '城南店 8 月布草清洗费用，供应商月结单', confidence_basis_points: 9600, evidence, blockers: [],
  },
  {
    id: 'candidate-2', short_id: 'C-62D9', revision: 1, status: 'INCOMPLETE', source_channel: 'weixin',
    source_message_id: 'message-2', received_at: '2026-08-23T17:35:00+08:00', business_unit: '机场店',
    category: '水费', amount_minor: 483260, currency: 'CNY', accounting_month: null,
    summary: '机场店水费，原消息未说明归属月份', confidence_basis_points: 8800, evidence,
    blockers: [{ code: 'MISSING_ACCOUNTING_MONTH', message: '缺少归属月份' }],
  },
  {
    id: 'candidate-3', short_id: 'C-5B17', revision: 2, status: 'CONFLICTED', source_channel: 'dingtalk',
    source_message_id: 'message-3', received_at: '2026-08-23T14:02:00+08:00', business_unit: '城南店',
    category: '银行收款', amount_minor: 1268000, currency: 'CNY', accounting_month: '2026-08',
    summary: '城南店银行收款，与另一条候选冲突', confidence_basis_points: 9400, evidence,
    blockers: [{ code: 'BUSINESS_KEY_CONFLICT', message: '相同凭证号金额不同' }],
  },
  {
    id: 'candidate-4', short_id: 'C-49E3', revision: 4, status: 'CONFIRMED', source_channel: 'dingtalk',
    source_message_id: 'message-4', received_at: '2026-08-21T11:28:00+08:00', business_unit: '江景店',
    category: '税费', amount_minor: 924050, currency: 'CNY', accounting_month: '2026-08',
    summary: '江景店本月税费缴款', confidence_basis_points: 9800, evidence, blockers: [],
  },
]

const reconciliation = {
  accounting_month: '2026-08', revision: 7, ready: false,
  blockers: [{ code: 'BUSINESS_KEY_CONFLICT', message: '相同凭证号金额不同' }],
  business_units: [{ name: '城南店', amounts_minor: { water: 512080, linen: 638000, bank_receipts: 4286000 } }],
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': status >= 400 ? 'application/problem+json' : 'application/json' },
  })
}

function installFetch({ items = candidates, failSessionOnce = false, failReconciliationAfterDecision = false }: {
  items?: ApiCandidate[]
  failSessionOnce?: boolean
  failReconciliationAfterDecision?: boolean
} = {}) {
  let shouldFailSession = failSessionOnce
  let decisionSaved = false
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url === '/api/v1/session') {
      if (shouldFailSession) {
        shouldFailSession = false
        return response({ title: '服务暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
      return response(session)
    }
    if (url === '/api/v1/candidates') return response({ items, next_cursor: null })
    if (url.startsWith('/api/v1/reconciliations/') && init?.method !== 'POST') {
      if (decisionSaved && failReconciliationAfterDecision) return response({ title: '对账投影暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      return response(reconciliation)
    }
    if (url === '/api/v1/connections') return response({ items: [] })
    if (url.includes('/decisions') && init?.method === 'POST') {
      decisionSaved = true
      const body = JSON.parse(String(init.body)) as { decision: string }
      const original = candidates.find((candidate) => url.includes(candidate.id))!
      return response({
        candidate: { ...original, revision: original.revision + 1, status: body.decision === 'IGNORE' ? 'IGNORED' : 'CONFIRMED' },
        event: { id: 'event-1', candidate_id: original.id, sequence: 1, decision: body.decision, actor: 'finance-admin', reason: 'review', created_at: '2026-08-24T10:00:00+08:00' },
      })
    }
    if (url.startsWith('/api/v1/candidates/')) {
      const candidate = candidates.find((item) => url.endsWith(item.id))!
      return response({ ...candidate, review_events: [] })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
}

function renderApp() {
  return render(<Theme><App /></Theme>)
}

describe('LedgerBridge Web API client', () => {
  beforeEach(() => vi.restoreAllMocks())
  afterEach(() => vi.restoreAllMocks())

  it('loads API projections and formats amount_minor as yuan', async () => {
    installFetch()
    renderApp()
    expect(screen.getByText('正在读取财务数据')).toBeInTheDocument()
    expect(screen.getByText('原型环境 · 合成 API 数据')).toBeInTheDocument()
    expect(await screen.findByText('¥6,380.00')).toBeInTheDocument()
    expect(screen.getByText('3 条')).toBeInTheDocument()
  })

  it('shows a request error and retries the full projection load', async () => {
    const fetchMock = installFetch({ failSessionOnce: true })
    renderApp()
    expect(await screen.findByText('数据读取失败')).toBeInTheDocument()
    expect(screen.getByText('服务暂不可用')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /重试/ }))
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/session')).toHaveLength(2)
  })

  it('posts a confirmed decision with CSRF, idempotency and revision', async () => {
    const fetchMock = installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    fireEvent.click(screen.getAllByRole('button', { name: '确认' })[0])

    expect(await screen.findByText(/C-8F21 已确认/)).toBeInTheDocument()
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(decisionCall).toBeDefined()
    const init = decisionCall![1]!
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse(String(init.body))).toMatchObject({ decision: 'CONFIRM', expected_revision: 3 })
    expect(fetchMock.mock.calls.filter(([input, requestInit]) => String(input) === '/api/v1/reconciliations/2026-08' && requestInit?.method !== 'POST')).toHaveLength(2)
  })

  it('keeps conflicts and missing months blocked', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    const disabledButtons = screen.getAllByRole('button', { name: '确认' }).filter((button) => button.hasAttribute('disabled'))
    expect(disabledButtons).toHaveLength(2)

    fireEvent.click(screen.getAllByText('机场店水费，原消息未说明归属月份')[0])
    expect(await screen.findByRole('button', { name: '保存更正并确认' })).toBeDisabled()
  })

  it('does not report a saved decision as failed when reconciliation refresh fails', async () => {
    installFetch({ failReconciliationAfterDecision: true })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    fireEvent.click(screen.getAllByRole('button', { name: '确认' })[0])
    expect(await screen.findByText('C-8F21 决定已保存，对账状态需刷新')).toBeInTheDocument()
    expect(screen.queryByText(/提交审核决定失败/)).not.toBeInTheDocument()
  })

  it('renders a real empty state when the API returns no candidates', async () => {
    installFetch({ items: [] })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    expect(screen.getByText('当前筛选下没有待审核项')).toBeInTheDocument()
  })
})
