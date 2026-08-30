import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Badge,
  Button,
  Dialog,
  DropdownMenu,
  Select,
  TextArea,
  TextField,
} from '@radix-ui/themes'
import {
  ArrowsClockwise,
  Bank,
  CaretRight,
  Check,
  CheckCircle,
  CloudArrowUp,
  ClockCounterClockwise,
  Copy,
  Database,
  DownloadSimple,
  FileText,
  FileXls,
  Fingerprint,
  FolderOpen,
  House,
  ImageSquare,
  Info,
  ListChecks,
  MagnifyingGlass,
  Paperclip,
  ShieldCheck,
  SignOut,
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
import { api, ApiError, majorToMinor, minorToMajor } from './api'
import type {
  ApiCandidate,
  AuthResult,
  AuthStatus,
  Candidate,
  CandidateCorrections,
  CandidateDetail,
  ConnectionStatus,
  EvidencePreview,
  EvidenceReference,
  Notice,
  Page,
  PayrollBatchListData,
  PayrollDashboardData,
  PayrollMaterialListData,
  PayrollReadResponse,
  PayrollStatusData,
  PayrollVerificationListData,
  Reconciliation as ReconciliationData,
  ReviewEvent,
  Session,
} from './types'

const CURRENT_MONTH = '2026-08'

const navigation: Array<{ id: Page; label: string; icon: typeof House }> = [
  { id: 'overview', label: '概览', icon: House },
  { id: 'payroll', label: '工资与发放验证', icon: FileXls },
  { id: 'personal-finance', label: '完整个人财务对账', icon: Bank },
  { id: 'review', label: '待审核', icon: ListChecks },
  { id: 'reconciliation', label: '原口径对账表', icon: Table },
  { id: 'company-reports', label: '各公司报表', icon: Database },
  { id: 'files', label: '文件与连接', icon: FolderOpen },
]

const pagePaths: Record<Page, string> = {
  overview: '/overview',
  'personal-finance': '/personal-finance',
  review: '/review',
  reconciliation: '/reconciliation',
  'company-reports': '/company-reports',
  payroll: '/payroll',
  files: '/files',
  audit: '/audit',
}

function pageFromPath(pathname: string): Page {
  const entry = Object.entries(pagePaths).find(([, path]) => path === pathname)
  return entry ? entry[0] as Page : 'overview'
}

type CandidateUpdateIntent = 'CONFIRM' | 'IGNORE' | 'RESOLVE_CONFLICT'

const currency = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  minimumFractionDigits: 2,
})

const sourceLabels: Record<ApiCandidate['source_channel'], Candidate['source']> = {
  telegram: 'Telegram',
  dingtalk: '钉钉',
  weixin: '微信',
  hermes: 'Hermes',
  outlook: '中行账单（复核材料）',
  controlled_upload: '照片凭证',
  synthetic: '合成数据',
}

function toCandidate(candidate: ApiCandidate | CandidateDetail): Candidate {
  const blockerCodes = new Set(candidate.blockers.map((blocker) => blocker.code))
  const reviewRisks = candidate.review_risks ?? []
  const source = candidate.summary.startsWith('微信 |')
    ? '微信'
    : candidate.summary.startsWith('支付宝 |')
      ? '支付宝'
      : sourceLabels[candidate.source_channel]
  return {
    id: candidate.id,
    shortId: candidate.short_id,
    revision: candidate.revision,
    source,
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
    reviewRisks,
    reviewEvents: 'review_events' in candidate ? candidate.review_events : [],
    incomplete: candidate.status === 'INCOMPLETE' || blockerCodes.has('MISSING_ACCOUNTING_MONTH'),
    conflict: candidate.status === 'CONFLICTED' || blockerCodes.has('BUSINESS_KEY_CONFLICT') || blockerCodes.has('DUPLICATE_MESSAGE') || blockerCodes.has('DUPLICATE_ATTACHMENT'),
    raw: candidate,
  }
}

function isBulkEligible(candidate: Candidate): boolean {
  return candidate.status === 'PENDING'
    && candidate.confidence >= 0.9
    && candidate.blockers.length === 0
    && candidate.reviewRisks.length === 0
    && !candidate.incomplete
    && !candidate.conflict
}

const materialRiskCodes = new Set([
  'FUNDING_STATEMENT_REQUIRED',
  'RELATED_ACCOUNT_STATEMENT_REQUIRED',
  'HOTEL_PAYOUT_STATEMENT_REQUIRED',
])

const platformInternalAccounts = new Set(['花呗', '余额宝', '账户余额', '零钱', '零钱通'])

function summaryFields(candidate: Candidate): string[] {
  return candidate.summary.split('|').map((value) => value.trim())
}

function counterpartyFor(candidate: Candidate): string {
  return summaryFields(candidate)[4] ?? ''
}

function paymentMethodFor(candidate: Candidate): string {
  return summaryFields(candidate)[5] ?? ''
}

function isPlatformInternalAccount(value: string): boolean {
  const normalized = value.trim().replace(/^(支付宝|微信)[:：]?/, '')
  return platformInternalAccounts.has(normalized)
}

function materialNameFor(candidate: Candidate, riskCode: string): string | null {
  if (!materialRiskCodes.has(riskCode)) return null
  if (riskCode === 'FUNDING_STATEMENT_REQUIRED') {
    const paymentMethod = paymentMethodFor(candidate)
    if (!paymentMethod) return '资金账户明细'
    return !isPlatformInternalAccount(paymentMethod) ? `${paymentMethod}明细` : null
  }
  if (riskCode === 'RELATED_ACCOUNT_STATEMENT_REQUIRED') {
    const counterparty = counterpartyFor(candidate)
    return isPlatformInternalAccount(counterparty) ? null : `${counterparty || '关联账户'}同期流水`
  }
  return '酒店平台收款银行流水'
}

function accountingMonthLabel(month: string | null): string {
  if (!month) return '期间待确认'
  const [year, monthNumber] = month.split('-')
  return `${year} 年 ${Number(monthNumber)} 月`
}

async function listRemainingCandidatePages(initialCursor: string) {
  const items: ApiCandidate[] = []
  const visited = new Set<string>()
  let cursor: string | null = initialCursor
  while (cursor) {
    if (visited.has(cursor)) throw new Error('候选分页游标重复，无法完整读取审核上下文')
    visited.add(cursor)
    const page = await api.listCandidates({ cursor })
    items.push(...page.items)
    cursor = page.next_cursor
  }
  return items
}

