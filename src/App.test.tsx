import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Theme } from '@radix-ui/themes'
import App from './App'
import type { ApiCandidate, AuthStatus, ReviewEvent } from './types'

const session = {
  principal: 'finance-admin',
  csrf_token: 'csrf-token-with-at-least-thirty-two-characters',
  expires_at: '2026-08-24T18:00:00+08:00',
}

const authenticatedStatus = {
  authenticated: true,
  setup_required: false,
  passkey_registered: true,
  recovery_setup_required: false,
  recovery_pending: false,
  principal: 'finance-admin',
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

const reviewEvents: ReviewEvent[] = [{
  id: 'event-seeded',
  candidate_id: 'candidate-4',
  sequence: 1,
  from_revision: 3,
  to_revision: 4,
  decision: 'CONFIRM',
  actor: 'finance-admin',
  reason: '已核对电子缴款书',
  changes: [{ field: 'status', previous_value: 'PENDING', new_value: 'CONFIRMED' }],
  conflict_resolution: null,
  created_at: '2026-08-21T11:35:00+08:00',
}]

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': status >= 400 ? 'application/problem+json' : 'application/json' },
  })
}

function installFetch(options: {
  items?: ApiCandidate[]
  failSessionOnce?: boolean
  failReconciliationAfterDecision?: boolean
  authStatus?: AuthStatus
  recoveryCodes?: string[]
  recoverySetupRequired?: boolean
  candidatePages?: Array<{ items: ApiCandidate[]; next_cursor: string | null }>
  reviewEventPages?: Array<{ items: ReviewEvent[]; next_cursor: string | null }>
  failReviewEvents?: boolean
} = {}) {
  const {
    items = candidates,
    failSessionOnce = false,
    failReconciliationAfterDecision = false,
    authStatus = authenticatedStatus,
    recoveryCodes = ['RECOVERY-ONE', 'RECOVERY-TWO'],
    recoverySetupRequired = false,
    candidatePages = [{ items, next_cursor: null }],
    reviewEventPages = [{ items: reviewEvents, next_cursor: null }],
    failReviewEvents = false,
  } = options
  let shouldFailSession = failSessionOnce
  let decisionSaved = false
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url === '/api/v1/auth/status') return response(authStatus)
    if (url === '/api/v1/auth/recovery/session') return response(session)
    if (url === '/api/v1/auth/passkey/login/options') return response({
      challenge: 'AQ', timeout: 60000, rpId: 'ledgerbridge.local', userVerification: 'required',
      allowCredentials: [{ type: 'public-key', id: 'Ag' }],
    })
    if (url === '/api/v1/auth/passkey/login/verify') return response(authenticatedStatus)
    if (url === '/api/v1/auth/passkey/register/options') return response({
      challenge: 'AQ', rp: { name: 'LedgerBridge', id: 'ledgerbridge.local' },
      user: { id: 'Ag', name: 'finance-admin', displayName: 'Finance Admin' },
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }], timeout: 60000,
    })
    if (url === '/api/v1/auth/passkey/register/verify') return response({ ...authenticatedStatus, recovery_codes: recoveryCodes })
    if (url === '/api/v1/auth/recovery') return response({
      ...authenticatedStatus,
      recovery_setup_required: recoverySetupRequired,
      recovery_pending: recoverySetupRequired,
      csrf_token: 'recovery-csrf-token-with-at-least-thirty-two-characters',
      expires_at: session.expires_at,
    })
    if (url === '/api/v1/session') {
      if (shouldFailSession) {
        shouldFailSession = false
        return response({ title: '服务暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
      return response(session)
    }
    if (url === '/api/v1/candidates' || url.startsWith('/api/v1/candidates?')) {
      const cursor = new URL(url, 'http://ledgerbridge.local').searchParams.get('cursor')
      return response(candidatePages[cursor ? 1 : 0] ?? { items: [], next_cursor: null })
    }
    if (url.startsWith('/api/v1/review-events')) {
      if (failReviewEvents) return response({ title: '操作记录暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      const cursor = new URL(url, 'http://ledgerbridge.local').searchParams.get('cursor')
      return response(reviewEventPages[cursor ? 1 : 0] ?? { items: [], next_cursor: null })
    }
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
        event: {
          id: 'event-1', candidate_id: original.id, sequence: 1,
          from_revision: original.revision, to_revision: original.revision + 1,
          decision: body.decision, actor: 'finance-admin', reason: 'review',
          changes: [{ field: 'status', previous_value: original.status, new_value: body.decision === 'IGNORE' ? 'IGNORED' : 'CONFIRMED' }],
          conflict_resolution: null, created_at: '2026-08-24T10:00:00+08:00',
        },
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

function mockCredentials(method: 'get' | 'create', implementation: (options?: CredentialCreationOptions | CredentialRequestOptions) => Promise<Credential | null>) {
  const credentials = { [method]: vi.fn(implementation) }
  Object.defineProperty(navigator, 'credentials', { configurable: true, value: credentials })
  return credentials[method]
}

const buffer = (...bytes: number[]) => Uint8Array.from(bytes).buffer

describe('LedgerBridge Web API client', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.history.replaceState({}, '', '/overview')
    Object.defineProperty(navigator, 'credentials', { configurable: true, value: undefined })
  })
  afterEach(() => {
    vi.restoreAllMocks()
    Object.defineProperty(navigator, 'credentials', { configurable: true, value: undefined })
  })

  it('does not load business APIs before authentication', async () => {
    const fetchMock = installFetch({ authStatus: { authenticated: false, setup_required: false, passkey_registered: true, recovery_setup_required: false, recovery_pending: false } })
    renderApp()
    expect(await screen.findByText('使用通行密钥登录')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/status', expect.anything())
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/session')).toBe(false)
  })

  it('logs in with converted WebAuthn assertion data before loading business APIs', async () => {
    const credential = {
      id: 'credential-1', rawId: buffer(3), type: 'public-key',
      response: { clientDataJSON: buffer(4), authenticatorData: buffer(5), signature: buffer(6), userHandle: null },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    const getCredential = mockCredentials('get', async () => credential)
    const fetchMock = installFetch({ authStatus: { authenticated: false, setup_required: false, passkey_registered: true, recovery_setup_required: false, recovery_pending: false } })
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: '使用通行密钥' }))
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()

    const publicKey = (getCredential.mock.calls[0][0] as CredentialRequestOptions).publicKey!
    expect(publicKey.challenge).toBeInstanceOf(ArrayBuffer)
    expect(publicKey.allowCredentials?.[0].id).toBeInstanceOf(ArrayBuffer)
    const verifyCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/passkey/login/verify')!
    expect(JSON.parse(String(verifyCall[1]?.body))).toMatchObject({
      credential: { rawId: 'Aw', response: { clientDataJSON: 'BA', authenticatorData: 'BQ', signature: 'Bg', userHandle: null } },
    })
  })

  it('shows registration recovery codes once and waits before loading business data', async () => {
    const credential = {
      id: 'credential-new', rawId: buffer(3), type: 'public-key',
      response: { clientDataJSON: buffer(4), attestationObject: buffer(5), getTransports: () => ['internal'] },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    mockCredentials('create', async () => credential)
    const fetchMock = installFetch({ authStatus: { authenticated: false, setup_required: true, passkey_registered: false, recovery_setup_required: false, recovery_pending: false } })
    renderApp()
    const setupInput = await screen.findByLabelText('首次设置码')
    expect(setupInput).toHaveAttribute('type', 'password')
    fireEvent.change(setupInput, { target: { value: 'setup-secret' } })
    fireEvent.click(screen.getByRole('button', { name: '创建通行密钥' }))

    expect(await screen.findByText('保存一次性恢复码')).toBeInTheDocument()
    expect(screen.getByText('RECOVERY-ONE')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/session')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '我已安全保存' }))
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
    expect(screen.queryByText('RECOVERY-ONE')).not.toBeInTheDocument()
  })

  it('explains a cancelled or denied passkey prompt and allows retry', async () => {
    mockCredentials('get', async () => { throw new DOMException('denied', 'NotAllowedError') })
    installFetch({ authStatus: { authenticated: false, setup_required: false, passkey_registered: true, recovery_setup_required: false, recovery_pending: false } })
    renderApp()
    const loginButton = await screen.findByRole('button', { name: '使用通行密钥' })
    fireEvent.click(loginButton)
    expect(await screen.findByText('未完成通行密钥验证。请确认系统提示后重试。')).toBeInTheDocument()
    expect(loginButton).not.toBeDisabled()
  })

  it('requires a new passkey and rotated recovery codes before recovery reaches business data', async () => {
    const credential = {
      id: 'credential-rotated', rawId: buffer(7), type: 'public-key',
      response: { clientDataJSON: buffer(8), attestationObject: buffer(9), getTransports: () => ['internal'] },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    mockCredentials('create', async () => credential)
    const fetchMock = installFetch({ authStatus: { authenticated: false, setup_required: false, passkey_registered: true, recovery_setup_required: false, recovery_pending: false }, recoverySetupRequired: true })
    renderApp()
    fireEvent.click(await screen.findByRole('button', { name: '无法使用通行密钥？使用恢复码' }))
    const recoveryInput = screen.getByLabelText('一次性恢复码')
    expect(recoveryInput).toHaveAttribute('type', 'password')
    fireEvent.change(recoveryInput, { target: { value: 'RECOVERY-SECRET' } })
    fireEvent.click(screen.getByRole('button', { name: '使用恢复码登录' }))
    expect(await screen.findByRole('heading', { name: '创建新的通行密钥' })).toBeInTheDocument()
    const recoveryCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/recovery')!
    expect(JSON.parse(String(recoveryCall[1]?.body))).toEqual({ recovery_code: 'RECOVERY-SECRET' })
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/session')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '创建新的通行密钥' }))
    expect(await screen.findByText('保存一次性恢复码')).toBeInTheDocument()
    const registerOptionCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/passkey/register/options')!
    const registerVerifyCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/passkey/register/verify')!
    expect(JSON.parse(String(registerOptionCall[1]?.body))).toEqual({ setup_code: '' })
    expect(JSON.parse(String(registerVerifyCall[1]?.body))).toMatchObject({ setup_code: '' })
    expect((registerOptionCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe('recovery-csrf-token-with-at-least-thirty-two-characters')
    expect((registerVerifyCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe('recovery-csrf-token-with-at-least-thirty-two-characters')
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/session')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '我已安全保存' }))
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
  })

  it('restores restricted recovery CSRF after a page refresh', async () => {
    const credential = {
      id: 'credential-refreshed', rawId: buffer(10), type: 'public-key',
      response: { clientDataJSON: buffer(11), attestationObject: buffer(12), getTransports: () => ['internal'] },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    mockCredentials('create', async () => credential)
    const fetchMock = installFetch({
      authStatus: {
        authenticated: false,
        setup_required: false,
        passkey_registered: true,
        recovery_setup_required: true,
        recovery_pending: true,
      },
    })
    renderApp()
    expect(await screen.findByRole('heading', { name: '创建新的通行密钥' })).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/auth/recovery/session')).toBe(true)
    const createButton = await screen.findByRole('button', { name: '创建新的通行密钥' })
    expect(createButton).not.toBeDisabled()
    fireEvent.click(createButton)
    expect(await screen.findByText('保存一次性恢复码')).toBeInTheDocument()
    const optionsCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/passkey/register/options')!
    expect((optionsCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
  })

  it('loads API projections and formats amount_minor as yuan', async () => {
    installFetch()
    renderApp()
    expect(screen.getByText('正在检查访问状态')).toBeInTheDocument()
    expect(await screen.findByText('原型环境 · 合成 API 数据')).toBeInTheDocument()
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

  it('keeps the selected section in the URL and filters the review queue', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /待审核/ })[0])
    expect(window.location.pathname).toBe('/review')

    fireEvent.change(screen.getByLabelText('搜索候选编号、门店或科目'), { target: { value: '机场店' } })
    expect(screen.getByText('机场店水费，原消息未说明归属月份')).toBeInTheDocument()
    expect(screen.queryByText('城南店 8 月布草清洗费用，供应商月结单')).not.toBeInTheDocument()
  })

  it('opens the append-only review history from the overview', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getByRole('button', { name: '查看操作记录' }))

    expect(window.location.pathname).toBe('/audit')
    expect(screen.getByRole('heading', { name: '审核操作记录' })).toBeInTheDocument()
    expect(await screen.findByText('C-49E3 · 江景店')).toBeInTheDocument()
    expect(screen.getByText('已核对电子缴款书')).toBeInTheDocument()
    expect(screen.getAllByText('待审核').length).toBeGreaterThan(0)
    expect(screen.getByText('已确认')).toBeInTheDocument()
  })

  it('loads later review-history pages through the returned cursor', async () => {
    const olderEvent: ReviewEvent = {
      ...reviewEvents[0],
      id: 'event-older',
      sequence: 2,
      reason: '较早的审核记录',
      created_at: '2026-08-20T09:00:00+08:00',
    }
    const fetchMock = installFetch({
      reviewEventPages: [
        { items: reviewEvents, next_cursor: '50' },
        { items: [olderEvent], next_cursor: null },
      ],
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getByRole('button', { name: '查看操作记录' }))
    await screen.findByText('已核对电子缴款书')
    fireEvent.click(screen.getByRole('button', { name: '加载更多记录' }))

    expect(await screen.findByText('较早的审核记录')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/review-events?cursor=50')).toBe(true)
  })

  it('loads later candidate pages for audit labels and search', async () => {
    const olderCandidate: ApiCandidate = {
      ...candidates[0],
      id: 'candidate-older',
      short_id: 'C-OLD1',
      business_unit: '海景店',
      category: '燃气费',
      summary: '仅用于审核上下文的较早候选',
    }
    const olderEvent: ReviewEvent = {
      ...reviewEvents[0],
      id: 'event-candidate-older',
      candidate_id: olderCandidate.id,
      reason: '核对较早候选',
    }
    const fetchMock = installFetch({
      candidatePages: [
        { items: candidates, next_cursor: '50' },
        { items: [olderCandidate], next_cursor: null },
      ],
      reviewEventPages: [{ items: [olderEvent], next_cursor: null }],
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getByRole('button', { name: '查看操作记录' }))

    expect(await screen.findByText('C-OLD1 · 海景店')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('搜索操作记录'), { target: { value: '燃气费' } })
    expect(screen.getByText('核对较早候选')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/candidates?cursor=50')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: '返回概览' }))
    fireEvent.click(screen.getAllByRole('button', { name: /待审核/ })[0])
    expect(screen.queryByText('仅用于审核上下文的较早候选')).not.toBeInTheDocument()
  })

  it('isolates review-history failures from the core overview', async () => {
    const fetchMock = installFetch({ failReviewEvents: true })
    renderApp()
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input]) => String(input).startsWith('/api/v1/review-events'))).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: '查看操作记录' }))
    expect(await screen.findByText('审核记录读取失败')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '返回概览' }))
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
  })

  it('resolves a conflicted candidate with an auditable resolution note', async () => {
    const fetchMock = installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /待审核/ })[0])
    fireEvent.click(screen.getByText('城南店银行收款，与另一条候选冲突'))

    const resolutionInput = await screen.findByLabelText('冲突处理依据')
    const resolveButton = screen.getByRole('button', { name: '解决冲突并确认' })
    expect(resolveButton).toBeDisabled()
    fireEvent.change(resolutionInput, { target: { value: '以银行电子回单金额为准' } })
    expect(resolveButton).not.toBeDisabled()
    fireEvent.click(resolveButton)

    expect(await screen.findByText(/C-5B17 冲突已解决/)).toBeInTheDocument()
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-3/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body))).toMatchObject({
      decision: 'RESOLVE_CONFLICT',
      expected_revision: 2,
      conflict_resolution: '以银行电子回单金额为准',
    })
  })

  it('routes blocked candidates to the required resolution flow', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    expect(screen.getByRole('button', { name: '处理冲突' })).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '补全月份' })).not.toBeDisabled()
    expect(screen.getAllByRole('button', { name: '确认' })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: '补全月份' }))
    expect(await screen.findByRole('button', { name: '保存更正并确认' })).toBeDisabled()
  })

  it('filters the queue by blocker status and keeps the counts visible', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])

    expect(screen.getByRole('button', { name: '冲突 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '缺月份 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '可确认 1' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '冲突 1' }))
    expect(screen.getByText('城南店银行收款，与另一条候选冲突')).toBeInTheDocument()
    expect(screen.queryByText('机场店水费，原消息未说明归属月份')).not.toBeInTheDocument()
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
