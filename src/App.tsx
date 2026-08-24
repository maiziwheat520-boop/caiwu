import { useCallback, useEffect, useState } from 'react'
import {
  Badge,
  Button,
  Dialog,
  DropdownMenu,
  Select,
  Separator,
  TextField,
} from '@radix-ui/themes'
import {
  ArrowsClockwise,
  Bank,
  CaretRight,
  Check,
  CheckCircle,
  CloudArrowUp,
  Copy,
  Database,
  FileText,
  Fingerprint,
  FolderOpen,
  House,
  Info,
  ListChecks,
  MagnifyingGlass,
  Paperclip,
  ShieldCheck,
  SignOut,
  SlidersHorizontal,
  Table,
  Warning,
  X,
} from '@phosphor-icons/react'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { api, majorToMinor, minorToMajor } from './api'
import type {
  ApiCandidate,
  AuthResult,
  AuthStatus,
  Candidate,
  CandidateCorrections,
  ConnectionStatus,
  Notice,
  Page,
  Reconciliation as ReconciliationData,
  Session,
} from './types'

const CURRENT_MONTH = '2026-08'

const navigation: Array<{ id: Page; label: string; icon: typeof House }> = [
  { id: 'overview', label: '概览', icon: House },
  { id: 'review', label: '待审核', icon: ListChecks },
  { id: 'reconciliation', label: '月度对账', icon: Table },
  { id: 'files', label: '文件与连接', icon: FolderOpen },
]

const currency = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})

const categoryTone: Record<string, 'blue' | 'green' | 'amber' | 'purple' | 'gray'> = {
  布草: 'purple',
  瓶装水: 'blue',
  水费: 'green',
  银行收款: 'amber',
  税费: 'gray',
}

const sourceLabels: Record<ApiCandidate['source_channel'], Candidate['source']> = {
  telegram: 'Telegram',
  dingtalk: '钉钉',
  weixin: '微信',
}

function toCandidate(candidate: ApiCandidate): Candidate {
  const blockerCodes = new Set(candidate.blockers.map((blocker) => blocker.code))
  return {
    id: candidate.id,
    shortId: candidate.short_id,
    revision: candidate.revision,
    source: sourceLabels[candidate.source_channel],
    sourceChannel: candidate.source_channel,
    receivedAt: new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(candidate.received_at)),
    businessUnit: candidate.business_unit,
    category: candidate.category,
    amount: minorToMajor(candidate.amount_minor),
    amountMinor: candidate.amount_minor,
    accountingMonth: candidate.accounting_month,
    summary: candidate.summary,
    evidence: candidate.evidence,
    confidence: candidate.confidence_basis_points / 10000,
    status: candidate.status,
    blockers: candidate.blockers,
    incomplete: candidate.status === 'INCOMPLETE' || blockerCodes.has('MISSING_ACCOUNTING_MONTH'),
    conflict: candidate.status === 'CONFLICTED' || blockerCodes.has('BUSINESS_KEY_CONFLICT') || blockerCodes.has('DUPLICATE_MESSAGE') || blockerCodes.has('DUPLICATE_ATTACHMENT'),
    raw: candidate,
  }
}

