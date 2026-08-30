import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Theme } from '@radix-ui/themes'
import App from './App'
import type { ApiCandidate, AuthStatus, EvidencePreview, ReviewEvent } from './types'

const session = {
  principal: 'finance-admin',
  csrf_token: 'csrf-token-with-at-least-thirty-two-characters',
  expires_at: '2026-08-24T18:00:00+08:00',
  runtime_mode: 'authenticated-preview' as const,
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
    summary: '城南店 8 月布草清洗费用，供应商月结单', confidence_basis_points: 9600, evidence, blockers: [], review_risks: [],
  },
  {
    id: 'candidate-2', short_id: 'C-62D9', revision: 1, status: 'INCOMPLETE', source_channel: 'weixin',
    source_message_id: 'message-2', received_at: '2026-08-23T17:35:00+08:00', business_unit: '机场店',
    category: '水费', amount_minor: 483260, currency: 'CNY', accounting_month: null,
    summary: '机场店水费，原消息未说明归属月份', confidence_basis_points: 8800, evidence,
    blockers: [{ code: 'MISSING_ACCOUNTING_MONTH', message: '缺少归属月份' }], review_risks: [],
  },
  {
    id: 'candidate-3', short_id: 'C-5B17', revision: 2, status: 'CONFLICTED', source_channel: 'dingtalk',
    source_message_id: 'message-3', received_at: '2026-08-23T14:02:00+08:00', business_unit: '城南店',
    category: '银行收款', amount_minor: 1268000, currency: 'CNY', accounting_month: '2026-08',
    summary: '城南店银行收款，与另一条候选冲突', confidence_basis_points: 9400, evidence,
    blockers: [{ code: 'BUSINESS_KEY_CONFLICT', message: '相同凭证号金额不同' }], review_risks: [],
  },
  {
    id: 'candidate-4', short_id: 'C-49E3', revision: 4, status: 'CONFIRMED', source_channel: 'dingtalk',
    source_message_id: 'message-4', received_at: '2026-08-21T11:28:00+08:00', business_unit: '江景店',
    category: '税费', amount_minor: 924050, currency: 'CNY', accounting_month: '2026-08',
    summary: '江景店本月税费缴款', confidence_basis_points: 9800, evidence, blockers: [], review_risks: [],
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
  runtimeMode?: 'synthetic-preview' | 'authenticated-preview' | 'core-backed'
  evidencePreview?: EvidencePreview
  unlockFailure?: boolean
  unlockGate?: Promise<void>
  failCandidateRefreshAfterUnlock?: boolean
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
    runtimeMode = 'authenticated-preview',
    evidencePreview = {
      kind: 'text',
      filename: 'message.txt',
      text: '原始消息内容已直接展示',
    },
    unlockFailure = false,
    unlockGate,
    failCandidateRefreshAfterUnlock = false,
  } = options
  let shouldFailSession = failSessionOnce
  let decisionSaved = false
  const unlockedSources = new Set<string>()
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    if (url === '/api/v1/auth/status') return response(authStatus)
    if (url === '/api/v1/auth/recovery/session') return response(session)
    if (url === '/api/v1/auth/passkey/login/options') return response({
      challenge: 'AQ', timeout: 60000, rpId: 'ledgerbridge.local', userVerification: 'required',
      allowCredentials: [{ type: 'public-key', id: 'Ag' }],
    })
    if (url === '/api/v1/auth/passkey/login/verify') return response(authenticatedStatus)
    if (url === '/api/v1/auth/passkey/add/authorize/options') return response({
      challenge: 'AQ', timeout: 60000, rpId: 'ledgerbridge.local', userVerification: 'required',
      allowCredentials: [{ type: 'public-key', id: 'Ag' }],
    })
    if (url === '/api/v1/auth/passkey/add/authorize/verify') return response({
      challenge: 'Aw', rp: { name: 'LedgerBridge', id: 'ledgerbridge.local' },
      user: { id: 'BA', name: 'finance-admin', displayName: 'Finance Admin' },
      pubKeyCredParams: [{ type: 'public-key', alg: -7 }], timeout: 60000,
      excludeCredentials: [{ type: 'public-key', id: 'BQ' }],
    })
    if (url === '/api/v1/auth/passkey/add/verify') return response({ added: true, passkey_count: 2 })
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
      return response({ ...session, runtime_mode: runtimeMode })
    }
    if (url === '/api/v1/candidates' || url.startsWith('/api/v1/candidates?')) {
      if (failCandidateRefreshAfterUnlock && unlockedSources.size > 0) {
        return response({ title: '候选刷新暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
      const cursor = new URL(url, 'http://ledgerbridge.local').searchParams.get('cursor')
      const page = candidatePages[cursor ? 1 : 0] ?? { items: [], next_cursor: null }
      return response(unlockedSources.size > 0 ? {
        ...page,
        items: page.items.map((candidate) => ({
          ...candidate,
          evidence: candidate.evidence.map((item) => item.unlock_status === 'PASSWORD_REQUIRED'
            && item.source_ref
            && unlockedSources.has(item.source_ref)
            ? { ...item, unlock_status: 'UNLOCKED' as const }
            : item),
        })),
      } : page)
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
    if (url.includes('/api/v1/evidence/') && url.includes('/preview?')) return response(evidencePreview)
    if (url === '/api/v1/evidence/unlocks' && init?.method === 'POST') {
      if (unlockGate) await unlockGate
      const submitted = JSON.parse(String(init.body)) as { source_ref: string; password: string }
      if (unlockFailure) {
        return response({ title: 'Core failure', detail: submitted.password, status: 422 }, 422)
      }
      unlockedSources.add(submitted.source_ref)
      return response({ unlocked: true })
    }
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
      const candidate = candidatePages.flatMap((page) => page.items).find((item) => url.endsWith(item.id))!
      const candidateEvents = reviewEventPages.flatMap((page) => page.items).filter((event) => event.candidate_id === candidate.id)
      return response({ ...candidate, review_events: candidateEvents })
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
    const createButton = await screen.findByRole('button', { name: '创建新的通行密钥' })
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/v1/auth/recovery/session')).toBe(true)
      expect(createButton).not.toBeDisabled()
    })
    fireEvent.click(createButton)
    expect(await screen.findByText('保存一次性恢复码')).toBeInTheDocument()
    const optionsCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/v1/auth/passkey/register/options')!
    expect((optionsCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
  })

  it('adds this device after step-up without replacing existing passkeys', async () => {
    const authorization = {
      id: 'credential-existing', rawId: buffer(5), type: 'public-key',
      response: { clientDataJSON: buffer(6), authenticatorData: buffer(7), signature: buffer(8), userHandle: null },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    const registration = {
      id: 'credential-new', rawId: buffer(9), type: 'public-key',
      response: { clientDataJSON: buffer(10), attestationObject: buffer(11), getTransports: () => ['internal'] },
      getClientExtensionResults: () => ({}),
    } as unknown as Credential
    const getCredential = vi.fn(async () => authorization)
    const createCredential = vi.fn(async () => registration)
    Object.defineProperty(navigator, 'credentials', {
      configurable: true,
      value: { get: getCredential, create: createCredential },
    })
    const fetchMock = installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    const accountButton = screen.getByRole('button', { name: /财务管理员/ })
    fireEvent.pointerDown(accountButton, { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: /添加这台设备/ }))
    expect(await screen.findByRole('heading', { name: '添加这台设备的通行密钥' })).toBeInTheDocument()
    expect(screen.getByText(/其他设备的密钥不会被撤销/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '开始登记' }))

    expect(await screen.findByText('这台设备已登记，当前共有 2 个通行密钥可登录。')).toBeInTheDocument()
    expect(getCredential).toHaveBeenCalledTimes(1)
    expect(createCredential).toHaveBeenCalledTimes(1)
    const addCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/api/v1/auth/passkey/add/'))
    expect(addCalls.map(([input]) => String(input))).toEqual([
      '/api/v1/auth/passkey/add/authorize/options',
      '/api/v1/auth/passkey/add/authorize/verify',
      '/api/v1/auth/passkey/add/verify',
    ])
    for (const [, init] of addCalls) {
      expect((init?.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
    }
  })

  it('loads API projections and formats amount_minor as yuan', async () => {
    installFetch()
    renderApp()
    expect(screen.getByText('正在检查访问状态')).toBeInTheDocument()
    expect(await screen.findByText('演示环境 · 登录已启用 · 合成业务数据')).toBeInTheDocument()
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

  it('posts a confirmed decision when the browser lacks crypto.randomUUID', async () => {
    const originalCrypto = globalThis.crypto
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        getRandomValues: originalCrypto.getRandomValues.bind(originalCrypto),
      },
    })
    try {
      const fetchMock = installFetch()
      renderApp()
      await screen.findByText('早上好，今天有几项需要确认')
      fireEvent.click(screen.getAllByText('待审核')[0])
      fireEvent.click(screen.getAllByRole('button', { name: '确认' })[0])

      expect(await screen.findByText(/C-8F21 已确认/)).toBeInTheDocument()
      const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
      expect(decisionCall).toBeDefined()
      expect((decisionCall?.[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      )
    } finally {
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: originalCrypto })
    }
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

  it('labels Core-backed sessions as formal Core data instead of synthetic data', async () => {
    installFetch({ runtimeMode: 'core-backed' })
    renderApp()
    expect(await screen.findByText('正式环境 · Core 实时业务数据')).toBeInTheDocument()
    expect(screen.getByText('Core 是唯一业务事实源')).toBeInTheDocument()
    expect(screen.queryByText('无真实财务数据')).not.toBeInTheDocument()
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

    fireEvent.click(screen.getByRole('button', { name: '查看候选与证据' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: '查看已确认候选' })).toBeInTheDocument()
    expect(await within(dialog).findByText('原始消息内容已直接展示')).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: '下载原文件：消息原文' })).toBeInTheDocument()
    expect(await within(dialog).findByText('已核对电子缴款书')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('营业单元')).toHaveAttribute('readonly')
    expect(within(dialog).queryByRole('button', { name: '忽略候选' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: '保存更正并确认' })).not.toBeInTheDocument()
  })

  it('renders spreadsheet evidence inline instead of presenting attachment chips', async () => {
    const workbookCandidate: ApiCandidate = {
      ...candidates[0],
      short_id: 'C-0139',
      source_channel: 'outlook',
      summary: '中行邮箱账单待复核：TX-0139',
      evidence: [
        {
          id: 'evidence-combined-workbook',
          kind: 'attachment',
          media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          sha256: 'a'.repeat(64),
          original_filename: 'combined-review.xlsx',
        },
        {
          id: 'evidence-workbook',
          kind: 'attachment',
          media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          sha256: 'b'.repeat(64),
          original_filename: 'boc-manual-review.xlsx',
        },
      ],
    }
    installFetch({
      items: [workbookCandidate],
      evidencePreview: {
        kind: 'spreadsheet',
        filename: 'boc-manual-review.xlsx',
        reference: 'TX-0139',
        matched: true,
        records: [{
          sheet: '26.5中行邮箱待复核',
          row_number: 26,
          header_row_number: 4,
          fields: [
            { label: '清单ID', value: 'TX-0139' },
            { label: '交易时间', value: '2026-05-18 09:30' },
            { label: '金额(元)', value: '¥80,000.00' },
            { label: '对方名称', value: '陈明哲' },
            { label: '自动分类', value: '内部往来' },
            { label: '预处理说明', value: '模型推断结果' },
          ],
        }],
        fallback: null,
      },
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    fireEvent.click(await screen.findByText('中行邮箱账单待复核：TX-0139'))

    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('账单 TX-0139')).toBeInTheDocument()
    expect(within(dialog).getByText('2026-05-18 09:30')).toBeInTheDocument()
    expect(within(dialog).getByText('¥80,000.00')).toBeInTheDocument()
    expect(within(dialog).getByText('陈明哲')).toBeInTheDocument()
    expect(within(dialog).queryByText('内部往来')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('模型推断结果')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('第 26 行')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('1 个附件')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: '下载原文件：boc-manual-review.xlsx' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('link', { name: '下载原文件：combined-review.xlsx' })).not.toBeInTheDocument()
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
    expect(screen.getByText('仅用于审核上下文的较早候选')).toBeInTheDocument()
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

  it('bulk-confirms only high-confidence candidates without Core risk flags', async () => {
    const hotelRiskCandidate: ApiCandidate = {
      ...candidates[0],
      id: 'candidate-risk',
      short_id: 'C-RISK1',
      summary: '酒店平台结算待关联银行流水',
      confidence_basis_points: 9900,
      review_risks: [{
        code: 'HOTEL_PAYOUT_STATEMENT_REQUIRED',
        message: '酒店平台结算或提现需关联收款银行流水，未匹配前保留人工审核',
      }],
    }
    const fundingRiskCandidate: ApiCandidate = {
      ...candidates[0],
      id: 'candidate-funding-risk',
      short_id: 'C-RISK2',
      summary: '微信消费使用银行卡支付，待关联银行流水',
      confidence_basis_points: 9900,
      review_risks: [{
        code: 'FUNDING_STATEMENT_REQUIRED',
        message: '平台交易使用银行或信用账户支付，需关联资金账户明细后再确认',
      }],
    }
    const fetchMock = installFetch({ items: [candidates[0], hotelRiskCandidate, fundingRiskCandidate] })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])

    expect(screen.getByText('2 条需补关联单据')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '一键审批 1 条' }))
    expect(await screen.findByText('已确认 1 条安全候选；风险项仍保留人工审核')).toBeInTheDocument()

    const decisionCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/decisions'))
    expect(decisionCalls).toHaveLength(1)
    expect(String(decisionCalls[0][0])).toContain('/candidate-1/decisions')
    expect(String(decisionCalls[0][0])).not.toContain('/candidate-risk/decisions')
    expect(String(decisionCalls[0][0])).not.toContain('/candidate-funding-risk/decisions')
  })

  it('filters the queue by blocker status and keeps the counts visible', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])

    expect(screen.getByRole('button', { name: '冲突 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '风险审核 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '可一键审批 1' })).toBeInTheDocument()
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

  it('shows a deduplicated evidence library and opens an associated candidate', async () => {
    const sharedEvidence = {
      id: 'evidence-may-bank',
      kind: 'attachment' as const,
      media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      sha256: 'c'.repeat(64),
      original_filename: 'may-bank-statement.xlsx',
    }
    const importedCandidates: ApiCandidate[] = [
      { ...candidates[0], id: 'candidate-evidence-pending', short_id: 'C-EV01', source_channel: 'outlook', accounting_month: '2026-05', summary: '银行流水待复核：TX-1001', evidence: [sharedEvidence] },
      { ...candidates[3], id: 'candidate-evidence-confirmed', short_id: 'C-EV02', source_channel: 'outlook', accounting_month: '2026-05', summary: '银行流水已确认：TX-1002', evidence: [{ ...sharedEvidence, id: 'evidence-may-bank-copy' }] },
    ]
    installFetch({ items: importedCandidates })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])
    expect(screen.getAllByText('may-bank-statement.xlsx')).toHaveLength(1)
    expect(screen.getByText('2026 年 5 月')).toBeInTheDocument()
    expect(screen.getByText('关联 2 条候选')).toBeInTheDocument()
    expect(screen.getByText('含待审核')).toBeInTheDocument()
    fireEvent.click(screen.getByText('关联 2 条候选'))
    fireEvent.click(screen.getByRole('button', { name: /C-EV01/ }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByText('原始消息内容已直接展示')).toBeInTheDocument()
  })

  it('shows a password-only dialog only for locked evidence, prevents duplicate submit, and refreshes after success', async () => {
    const sourceRef = '21000000-0000-4000-8000-000000000001'
    const secondSourceRef = '21000000-0000-4000-8000-000000000004'
    const lockedEvidence = {
      id: '21000000-0000-4000-8000-000000000002',
      kind: 'attachment' as const,
      media_type: 'application/zip',
      sha256: 'd'.repeat(64),
      original_filename: 'encrypted-statement.zip',
      unlock_status: 'PASSWORD_REQUIRED' as const,
      source_ref: sourceRef,
    }
    const secondLockedEvidence = {
      ...lockedEvidence,
      id: '21000000-0000-4000-8000-000000000005',
      original_filename: 'forwarded-encrypted-statement.zip',
      source_ref: secondSourceRef,
    }
    const ordinaryEvidence = {
      ...lockedEvidence,
      id: '21000000-0000-4000-8000-000000000003',
      original_filename: 'ordinary-statement.xlsx',
      unlock_status: 'NOT_REQUIRED' as const,
      source_ref: null,
    }
    let releaseUnlock!: () => void
    const unlockGate = new Promise<void>((resolve) => { releaseUnlock = resolve })
    const fetchMock = installFetch({
      items: [
        { ...candidates[0], id: 'candidate-locked-evidence', short_id: 'C-LK01', evidence: [lockedEvidence] },
        { ...candidates[2], id: 'candidate-second-locked-evidence', short_id: 'C-LK02', evidence: [secondLockedEvidence] },
        { ...candidates[1], id: 'candidate-ordinary-evidence', short_id: 'C-LK02', evidence: [ordinaryEvidence] },
      ],
      unlockGate,
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])

    const unlockButton = screen.getAllByRole('button', { name: '输入解压密码' })[0]
    expect(screen.getAllByRole('button', { name: '输入解压密码' })).toHaveLength(2)
    expect(screen.getByText('ordinary-statement.xlsx')).toBeInTheDocument()
    fireEvent.click(unlockButton)
    const passwordInput = screen.getByLabelText('解压密码')
    expect(passwordInput).toHaveAttribute('type', 'password')
    fireEvent.change(passwordInput, { target: { value: 'ephemeral-test-password' } })
    const submitButton = screen.getByRole('button', { name: '解锁账单' })
    fireEvent.click(submitButton)
    fireEvent.click(submitButton)

    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')).toHaveLength(1))
    releaseUnlock()
    expect(await screen.findByText('账单已解锁，数据已刷新')).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByRole('button', { name: '输入解压密码' })).toHaveLength(1))
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/candidates').length).toBeGreaterThanOrEqual(2)

    const unlockCall = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')[0]
    expect(unlockCall[1]?.credentials).toBe('same-origin')
    expect((unlockCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
    expect((unlockCall[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse(String(unlockCall[1]?.body))).toEqual({ source_ref: sourceRef, password: 'ephemeral-test-password' })
    expect(document.body).not.toHaveTextContent('ephemeral-test-password')

    fireEvent.click(screen.getByRole('button', { name: '输入解压密码' }))
    fireEvent.change(screen.getByLabelText('解压密码'), { target: { value: 'second-ephemeral-password' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁账单' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')).toHaveLength(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: '输入解压密码' })).not.toBeInTheDocument())
    const secondUnlockCall = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')[1]
    expect(JSON.parse(String(secondUnlockCall[1]?.body))).toEqual({ source_ref: secondSourceRef, password: 'second-ephemeral-password' })
    expect(document.body).not.toHaveTextContent('second-ephemeral-password')
  })

  it('clears the password and hides Core failure text after an unlock failure', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const lockedEvidence = {
      id: '22000000-0000-4000-8000-000000000002',
      kind: 'attachment' as const,
      media_type: 'application/zip',
      sha256: 'f'.repeat(64),
      original_filename: 'locked.zip',
      unlock_status: 'PASSWORD_REQUIRED' as const,
      source_ref: '22000000-0000-4000-8000-000000000001',
    }
    installFetch({
      items: [{ ...candidates[0], id: 'candidate-failed-unlock', short_id: 'C-LK03', evidence: [lockedEvidence] }],
      unlockFailure: true,
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])
    fireEvent.click(screen.getByRole('button', { name: '输入解压密码' }))
    const passwordInput = screen.getByLabelText('解压密码')
    fireEvent.change(passwordInput, { target: { value: 'must-not-leak' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁账单' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('账单解锁失败，请检查密码后重试')
    expect(passwordInput).toHaveValue('')
    expect(document.body).not.toHaveTextContent('must-not-leak')
    expect(JSON.stringify(consoleError.mock.calls)).not.toContain('must-not-leak')
  })

  it('reports an accepted unlock separately when the candidate list refresh fails', async () => {
    const lockedEvidence = {
      id: '22500000-0000-4000-8000-000000000002',
      kind: 'attachment' as const,
      media_type: 'application/zip',
      sha256: '1'.repeat(64),
      original_filename: 'refresh-failure.zip',
      unlock_status: 'PASSWORD_REQUIRED' as const,
      source_ref: '22500000-0000-4000-8000-000000000001',
    }
    installFetch({
      items: [{ ...candidates[0], id: 'candidate-refresh-failure', short_id: 'C-LK04', evidence: [lockedEvidence] }],
      failCandidateRefreshAfterUnlock: true,
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])
    fireEvent.click(screen.getByRole('button', { name: '输入解压密码' }))
    fireEvent.change(screen.getByLabelText('解压密码'), { target: { value: 'accepted-password' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁账单' }))

    expect(await screen.findByText('已解锁，但列表刷新失败，请重试刷新')).toBeInTheDocument()
    expect(screen.queryByText('账单已解锁，数据已刷新')).not.toBeInTheDocument()
    expect(screen.queryByText('账单解锁失败，请检查密码后重试')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('解压密码')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('accepted-password')
  })

  it('aggregates only Core material risks and excludes platform internal accounts', async () => {
    const materialCandidates: ApiCandidate[] = [
      { ...candidates[0], id: 'candidate-ccb-gap', short_id: 'C-GAP1', accounting_month: '2026-05', summary: '微信 | 2026-05-02 | 支出 | 商户消费 | 通信商 | 中国建设银行储蓄卡(7564) | 支付成功', review_risks: [{ code: 'FUNDING_STATEMENT_REQUIRED', message: '需关联资金账户明细后再确认' }] },
      ...[1, 2].map((index): ApiCandidate => ({ ...candidates[0], id: `candidate-related-gap-${index}`, short_id: `C-GAP${index + 1}`, accounting_month: '2026-05', summary: `支付宝 | 2026-05-0${index + 3} | 支出 | 投资理财 | 网商银行 | 账户余额 | 交易成功`, review_risks: [{ code: 'RELATED_ACCOUNT_STATEMENT_REQUIRED', message: '需提交并关联另一侧账户同期流水' }] })),
      { ...candidates[0], id: 'candidate-huabei-internal', short_id: 'C-HB01', accounting_month: '2026-05', summary: '支付宝 | 2026-05-08 | 支出 | 商户消费 | 便利店 | 花呗 | 交易成功', review_risks: [{ code: 'FUNDING_STATEMENT_REQUIRED', message: '需关联资金账户明细后再确认' }] },
      { ...candidates[0], id: 'candidate-hotel-gap', short_id: 'C-HOTEL', accounting_month: '2026-05', summary: 'OCR账单待复核: CTRIP_EBOOKING 2026-05-25:2026-05-31', review_risks: [{ code: 'HOTEL_PAYOUT_STATEMENT_REQUIRED', message: '需关联收款银行流水' }] },
    ]
    installFetch({ items: materialCandidates })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])
    expect(screen.getByText('中国建设银行储蓄卡(7564)明细')).toBeInTheDocument()
    expect(screen.getByText('网商银行同期流水')).toBeInTheDocument()
    expect(screen.getByText('影响 2 条记录')).toBeInTheDocument()
    expect(screen.getByText('酒店平台收款银行流水')).toBeInTheDocument()
    expect(screen.queryByText('花呗明细')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByText('待审核')[0])
    expect(screen.getByText('4 条需补关联单据')).toBeInTheDocument()
  })

  it('groups ordinary transfers by counterparty and filters to the selected object', async () => {
    const transferCandidates: ApiCandidate[] = [
      ...[-10000, 4000].map((amount, index): ApiCandidate => ({ ...candidates[0], id: `candidate-wangshang-${index}`, short_id: `C-WS0${index + 1}`, amount_minor: amount, accounting_month: '2026-05', summary: `支付宝 | 2026-05-0${index + 8} | ${amount < 0 ? '支出' : '收入'} | 投资理财 | 网商银行 | 账户余额 | 交易成功`, review_risks: [{ code: 'RELATED_ACCOUNT_STATEMENT_REQUIRED', message: '需关联另一侧账户同期流水' }] })),
      { ...candidates[0], id: 'candidate-known-transfer', short_id: 'C-ZS01', amount_minor: -2000, summary: '微信 | 2026-05-12 | 支出 | 转账 | 张三 | / | 对方已收钱', review_risks: [{ code: 'TRANSFER_REVIEW_REQUIRED', message: '需人工确认收付款方及资金性质' }] },
      { ...candidates[0], id: 'candidate-unknown-transfer', short_id: 'C-UNK1', amount_minor: -3000, summary: '微信 | 2026-05-13 | 支出 | 转账 |  | / | 对方已收钱', review_risks: [{ code: 'TRANSFER_REVIEW_REQUIRED', message: '需人工确认收付款方及资金性质' }] },
      { ...candidates[0], id: 'candidate-internal-wallet', short_id: 'C-WAL1', amount_minor: -5000, summary: '支付宝 | 2026-05-14 | 支出 | 余额互转 | 余额宝 | 账户余额 | 交易成功', review_risks: [{ code: 'RELATED_ACCOUNT_STATEMENT_REQUIRED', message: '需关联另一侧账户同期流水' }] },
    ]
    installFetch({ items: transferCandidates })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    const wangshangGroup = screen.getByRole('button', { name: '查看网商银行 2 笔' })
    expect(wangshangGroup).toBeInTheDocument()
    expect(within(wangshangGroup).getByText('身份待分类')).toBeInTheDocument()
    expect(within(wangshangGroup).getByText(/关系待确认/)).toBeInTheDocument()
    expect(screen.getByText('净额 -¥60.00')).toBeInTheDocument()
    const zhangsanGroup = screen.getByRole('button', { name: '查看张三 1 笔' })
    expect(within(zhangsanGroup).getByText('身份待分类')).toBeInTheDocument()
    expect(within(zhangsanGroup).getByText(/关系待确认/)).toBeInTheDocument()
    const unknownGroup = screen.getByRole('button', { name: '查看未识别对象 1 笔' })
    expect(within(unknownGroup).getByText('身份待分类')).toBeInTheDocument()
    expect(screen.queryByText('已知业务对象')).not.toBeInTheDocument()
    expect(screen.queryByText('本人内部/关联方')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^查看余额宝/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看网商银行 2 笔' }))
    expect(screen.getByText('C-WS01')).toBeInTheDocument()
    expect(screen.getByText('C-WS02')).toBeInTheDocument()
    expect(screen.queryByText('C-ZS01')).not.toBeInTheDocument()
  })

  it('reaches the three business reporting entry points', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])
    expect(screen.getByRole('heading', { name: '完整个人财务对账' })).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /原口径对账表/ })[0])
    expect(screen.getByRole('heading', { name: '2026 年 8 月对账草稿' })).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /各公司报表/ })[0])
    expect(screen.getByRole('heading', { name: '各公司报表' })).toBeInTheDocument()
    expect(screen.getByText('按公司主体汇总将在后续接入')).toBeInTheDocument()
  })

  it('keeps the payroll integration status reachable from its route and both navigation surfaces', async () => {
    window.history.replaceState({}, '', '/payroll')
    installFetch({ runtimeMode: 'core-backed' })
    renderApp()

    expect(await screen.findByRole('heading', { name: '工资与发放验证' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '工资与发放验证' })).toHaveLength(2)
    expect(screen.getByRole('heading', { name: '当前可做' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '暂不可做' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '接通条件' })).toBeInTheDocument()
    expect(screen.getByText('只读工资发布契约已部署')).toBeInTheDocument()
    expect(screen.getByText('正式工资服务尚未连接')).toBeInTheDocument()
    expect(screen.getByText('真实发薪和银行提交不可用')).toBeInTheDocument()
    expect(screen.queryByText(/127\.0\.0\.1/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|银行提交/ })).not.toBeInTheDocument()
  })
})