function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [authError, setAuthError] = useState<string | null>(null)
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname))
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [session, setSession] = useState<Session | null>(null)
  const [reconciliation, setReconciliation] = useState<ReconciliationData | null>(null)
  const [selectedMonth, setSelectedMonth] = useState(CURRENT_MONTH)
  const [connections, setConnections] = useState<ConnectionStatus[]>([])
  const [reviewEvents, setReviewEvents] = useState<ReviewEvent[]>([])
  const [auditCandidates, setAuditCandidates] = useState<Candidate[]>([])
  const [reviewEventCursor, setReviewEventCursor] = useState<string | null>(null)
  const [reviewEventsLoading, setReviewEventsLoading] = useState(false)
  const [reviewEventsError, setReviewEventsError] = useState<string | null>(null)
  const candidateCursorRef = useRef<string | null>(null)
  const candidateDetailRequestRef = useRef(0)
  const businessDataLoadedRef = useRef(false)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [decisionBusyId, setDecisionBusyId] = useState<string | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  const [draftBusy, setDraftBusy] = useState(false)
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [passkeyDialogOpen, setPasskeyDialogOpen] = useState(false)
  const [passkeyBusy, setPasskeyBusy] = useState(false)
  const [passkeyError, setPasskeyError] = useState<string | null>(null)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)
  const [candidateDetailLoadingId, setCandidateDetailLoadingId] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  const navigate = useCallback((nextPage: Page, replace = false) => {
    const nextPath = pagePaths[nextPage]
    if (window.location.pathname !== nextPath) {
      window.history[replace ? 'replaceState' : 'pushState']({}, '', nextPath)
    }
    setPage(nextPage)
  }, [])

  useEffect(() => {
    if (!Object.values(pagePaths).includes(window.location.pathname)) {
      window.history.replaceState({}, '', pagePaths.overview)
    }
    const handlePopState = () => setPage(pageFromPath(window.location.pathname))
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [navigate])

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
      const remainingCandidates = candidateData.next_cursor
        ? await listRemainingCandidatePages(candidateData.next_cursor)
        : []
      setCandidates([...candidateData.items, ...remainingCandidates].map(toCandidate))
      setAuditCandidates([])
      candidateCursorRef.current = null
      setReconciliation(reconciliationData)
      setConnections(connectionData)
      businessDataLoadedRef.current = true
      return true
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '无法读取财务数据')
      return false
    } finally {
      setLoading(false)
    }
  }, [selectedMonth])

  const loadReviewEvents = useCallback(async (cursor?: string, includeCandidatePages = false) => {
    setReviewEventsLoading(true)
    setReviewEventsError(null)
    try {
      const remainingCandidateCursor = includeCandidatePages ? candidateCursorRef.current : null
      const [result, additionalCandidates] = await Promise.all([
        api.listReviewEvents(cursor),
        remainingCandidateCursor ? listRemainingCandidatePages(remainingCandidateCursor) : Promise.resolve([]),
      ])
      setReviewEvents((current) => {
        const combined = cursor ? [...current, ...result.items] : result.items
        return [...new Map(combined.map((event) => [event.id, event])).values()]
      })
      if (includeCandidatePages && remainingCandidateCursor) setAuditCandidates(additionalCandidates.map(toCandidate))
      setReviewEventCursor(result.next_cursor)
    } catch (error) {
      setReviewEventsError(error instanceof Error ? error.message : '无法读取审核操作记录')
    } finally {
      setReviewEventsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!authStatus?.authenticated || authStatus.recovery_setup_required) return
    if (page === 'payroll') return
    if (businessDataLoadedRef.current) return
    const loadTimer = window.setTimeout(() => void loadData(), 0)
    return () => window.clearTimeout(loadTimer)
  }, [authStatus?.authenticated, authStatus?.recovery_setup_required, loadData, page])

  useEffect(() => {
    if (!authStatus?.authenticated || authStatus.recovery_setup_required || loading || page !== 'audit') return
    const loadTimer = window.setTimeout(() => void loadReviewEvents(undefined, true), 0)
    return () => window.clearTimeout(loadTimer)
  }, [authStatus?.authenticated, authStatus?.recovery_setup_required, loadReviewEvents, loading, page])

  const changeMonth = (month: string) => setSelectedMonth(month)

  const pendingCandidates = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const confirmedCandidates = candidates.filter((candidate) => candidate.status === 'CONFIRMED')

  const updateCandidate = async (
    candidate: Candidate,
    intent: CandidateUpdateIntent,
    corrections?: CandidateCorrections,
    conflictResolution?: string,
  ) => {
    if (!session) {
      setNotice({ tone: 'error', message: '会话尚未就绪，请刷新后重试' })
      return
    }
    if (intent === 'CONFIRM' && (candidate.conflict || (candidate.incomplete && !corrections?.accounting_month))) {
      setNotice({ tone: 'error', message: `${candidate.shortId} 仍有阻断项，不能确认` })
      return
    }
    if (intent === 'RESOLVE_CONFLICT' && (!candidate.conflict || !conflictResolution?.trim())) {
      setNotice({ tone: 'error', message: `${candidate.shortId} 缺少冲突处理依据` })
      return
    }
    setDecisionBusyId(candidate.id)
    try {
      const result = await api.appendDecision({
        candidate: candidate.raw,
        decision: intent === 'IGNORE' ? 'IGNORE' : intent === 'RESOLVE_CONFLICT' ? 'RESOLVE_CONFLICT' : corrections ? 'CORRECT_AND_CONFIRM' : 'CONFIRM',
        reason: intent === 'IGNORE' ? 'Web 审核：忽略候选' : intent === 'RESOLVE_CONFLICT' ? 'Web 审核：解决冲突并确认' : corrections ? 'Web 审核：更正并确认' : 'Web 审核：确认候选',
        corrections,
        conflictResolution: conflictResolution?.trim(),
        csrfToken: session.csrf_token,
      })
      const updated = toCandidate(result.candidate)
      setCandidates((items) => items.map((item) => (item.id === updated.id ? updated : item)))
      setReviewEvents((items) => [result.event, ...items.filter((item) => item.id !== result.event.id)])
      setSelectedCandidate(null)
      try {
        const refreshedReconciliation = await api.getReconciliation(selectedMonth)
        setReconciliation(refreshedReconciliation)
        setNotice({
          tone: 'success',
          message: intent === 'IGNORE'
            ? `${candidate.shortId} 已忽略，原始证据仍保留`
            : intent === 'RESOLVE_CONFLICT'
              ? `${candidate.shortId} 冲突已解决并进入本月草稿数据`
              : `${candidate.shortId} 已确认并进入本月草稿数据`,
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

  const bulkConfirmCandidates = async (eligible: Candidate[]) => {
    if (!session || batchBusy || eligible.length === 0) return
    if (!window.confirm(`将一次确认 ${eligible.length} 条置信度不低于 90% 且无风险提示的账单。确认继续？`)) return
    setBatchBusy(true)
    let confirmed = 0
    let failed = 0
    try {
      for (let offset = 0; offset < eligible.length; offset += 6) {
        const chunk = eligible.slice(offset, offset + 6)
        const results = await Promise.allSettled(chunk.map((candidate) => api.appendDecision({
          candidate: candidate.raw,
          decision: 'CONFIRM',
          reason: 'Web 例外审核：批量确认高置信度且无风险候选',
          csrfToken: session.csrf_token,
        })))
        confirmed += results.filter((result) => result.status === 'fulfilled').length
        failed += results.filter((result) => result.status === 'rejected').length
      }
      await loadData()
      setNotice({
        tone: failed === 0 ? 'success' : 'info',
        message: failed === 0
          ? `已确认 ${confirmed} 条安全候选；风险项仍保留人工审核`
          : `已确认 ${confirmed} 条，${failed} 条因状态变化或网络问题未处理，请刷新后重试`,
      })
    } finally {
      setBatchBusy(false)
    }
  }

  const openCandidate = async (candidate: Candidate) => {
    const requestId = ++candidateDetailRequestRef.current
    setSelectedCandidate(candidate)
    setCandidateDetailLoadingId(candidate.id)
    try {
      const detail = await api.getCandidate(candidate.id)
      setSelectedCandidate((current) => current?.id === candidate.id ? toCandidate(detail) : current)
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? `证据详情读取失败：${error.message}` : '证据详情读取失败' })
    } finally {
      if (candidateDetailRequestRef.current === requestId) setCandidateDetailLoadingId(null)
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
      setReviewEvents([])
      setAuditCandidates([])
      setReviewEventCursor(null)
      setReviewEventsError(null)
      candidateCursorRef.current = null
      setSelectedCandidate(null)
      setCandidateDetailLoadingId(null)
      candidateDetailRequestRef.current += 1
      navigate('overview', true)
      setLoading(true)
      setNotice(null)
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '退出失败，请重试' })
    } finally {
      setLogoutBusy(false)
    }
  }

  const addPasskey = async () => {
    if (!session) {
      setPasskeyError('会话信息尚未就绪，请刷新后重试。')
      return
    }
    setPasskeyBusy(true)
    setPasskeyError(null)
    try {
      const result = await api.addPasskey(session.csrf_token)
      setPasskeyDialogOpen(false)
      setNotice({ tone: 'success', message: `这台设备已登记，当前共有 ${result.passkey_count} 个通行密钥可登录。` })
    } catch (error) {
      setPasskeyError(authErrorMessage(error))
    } finally {
      setPasskeyBusy(false)
    }
  }

  const renderPage = () => {
    if (page === 'overview') {
      return (
        <Overview
          pending={pendingCandidates}
          confirmed={confirmedCandidates}
          reconciliation={reconciliation}
          connections={connections}
          onNavigate={navigate}
          onOpenCandidate={openCandidate}
        />
      )
    }
    if (page === 'personal-finance') {
      return <PersonalFinanceOverview candidates={candidates} onNavigate={navigate} />
    }
    if (page === 'review') {
      return (
        <ReviewQueue
          candidates={pendingCandidates}
          onOpenCandidate={openCandidate}
          onUpdate={updateCandidate}
          onRefresh={loadData}
          busyId={decisionBusyId}
          batchBusy={batchBusy}
          onBatchConfirm={bulkConfirmCandidates}
        />
      )
    }
    if (page === 'reconciliation') {
      return <Reconciliation data={reconciliation} confirmed={confirmedCandidates} selectedMonth={selectedMonth} onMonthChange={changeMonth} onGenerate={generateDraft} generating={draftBusy} onNavigate={navigate} />
    }
    if (page === 'company-reports') {
      return <CompanyReports />
    }
    if (page === 'payroll') {
      return <PayrollVerificationStatus />
    }
    if (page === 'audit') {
      return (
        <AuditLog
          events={reviewEvents}
          candidates={[...candidates, ...auditCandidates]}
          nextCursor={reviewEventCursor}
          loading={reviewEventsLoading}
          error={reviewEventsError}
          onLoadMore={(cursor) => void loadReviewEvents(cursor)}
          onRetry={() => void loadReviewEvents(
            reviewEvents.length > 0 ? reviewEventCursor ?? undefined : undefined,
            reviewEvents.length === 0,
          )}
          onOpenCandidate={openCandidate}
          onNavigate={navigate}
        />
      )
    }
    return <FilesAndConnections candidates={candidates} connections={connections} csrfToken={session?.csrf_token ?? null} onOpenCandidate={openCandidate} onRefresh={loadData} onNotice={setNotice} />
  }

  if (authLoading) return <AuthFrame><LoadingState title="正在检查访问状态" description="正在确认此设备的单用户会话。" /></AuthFrame>
  if (authError) return <AuthFrame><ErrorState message={authError} onRetry={loadAuthStatus} /></AuthFrame>
  if (!authStatus?.authenticated || authStatus.recovery_setup_required) {
    return <AuthScreen status={authStatus} onAuthenticated={completeAuthentication} onRecoveryCancelled={loadAuthStatus} />
  }

  const isCoreBacked = session?.runtime_mode === 'core-backed'

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
                onClick={() => navigate(item.id)}
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
            <span>{isCoreBacked ? '正式数据环境' : '演示数据环境'}</span>
          </div>
          <span>{isCoreBacked ? 'Core 是唯一业务事实源' : '无真实财务数据'}</span>
        </div>
      </aside>

      <div className="main-column">
        <header className="topbar">
          <div className="mobile-brand"><Brand compact /></div>
          <div className="prototype-flag">
            <span className="flag-dot" />
            {isCoreBacked
              ? '正式环境 · Core 实时业务数据'
              : '演示环境 · 登录已启用 · 合成业务数据'}
          </div>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger>
              <Button variant="soft" color="gray" size="2">
                <span className="avatar">W</span>
                <span className="account-label">财务管理员</span>
              </Button>
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end">
              <DropdownMenu.Item onSelect={() => { setPasskeyError(null); setPasskeyDialogOpen(true) }}><Fingerprint size={15} />添加这台设备</DropdownMenu.Item>
              <DropdownMenu.Item onSelect={() => navigate('audit')}><ClockCounterClockwise size={15} />操作记录</DropdownMenu.Item>
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
          {page === 'payroll'
            ? renderPage()
            : loading
              ? <LoadingState />
              : loadError
                ? <ErrorState message={loadError} onRetry={loadData} />
                : renderPage()}
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
              onClick={() => navigate(item.id)}
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

      <Dialog.Root
        open={passkeyDialogOpen}
        onOpenChange={(open) => {
          if (!passkeyBusy) {
            setPasskeyDialogOpen(open)
            if (!open) setPasskeyError(null)
          }
        }}
      >
        <Dialog.Content maxWidth="480px">
          <Dialog.Title>添加这台设备的通行密钥</Dialog.Title>
          <Dialog.Description>
            系统会先要求你使用一个已登记的通行密钥确认身份，再按当前设备的系统提示（Windows Hello、指纹或屏幕锁）创建独立密钥。其他设备的密钥不会被撤销。
          </Dialog.Description>
          {passkeyError ? <div className="auth-error" role="alert"><Warning size={17} />{passkeyError}</div> : null}
          <div className="auth-actions">
            <Button type="button" variant="outline" disabled={passkeyBusy} onClick={() => setPasskeyDialogOpen(false)}>取消</Button>
            <Button type="button" disabled={passkeyBusy} onClick={() => void addPasskey()}><Fingerprint size={18} />{passkeyBusy ? '正在登记' : '开始登记'}</Button>
          </div>
        </Dialog.Content>
      </Dialog.Root>

      {selectedCandidate ? (
        <CandidateDialog
          key={`${selectedCandidate.id}:${selectedCandidate.revision}`}
          candidate={selectedCandidate}
          onClose={() => setSelectedCandidate(null)}
          onUpdate={updateCandidate}
          busy={selectedCandidate.id === decisionBusyId}
          detailLoading={candidateDetailLoadingId === selectedCandidate.id}
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
            {!firstSetup ? <p className="auth-inline-status">退出会保留恢复锁定，并需要使用另一枚恢复码才能继续。</p> : null}
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
  reconciliation,
  connections,
  onNavigate,
  onOpenCandidate,
}: {
  pending: Candidate[]
  confirmed: Candidate[]
  reconciliation: ReconciliationData | null
  connections: ConnectionStatus[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const confirmedTotal = confirmed.reduce((total, candidate) => total + candidate.amount, 0)
  const candidates = [...pending, ...confirmed]
  const conflictCount = pending.filter((candidate) => candidate.conflict).length
  const businessUnits = Array.from(new Set([
    ...candidates.map((candidate) => candidate.businessUnit),
    ...(reconciliation?.business_units.map((unit) => unit.name) ?? []),
  ])).sort((left, right) => left.localeCompare(right, 'zh-CN'))
  const connectedCount = connections.filter((connection) => connection.state === 'CONNECTED').length
  const candidateTotal = candidates.length
  const reviewProgress = candidateTotal > 0 ? Math.round((confirmed.length / candidateTotal) * 100) : 100
  const unitStates = businessUnits.map((unit) => {
    const unresolved = pending.filter((candidate) => candidate.businessUnit === unit)
    const conflicts = unresolved.filter((candidate) => candidate.conflict).length
    const incomplete = unresolved.filter((candidate) => candidate.incomplete && !candidate.conflict).length
    if (conflicts > 0) return { unit, detail: `${conflicts} 条冲突`, tone: 'warn', icon: <Warning size={17} /> }
    if (incomplete > 0) return { unit, detail: `${incomplete} 条信息不完整`, tone: 'info', icon: <Info size={17} /> }
    if (unresolved.length > 0) return { unit, detail: `${unresolved.length} 条待确认`, tone: 'info', icon: <Info size={17} /> }
    return { unit, detail: '当前数据已处理', tone: 'ok', icon: <Check size={17} /> }
  })
  return (
    <>
      <PageHeader
        eyebrow="2026 年 8 月"
        title="早上好，今天有几项需要确认"
        description="消息只会形成候选数据。经过你确认后，才会进入月度对账草稿。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />开始审核</Button>}
      />

      <section className="metric-grid" aria-label="本月概览">
        <Metric primary label="今日处理队列" value={`${pending.length} 条`} detail={conflictCount > 0 ? `优先处理 ${conflictCount} 条冲突候选` : '当前没有冲突候选'} tone={pending.length > 0 ? 'attention' : undefined} icon={<ListChecks size={20} />} />
        <Metric label="本月已确认" value={currency.format(confirmedTotal)} detail={`${confirmed.length} 条可用于草稿`} icon={<CheckCircle size={20} />} />
        <Metric label="覆盖营业单元" value={`${businessUnits.length} 家`} detail={businessUnits.length > 0 ? businessUnits.join('、') : '尚无营业单元数据'} icon={<Database size={20} />} />
        <Metric label="数据连接" value={`${connectedCount} / ${connections.length}`} detail={connections.length > 0 ? `${connections.length - connectedCount} 项未连接或状态异常` : '尚未返回连接状态'} tone={connections.length > 0 && connectedCount < connections.length ? 'attention' : undefined} icon={<CloudArrowUp size={20} />} />
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
              <h2>候选处理进度</h2>
              <p>{reconciliation?.ready ? '本月草稿已满足生成条件' : `仍有 ${reconciliation?.blockers.length ?? pending.length} 个阻断项`}</p>
            </div>
            <span className="readiness-score">{reviewProgress}%</span>
          </div>
          <div className="progress-track" role="progressbar" aria-label="候选处理进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={reviewProgress}><span style={{ width: `${reviewProgress}%` }} /></div>
          <div className="readiness-list">
            {unitStates.map((state) => <StatusLine key={state.unit} icon={state.icon} label={state.unit} detail={state.detail} tone={state.tone} />)}
            {unitStates.length === 0 ? <StatusLine icon={<Check size={17} />} label="本月" detail="暂无候选" tone="ok" /> : null}
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
        <Button variant="outline" color="gray" onClick={() => onNavigate('audit')}>查看操作记录</Button>
      </section>
    </>
  )
}

const decisionLabels: Record<ReviewEvent['decision'], string> = {
  CONFIRM: '确认候选',
  CORRECT_AND_CONFIRM: '更正并确认',
  IGNORE: '忽略候选',
  RESOLVE_CONFLICT: '解决冲突',
}

const decisionColors: Record<ReviewEvent['decision'], 'green' | 'blue' | 'gray' | 'red'> = {
  CONFIRM: 'green',
  CORRECT_AND_CONFIRM: 'blue',
  IGNORE: 'gray',
  RESOLVE_CONFLICT: 'red',
}

const auditFieldLabels: Record<ReviewEvent['changes'][number]['field'], string> = {
  business_unit: '营业单元',
  category: '科目',
  amount_minor: '金额',
  accounting_month: '归属月份',
  status: '状态',
}

const auditStatusLabels: Record<string, string> = {
  INCOMPLETE: '信息不完整',
  PENDING: '待审核',
  CONFLICTED: '存在冲突',
  CONFIRMED: '已确认',
  IGNORED: '已忽略',
  SUPERSEDED: '已被更正',
}

function formatAuditValue(field: ReviewEvent['changes'][number]['field'], value: string | number | null) {
  if (value === null) return '未填写'
  if (field === 'amount_minor' && typeof value === 'number') return currency.format(minorToMajor(value))
  if (field === 'status' && typeof value === 'string') return auditStatusLabels[value] ?? value
  return String(value)
}

function AuditLog({ events, candidates, nextCursor, loading, error, onLoadMore, onRetry, onOpenCandidate, onNavigate }: {
  events: ReviewEvent[]
  candidates: Candidate[]
  nextCursor: string | null
  loading: boolean
  error: string | null
  onLoadMore: (cursor: string) => void
  onRetry: () => void
  onOpenCandidate: (candidate: Candidate) => void
  onNavigate: (page: Page) => void
}) {
  const [query, setQuery] = useState('')
  const [decision, setDecision] = useState<'ALL' | ReviewEvent['decision']>('ALL')
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]))
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const filtered = events.filter((event) => {
    if (decision !== 'ALL' && event.decision !== decision) return false
    if (!normalizedQuery) return true
    const candidate = candidateById.get(event.candidate_id)
    return [
      candidate?.shortId,
      candidate?.businessUnit,
      candidate?.category,
      event.actor,
      event.reason,
      event.conflict_resolution,
      decisionLabels[event.decision],
    ].some((value) => value?.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
  })

  return (
    <>
      <PageHeader
        eyebrow="只读 · 追加式记录"
        title="审核操作记录"
        description="这里展示候选的确认、更正、冲突处置与忽略记录。合成预览不会读取真实财务审计数据。"
        action={<Button variant="outline" color="gray" onClick={() => onNavigate('overview')}>返回概览</Button>}
      />

      <section className="panel audit-log-panel">
        {loading && events.length === 0 ? (
          <LoadingState title="正在读取审核记录" description="正在加载追加式操作历史。" />
        ) : error && events.length === 0 ? (
          <div className="audit-load-state" role="alert">
            <Warning size={28} weight="fill" />
            <h2>审核记录读取失败</h2>
            <p>{error}</p>
            <Button onClick={onRetry}><ArrowsClockwise size={17} />重试</Button>
          </div>
        ) : <>
          <div className="audit-toolbar">
            <div>
              <strong>{nextCursor ? `已加载 ${filtered.length} 条` : `${filtered.length} 条记录`}</strong>
              <span>按最新操作排序</span>
            </div>
            <Select.Root value={decision} onValueChange={(value) => setDecision(value as 'ALL' | ReviewEvent['decision'])}>
              <Select.Trigger aria-label="筛选操作类型" />
              <Select.Content>
                <Select.Item value="ALL">全部操作</Select.Item>
                <Select.Item value="CONFIRM">确认候选</Select.Item>
                <Select.Item value="CORRECT_AND_CONFIRM">更正并确认</Select.Item>
                <Select.Item value="RESOLVE_CONFLICT">解决冲突</Select.Item>
                <Select.Item value="IGNORE">忽略候选</Select.Item>
              </Select.Content>
            </Select.Root>
            <TextField.Root
              aria-label="搜索操作记录"
              className="audit-search"
              placeholder="搜索候选、门店、科目或原因"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            >
              <TextField.Slot><MagnifyingGlass size={15} /></TextField.Slot>
            </TextField.Root>
          </div>

          {error ? <div className="audit-inline-error" role="alert"><Warning size={16} />{error}<Button size="1" variant="soft" onClick={onRetry}>重试</Button></div> : null}

          {filtered.length > 0 ? (
            <div className="audit-timeline">
              {filtered.map((event) => {
                const candidate = candidateById.get(event.candidate_id)
                return (
                  <article className="audit-event" key={event.id}>
                    <div className="audit-marker"><ClockCounterClockwise size={17} weight="bold" /></div>
                    <div className="audit-event-card">
                      <div className="audit-event-heading">
                        <div>
                          <Badge color={decisionColors[event.decision]}>{decisionLabels[event.decision]}</Badge>
                          <strong>{candidate?.shortId ?? '未知候选'} · {candidate?.businessUnit ?? '未分配营业单元'}</strong>
                        </div>
                        <time dateTime={event.created_at}>{new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(event.created_at))}</time>
                      </div>
                      <p className="audit-reason">{event.reason}</p>
                      <div className="audit-meta">
                        <span>{candidate?.category ?? '未知科目'}</span>
                        <span>修订 {event.from_revision} → {event.to_revision}</span>
                        <span>操作者：{event.actor}</span>
                        {candidate ? <Button size="1" variant="ghost" color="gray" onClick={() => onOpenCandidate(candidate)}><FileText size={14} />查看候选与证据</Button> : null}
                      </div>
                      {event.changes.length > 0 ? (
                        <ul className="audit-changes">
                          {event.changes.map((change, index) => (
                            <li key={`${event.id}:${change.field}:${index}`}>
                              <strong>{auditFieldLabels[change.field]}</strong>
                              <span>{formatAuditValue(change.field, change.previous_value)}</span>
                              <CaretRight size={13} />
                              <span>{formatAuditValue(change.field, change.new_value)}</span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {event.conflict_resolution ? <p className="audit-resolution"><strong>冲突处理依据</strong>{event.conflict_resolution}</p> : null}
                    </div>
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="empty-state audit-empty">
              <ClockCounterClockwise size={30} />
              <h2>没有匹配的操作记录</h2>
              <p>{events.length > 0 ? '请调整筛选条件或搜索词。' : '完成一次候选审核后，记录会显示在这里。'}</p>
            </div>
          )}

          {nextCursor ? (
            <div className="audit-load-more">
              <Button variant="outline" color="gray" disabled={loading} onClick={() => onLoadMore(nextCursor)}>
                <ArrowsClockwise className={loading ? 'state-spinner' : undefined} size={16} />
                {loading ? '正在加载' : '加载更多记录'}
              </Button>
            </div>
          ) : null}
        </>}
      </section>
    </>
  )
}

function Metric({ label, value, detail, icon, tone, primary = false }: { label: string; value: string; detail: string; icon: React.ReactNode; tone?: 'attention'; primary?: boolean }) {
  return (
    <article className={`metric ${primary ? 'primary' : ''} ${tone === 'attention' ? 'attention' : ''}`}>
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

function PersonalFinanceOverview({ candidates, onNavigate }: { candidates: Candidate[]; onNavigate: (page: Page) => void }) {
  const pending = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const evidenceCount = new Set(candidates.flatMap((candidate) => candidate.evidence.map((evidence) => evidence.id))).size
  const months = new Set(candidates.map((candidate) => candidate.accountingMonth).filter(Boolean)).size
  return (
    <>
      <PageHeader
        eyebrow="个人财务"
        title="完整个人财务对账"
        description="汇总当前已导入的账单、凭证和待审核事项；所有数字均可回到原始材料。"
        action={<Button onClick={() => onNavigate('review')}><ListChecks size={17} />处理待审核</Button>}
      />
      <section className="metric-grid" aria-label="个人财务材料概览">
        <Metric primary label="全部候选" value={`${candidates.length} 条`} detail={`${pending.length} 条仍待确认`} tone={pending.length > 0 ? 'attention' : undefined} icon={<ListChecks size={20} />} />
        <Metric label="原始材料" value={`${evidenceCount} 份`} detail="已按证据编号去重" icon={<FolderOpen size={20} />} />
        <Metric label="覆盖期间" value={`${months} 个月`} detail="来自当前 Core 候选事实" icon={<Table size={20} />} />
      </section>
      <section className="panel report-entry-panel">
        <div><h2>从真实材料继续</h2><p>先确认风险候选，再检查已导入材料和仍需补交的账单。</p></div>
        <div className="review-header-actions">
          <Button onClick={() => onNavigate('review')}>查看待审核</Button>
          <Button variant="outline" color="gray" onClick={() => onNavigate('files')}>查看材料总览</Button>
        </div>
      </section>
    </>
  )
}

function CompanyReports() {
  return (
    <>
      <PageHeader eyebrow="公司维度" title="各公司报表" description="公司报表会复用已确认的真实候选，并按公司主体隔离展示。" />
      <section className="panel planning-panel">
        <Database size={34} weight="light" />
        <div><h2>按公司主体汇总将在后续接入</h2><p>当前不生成推测数据；待 Core 提供稳定的公司主体字段后再展示资产、收支与对账状态。</p></div>
      </section>
    </>
  )
}

type PayrollLiveViews = {
  dashboard: PayrollReadResponse<PayrollDashboardData>
  materials: PayrollReadResponse<PayrollMaterialListData>
  batches: PayrollReadResponse<PayrollBatchListData>
  verification: PayrollReadResponse<PayrollVerificationListData>
}

const payrollMaterialStatusLabel = (status: string) => ({
  NEEDS_REVIEW: '待人工审核',
  REVIEWED: '已审核',
  REJECTED: '已拒绝',
}[status] ?? '状态待确认')

const payrollBatchStatusLabel = (status: string) => ({
  draft: '草稿',
  in_review: '审核中',
  approved: '已批准',
  locked: '已锁定',
}[status] ?? '状态待确认')

const payrollVerificationStatusLabel = (status: string) => ({
  matched: '已匹配',
  partial: '部分匹配',
  unmatched: '待核对',
}[status] ?? '状态待确认')

const maskPayrollRef = (value: string) => value.length <= 10
  ? value
  : `${value.slice(0, 4)}••••${value.slice(-4)}`

const payrollViewsAreConsistent = (
  status: PayrollReadResponse<PayrollStatusData>,
  views: PayrollLiveViews,
) => {
  const revisions = [
    status.data.projection_revision,
    views.dashboard.data.projection_revision,
    views.materials.data.projection_revision,
    views.batches.data.projection_revision,
    views.verification.data.projection_revision,
  ]
  const etags = [
    status.data.etag,
    views.dashboard.data.etag,
    views.materials.data.etag,
    views.batches.data.etag,
    views.verification.data.etag,
  ]
  const companies = [
    status.company_id,
    views.dashboard.company_id,
    views.materials.company_id,
    views.batches.company_id,
    views.verification.company_id,
  ]
  return revisions.every((revision) => revision === revisions[0])
    && etags.every((etag) => etag === etags[0])
    && companies.every((companyId) => companyId === companies[0])
}

async function readPayrollViews(): Promise<PayrollLiveViews> {
  const [dashboard, materials, batches, verification] = await Promise.all([
    api.getPayrollDashboard(),
    api.listPayrollMaterials(),
    api.listPayrollBatches(),
    api.listPayrollVerification(),
  ])
  return { dashboard, materials, batches, verification }
}

function PayrollVerificationStatus() {
  const [status, setStatus] = useState<PayrollReadResponse<PayrollStatusData> | null>(null)
  const [views, setViews] = useState<PayrollLiveViews | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [csrfToken, setCsrfToken] = useState<string | null>(null)
  const [selectedEvidence, setSelectedEvidence] = useState<string[]>([])
  const [selectedBatchId, setSelectedBatchId] = useState('')
  const [commandBusy, setCommandBusy] = useState(false)
  const [commandMessage, setCommandMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)
  const commandBusyRef = useRef(false)

  const loadPayroll = useCallback(async () => {
    setLoading(true)
    setError(null)
    setViews(null)
    try {
      const [sessionResponse, statusResponse] = await Promise.all([
        api.getSession(),
        api.getPayrollStatus(),
      ])
      setCsrfToken(sessionResponse.csrf_token)
      setStatus(statusResponse)
      if (!statusResponse.data.live_data_ready) return

      let nextViews = await readPayrollViews()
      if (!payrollViewsAreConsistent(statusResponse, nextViews)) nextViews = await readPayrollViews()
      if (!payrollViewsAreConsistent(statusResponse, nextViews)) {
        setError('数据正在刷新，请稍后重试')
        return
      }
      setViews(nextViews)
    } catch {
      setStatus(null)
      setError('工资服务暂不可用，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => void loadPayroll(), 0)
    return () => window.clearTimeout(timer)
  }, [loadPayroll])

  const dashboard = views?.dashboard.data.dashboard
  const materials = views?.materials.data.items ?? []
  const batches = views?.batches.data.items ?? []
  const verification = views?.verification.data
  const canVerifyReceipts = status?.data.live_data_ready === true
    && status.data.capabilities.commands_enabled
    && status.data.capabilities.allowed_actions.includes('VERIFY_RECEIPTS')
  const eligibleBatches = batches.filter((batch) => verification?.available_evidence.some(
    (evidence) => evidence.status === 'READY_FOR_MATCHING' && evidence.period === batch.pay_period,
  ))
  const selectedBatch = eligibleBatches.find((batch) => batch.batch_id === selectedBatchId)
    ?? (eligibleBatches.length === 1 ? eligibleBatches[0] : null)
  const evidenceForBatch = selectedBatch
    ? verification?.available_evidence.filter((evidence) => evidence.period === selectedBatch.pay_period) ?? []
    : []

  const togglePayrollEvidence = (artifactId: string) => {
    setSelectedEvidence((current) => current.includes(artifactId)
      ? current.filter((item) => item !== artifactId)
      : [...current, artifactId])
  }

  const submitPayrollVerification = async () => {
    if (commandBusyRef.current || !canVerifyReceipts || !selectedBatch || !csrfToken || selectedEvidence.length === 0) return
    commandBusyRef.current = true
    setCommandBusy(true)
    setCommandMessage(null)
    try {
      await api.verifyPayrollReceipts({
        batchId: selectedBatch.batch_id,
        expectedRevision: selectedBatch.version,
        sourceArtifactIds: selectedEvidence,
        csrfToken,
      })
      setCommandMessage({ tone: 'info', text: '请求已受理，正在刷新真实验证结果' })
      try {
        const refreshedViews = await readPayrollViews()
        if (!status || !payrollViewsAreConsistent(status, refreshedViews)) {
          setCommandMessage({ tone: 'info', text: '请求已受理，结果待刷新' })
        } else {
          setViews(refreshedViews)
          setSelectedEvidence([])
          setCommandMessage({ tone: 'info', text: '请求已受理，验证结果已刷新' })
        }
      } catch {
        setCommandMessage({ tone: 'info', text: '请求已受理，结果待刷新' })
      }
    } catch (requestError) {
      const statusCode = requestError instanceof ApiError ? requestError.status : 0
      const text = statusCode === 403
        ? '当前会话无权提交发放验证'
        : statusCode === 409
          ? '工资批次已更新，请刷新后重新核对'
          : statusCode === 422
            ? '所选证据不再可用，请刷新后重试'
            : '发放验证暂不可用，请稍后重试'
      setCommandMessage({ tone: 'error', text })
    } finally {
      commandBusyRef.current = false
      setCommandBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="工资模块"
        title="工资与发放验证"
        description="读取当前公司经过服务端隔离和脱敏的工资材料、批次与发放验证投影。"
      />

      {loading ? <LoadingState /> : error ? <ErrorState message={error} onRetry={loadPayroll} /> : null}

      {!loading && !error && status && !status.data.live_data_ready ? (
        <>
          <div className="payroll-status-banner" role="status">
            <Info size={20} weight="fill" />
            <div><strong>工资服务已连通，但正式数据投影尚未就绪</strong><span>只报告服务端真实状态，不用空值代替尚未生成的正式数据。</span></div>
            <Badge color="amber">未就绪</Badge>
          </div>
          {status.data.setup_summary?.provider_connected ? (
            <section className="panel payroll-setup-progress" aria-label="工资材料接入进度">
              <Database size={28} weight="light" />
              <div>
                <h2>服务已接通，待归属材料 {status.data.setup_summary.unassigned_material_count} 份</h2>
                <p>已识别可处理材料 {status.data.setup_summary.ready_material_count} 份，公司已映射 {status.data.setup_summary.company_mapped_material_count} 份；完成公司归属后生成正式工资投影。</p>
              </div>
            </section>
          ) : null}
          <section className="payroll-status-grid" aria-label="工资模块连接状态">
            <article className="payroll-status-card ready">
              <CheckCircle size={26} weight="fill" />
              <div><h2>当前可做</h2><strong>只读工资发布契约已部署</strong><p>服务连通状态可核验，正式业务投影生成后才会展示材料与批次。</p></div>
            </article>
            <article className="payroll-status-card blocked">
              <Warning size={26} weight="fill" />
              <div><h2>暂不可做</h2><strong>真实发薪和银行提交不可用</strong><p>页面没有付款、发薪或银行提交入口。</p></div>
            </article>
          </section>
        </>
      ) : null}

      {!loading && !error && dashboard && verification ? (
        <div className="payroll-live-page">
          <section className="panel payroll-live-section" aria-labelledby="payroll-material-summary">
            <div className="section-heading payroll-live-heading">
              <div><span>正式投影</span><h2 id="payroll-material-summary">真实材料汇总</h2></div>
              <Badge color={dashboard.materials_needing_review_count > 0 ? 'amber' : 'green'}>
                {dashboard.materials_needing_review_count > 0 ? '需要复核' : '已就绪'}
              </Badge>
            </div>
            <div className="payroll-metric-grid">
              <div><strong>材料 {dashboard.material_count} 份</strong><span>当前公司已归属材料</span></div>
              <div><strong>待人工审核 {dashboard.materials_needing_review_count} 份</strong><span>仍需会计核对</span></div>
              <div><strong>待归属 {dashboard.unassigned_material_count} 份</strong><span>仅展示聚合数量</span></div>
              <div><strong>批次 {dashboard.batch_count} 个</strong><span>当前公司工资批次</span></div>
              <div><strong>验证结果 {verification.items.length} 条</strong><span>真实发放核验投影</span></div>
            </div>
            {materials.length > 0 ? (
              <div className="payroll-record-list" aria-label="工资材料">
                {materials.map((material) => (
                  <article key={material.material_id}>
                    <div><strong>{material.period}</strong><span>{material.material_type === 'PAYROLL_SHEET' ? '工资表' : '受控工资材料'}</span></div>
                    <Badge color={material.status === 'NEEDS_REVIEW' ? 'amber' : 'green'}>{payrollMaterialStatusLabel(material.status)}</Badge>
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前公司暂无工资材料</p>}
          </section>

          <section className="panel payroll-live-section" aria-labelledby="payroll-batches-heading">
            <div className="section-heading payroll-live-heading"><div><span>批次</span><h2 id="payroll-batches-heading">公司内工资批次</h2></div></div>
            {batches.length > 0 ? (
              <div className="payroll-batch-grid">
                {batches.map((batch) => (
                  <article key={batch.batch_id}>
                    <div className="payroll-batch-title"><strong>{batch.pay_period}</strong><Badge color="gray">{payrollBatchStatusLabel(batch.status)}</Badge></div>
                    <dl>
                      <div><dt>人数</dt><dd>{batch.employee_count}</dd></div>
                      <div><dt>应发合计</dt><dd>{currency.format(minorToMajor(batch.gross_pay_minor))}</dd></div>
                      <div><dt>实发合计</dt><dd>{currency.format(minorToMajor(batch.net_pay_minor))}</dd></div>
                      <div><dt>异常</dt><dd>{batch.active_exception_count}</dd></div>
                    </dl>
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前公司暂无工资批次</p>}
          </section>

          <section className="panel payroll-live-section" aria-labelledby="payroll-verification-heading">
            <div className="section-heading payroll-live-heading"><div><span>受控核验</span><h2 id="payroll-verification-heading">发放验证结果</h2></div></div>
            {verification.items.length > 0 ? (
              <div className="payroll-record-list" aria-label="发放验证记录">
                {verification.items.map((item) => (
                  <article key={item.verification_id} className="payroll-verification-record">
                    <div><strong>{item.pay_period}</strong><span>批次 {maskPayrollRef(item.batch_id)}</span></div>
                    <Badge color={item.overall_status === 'matched' ? 'green' : 'amber'}>{payrollVerificationStatusLabel(item.overall_status)}</Badge>
                    {item.results.length > 0 ? (
                      <ul>
                        {item.results.map((result) => (
                          <li key={`${result.employee_id}-${result.account_id}`}>
                            <span>员工 {maskPayrollRef(result.employee_id)} · 账户 {maskPayrollRef(result.account_id)}</span>
                            <strong>{payrollVerificationStatusLabel(result.match_status)}</strong>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </article>
                ))}
              </div>
            ) : <p className="payroll-empty-state">当前还没有发放验证结果</p>}

            <div className="payroll-evidence-panel">
              <h3>可用于核验的真实证据</h3>
              {verification.available_evidence.length === 0 ? (
                <p className="payroll-evidence-required">请先导入发放回单/流水</p>
              ) : canVerifyReceipts ? (
                <div className="payroll-evidence-command">
                  {eligibleBatches.length > 1 ? (
                    <label>
                      <span>选择工资批次</span>
                      <select value={selectedBatchId} onChange={(event) => { setSelectedBatchId(event.target.value); setSelectedEvidence([]) }}>
                        <option value="">请选择</option>
                        {eligibleBatches.map((batch) => <option key={batch.batch_id} value={batch.batch_id}>{batch.pay_period}</option>)}
                      </select>
                    </label>
                  ) : null}
                  <div className="payroll-evidence-options">
                    {evidenceForBatch.map((evidence) => (
                      <label key={evidence.artifact_id}>
                        <input
                          type="checkbox"
                          checked={selectedEvidence.includes(evidence.artifact_id)}
                          onChange={() => togglePayrollEvidence(evidence.artifact_id)}
                        />
                        <span>{evidence.display_label}</span>
                      </label>
                    ))}
                  </div>
                  {selectedBatch ? (
                    <Button
                      disabled={commandBusy || selectedEvidence.length === 0}
                      onClick={() => void submitPayrollVerification()}
                    >
                      {commandBusy ? '正在提交' : '提交发放验证'}
                    </Button>
                  ) : null}
                </div>
              ) : (
                <ul>
                  {verification.available_evidence.map((evidence) => (
                    <li key={evidence.artifact_id}><ShieldCheck size={18} weight="fill" /><span>{evidence.display_label}</span></li>
                  ))}
                </ul>
              )}
              {commandMessage ? <p className={`payroll-command-message ${commandMessage.tone}`} role={commandMessage.tone === 'error' ? 'alert' : 'status'}>{commandMessage.text}</p> : null}
            </div>
          </section>
        </div>
      ) : null}
    </>
  )
}

function ReviewQueue({ candidates, onOpenCandidate, onUpdate, onRefresh, busyId, batchBusy, onBatchConfirm }: {
  candidates: Candidate[]
  onOpenCandidate: (candidate: Candidate) => void
  onUpdate: (candidate: Candidate, intent: CandidateUpdateIntent, corrections?: CandidateCorrections, conflictResolution?: string) => void
  onRefresh: () => void
  busyId: string | null
  batchBusy: boolean
  onBatchConfirm: (candidates: Candidate[]) => void
}) {
  const [sourceFilter, setSourceFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState<'all' | 'conflict' | 'incomplete' | 'ready'>('all')
  const [transferObjectFilter, setTransferObjectFilter] = useState('all')
  const [query, setQuery] = useState('')
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const bulkEligible = candidates.filter(isBulkEligible)
  const evidenceReminderCount = candidates.filter((candidate) => candidate.reviewRisks.some((risk) => materialNameFor(candidate, risk.code) !== null)).length
  const statusCounts = {
    all: candidates.length,
    conflict: candidates.filter((candidate) => candidate.conflict).length,
    incomplete: candidates.filter((candidate) => candidate.incomplete && !candidate.conflict).length,
    ready: bulkEligible.length,
  }
  const transferObjects = [...candidates.reduce((groups, candidate) => {
    const fields = summaryFields(candidate)
    const counterparty = counterpartyFor(candidate)
    const hasRelatedRisk = candidate.reviewRisks.some((risk) => risk.code === 'RELATED_ACCOUNT_STATEMENT_REQUIRED')
    const hasTransferRisk = candidate.reviewRisks.some((risk) => risk.code === 'TRANSFER_REVIEW_REQUIRED')
    const isTransfer = hasRelatedRisk || hasTransferRisk || /转账|提现|投资理财|余额互转|信用卡还款|信用借还/.test(fields[3] ?? '')
    if (!isTransfer || isPlatformInternalAccount(counterparty)) return groups
    const name = counterparty || '未识别对象'
    const current = groups.get(name) ?? {
      name,
      category: '身份待分类',
      candidates: [] as Candidate[],
      netMinor: 0,
      highestRisk: '常规复核',
    }
    current.candidates.push(candidate)
    current.netMinor += candidate.amountMinor
    if (candidate.conflict) current.highestRisk = '凭证或金额冲突'
    else if (hasRelatedRisk && current.highestRisk !== '凭证或金额冲突') current.highestRisk = '需关联另一侧流水'
    else if (hasTransferRisk && current.highestRisk === '常规复核') current.highestRisk = '需人工确认'
    groups.set(name, current)
    return groups
  }, new Map<string, { name: string; category: string; candidates: Candidate[]; netMinor: number; highestRisk: string }>()).values()]
    .sort((left, right) => right.candidates.length - left.candidates.length || left.name.localeCompare(right.name, 'zh-CN'))
  const filtered = [...candidates].sort((left, right) => {
    const rank = (candidate: Candidate) => candidate.conflict ? 0 : candidate.incomplete || candidate.reviewRisks.length > 0 ? 1 : 2
    return rank(left) - rank(right)
  }).filter((candidate) => {
    const matchesSource = sourceFilter === 'all' || candidate.source === sourceFilter
    const matchesStatus = statusFilter === 'all'
      || (statusFilter === 'conflict' && candidate.conflict)
      || (statusFilter === 'incomplete' && (candidate.incomplete || candidate.reviewRisks.length > 0) && !candidate.conflict)
      || (statusFilter === 'ready' && isBulkEligible(candidate))
    const matchesQuery = !normalizedQuery || [
      candidate.shortId,
      candidate.businessUnit,
      candidate.category,
      candidate.summary,
    ].some((value) => value.toLocaleLowerCase('zh-CN').includes(normalizedQuery))
    const matchesTransferObject = transferObjectFilter === 'all'
      || (transferObjectFilter === '未识别对象' ? !counterpartyFor(candidate) : counterpartyFor(candidate) === transferObjectFilter)
    return matchesSource && matchesStatus && matchesQuery && matchesTransferObject
  })
  return (
    <>
      <PageHeader
        eyebrow="人工确认队列"
        title="待审核候选"
        description="高置信度且无风险的账单可批量确认，其余只保留真正需要判断的项目。"
        action={<div className="review-header-actions"><Button disabled={batchBusy || bulkEligible.length === 0} onClick={() => onBatchConfirm(bulkEligible)}><ListChecks size={17} />一键审批 {bulkEligible.length} 条</Button><Button disabled={batchBusy} variant="outline" color="gray" onClick={onRefresh}><ArrowsClockwise size={17} />刷新</Button></div>}
      />
      {evidenceReminderCount > 0 ? (
        <div className="evidence-reminder" role="status">
          <Warning size={19} />
          <div><strong>{evidenceReminderCount} 条需补关联单据</strong><span>银行卡或信用账户支付需关联资金明细；内部转账需另一侧流水；酒店平台结算需匹配银行入账。</span></div>
        </div>
      ) : null}
      {transferObjects.length > 0 ? (
        <section className="transfer-object-section" aria-label="按转账对象筛选">
          <div className="panel-heading">
            <div><h2>按转账对象</h2><p>按摘要里的交易对方聚合，不用银行名称推断账户归属。</p></div>
            <Button variant="ghost" onClick={() => setTransferObjectFilter('all')}>全部对象</Button>
          </div>
          <div className="transfer-object-groups">
            {transferObjects.map((group) => (
              <button aria-label={`查看${group.name} ${group.candidates.length} 笔`} aria-pressed={transferObjectFilter === group.name} className={`transfer-object-card ${transferObjectFilter === group.name ? 'active' : ''}`} key={group.name} onClick={() => setTransferObjectFilter(group.name)} type="button">
                <span className="transfer-object-heading"><strong>{group.name}</strong><Badge color="amber">{group.category}</Badge></span>
                <span>{group.candidates.length} 笔</span>
                <span>净额 {currency.format(minorToMajor(group.netMinor))}</span>
                <small>关系待确认 · 最高风险：{group.highestRisk}</small>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      <div className="review-toolbar">
        <div className="queue-summary">
          <div>
            <span>阻断优先</span>
            <strong>{filtered.length} 条待处理</strong>
          </div>
          <div className="status-filters" role="group" aria-label="处理状态筛选">
            {([
              ['all', '全部'],
              ['conflict', '冲突'],
              ['incomplete', '风险审核'],
              ['ready', '可一键审批'],
            ] as const).map(([value, label]) => (
              <button aria-label={`${label} ${statusCounts[value]}`} aria-pressed={statusFilter === value} className={statusFilter === value ? 'active' : ''} key={value} onClick={() => setStatusFilter(value)} type="button">
                <span>{label}</span><strong>{statusCounts[value]}</strong>
              </button>
            ))}
          </div>
        </div>
        <div className="filter-bar">
          <div className="filter-tabs" role="group" aria-label="来源筛选">
            {[['all', '全部来源'], ...[...new Set(candidates.map((candidate) => candidate.source))].map((source) => [source, source])].map(([value, label]) => (
              <button aria-pressed={sourceFilter === value} className={sourceFilter === value ? 'active' : ''} key={value} onClick={() => setSourceFilter(value)} type="button">{label}</button>
            ))}
          </div>
          <TextField.Root aria-label="搜索候选编号、门店或科目" className="search-field" placeholder="搜索候选编号、门店或科目" value={query} onChange={(event) => setQuery(event.target.value)}>
            <TextField.Slot><MagnifyingGlass size={16} /></TextField.Slot>
          </TextField.Root>
        </div>
      </div>

      <section className="review-list" aria-label="候选数据列表">
        {filtered.length === 0 ? (
          <div className="empty-state">
            <CheckCircle size={34} weight="light" />
            <h2>当前筛选下没有待审核项</h2>
            <p>新的财务候选会在这里出现。</p>
          </div>
        ) : filtered.map((candidate) => (
          <article className={`candidate-card ${candidate.conflict ? 'has-conflict' : candidate.incomplete || candidate.reviewRisks.length > 0 ? 'is-incomplete' : 'is-ready'}`} key={candidate.id}>
            <div className="candidate-source">
              <SourceIcon source={candidate.source} />
              <div>
                <strong>{candidate.source}</strong>
                <span>{candidate.receivedAt}</span>
              </div>
            </div>
            <button className="candidate-body" onClick={() => onOpenCandidate(candidate)} type="button">
              <div className="candidate-tags">
                <Badge color="gray">{candidate.category}</Badge>
                {candidate.conflict ? <Badge color="red">金额或凭证冲突</Badge> : null}
                {candidate.incomplete ? <Badge color="amber">缺少归属月份</Badge> : null}
                {candidate.reviewRisks.length > 0 ? <Badge color="amber">风险项·需人工审核</Badge> : null}
              </div>
              <h2>{candidate.businessUnit} · {candidate.category}</h2>
              <p>{candidate.summary}</p>
              <div className="candidate-meta">
                <span>{candidate.businessUnit}</span>
                <span>{candidate.accountingMonth ?? '建议归入 2026-08'}</span>
                <span>置信度 {Math.round(candidate.confidence * 100)}%</span>
                {candidate.evidence.some((item) => item.kind === 'attachment') ? <span><Paperclip size={14} />{candidate.evidence.filter((item) => item.kind === 'attachment').length} 个附件</span> : null}
              </div>
              {candidate.reviewRisks[0] ? <div className="candidate-risk"><Warning size={14} />{candidate.reviewRisks[0].message}</div> : null}
            </button>
            <div className="candidate-amount">
              <span>提取金额</span>
              <strong>{currency.format(candidate.amount)}</strong>
              <small>{candidate.shortId}</small>
            </div>
            <div className="candidate-actions">
              <Button disabled={busyId === candidate.id} variant="soft" color="gray" onClick={() => onUpdate(candidate, 'IGNORE')}><X size={16} />忽略</Button>
              {candidate.conflict ? (
                <Button disabled={busyId === candidate.id} color="red" variant="soft" onClick={() => onOpenCandidate(candidate)}><Warning size={16} />处理冲突</Button>
              ) : candidate.incomplete ? (
                <Button disabled={busyId === candidate.id} color="amber" variant="soft" onClick={() => onOpenCandidate(candidate)}><Info size={16} />补全月份</Button>
              ) : candidate.reviewRisks.length > 0 ? (
                <Button disabled={busyId === candidate.id} color="amber" variant="soft" onClick={() => onOpenCandidate(candidate)}><Warning size={16} />人工审核</Button>
              ) : (
                <Button disabled={busyId === candidate.id} onClick={() => onUpdate(candidate, 'CONFIRM')}><Check size={16} />确认</Button>
              )}
            </div>
          </article>
        ))}
      </section>
    </>
  )
}

function SourceIcon({ source }: { source: Candidate['source'] }) {
  const initials: Record<string, string> = {
    Telegram: 'T',
    钉钉: '钉',
    微信: '微',
    支付宝: '支',
    Hermes: 'H',
    '中行账单（复核材料）': '银',
    照片凭证: '照',
    合成数据: '合',
  }
  return <span className={`source-icon source-${source}`}>{initials[source] || '?'}</span>
}

function evidenceLookupReference(candidate: Candidate): string {
  return candidate.summary.match(/\bTX-[0-9]{4,8}\b/)?.[0] ?? candidate.shortId
}

const CONTROLLED_PHOTO_EVIDENCE: Array<{ summary: string; digestPrefix: string }> = [
  { summary: '薇旭美团', digestPrefix: '920f69115b96' },
  { summary: '薇旭携程', digestPrefix: '29f7c422799c' },
  { summary: '景怡美团', digestPrefix: 'd9a2e8132642' },
]

function evidenceForBillConfirmation(candidate: Candidate): EvidenceReference[] {
  if (candidate.source === '中行账单（复核材料）') {
    const manualReview = candidate.evidence.find((item) => item.original_filename === 'boc-manual-review.xlsx')
    const spreadsheet = candidate.evidence.find((item) => item.media_type.includes('spreadsheet'))
    return manualReview ? [manualReview] : spreadsheet ? [spreadsheet] : []
  }
  if (candidate.source === '照片凭证') {
    const mapping = CONTROLLED_PHOTO_EVIDENCE.find((item) => candidate.summary.includes(item.summary))
    const matchingImage = mapping
      ? candidate.evidence.find((item) => item.sha256?.startsWith(mapping.digestPrefix))
      : undefined
    const firstImage = candidate.evidence.find((item) => item.media_type.startsWith('image/'))
    return matchingImage ? [matchingImage] : firstImage ? [firstImage] : []
  }
  return candidate.evidence.slice(0, 1)
}

function billIdentityFields(fields: Array<{ label: string; value: string }>) {
  const priorities = [
    ['交易时间', '交易日期', '交易日', '记账日期', '记账日', '日期', '时间'],
    ['金额(元)', '交易金额', '账单金额', '付款金额', '收款金额', '金额'],
    ['对方名称', '交易对方', '对方户名', '收款人', '付款人', '商户名称', '商户', '户名'],
  ]
  const selected: Array<{ label: string; value: string }> = []
  for (const aliases of priorities) {
    const match = fields.find((field) => aliases.some((alias) => field.label.trim().includes(alias)))
    if (match && !selected.includes(match)) selected.push(match)
  }
  return selected
}

function EvidencePreviewPanel({ evidence, reference }: {
  evidence: EvidenceReference
  reference: string
}) {
  const requestKey = `${evidence.id}:${reference}`
  const [result, setResult] = useState<{
    key: string
    preview: EvidencePreview | null
    error: string | null
  }>({ key: '', preview: null, error: null })

  useEffect(() => {
    let active = true
    api.getEvidencePreview(evidence.id, reference)
      .then((value) => { if (active) setResult({ key: requestKey, preview: value, error: null }) })
      .catch((reason: unknown) => {
        if (active) setResult({
          key: requestKey,
          preview: null,
          error: reason instanceof Error ? reason.message : '证据内容暂时无法读取',
        })
      })
    return () => { active = false }
  }, [evidence.id, reference, requestKey])

  const preview = result.key === requestKey ? result.preview : null
  const error = result.key === requestKey ? result.error : null

  const filename = evidence.original_filename ?? (evidence.kind === 'message' ? '消息原文' : '原始文件')
  const downloadHref = `/api/v1/evidence/${encodeURIComponent(evidence.id)}/content`

  return (
    <article className="evidence-preview-card">
      <header>
        <div>
          {preview?.kind === 'image' ? <ImageSquare size={17} /> : preview?.kind === 'spreadsheet' ? <FileXls size={17} /> : <FileText size={17} />}
          <span>{preview?.filename ?? filename}</span>
        </div>
        <a href={downloadHref} aria-label={`下载原文件：${filename}`} title="下载原文件">
          <DownloadSimple size={16} />
        </a>
      </header>

      {!preview && !error ? (
        <div className="evidence-preview-state" role="status"><ArrowsClockwise className="state-spinner" size={17} />正在读取证据内容</div>
      ) : null}
      {error ? (
        <div className="evidence-preview-state error"><Warning size={17} />{error}</div>
      ) : null}
      {preview?.kind === 'image' ? (
        <img className="evidence-image" src={preview.data_url} alt={`${preview.filename} 原始证据`} />
      ) : null}
      {preview?.kind === 'text' ? (
        <pre className="evidence-text">{preview.text}</pre>
      ) : null}
      {preview?.kind === 'spreadsheet' && preview.matched ? (
        <div className="evidence-records">
          {preview.records.map((record) => (
            <section key={`${record.sheet}-${record.row_number}`}>
              <div className="evidence-record-meta"><span>账单 {preview.reference ?? reference}</span><small>识别摘要</small></div>
              <dl>
                {billIdentityFields(record.fields).map((field, index) => (
                  <div key={`${index}-${field.label}`}><dt>{field.label}</dt><dd>{field.value}</dd></div>
                ))}
              </dl>
            </section>
          ))}
        </div>
      ) : null}
      {preview?.kind === 'spreadsheet' && !preview.matched && preview.fallback ? (
        <div className="evidence-sheet-fallback">
          <div className="evidence-record-meta"><span>{preview.fallback.sheet}</span><small>内容预览</small></div>
          <div className="evidence-sheet-scroll">
            <table>
              <tbody>
                {preview.fallback.rows.map((row) => (
                  <tr key={row.row_number}><th scope="row">{row.row_number}</th>{row.cells.map((cell, index) => <td key={index}>{cell}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
      {preview?.kind === 'unsupported' ? (
        <div className="evidence-preview-state"><Info size={17} />{preview.reason}</div>
      ) : null}
    </article>
  )
}

function CandidateDialog({ candidate, onClose, onUpdate, busy, detailLoading }: {
  candidate: Candidate
  onClose: () => void
  onUpdate: (candidate: Candidate, intent: CandidateUpdateIntent, corrections?: CandidateCorrections, conflictResolution?: string) => void
  busy: boolean
  detailLoading: boolean
}) {
  const [businessUnit, setBusinessUnit] = useState(candidate.businessUnit)
  const [category, setCategory] = useState(candidate.category)
  const [amount, setAmount] = useState(candidate.amount.toFixed(2))
  const [accountingMonth, setAccountingMonth] = useState(candidate.accountingMonth ?? '')
  const [conflictResolution, setConflictResolution] = useState('')
  const readOnly = ['CONFIRMED', 'IGNORED', 'SUPERSEDED'].includes(candidate.status)

  const dialogTitle = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '查看已确认候选'
      : candidate.status === 'IGNORED'
        ? '查看已忽略候选'
        : '查看已被取代候选'
    : candidate.conflict
      ? '处理金额或凭证冲突'
      : candidate.incomplete
        ? '补全候选信息'
        : '核对候选数据'
  const statusTitle = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '候选已确认'
      : candidate.status === 'IGNORED'
        ? '候选已忽略'
        : '当前修订已被取代'
    : candidate.conflict
      ? '必须先说明采用哪份证据'
      : candidate.incomplete
        ? '必须补全归属月份'
        : '字段完整，可以确认'
  const statusDescription = readOnly
    ? candidate.status === 'CONFIRMED'
      ? '字段在此处只读；后续变化必须形成新的追加事件。'
      : candidate.status === 'IGNORED'
        ? '候选已从草稿数据中排除，原始证据和审核记录仍保留。'
        : '该修订仅用于历史追溯，不能覆盖后续修订。'
    : candidate.conflict
      ? '处理依据会随审核事件一起留存。'
      : candidate.incomplete
        ? '系统建议仅供参考，请人工核对。'
        : '确认后候选会进入月度对账草稿。'

  const parsedAmount = Number(amount)
  const formComplete = businessUnit.trim().length > 0
    && category.trim().length > 0
    && Number.isFinite(parsedAmount)
    && accountingMonth.length > 0
  const confirmBlocked = readOnly || busy || !formComplete || (candidate.conflict && conflictResolution.trim().length === 0)

  const submitCorrection = () => {
    if (confirmBlocked) return
    onUpdate(candidate, candidate.conflict ? 'RESOLVE_CONFLICT' : 'CONFIRM', {
      business_unit: businessUnit.trim(),
      category: category.trim(),
      amount_minor: majorToMinor(parsedAmount),
      accounting_month: accountingMonth,
    }, candidate.conflict ? conflictResolution.trim() : undefined)
  }

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <Dialog.Content className="candidate-dialog" maxWidth="1120px">
        <div className="dialog-kicker"><SourceIcon source={candidate.source} /><span>{candidate.source} · {candidate.receivedAt} · {candidate.shortId}</span></div>
        <Dialog.Title>{dialogTitle}</Dialog.Title>
        <Dialog.Description>{readOnly ? '只读核对原始证据、当前字段与追加式审核历史。' : '左侧核对原始证据，右侧确认入账字段。原始消息不会被覆盖。'}</Dialog.Description>
        <div className={`dialog-status ${readOnly ? candidate.status === 'CONFIRMED' ? 'ready' : 'terminal' : candidate.conflict ? 'danger' : candidate.incomplete ? 'warning' : 'ready'}`}>
          {readOnly && candidate.status !== 'CONFIRMED' ? <Info size={19} weight="fill" /> : candidate.conflict ? <Warning size={19} weight="fill" /> : candidate.incomplete ? <Info size={19} weight="fill" /> : <CheckCircle size={19} weight="fill" />}
          <div>
            <strong>{statusTitle}</strong>
            <span>{statusDescription}</span>
          </div>
        </div>
        <div className="dialog-layout">
          <section className="dialog-evidence-pane" aria-labelledby="evidence-heading">
            <span className="section-label" id="evidence-heading">账单凭证</span>
            <div className="evidence-box">
              <blockquote>{candidate.summary}</blockquote>
              <div className="evidence-previews">
                {evidenceForBillConfirmation(candidate).map((item) => <EvidencePreviewPanel evidence={item} reference={evidenceLookupReference(candidate)} key={item.id} />)}
              </div>
            </div>
          </section>
          <section className="dialog-fields-pane" aria-labelledby="fields-heading">
            <span className="section-label" id="fields-heading">提取字段</span>
            <div className="field-grid">
              <label htmlFor="candidate-business-unit"><span>营业单元</span><TextField.Root id="candidate-business-unit" readOnly={readOnly} value={businessUnit} onChange={(event) => setBusinessUnit(event.target.value)} /></label>
              <label htmlFor="candidate-category"><span>科目</span><TextField.Root id="candidate-category" readOnly={readOnly} value={category} onChange={(event) => setCategory(event.target.value)} /></label>
              <label htmlFor="candidate-amount"><span>金额</span><TextField.Root id="candidate-amount" inputMode="decimal" readOnly={readOnly} value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
              <label>
                <span id="candidate-month-label">归属月份</span>
                <Select.Root disabled={readOnly} value={accountingMonth} onValueChange={setAccountingMonth}>
                  <Select.Trigger aria-labelledby="candidate-month-label" placeholder="请选择归属月份" />
                  <Select.Content><Select.Item value="2026-08">2026 年 8 月</Select.Item><Select.Item value="2026-07">2026 年 7 月</Select.Item></Select.Content>
                </Select.Root>
              </label>
            </div>
            {candidate.conflict && !readOnly ? <>
              <label className="conflict-resolution-field" htmlFor="candidate-conflict-resolution">
                <span>冲突处理依据</span>
                <TextArea id="candidate-conflict-resolution" placeholder="例如：以银行电子回单金额为准" value={conflictResolution} onChange={(event) => setConflictResolution(event.target.value)} resize="vertical" />
              </label>
              <div className="blocking-note"><Warning size={18} weight="fill" /><span><strong>需要记录处理依据</strong>说明采用哪份证据以及原因，提交后将以追加事件保留。</span></div>
            </> : null}
            {candidate.incomplete && !readOnly ? <div className="blocking-note amber"><Info size={18} weight="fill" /><span><strong>月份为系统建议</strong>请确认归属月份后再提交。</span></div> : null}
          </section>
        </div>

        <section className="candidate-history" aria-labelledby="candidate-history-heading">
          <div className="candidate-history-heading">
            <span className="section-label" id="candidate-history-heading">审核历史</span>
            <span>{candidate.reviewEvents.length} 条追加记录</span>
          </div>
          {detailLoading ? (
            <div className="candidate-history-loading" role="status"><ArrowsClockwise className="state-spinner" size={17} />正在读取审核历史</div>
          ) : candidate.reviewEvents.length > 0 ? (
            <ol>
              {candidate.reviewEvents.map((event) => (
                <li key={event.id}>
                  <span className="candidate-history-sequence">{event.sequence}</span>
                  <div>
                    <div className="candidate-history-meta">
                      <Badge color={decisionColors[event.decision]}>{decisionLabels[event.decision]}</Badge>
                      <time dateTime={event.created_at}>{new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(event.created_at))}</time>
                    </div>
                    <strong>{event.reason}</strong>
                    <span>修订 {event.from_revision} → {event.to_revision} · {event.changes.map((change) => auditFieldLabels[change.field]).join('、') || '无字段变化'}</span>
                    {event.conflict_resolution ? <small>冲突依据：{event.conflict_resolution}</small> : null}
                  </div>
                </li>
              ))}
            </ol>
          ) : <p className="candidate-history-empty">此候选尚无审核事件。</p>}
        </section>
        <div className="dialog-actions">
          <Button variant="soft" color="gray" onClick={onClose}>{readOnly ? '关闭' : '取消'}</Button>
          {!readOnly ? <>
            <Button disabled={busy} variant="outline" color="gray" onClick={() => onUpdate(candidate, 'IGNORE')}>忽略候选</Button>
            <Button disabled={confirmBlocked} onClick={submitCorrection}>{candidate.conflict ? '解决冲突并确认' : '保存更正并确认'}</Button>
          </> : null}
        </div>
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
  columnHelper.group({
    id: 'business-unit',
    header: '营业单元',
    columns: [columnHelper.accessor('unit', { header: '门店', cell: (info) => <strong>{info.getValue()}</strong> })],
  }),
  columnHelper.group({
    id: 'operating-expenses',
    header: '运营支出',
    columns: [
      columnHelper.accessor('waterMinor', { header: '水费', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
      columnHelper.accessor('taxMinor', { header: '税费', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
      columnHelper.accessor('linenMinor', { header: '布草', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
      columnHelper.accessor('bottledWaterMinor', { header: '瓶装水', cell: (info) => currency.format(minorToMajor(info.getValue())) }),
    ],
  }),
  columnHelper.group({
    id: 'bank-receipts',
    header: '银行收款',
    columns: [columnHelper.accessor('receiptsMinor', { header: '入账金额', cell: (info) => currency.format(minorToMajor(info.getValue())) })],
  }),
  columnHelper.group({
    id: 'draft-state',
    header: '处理状态',
    columns: [columnHelper.accessor('readiness', {
      header: '草稿状态',
      cell: (info) => {
        const value = info.getValue()
        return <Badge color={value === '可生成' ? 'green' : 'amber'}>{value}</Badge>
      },
    })],
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
  const expensesTotalMinor = rows.reduce((sum, row) => sum + row.waterMinor + row.taxMinor + row.linenMinor + row.bottledWaterMinor, 0)
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
        <Metric label="运营支出" value={currency.format(minorToMajor(expensesTotalMinor))} detail={`跨 ${rows.length} 个营业单元`} icon={<Bank size={20} />} />
        <Metric label="银行收款" value={currency.format(minorToMajor(receiptsTotalMinor))} detail="与运营支出分开统计" icon={<CloudArrowUp size={20} />} />
        <Metric label="已确认来源" value={`${confirmed.length} 条`} detail="均可回溯至原始证据" icon={<CheckCircle size={20} />} />
        <Metric label="待处理" value={`${data?.blockers.length ?? 0} 条`} detail={ready ? '无草稿阻断项' : '需先处理阻断项'} icon={<Warning size={20} />} tone={ready ? undefined : 'attention'} />
      </section>

      <section className="panel table-panel">
        <div className="panel-heading">
          <div><h2>营业单元汇总</h2><p>只展示审核通过或既有数据库中的数据</p></div>
          <Button disabled={!ready || generating} onClick={onGenerate}><FileText size={17} />{generating ? '正在提交' : '生成对账草稿'}</Button>
        </div>
        <div className="desktop-table-wrap">
          <table>
            <thead>{table.getHeaderGroups().map((group) => <tr key={group.id}>{group.headers.map((header) => <th colSpan={header.colSpan} data-column={header.column.id} key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
            <tbody>{table.getRowModel().rows.map((row) => <tr key={row.id}>{row.getVisibleCells().map((cell) => <td data-column={cell.column.id} key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
            <tfoot><tr><td>本月合计</td><td colSpan={4}>{currency.format(minorToMajor(expensesTotalMinor))}</td><td>{currency.format(minorToMajor(receiptsTotalMinor))}</td><td><Badge color={ready ? 'green' : 'red'}>{ready ? '就绪' : '阻断'}</Badge></td></tr></tfoot>
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

function EvidenceUnlockDialog({ evidence, csrfToken, onClose, onUnlocked }: {
  evidence: EvidenceReference
  csrfToken: string
  onClose: () => void
  onUnlocked: () => Promise<void>
}) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy || password.length === 0 || !evidence.source_ref) return
    setBusy(true)
    setError(null)
    const submittedPassword = { value: password }
    let unlocked = false
    setPassword('')
    try {
      await api.unlockEvidence({
        sourceRef: evidence.source_ref,
        password: submittedPassword.value,
        csrfToken,
      })
      unlocked = true
    } catch (unlockError) {
      setError(unlockError instanceof Error ? unlockError.message : '账单解锁失败，请检查密码后重试')
    } finally {
      submittedPassword.value = ''
      setPassword('')
      setBusy(false)
    }
    if (!unlocked) return
    onClose()
    await onUnlocked()
  }

  return (
    <Dialog.Root open onOpenChange={(open) => {
      if (!open && !busy) {
        setPassword('')
        setError(null)
        onClose()
      }
    }}>
      <Dialog.Content maxWidth="460px">
        <Dialog.Title>输入账单解压密码</Dialog.Title>
        <Dialog.Description>密码只用于本次解锁请求，提交后立即从输入框清除。</Dialog.Description>
        <form className="evidence-unlock-form" onSubmit={(event) => void submit(event)}>
          <label htmlFor="evidence-unlock-password">解压密码</label>
          <TextField.Root
            autoComplete="off"
            autoFocus
            id="evidence-unlock-password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-describedby={error ? 'evidence-unlock-error' : undefined}
          />
          {error ? <div className="auth-error" id="evidence-unlock-error" role="alert"><Warning size={17} />{error}</div> : null}
          <div className="auth-actions">
            <Button type="button" variant="outline" color="gray" disabled={busy} onClick={() => {
              setPassword('')
              setError(null)
              onClose()
            }}>取消</Button>
            <Button type="submit" disabled={busy || password.length === 0}>{busy ? '正在解锁' : '解锁账单'}</Button>
          </div>
        </form>
      </Dialog.Content>
    </Dialog.Root>
  )
}

function FilesAndConnections({ candidates, connections, csrfToken, onOpenCandidate, onRefresh, onNotice }: {
  candidates: Candidate[]
  connections: ConnectionStatus[]
  csrfToken: string | null
  onOpenCandidate: (candidate: Candidate) => void
  onRefresh: () => Promise<boolean>
  onNotice: (notice: Notice) => void
}) {
  const [unlockEvidence, setUnlockEvidence] = useState<EvidenceReference | null>(null)
  const connection = (id: ConnectionStatus['id']) => connections.find((item) => item.id === id)
  const evidenceLibrary = [...candidates.reduce((items, candidate) => {
    for (const evidence of candidate.evidence) {
      const contentKey = evidence.sha256 ? `sha256:${evidence.sha256.toLowerCase()}` : `id:${evidence.id}`
      const unlockKey = evidence.unlock_status || evidence.source_ref
        ? `:unlock:${evidence.unlock_status ?? 'UNKNOWN'}:${evidence.source_ref ?? 'none'}`
        : ''
      const dedupeKey = `${contentKey}${unlockKey}`
      const current = items.get(dedupeKey) ?? {
        evidence,
        candidates: [] as Candidate[],
        sources: new Set<string>(),
        periods: new Set<string>(),
      }
      if (!current.candidates.some((item) => item.id === candidate.id)) current.candidates.push(candidate)
      current.sources.add(candidate.source)
      if (candidate.accountingMonth) current.periods.add(candidate.accountingMonth)
      items.set(dedupeKey, current)
    }
    return items
  }, new Map<string, { evidence: EvidenceReference; candidates: Candidate[]; sources: Set<string>; periods: Set<string> }>()).values()]
  const materialGaps = [...candidates.reduce((items, candidate) => {
    for (const risk of candidate.reviewRisks) {
      if (!materialRiskCodes.has(risk.code)) continue
      const material = materialNameFor(candidate, risk.code)
      if (!material) continue
      const period = candidate.accountingMonth ?? ''
      const key = `${risk.code}:${material}:${period}`
      const current = items.get(key) ?? { material, period, reasons: new Set<string>(), candidates: new Set<string>() }
      current.reasons.add(risk.message)
      current.candidates.add(candidate.id)
      items.set(key, current)
    }
    return items
  }, new Map<string, { material: string; period: string; reasons: Set<string>; candidates: Set<string> }>()).values()]
  return (
    <>
      <PageHeader
        eyebrow="服务与文件"
        title="文件与连接"
        description="状态来自同源 API。界面不显示令牌或其他敏感凭据。"
        action={<Button variant="outline" color="gray" onClick={onRefresh}><ArrowsClockwise size={17} />重新检查</Button>}
      />

      <section className="panel evidence-library-panel">
        <div className="panel-heading"><div><h2>已导入账单与凭证</h2><p>按证据编号去重，展开后可进入关联候选查看原始内容。</p></div><Badge color="gray">{evidenceLibrary.length} 份</Badge></div>
        {evidenceLibrary.length > 0 ? (
          <div className="evidence-library-list">
            {evidenceLibrary.map((item) => {
              const hasPending = item.candidates.some((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
              const allConfirmed = item.candidates.every((candidate) => candidate.status === 'CONFIRMED')
              const status = hasPending ? '含待审核' : allConfirmed ? '已确认' : '已归档'
              return (
                <article className="evidence-library-item" key={item.evidence.id}>
                  <div className="evidence-library-title"><FileText size={20} /><div><strong>{item.evidence.original_filename ?? (item.evidence.kind === 'message' ? '消息原文' : '原始文件')}</strong><span>{[...item.sources].join('、')}</span></div><Badge color={hasPending ? 'amber' : allConfirmed ? 'green' : 'gray'}>{status}</Badge></div>
                  <div className="evidence-library-meta"><span>{[...item.periods].map(accountingMonthLabel).join('、') || '期间待确认'}</span><span>证据 {item.evidence.id}</span></div>
                  {item.evidence.unlock_status === 'PASSWORD_REQUIRED' ? (
                    <Button className="evidence-unlock-button" size="1" variant="soft" color="amber" disabled={!csrfToken || !item.evidence.source_ref} onClick={() => setUnlockEvidence(item.evidence)}>输入解压密码</Button>
                  ) : null}
                  <details>
                    <summary>关联 {item.candidates.length} 条候选</summary>
                    <div className="evidence-candidate-links">
                      {item.candidates.map((candidate) => <button key={candidate.id} onClick={() => onOpenCandidate(candidate)} type="button">{candidate.shortId} · {candidate.businessUnit} · {candidate.category}</button>)}
                    </div>
                  </details>
                </article>
              )
            })}
          </div>
        ) : <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚无已导入材料</h2><p>Core 返回的候选证据会显示在这里。</p></div>}
      </section>

      <section className="panel material-gap-panel">
        <div className="panel-heading"><div><h2>待补账单清单</h2><p>只展示 Core 明确标记的资金、关联账户和酒店结算材料缺口。</p></div><Badge color={materialGaps.length > 0 ? 'amber' : 'green'}>{materialGaps.length} 项</Badge></div>
        {materialGaps.length > 0 ? (
          <div className="material-gap-list">
            {materialGaps.map((gap) => (
              <article className="material-gap-card" key={`${gap.material}:${gap.period}`}>
                <Warning size={20} />
                <div><strong>{gap.material}</strong><span>{accountingMonthLabel(gap.period || null)}</span><span>影响 {gap.candidates.size} 条记录</span><p>{[...gap.reasons].join('；')}</p></div>
              </article>
            ))}
          </div>
        ) : <div className="empty-state compact-empty"><CheckCircle size={34} weight="light" /><h2>当前没有明确材料缺口</h2><p>这里只依据 Core 风险事实，不推测缺失账单。</p></div>}
      </section>

      <div className="connection-grid">
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon onedrive"><CloudArrowUp size={24} weight="fill" /></div><div><h2>OneDrive Personal</h2><p>应用专用文件夹</p></div><ConnectionBadge connection={connection('onedrive_appfolder')} /></div>
          <p>仅访问 <code>Apps/LedgerBridge</code>，不读取 OneDrive 中的其他文件。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>计划权限：Files.ReadWrite.AppFolder</span></div>
          <Button disabled>连接功能尚未开放</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon hermes"><Database size={24} weight="fill" /></div><div><h2>Hermes 消息入口</h2><p>Telegram、钉钉、微信</p></div><ConnectionBadge connection={connection('hermes_ingress')} /></div>
          <p>只处理启用后的主账号私聊。家庭账号、群聊和历史消息均不在范围内。</p>
          <div className="permission-line"><ShieldCheck size={17} /><span>附件在消息入口即时提取与留证</span></div>
          <Button variant="soft" color="gray" disabled>规则由后台配置</Button>
        </section>
        <section className="panel connection-card">
          <div className="connection-title"><div className="service-icon office"><FileText size={24} weight="fill" /></div><div><h2>LibreOffice 计算服务</h2><p>Hermes 后台进程</p></div><ConnectionBadge connection={connection('libreoffice_worker')} /></div>
          <p>在临时副本上重算工作簿，检查公式错误和关键值，不覆盖原始文件。</p>
          <div className="permission-line"><Info size={17} /><span>结果标记为 LibreOffice 已验证</span></div>
          <Button variant="soft" color="gray" disabled>策略由后台配置</Button>
        </section>
      </div>

      <section className="panel files-panel">
        <div className="panel-heading"><div><h2>最近的工作簿</h2><p>连接 OneDrive 后显示应用文件夹中的版本</p></div><Button variant="outline" color="gray" disabled><CloudArrowUp size={17} />上传副本</Button></div>
        <div className="empty-state compact-empty"><FolderOpen size={34} weight="light" /><h2>尚未连接文件来源</h2><p>连接后，系统会显示可用于月度对账的工作簿副本。</p></div>
      </section>

      {unlockEvidence && csrfToken ? (
        <EvidenceUnlockDialog
          evidence={unlockEvidence}
          csrfToken={csrfToken}
          onClose={() => setUnlockEvidence(null)}
          onUnlocked={async () => {
            const refreshed = await onRefresh()
            onNotice(refreshed
              ? { tone: 'success', message: '账单已解锁，数据已刷新' }
              : { tone: 'info', message: '已解锁，但列表刷新失败，请重试刷新' })
          }}
        />
      ) : null}
    </>
  )
}

export default App