function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [authError, setAuthError] = useState<string | null>(null)
  const [page, setPage] = useState<Page>('overview')
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [reconciliation, setReconciliation] = useState<ReconciliationData | null>(null)
  const [selectedMonth, setSelectedMonth] = useState(CURRENT_MONTH)
  const [connections, setConnections] = useState<ConnectionStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [decisionBusyId, setDecisionBusyId] = useState<string | null>(null)
  const [draftBusy, setDraftBusy] = useState(false)
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  const loadAuthStatus = useCallback(async () => {
    setAuthLoading(true)
    setAuthError(null)
    try {
      setAuthStatus(await api.getAuthStatus())
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : '无法读取认证状态')
    } finally {
      setAuthLoading(false)
    }
  }, [])

  useEffect(() => {
    const authTimer = window.setTimeout(() => void loadAuthStatus(), 0)
    return () => window.clearTimeout(authTimer)
  }, [loadAuthStatus])

  const loadData = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [sessionData, candidateData, reconciliationData, connectionData] = await Promise.all([
        api.getSession(),
        api.listCandidates(),
        api.getReconciliation(selectedMonth),
        api.listConnections(),
      ])
      setSession(sessionData)
      setCandidates(candidateData.items.map(toCandidate))
      setReconciliation(reconciliationData)
      setConnections(connectionData)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '无法读取财务数据')
    } finally {
      setLoading(false)
    }
  }, [selectedMonth])

  useEffect(() => {
    if (!authStatus?.authenticated || authStatus.recovery_setup_required) return
    const loadTimer = window.setTimeout(() => void loadData(), 0)
    return () => window.clearTimeout(loadTimer)
  }, [authStatus?.authenticated, authStatus?.recovery_setup_required, loadData])

  const changeMonth = (month: string) => setSelectedMonth(month)

  const pendingCandidates = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const confirmedCandidates = candidates.filter((candidate) => candidate.status === 'CONFIRMED')

  const updateCandidate = async (candidate: Candidate, status: 'CONFIRMED' | 'IGNORED', corrections?: CandidateCorrections) => {
    if (!session) {
      setNotice({ tone: 'error', message: '会话尚未就绪，请刷新后重试' })
      return
    }
    if (status === 'CONFIRMED' && (candidate.conflict || (candidate.incomplete && !corrections?.accounting_month))) {
      setNotice({ tone: 'error', message: `${candidate.shortId} 仍有阻断项，不能确认` })
      return
    }
    setDecisionBusyId(candidate.id)
    try {
      const result = await api.appendDecision({
        candidate: candidate.raw,
        decision: status === 'IGNORED' ? 'IGNORE' : corrections ? 'CORRECT_AND_CONFIRM' : 'CONFIRM',
        reason: status === 'IGNORED' ? 'Web 审核：忽略候选' : corrections ? 'Web 审核：更正并确认' : 'Web 审核：确认候选',
        corrections,
        csrfToken: session.csrf_token,
      })
      const updated = toCandidate(result.candidate)
      setCandidates((items) => items.map((item) => (item.id === updated.id ? updated : item)))
      setSelectedCandidate(null)
      try {
        const refreshedReconciliation = await api.getReconciliation(selectedMonth)
        setReconciliation(refreshedReconciliation)
        setNotice({
          tone: 'success',
          message: status === 'CONFIRMED' ? `${candidate.shortId} 已确认并进入本月草稿数据` : `${candidate.shortId} 已忽略，原始证据仍保留`,
        })
      } catch {
        setNotice({ tone: 'info', message: `${candidate.shortId} 决定已保存，对账状态需刷新` })
      }
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '提交审核决定失败，请重试' })
    } finally {
      setDecisionBusyId(null)
    }
  }

  const openCandidate = async (candidate: Candidate) => {
    setSelectedCandidate(candidate)
    try {
      const detail = await api.getCandidate(candidate.id)
      setSelectedCandidate((current) => current?.id === candidate.id ? toCandidate(detail) : current)
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? `证据详情读取失败：${error.message}` : '证据详情读取失败' })
    }
  }

  const generateDraft = async () => {
    if (!session || !reconciliation || !reconciliation.ready || reconciliation.blockers.length > 0) {
      setNotice({ tone: 'error', message: '当前对账仍有阻断项，不能生成草稿' })
      return
    }
    setDraftBusy(true)
    try {
      const draft = await api.createWorkbookDraft({
        accountingMonth: reconciliation.accounting_month,
        expectedRevision: reconciliation.revision,
        csrfToken: session.csrf_token,
      })
      setNotice({ tone: 'success', message: `对账草稿已进入${draft.status === 'QUEUED' ? '生成队列' : '处理流程'}` })
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '草稿生成请求失败' })
    } finally {
      setDraftBusy(false)
    }
  }

  const completeAuthentication = (result: AuthResult) => {
    setAuthStatus({
      authenticated: result.authenticated,
      setup_required: result.setup_required,
      passkey_registered: result.passkey_registered,
      recovery_setup_required: result.recovery_setup_required,
      recovery_pending: result.recovery_pending,
      principal: result.principal,
    })
    setAuthError(null)
    setLoading(true)
  }

  const logout = async () => {
    if (!session) {
      setNotice({ tone: 'error', message: '会话信息尚未就绪，无法安全退出' })
      return
    }
    setLogoutBusy(true)
    try {
      await api.logout(session.csrf_token)
      setAuthStatus({ authenticated: false, setup_required: false, passkey_registered: true, recovery_setup_required: false, recovery_pending: false })
      setSession(null)
      setCandidates([])
      setReconciliation(null)
      setConnections([])
      setPage('overview')
      setLoading(true)
      setNotice(null)
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '退出失败，请重试' })
    } finally {
      setLogoutBusy(false)
    }
  }

  const renderPage = () => {
    if (page === 'overview') {
      return (
        <Overview
          pending={pendingCandidates}
          confirmed={confirmedCandidates}
          onNavigate={setPage}
          onOpenCandidate={openCandidate}
        />
      )
    }
    if (page === 'review') {
      return (
        <ReviewQueue
          candidates={pendingCandidates}
          onOpenCandidate={openCandidate}
          onUpdate={updateCandidate}
          onRefresh={loadData}
          busyId={decisionBusyId}
        />
      )
    }
    if (page === 'reconciliation') {
      return <Reconciliation data={reconciliation} confirmed={confirmedCandidates} selectedMonth={selectedMonth} onMonthChange={changeMonth} onGenerate={generateDraft} generating={draftBusy} onNavigate={setPage} />
    }
    return <FilesAndConnections connections={connections} onRefresh={loadData} />
  }

  if (authLoading) return <AuthFrame><LoadingState title="正在检查访问状态" description="正在确认此设备的单用户会话。" /></AuthFrame>
  if (authError) return <AuthFrame><ErrorState message={authError} onRetry={loadAuthStatus} /></AuthFrame>
  if (!authStatus?.authenticated || authStatus.recovery_setup_required) {
    return <AuthScreen status={authStatus} onAuthenticated={completeAuthentication} onRecoveryCancelled={loadAuthStatus} />
  }

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <Brand />
        <nav className="side-nav">
          {navigation.map((item) => {
            const Icon = item.icon
            return (
              <button
                className={`nav-item ${page === item.id ? 'active' : ''}`}
                aria-current={page === item.id ? 'page' : undefined}
                key={item.id}
                onClick={() => setPage(item.id)}
                type="button"
              >
                <Icon size={19} weight={page === item.id ? 'fill' : 'regular'} />
                <span>{item.label}</span>
                {item.id === 'review' && pendingCandidates.length > 0 ? (
                  <span className="nav-count">{pendingCandidates.length}</span>
                ) : null}
              </button>
            )
          })}
        </nav>
        <div className="sidebar-foot">
          <div className="secure-line">
            <ShieldCheck size={17} weight="fill" />
            <span>合成预览环境</span>
          </div>
          <span>无真实财务数据</span>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="mobile-brand"><Brand compact /></div>
          <div className="prototype-flag">
            <span className="flag-dot" />
            原型环境 · 合成 API 数据
          </div>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger>
              <Button variant="soft" color="gray" size="2">
                <span className="avatar">W</span>
                <span className="account-label">财务管理员</span>
              </Button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item>通行密钥设置</DropdownMenu.Item>
              <DropdownMenu.Item>操作记录</DropdownMenu.Item>
              <DropdownMenu.Separator />
              <DropdownMenu.Item color="red" disabled={logoutBusy} onSelect={() => void logout()}><SignOut size={15} />{logoutBusy ? '正在退出' : '安全退出'}</DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        </header>

        {notice ? (
          <div className={`notice ${notice.tone}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
            {notice.tone === 'error' ? <Warning size={18} weight="fill" /> : notice.tone === 'info' ? <Info size={18} weight="fill" /> : <CheckCircle size={18} weight="fill" />}
            <span>{notice.message}</span>
            <button aria-label="关闭提示" onClick={() => setNotice(null)} type="button"><X size={16} /></button>
          </div>
        ) : null}

        <main className="content">
          {loading ? <LoadingState /> : loadError ? <ErrorState message={loadError} onRetry={loadData} /> : renderPage()}
        </main>
      </div>

      <nav className="bottom-nav" aria-label="移动端主导航">
        {navigation.map((item) => {
          const Icon = item.icon
          return (
            <button
              className={page === item.id ? 'active' : ''}
              aria-current={page === item.id ? 'page' : undefined}
              key={item.id}
              onClick={() => setPage(item.id)}
              type="button"
            >
              <span className="bottom-icon-wrap">
                <Icon size={21} weight={page === item.id ? 'fill' : 'regular'} />
                {item.id === 'review' && pendingCandidates.length > 0 ? <i>{pendingCandidates.length}</i> : null}
              </span>
              {item.label}
            </button>
          )
        })}
      </nav>

      {selectedCandidate ? (
        <CandidateDialog
          key={`${selectedCandidate.id}:${selectedCandidate.revision}`}
          candidate={selectedCandidate}
          onClose={() => setSelectedCandidate(null)}
          onUpdate={updateCandidate}
          busy={selectedCandidate.id === decisionBusyId}
        />
      ) : null}
    </div>
  )
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`brand ${compact ? 'compact' : ''}`}>
      <div className="brand-mark"><Bank size={21} weight="fill" /></div>
      <div>
        <strong>LedgerBridge</strong>
        {!compact ? <span>财务工作台</span> : null}
      </div>
    </div>
  )
}

function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action ? <div className="page-action">{action}</div> : null}
    </div>
  )
}

function AuthFrame({ children }: { children: React.ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-brand"><Brand /></div>
      {children}
      <p className="auth-footnote"><ShieldCheck size={15} />认证凭据由当前设备与同源服务完成验证</p>
    </main>
  )
}

function authErrorMessage(error: unknown) {
  if (error instanceof DOMException && error.name === 'AbortError') return '通行密钥操作已取消，可以重新尝试。'
  if (error instanceof DOMException && error.name === 'NotAllowedError') return '未完成通行密钥验证。请确认系统提示后重试。'
  return error instanceof Error ? error.message : '认证失败，请重试。'
}

function AuthScreen({ status, onAuthenticated, onRecoveryCancelled }: {
  status: AuthStatus | null
  onAuthenticated: (result: AuthResult) => void
  onRecoveryCancelled: () => Promise<void>
}) {
  const [setupCode, setSetupCode] = useState('')
  const [recoveryCode, setRecoveryCode] = useState('')
  const [showRecovery, setShowRecovery] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([])
  const [registrationResult, setRegistrationResult] = useState<AuthResult | null>(null)
  const [recoverySetupResult, setRecoverySetupResult] = useState<AuthResult | null>(null)
  const [copyStatus, setCopyStatus] = useState<string | null>(null)

  useEffect(() => {
    if (!status?.recovery_setup_required || recoverySetupResult?.csrf_token) return
    let active = true
    void api.getRecoverySession().then((sessionData) => {
      if (!active) return
      setRecoverySetupResult({ ...status, csrf_token: sessionData.csrf_token, expires_at: sessionData.expires_at })
    }).catch((sessionError) => {
      if (active) setError(authErrorMessage(sessionError))
    })
    return () => { active = false }
  }, [recoverySetupResult?.csrf_token, status])

  const register = async (event: React.FormEvent) => {
    event.preventDefault()
    const firstSetup = Boolean(status?.setup_required)
    if (firstSetup && !setupCode) return
    if (!firstSetup && !recoverySetupResult?.csrf_token) {
      setError('恢复会话尚未就绪，请稍后重试。')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await api.registerPasskey(
        firstSetup ? setupCode : '',
        firstSetup ? undefined : recoverySetupResult?.csrf_token,
      )
      setSetupCode('')
      if (result.recovery_codes?.length) {
        setRecoveryCodes(result.recovery_codes)
        setRegistrationResult(result)
      } else {
        onAuthenticated(result)
      }
    } catch (authError) {
      setError(authErrorMessage(authError))
    } finally {
      setBusy(false)
    }
  }

  const login = async () => {
    setBusy(true)
    setError(null)
    try {
      onAuthenticated(await api.loginWithPasskey())
    } catch (authError) {
      setError(authErrorMessage(authError))
    } finally {
      setBusy(false)
    }
  }

  const recover = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!recoveryCode) return
    setBusy(true)
    setError(null)
    try {
      const code = recoveryCode
      setRecoveryCode('')
      const result = await api.recoverSession(code)
      if (result.recovery_setup_required) {
        setRecoverySetupResult(result)
        setShowRecovery(false)
      } else {
        onAuthenticated(result)
      }
    } catch (authError) {
      setRecoveryCode('')
      setError(authErrorMessage(authError))
    } finally {
      setBusy(false)
    }
  }

  const cancelRecoverySession = async () => {
    const csrfToken = recoverySetupResult?.csrf_token
    if (!csrfToken) return
    setBusy(true)
    setError(null)
    try {
      await api.logout(csrfToken)
      setRecoverySetupResult(null)
      await onRecoveryCancelled()
    } catch (logoutError) {
      setError(authErrorMessage(logoutError))
    } finally {
      setBusy(false)
    }
  }

  const copyRecoveryCodes = async () => {
    try {
      if (!navigator.clipboard) throw new Error('当前浏览器不允许复制，请手动保存。')
      await navigator.clipboard.writeText(recoveryCodes.join('\n'))
      setCopyStatus('恢复码已复制，请保存到安全位置。')
    } catch (copyError) {
      setCopyStatus(copyError instanceof Error ? copyError.message : '复制失败，请手动保存。')
    }
  }

  if (recoveryCodes.length > 0 && registrationResult) {
    return (
      <AuthFrame>
        <section className="auth-card recovery-codes-card" aria-labelledby="recovery-codes-title">
          <div className="auth-icon success"><ShieldCheck size={27} weight="fill" /></div>
          <span className="eyebrow">仅显示一次</span>
          <h1 id="recovery-codes-title">保存一次性恢复码</h1>
          <p>每个恢复码只能使用一次。请保存在密码管理器或其他安全位置，离开此页后不再显示。</p>
          <ol className="recovery-code-list" aria-label="一次性恢复码">
            {recoveryCodes.map((code) => <li key={code}><code>{code}</code></li>)}
          </ol>
          {copyStatus ? <p className="auth-inline-status" role="status">{copyStatus}</p> : null}
          <div className="auth-actions">
            <Button type="button" variant="outline" onClick={() => void copyRecoveryCodes()}><Copy size={17} />复制恢复码</Button>
            <Button type="button" onClick={() => onAuthenticated(registrationResult)}>我已安全保存</Button>
          </div>
        </section>
      </AuthFrame>
    )
  }

  if (status?.setup_required || status?.recovery_setup_required || recoverySetupResult?.recovery_setup_required) {
    const firstSetup = Boolean(status?.setup_required)
    return (
      <AuthFrame>
        <section className="auth-card" aria-labelledby="passkey-setup-title">
          <div className="auth-icon"><Fingerprint size={29} /></div>
          <span className="eyebrow">{firstSetup ? '首次安全设置' : '恢复后安全轮换'}</span>
          <h1 id="passkey-setup-title">{firstSetup ? '创建你的通行密钥' : '创建新的通行密钥'}</h1>
          <p>{firstSetup ? '输入部署时生成的设置码，然后使用此设备的指纹、面容或系统解锁方式完成登记。' : '恢复码只用于恢复访问。请立即创建新的通行密钥，完成后旧恢复码将被轮换。'}</p>
          <form onSubmit={(event) => void register(event)}>
            {firstSetup ? <><label htmlFor="setup-code">首次设置码</label><TextField.Root id="setup-code" type="password" autoComplete="one-time-code" value={setupCode} onChange={(event) => setSetupCode(event.target.value)} aria-describedby={error ? 'auth-error' : undefined} /></> : null}
            {error ? <div className="auth-error" id="auth-error" role="alert"><Warning size={17} />{error}</div> : null}
            <Button type="submit" disabled={(firstSetup && !setupCode) || (!firstSetup && !recoverySetupResult?.csrf_token) || busy}><Fingerprint size={18} />{busy ? '正在创建' : firstSetup ? '创建通行密钥' : recoverySetupResult?.csrf_token ? '创建新的通行密钥' : '正在恢复安全会话'}</Button>
            {!firstSetup ? <Button type="button" variant="outline" disabled={busy || !recoverySetupResult?.csrf_token} onClick={() => void cancelRecoverySession()}>退出本次恢复会话</Button> : null}
          </form>
        </section>
      </AuthFrame>
    )
  }

  const recoveryOnly = Boolean(status?.recovery_pending)

  return (
    <AuthFrame>
      <section className="auth-card" aria-labelledby="passkey-login-title">
        <div className="auth-icon"><Fingerprint size={29} /></div>
        <span className="eyebrow">LedgerBridge 单用户访问</span>
        <h1 id="passkey-login-title">{showRecovery || recoveryOnly ? recoveryOnly ? '继续账户恢复' : '使用一次性恢复码' : '使用通行密钥登录'}</h1>
        <p>{showRecovery || recoveryOnly ? recoveryOnly ? '旧通行密钥已冻结。请使用另一枚一次性恢复码继续创建新通行密钥。' : '恢复码提交后立即从输入框清除，且成功使用后失效。' : '使用此设备的指纹、面容或系统解锁方式确认身份。'}</p>
        {showRecovery || recoveryOnly ? (
          <form onSubmit={(event) => void recover(event)}>
            <label htmlFor="recovery-code">一次性恢复码</label>
            <TextField.Root id="recovery-code" type="password" autoComplete="one-time-code" value={recoveryCode} onChange={(event) => setRecoveryCode(event.target.value)} aria-describedby={error ? 'auth-error' : undefined} />
            {error ? <div className="auth-error" id="auth-error" role="alert"><Warning size={17} />{error}</div> : null}
            <Button type="submit" disabled={!recoveryCode || busy}><ShieldCheck size={18} />{busy ? '正在验证' : '使用恢复码登录'}</Button>
          </form>
        ) : (
          <div className="auth-login-actions">
            {error ? <div className="auth-error" id="auth-error" role="alert"><Warning size={17} />{error}</div> : null}
            <Button type="button" disabled={busy} onClick={() => void login()}><Fingerprint size={18} />{busy ? '正在验证' : '使用通行密钥'}</Button>
          </div>
        )}
        {!recoveryOnly ? <button className="auth-mode-switch" type="button" disabled={busy} onClick={() => { setShowRecovery((current) => !current); setError(null) }}>
          {showRecovery ? '返回通行密钥登录' : '无法使用通行密钥？使用恢复码'}
        </button> : null}
      </section>
    </AuthFrame>
  )
}

function LoadingState({ title = '正在读取财务数据', description = '正在连接同源 API，并校验当前会话。' }: { title?: string; description?: string }) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <ArrowsClockwise className="state-spinner" size={30} />
      <h1>{title}</h1>
      <p>{description}</p>
    </div>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="state-panel error-state" role="alert">
      <Warning size={31} weight="fill" />
      <h1>数据读取失败</h1>
      <p>{message}</p>
      <Button onClick={onRetry}><ArrowsClockwise size={17} />重试</Button>
    </div>
  )
}

function Overview({
  pending,
  confirmed,
  onNavigate,
  onOpenCandidate,
}: {
  pending: Candidate[]
  confirmed: Candidate[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const confirmedTotal = confirmed.reduce((total, candidate) => total + candidate.amount, 0)
  return (
    <>
      <PageHeader
        eyebrow="2026 年 8 月"
        title="早上好，今天有几项需要确认"
        description="消息只会形成候选数据。经过你确认后，才会进入月度对账草稿。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />开始审核</Button>}
      />

      <section className="metric-grid" aria-label="本月概览">
        <Metric label="待审核候选" value={`${pending.length} 条`} detail="其中 1 条存在冲突" tone="attention" icon={<ListChecks size={20} />} />
        <Metric label="本月已确认" value={currency.format(confirmedTotal)} detail={`${confirmed.length} 条可用于草稿`} icon={<CheckCircle size={20} />} />
        <Metric label="覆盖营业单元" value="3 家" detail="城南店、江景店、机场店" icon={<Database size={20} />} />
        <Metric label="数据连接" value="2 / 3" detail="消息入口与计算服务正常" icon={<CloudArrowUp size={20} />} />
      </section>

      <div className="overview-grid">
        <section className="panel queue-preview">
          <div className="panel-heading">
            <div>
              <h2>待审核</h2>
              <p>按风险和完整度排序</p>
            </div>
            <Button variant="ghost" onClick={() => onNavigate('review')}>查看全部<CaretRight size={15} /></Button>
          </div>
          <div className="preview-list">
            {pending.slice(0, 3).map((candidate) => (
              <button className="preview-row" key={candidate.id} onClick={() => onOpenCandidate(candidate)} type="button">
                <SourceIcon source={candidate.source} />
                <span className="preview-main">
                  <strong>{candidate.summary}</strong>
                  <small>{candidate.businessUnit} · {candidate.receivedAt} · {candidate.shortId}</small>
                </span>
                <span className="preview-value">
                  <strong>{currency.format(candidate.amount)}</strong>
                  {candidate.conflict ? <Badge color="red">冲突</Badge> : candidate.incomplete ? <Badge color="amber">缺月份</Badge> : <Badge color="blue">待确认</Badge>}
                </span>
                <CaretRight className="row-caret" size={17} />
              </button>
            ))}
          </div>
        </section>

        <section className="panel readiness-panel">
          <div className="panel-heading">
            <div>
              <h2>本月对账就绪度</h2>
              <p>在生成草稿前解决阻断项</p>
            </div>
            <span className="readiness-score">76%</span>
          </div>
          <div className="progress-track"><span style={{ width: '76%' }} /></div>
          <div className="readiness-list">
            <StatusLine icon={<Check size={17} />} label="江景店" detail="数据完整" tone="ok" />
            <StatusLine icon={<Warning size={17} />} label="城南店" detail="1 条收款冲突" tone="warn" />
            <StatusLine icon={<Info size={17} />} label="机场店" detail="1 条记录缺月份" tone="info" />
          </div>
          <Button className="full-button" variant="soft" onClick={() => onNavigate('reconciliation')}>查看月度对账</Button>
        </section>
      </div>

      <section className="panel audit-strip">
        <div className="audit-icon"><ShieldCheck size={23} weight="fill" /></div>
        <div>
          <h2>每个数字都能回到原始消息</h2>
          <p>确认、更正和忽略均以追加记录保存，不覆盖原始证据。</p>
        </div>
        <Button variant="outline" color="gray">查看操作记录</Button>
      </section>
    </>
  )
}

function Metric({ label, value, detail, icon, tone }: { label: string; value: string; detail: string; icon: React.ReactNode; tone?: 'attention' }) {
  return (
    <article className={`metric ${tone === 'attention' ? 'attention' : ''}`}>
      <div className="metric-icon">{icon}</div>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  )
}

function StatusLine({ icon, label, detail, tone }: { icon: React.ReactNode; label: string; detail: string; tone: string }) {
  return (
    <div className={`status-line ${tone}`}>
      <span className="status-icon">{icon}</span>
      <strong>{label}</strong>
      <span>{detail}</span>
    </div>
  )
}

function ReviewQueue({ candidates, onOpenCandidate, onUpdate, onRefresh, busyId }: {
  candidates: Candidate[]
  onOpenCandidate: (candidate: Candidate) => void
  onUpdate: (candidate: Candidate, status: 'CONFIRMED' | 'IGNORED', corrections?: CandidateCorrections) => void
  onRefresh: () => void
  busyId: string | null
}) {
  const [filter, setFilter] = useState('all')
  const filtered = candidates.filter((candidate) => filter === 'all' || candidate.source === filter)
  return (
    <>
      <PageHeader
        eyebrow="人工确认队列"
        title="待审核候选"
        description="逐条核对字段与原始证据。确认不会直接生成正式凭证。"
        action={<Button variant="outline" color="gray" onClick={onRefresh}><ArrowsClockwise size={17} />刷新</Button>}
      />
      <div className="filter-bar">
        <div className="filter-tabs" role="group" aria-label="来源筛选">
          {[
            ['all', '全部'],
            ['Telegram', 'Telegram'],
            ['钉钉', '钉钉'],
            ['微信', '微信'],
          ].map(([value, label]) => (
            <button aria-pressed={filter === value} className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)} type="button">{label}</button>
          ))}
        </div>
        <TextField.Root aria-label="搜索候选编号、门店或科目" className="search-field" placeholder="搜索候选编号、门店或科目">
          <TextField.Slot><MagnifyingGlass size={16} /></TextField.Slot>
        </TextField.Root>
        <Button variant="soft" color="gray"><SlidersHorizontal size={17} />筛选</Button>
      </div>

      <section className="review-list" aria-label="候选数据列表">
        {filtered.length === 0 ? (
          <div className="empty-state">
            <CheckCircle size={34} weight="light" />
            <h2>当前筛选下没有待审核项</h2>
            <p>新的财务候选会在这里出现。</p>
          </div>
        ) : filtered.map((candidate) => (
          <article className={`candidate-card ${candidate.conflict ? 'has-conflict' : ''}`} key={candidate.id}>
            <div className="candidate-source">
              <SourceIcon source={candidate.source} />
              <div>
                <strong>{candidate.source}</strong>
                <span>{candidate.receivedAt}</span>
              </div>
            </div>
            <button className="candidate-body" onClick={() => onOpenCandidate(candidate)} type="button">
              <div className="candidate-tags">
                <Badge color={categoryTone[candidate.category] ?? 'gray'}>{candidate.category}</Badge>
                {candidate.conflict ? <Badge color="red">金额或凭证冲突</Badge> : null}
                {candidate.incomplete ? <Badge color="amber">缺少归属月份</Badge> : null}
              </div>
              <h2>{candidate.summary}</h2>
              <p>{candidate.summary}</p>
              <div className="candidate-meta">
                <span>{candidate.businessUnit}</span>
                <span>{candidate.accountingMonth ?? '建议归入 2026-08'}</span>
                <span>置信度 {Math.round(candidate.confidence * 100)}%</span>
                {candidate.evidence.some((item) => item.kind === 'attachment') ? <span><Paperclip size={14} />{candidate.evidence.filter((item) => item.kind === 'attachment').length} 个附件</span> : null}
              </div>
            </button>
            <div className="candidate-amount">
              <span>提取金额</span>
              <strong>{currency.format(candidate.amount)}</strong>
              <small>{candidate.shortId}</small>
            </div>
            <div className="candidate-actions">
              <Button disabled={busyId === candidate.id} variant="soft" color="gray" onClick={() => onUpdate(candidate, 'IGNORED')}><X size={16} />忽略</Button>
              <Button disabled={busyId === candidate.id || candidate.conflict || candidate.incomplete} onClick={() => onUpdate(candidate, 'CONFIRMED')}><Check size={16} />确认</Button>
            </div>
          </article>
        ))}
      </section>
    </>
  )
}

function SourceIcon({ source }: { source: Candidate['source'] }) {
  const initials: Record<Candidate['source'], string> = { Telegram: 'T', 钉钉: '钉', 微信: '微' }
  return <span className={`source-icon source-${source}`}>{initials[source]}</span>
}

function CandidateDialog({ candidate, onClose, onUpdate, busy }: {
  candidate: Candidate
  onClose: () => void
  onUpdate: (candidate: Candidate, status: 'CONFIRMED' | 'IGNORED', corrections?: CandidateCorrections) => void
  busy: boolean
}) {
  const [businessUnit, setBusinessUnit] = useState(candidate.businessUnit)
  const [category, setCategory] = useState(candidate.category)
  const [amount, setAmount] = useState(candidate.amount.toFixed(2))
  const [accountingMonth, setAccountingMonth] = useState(candidate.accountingMonth ?? '')

  const parsedAmount = Number(amount)
  const formComplete = businessUnit.trim().length > 0
    && category.trim().length > 0
    && Number.isFinite(parsedAmount)
    && accountingMonth.length > 0
  const confirmBlocked = busy || candidate.conflict || !formComplete

  const submitCorrection = () => {
    if (confirmBlocked) return
    onUpdate(candidate, 'CONFIRMED', {
      business_unit: businessUnit.trim(),
      category: category.trim(),
      amount_minor: majorToMinor(parsedAmount),
      accounting_month: accountingMonth,
    })
  }

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <Dialog.Content className="candidate-dialog" maxWidth="680px">
          <>
            <div className="dialog-kicker"><SourceIcon source={candidate.source} /><span>{candidate.source} · {candidate.receivedAt} · {candidate.shortId}</span></div>
            <Dialog.Title>核对候选数据</Dialog.Title>
            <Dialog.Description>字段来自规则与模型提取，原始消息保持不变。</Dialog.Description>
            <div className="evidence-box">
              <span className="section-label">原始证据引用</span>
              <blockquote>{candidate.summary}</blockquote>
              <div className="evidence-links">
                {candidate.evidence.map((item) => (
                  <a href={`/api/v1/evidence/${encodeURIComponent(item.id)}/content`} key={item.id}>
                    <Paperclip size={16} />{item.original_filename ?? (item.kind === 'message' ? '消息原文' : '附件')}
                  </a>
                ))}
              </div>
            </div>
            <div className="field-grid">
              <label htmlFor="candidate-business-unit"><span>营业单元</span><TextField.Root id="candidate-business-unit" value={businessUnit} onChange={(event) => setBusinessUnit(event.target.value)} /></label>
              <label htmlFor="candidate-category"><span>科目</span><TextField.Root id="candidate-category" value={category} onChange={(event) => setCategory(event.target.value)} /></label>
              <label htmlFor="candidate-amount"><span>金额</span><TextField.Root id="candidate-amount" inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
              <label>
                <span id="candidate-month-label">归属月份</span>
                <Select.Root value={accountingMonth} onValueChange={setAccountingMonth}>
                  <Select.Trigger aria-labelledby="candidate-month-label" placeholder="请选择归属月份" />
                  <Select.Content><Select.Item value="2026-08">2026 年 8 月</Select.Item><Select.Item value="2026-07">2026 年 7 月</Select.Item></Select.Content>
                </Select.Root>
              </label>
            </div>
            {candidate.conflict ? <div className="blocking-note"><Warning size={18} weight="fill" /><span><strong>需要先处理冲突</strong>另一条候选使用了相同凭证号但金额不同。</span></div> : null}
            {candidate.incomplete ? <div className="blocking-note amber"><Info size={18} weight="fill" /><span><strong>月份为系统建议</strong>请确认归属月份后再提交。</span></div> : null}
            <Separator my="4" size="4" />
            <div className="dialog-actions">
              <Button variant="soft" color="gray" onClick={onClose}>取消</Button>
              <Button disabled={busy} variant="outline" color="gray" onClick={() => onUpdate(candidate, 'IGNORED')}>忽略候选</Button>
              <Button disabled={confirmBlocked} onClick={submitCorrection}>保存更正并确认</Button>
            </div>
          </>
      </Dialog.Content>
    </Dialog.Root>
  )
}

type ReconciliationRow = {
  unit: string
  waterMinor: number
  taxMinor: number
  linenMinor: number
  bottledWaterMinor: number
  receiptsMinor: number
  readiness: string
}

function amountFor(amounts: Record<string, number>, ...keys: string[]) {
  const key = keys.find((candidate) => candidate in amounts)
  return key ? amounts[key] : 0
}

function toReconciliationRows(data: ReconciliationData | null): ReconciliationRow[] {
  if (!data) return []
  return data.business_units.map((unit) => ({
    unit: unit.name,
    waterMinor: amountFor(unit.amounts_minor, 'water', 'water_fee', '水费'),
    taxMinor: amountFor(unit.amounts_minor, 'tax', 'tax_fee', '税费'),
    linenMinor: amountFor(unit.amounts_minor, 'linen', 'linen_fee', '布草'),
    bottledWaterMinor: amountFor(unit.amounts_minor, 'bottled_water', 'bottledWater', '瓶装水'),
    receiptsMinor: amountFor(unit.amounts_minor, 'bank_receipts', 'receipts', '银行收款'),
    readiness: data.ready ? '可生成' : '待处理',
  }))
}

const columnHelper = createColumnHelper<ReconciliationRow>()
const columns = [
  columnHelper.accessor('unit', { header: '营业单元', cell: (info) => <strong>{info.getValue()}</strong> }),
  columnHelper.accessor('waterMinor', { header: '水费', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
  columnHelper.accessor('taxMinor', { header: '税费', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
  columnHelper.accessor('linenMinor', { header: '布草', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
  columnHelper.accessor('bottledWaterMinor', { header: '瓶装水', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
  columnHelper.accessor('receiptsMinor', { header: '银行收款', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
  columnHelper.accessor('readiness', {
    header: '状态',
    cell: (info) => {
      const value = info.getValue()
      return <Badge color={value === '可生成' ? 'green' : 'amber'}>{value}</Badge>
    },
  }),
]

function Reconciliation({ data, confirmed, selectedMonth, onMonthChange, onGenerate, generating, onNavigate }: {
  data: ReconciliationData | null
  confirmed: Candidate[]
  selectedMonth: string
  onMonthChange: (month: string) => void
  onGenerate: () => void
  generating: boolean
  onNavigate: (page: Page) => void
}) {
  const rows = toReconciliationRows(data)
  const table = useReactTable({ data: rows, columns, getCoreRowModel: getCoreRowModel() })
  const monthTotalMinor = rows.reduce((sum, row) => sum + row.waterMinor + row.taxMinor + row.linenMinor + row.bottledWaterMinor + row.receiptsMinor, 0)
  const receiptsTotalMinor = rows.reduce((sum, row) => sum + row.receiptsMinor, 0)
  const ready = Boolean(data?.ready && data.blockers.length === 0)
  const monthLabel = selectedMonth === '2026-08' ? '2026 年 8 月' : '2026 年 7 月'
  return (
    <>
      <PageHeader
        eyebrow="酒店月度对账"
        title={`${monthLabel}对账草稿`}
        description="当前是预览层。确认保存仍由原程序完成，候选不会直接入正式账。"
        action={<Select.Root value={selectedMonth} onValueChange={onMonthChange}><Select.Trigger aria-label="选择对账月份" /><Select.Content><Select.Item value="2026-08">2026 年 8 月</Select.Item><Select.Item value="2026-07">2026 年 7 月</Select.Item></Select.Content></Select.Root>}
      />

      {!ready ? (
        <div className="blocking-banner" role="alert">
          <Warning size={21} weight="fill" />
          <div><strong>本月草稿尚不可生成</strong><span>{data?.blockers.map((blocker) => blocker.message).join('；') || '对账数据尚未就绪。'}</span></div>
          <Button color="red" variant="soft" onClick={() => onNavigate('review')}>处理阻断项</Button>
        </div>
      ) : null}

      <section className="metric-grid reconciliation-metrics">
        <Metric label="本月汇总" value={currency.format(minorToMajor(monthTotalMinor))} detail={`跨 ${rows.length} 个营业单元`} icon={<Bank size={20} />} />
        <Metric label="已确认来源" value={`${confirmed.length} 条`} detail="均可回溯至原始证据" icon={<CheckCircle size={20} />} />
        <Metric label="待处理" value={`${data?.blockers.length ?? 0} 条`} detail={ready ? '无草稿阻断项' : '需先处理阻断项'} icon={<Warning size={20} />} tone={ready ? undefined : 'attention'} />
        <Metric label="计算验证" value="待运行" detail="草稿生成后由 LibreOffice 校验" icon={<ArrowsClockwise size={20} />} />
      </section>

      <section className="panel table-panel">
        <div className="panel-heading">
          <div><h2>营业单元汇总</h2><p>只展示审核通过或既有数据库中的数据</p></div>
          <Button disabled={!ready || generating} onClick={onGenerate}><FileText size={17} />{generating ? '正在提交' : '生成对账草稿'}</Button>
        </div>
        <div className="desktop-table-wrap">
          <table>
            <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
            <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
            <tfoot><tr><td>本月合计</td><td colSpan={4}>按营业单元与科目汇总</td><td>{currency.format(minorToMajor(receiptsTotalMinor))}</td><td><Badge color={ready ? 'green' : 'red'}>{ready ? '就绪' : '阻断'}</Badge></td></tr></tfoot>
          </table>
        </div>
        <div className="mobile-reconciliation-list">
          {rows.map((row) => (
            <article key={row.unit}>
              <div><strong>{row.unit}</strong><Badge color={row.readiness === '可生成' ? 'green' : 'amber'}>{row.readiness}</Badge></div>
              <dl><dt>运营支出</dt><dd>{currency.format(minorToMajor(row.waterMinor + row.taxMinor + row.linenMinor + row.bottledWaterMinor))}</dd><dt>银行收款</dt><dd>{currency.format(minorToMajor(row.receiptsMinor))}</dd></dl>
            </article>
          ))}
          {rows.length === 0 ? <div className="empty-state compact-empty"><Table size={32} /><h2>本月没有对账数据</h2><p>审核通过的候选和既有数据库汇总会显示在这里。</p></div> : null}
          <p className="mobile-grid-note"><Info size={16} />完整科目网格请在平板或电脑查看。</p>
        </div>
      </section>

      <section className="panel provenance-panel">
        <div className="panel-heading"><div><h2>数据来源说明</h2><p>每次生成都记录输入版本与计算结果</p></div></div>
        <div className="provenance-steps">
          <div><span>1</span><strong>人工确认</strong><small>消息候选</small></div>
          <CaretRight size={18} />
          <div><span>2</span><strong>写入草稿</strong><small>不可直接入账</small></div>
          <CaretRight size={18} />
          <div><span>3</span><strong>公式校验</strong><small>LibreOffice</small></div>
          <CaretRight size={18} />
          <div><span>4</span><strong>原程序确认</strong><small>正式保存</small></div>
        </div>
      </section>
    </>
  )
}

const connectionStateLabel: Record<ConnectionStatus['state'], string> = {
  CONNECTED: '已连接',
  DISCONNECTED: '已断开',
  DEGRADED: '服务降级',
  NOT_CONFIGURED: '未配置',
}

function ConnectionBadge({ connection }: { connection?: ConnectionStatus }) {
  const state = connection?.state ?? 'NOT_CONFIGURED'
  const color = state === 'CONNECTED' ? 'green' : state === 'DEGRADED' ? 'amber' : 'gray'
  return <Badge color={color}>{connectionStateLabel[state]}</Badge>
}

function FilesAndConnections({ connections, onRefresh }: { connections: ConnectionStatus[]; onRefresh: () => void }) {
  const connection = (id: ConnectionStatus['id']) => connections.find((item) => item.id === id)
  return (
    <>
      <PageHeader
        eyebrow="服务与文件"
        title="文件与连接"
        description="状态来自同源 API。界面不显示令牌或其他敏感凭据。"
        action={<Button variant="outline" color="gray" onClick={onRefresh}><ArrowsClockwise size={17} />重新检查</Button>}
      />

      <div className="connection-grid">
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon onedrive"><CloudArrowUp size={24} weight="fill" /></div><div><h2>OneDrive Personal</h2><p>应用专用文件夹</p></div><ConnectionBadge connection={connection('onedrive_appfolder')} /></div>
          <p>仅访问 <code>Apps/LedgerBridge</code>，不读取 OneDrive 中的其他文件。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>计划权限：Files.ReadWrite.AppFolder</span></div>
          <Button>连接 OneDrive</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon hermes"><Database size={24} weight="fill" /></div><div><h2>Hermes 消息入口</h2><p>Telegram、钉钉、微信</p></div><ConnectionBadge connection={connection('hermes_ingress')} /></div>
          <p>只处理启用后的主账号私聊。家庭账号、群聊和历史消息均不在范围内。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>附件在消息入口即时提取与留证</span></div>
          <Button variant="soft" color="gray">查看入口规则</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon office"><FileText size={24} weight="fill" /></div><div><h2>LibreOffice 计算服务</h2><p>Hermes 后台进程</p></div><ConnectionBadge connection={connection('libreoffice_worker')} /></div>
          <p>在临时副本上重算工作簿，检查公式错误和关键值，不覆盖原始文件。</p>
          <div className="permission-line"><Info size={17} /><span>结果标记为 LibreOffice 已验证</span></div>
          <Button variant="soft" color="gray">查看验证策略</Button>
        </section>
      </div>

      <section className="panel files-panel">
        <div className="panel-heading"><div><h2>最近的工作簿</h2><p>连接 OneDrive 后显示应用文件夹中的版本</p></div><Button variant="outline" color="gray" disabled><CloudArrowUp size={17} />上传副本</Button></div>
        <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚未连接文件来源</h2><p>连接后，系统会显示可用于月度对账的工作簿副本。</p></div>
      </section>
    </>
  )
}

export default App
