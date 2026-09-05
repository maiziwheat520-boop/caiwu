import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import {
  Badge,
  Button,
  Dialog,
  DropdownMenu,
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
  FileXls,
  Fingerprint,
  House,
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
  api,
  ApiError,
  minorToMajor,
} from './api'
import type {
  AccountingDimensions,
  AuthResult,
  AuthStatus,
  Candidate,
  CandidateCorrections,
  CandidateDetail,
  ClassificationGroup,
  ClassificationTarget,
  ConnectionStatus,
  Notice,
  Page,
  PersonalBankStatement,
  PersonalBankTransactionsResponse,
  Reconciliation as ReconciliationData,
  ReviewEvent,
  Session,
} from './types'
import { ErrorState, LoadingState, Metric, PageHeader, StatusLine } from './shared/PagePrimitives'
import { currency } from './shared/format'
import { AuditLog } from './audit/AuditLog'
import { CandidateDialog } from './candidates/CandidateDialog'
import { toCandidate } from './candidates/candidateMapping'
import {
  counterpartyFor,
  isPlatformInternalAccount,
  materialNameFor,
  summaryFields,
} from './personal-finance/personalFinanceRules'
import { FilesAndConnections } from './connections/FilesAndConnections'
import { PersonalFinanceOverview } from './personal-finance/PersonalFinanceOverview'
import { SourceIcon } from './candidates/candidatePresentation'
import { accountingMonthLabel, type CandidateUpdateIntent } from './candidates/candidateLabels'
import { CompanyBankStatementReviewPanel } from './company-reports/CompanyBankStatementReviewPanel'
import { CompanyTransactionClassificationPanel } from './company-reports/CompanyTransactionClassificationPanel'

const CompanyReportsPage = lazy(() => import('./company-reports/CompanyReportsPage')
  .then((module) => ({ default: module.CompanyReportsPage })))
const OriginalReconciliationPage = lazy(() => import('./original-reconciliation/OriginalReconciliationPage')
  .then((module) => ({ default: module.OriginalReconciliationPage })))
const PayrollWorkspacePage = lazy(() => import('./payroll/PayrollWorkspacePage')
  .then((module) => ({ default: module.PayrollWorkspacePage })))

const CURRENT_MONTH = '2026-08'
const CLASSIFICATION_GROUPS_UNAVAILABLE_NOTICE = '同类批量归类暂不可用，可继续逐笔审核'

const navigation: Array<{ id: Page; label: string; icon: typeof House }> = [
  { id: 'overview', label: '概览', icon: House },
  { id: 'payroll', label: '工资与发放验证', icon: FileXls },
  { id: 'personal-finance', label: '完整个人财务对账', icon: Bank },
  { id: 'reconciliation', label: '月度对账', icon: Table },
  { id: 'company-reports', label: '各公司报表', icon: Database },
]

const pagePaths: Record<Page, string> = {
  overview: '/overview',
  'personal-finance': '/personal-finance',
  review: '/review',
  reconciliation: '/reconciliation',
  'original-reconciliation': '/original-reconciliation',
  'company-reports': '/company-reports',
  payroll: '/payroll',
  files: '/files',
  audit: '/audit',
}

function pageFromPath(pathname: string): Page {
  if (pathname === pagePaths['original-reconciliation']) return 'reconciliation'
  if (pathname === pagePaths.review || pathname === pagePaths.files) return 'overview'
  const entry = Object.entries(pagePaths).find(([, path]) => path === pathname)
  return entry ? entry[0] as Page : 'overview'
}

function scrollToOverviewSection(anchor: '#overview-summary' | '#review' | '#files') {
  const scroll = () => document.getElementById(anchor.slice(1))?.scrollIntoView?.({ block: 'start' })
  if (typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(scroll)
  } else {
    scroll()
  }
}


function isBulkEligible(candidate: Candidate, classificationGroups: ClassificationGroup[]): boolean {
  const groupedMember = classificationGroups
    .flatMap((group) => group.members)
    .find((member) => member.candidate_ref === candidate.id)
  return candidate.status === 'PENDING'
    && candidate.confidence >= 0.9
    && candidate.blockers.length === 0
    && candidate.reviewRisks.length === 0
    && !candidate.incomplete
    && !candidate.conflict
    && (!groupedMember || (groupedMember.one_click_eligible && !groupedMember.amount_outlier))
}

const AUDIT_CANDIDATE_FETCH_CONCURRENCY = 6

/**
 * The audit log only labels the events it renders, so it needs those events'
 * candidates -- not every candidate in the ledger.  Fetching the referenced
 * ones keeps the cost proportional to one page of events instead of walking
 * the whole candidate collection into the browser.
 */
async function fetchCandidatesByIds(ids: string[]): Promise<CandidateDetail[]> {
  const found: CandidateDetail[] = []
  const queue = [...ids]
  const workers = Array.from(
    { length: Math.min(AUDIT_CANDIDATE_FETCH_CONCURRENCY, queue.length) },
    async () => {
      for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
        try {
          found.push(await api.getCandidate(id))
        } catch {
          // A single unreadable candidate must not blank the whole audit page;
          // the row falls back to its "unknown candidate" labels.
        }
      }
    },
  )
  await Promise.all(workers)
  return found
}

function App() {
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null)
  const [authLoading, setAuthLoading] = useState(true)
  const [authError, setAuthError] = useState<string | null>(null)
  const [page, setPage] = useState<Page>(() => pageFromPath(window.location.pathname))
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [classificationGroups, setClassificationGroups] = useState<ClassificationGroup[]>([])
  const [classificationGroupsAvailable, setClassificationGroupsAvailable] = useState(false)
  const [session, setSession] = useState<Session | null>(null)
  const [reconciliation, setReconciliation] = useState<ReconciliationData | null>(null)
  const selectedMonth = CURRENT_MONTH
  const [connections, setConnections] = useState<ConnectionStatus[]>([])
  const [reviewEvents, setReviewEvents] = useState<ReviewEvent[]>([])
  const [auditCandidates, setAuditCandidates] = useState<Candidate[]>([])
  const [reviewEventCursor, setReviewEventCursor] = useState<string | null>(null)
  const [reviewEventsLoading, setReviewEventsLoading] = useState(false)
  const [reviewEventsError, setReviewEventsError] = useState<string | null>(null)
  const candidateDetailRequestRef = useRef(0)
  const knownCandidateIdsRef = useRef<Set<string>>(new Set())
  const auditCandidateIdsRef = useRef<Set<string>>(new Set())
  const businessDataLoadedRef = useRef(false)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [decisionBusyId, setDecisionBusyId] = useState<string | null>(null)
  const [batchBusy, setBatchBusy] = useState(false)
  const [logoutBusy, setLogoutBusy] = useState(false)
  const [passkeyDialogOpen, setPasskeyDialogOpen] = useState(false)
  const [passkeyBusy, setPasskeyBusy] = useState(false)
  const [passkeyError, setPasskeyError] = useState<string | null>(null)
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null)
  const [candidateDetailLoadingId, setCandidateDetailLoadingId] = useState<string | null>(null)
  const [candidateDetailReadyId, setCandidateDetailReadyId] = useState<string | null>(null)
  const [accountingDimensions, setAccountingDimensions] = useState<AccountingDimensions | null>(null)
  const [accountingDimensionsError, setAccountingDimensionsError] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [personalBankData, setPersonalBankData] = useState<PersonalBankTransactionsResponse | null>(null)

  const navigate = useCallback((nextPage: Page, replace = false) => {
    const overviewAnchor = nextPage === 'review' ? '#review' : nextPage === 'files' ? '#files' : ''
    const nextPath = overviewAnchor ? `${pagePaths.overview}${overviewAnchor}` : pagePaths[nextPage]
    if (`${window.location.pathname}${window.location.search}${window.location.hash}` !== nextPath) {
      window.history[replace ? 'replaceState' : 'pushState']({}, '', nextPath)
    }
    setPage(overviewAnchor ? 'overview' : nextPage)
    if (overviewAnchor) {
      scrollToOverviewSection(overviewAnchor)
    } else if (nextPage === 'overview') {
      scrollToOverviewSection('#overview-summary')
    }
  }, [])

  useEffect(() => {
    if (window.location.pathname === pagePaths.review || window.location.pathname === pagePaths.files) {
      const anchor = window.location.pathname === pagePaths.review ? '#review' : '#files'
      window.history.replaceState({}, '', `${pagePaths.overview}${anchor}`)
    }
    if (
      window.location.pathname === pagePaths['original-reconciliation']
      || (window.location.pathname === pagePaths.reconciliation && window.location.search)
    ) {
      window.history.replaceState({}, '', pagePaths.reconciliation)
    }
    if (!Object.values(pagePaths).includes(window.location.pathname)) {
      window.history.replaceState({}, '', pagePaths.overview)
    }
    const handlePopState = () => {
      setPage(pageFromPath(window.location.pathname))
      if (window.location.hash === '#review' || window.location.hash === '#files') {
        scrollToOverviewSection(window.location.hash)
      } else if (window.location.pathname === pagePaths.overview) {
        scrollToOverviewSection('#overview-summary')
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [navigate])

  useEffect(() => {
    if (page !== 'overview' || authLoading || loading) return
    if (window.location.hash === '#review' || window.location.hash === '#files') {
      scrollToOverviewSection(window.location.hash)
    }
  }, [authLoading, loading, page])

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
    setClassificationGroups([])
    setClassificationGroupsAvailable(false)
    setSelectedCandidate(null)
    try {
      const sessionData = await api.getSession()
      setSession(sessionData)
      const classificationGroupRequest = api.listClassificationGroups().then(
        (value) => ({ status: 'fulfilled' as const, value }),
        () => ({ status: 'rejected' as const }),
      )
      const personalBankRequest = api.getPersonalBankTransactions().then(
        (value) => ({ status: 'fulfilled' as const, value }),
        () => ({ status: 'rejected' as const }),
      )
      const reconciliationRequest = api.getReconciliation(selectedMonth).then(
        (value) => ({ status: 'fulfilled' as const, value }),
        () => ({ status: 'rejected' as const }),
      )
      const [
        candidateData,
        reconciliationData,
        connectionData,
        classificationGroupResult,
        personalBankResult,
      ] = await Promise.all([
        api.listCandidates(),
        reconciliationRequest,
        api.listConnections(),
        classificationGroupRequest,
        personalBankRequest,
      ])
      setCandidates(candidateData.items.map(toCandidate))
      if (classificationGroupResult.status === 'fulfilled') {
        setClassificationGroups(classificationGroupResult.value.items)
        setClassificationGroupsAvailable(true)
        setNotice((current) => (
          current?.message === CLASSIFICATION_GROUPS_UNAVAILABLE_NOTICE ? null : current
        ))
      } else {
        setClassificationGroups([])
        setClassificationGroupsAvailable(false)
        setNotice({ tone: 'info', message: CLASSIFICATION_GROUPS_UNAVAILABLE_NOTICE })
      }
      setAuditCandidates([])
      setReconciliation(reconciliationData.status === 'fulfilled' ? reconciliationData.value : null)
      setConnections(connectionData)
      setPersonalBankData(personalBankResult.status === 'fulfilled' ? personalBankResult.value : null)
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
      const result = await api.listReviewEvents(cursor)
      setReviewEvents((current) => {
        const combined = cursor ? [...current, ...result.items] : result.items
        return [...new Map(combined.map((event) => [event.id, event])).values()]
      })
      if (includeCandidatePages) {
        const known = new Set([
          ...knownCandidateIdsRef.current,
          ...auditCandidateIdsRef.current,
        ])
        const missing = [...new Set(result.items.map((event) => event.candidate_id))].filter(
          (id) => !known.has(id),
        )
        if (missing.length > 0) {
          const fetched = (await fetchCandidatesByIds(missing)).map(toCandidate)
          if (fetched.length > 0) {
            setAuditCandidates((current) => [
              ...new Map(
                [...current, ...fetched].map((candidate) => [candidate.id, candidate]),
              ).values(),
            ])
          }
        }
      }
      setReviewEventCursor(result.next_cursor)
    } catch (error) {
      setReviewEventsError(error instanceof Error ? error.message : '无法读取审核操作记录')
    } finally {
      setReviewEventsLoading(false)
    }
  }, [])

  useEffect(() => {
    knownCandidateIdsRef.current = new Set(candidates.map((candidate) => candidate.id))
  }, [candidates])

  useEffect(() => {
    auditCandidateIdsRef.current = new Set(auditCandidates.map((candidate) => candidate.id))
  }, [auditCandidates])

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

  const pendingCandidates = candidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const pendingBankStatements = personalBankData?.statements.filter((statement) => statement.review_status === 'PENDING') ?? []
  const pendingReviewCount = pendingCandidates.length + pendingBankStatements.length
  const overviewMonthCandidates = candidates.filter((candidate) => candidate.accountingMonth === selectedMonth)
  const overviewMonthPending = overviewMonthCandidates.filter((candidate) => ['PENDING', 'INCOMPLETE', 'CONFLICTED'].includes(candidate.status))
  const overviewMonthConfirmed = overviewMonthCandidates.filter((candidate) => candidate.status === 'CONFIRMED')

  const updateCandidate = async (
    candidate: Candidate,
    intent: CandidateUpdateIntent,
    corrections?: CandidateCorrections,
    conflictResolution?: string,
    reviewNote?: string,
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
      const defaultReason = intent === 'IGNORE'
        ? 'Web 审核：忽略候选'
        : intent === 'RESOLVE_CONFLICT'
          ? 'Web 审核：解决冲突并确认'
          : corrections
            ? 'Web 审核：更正并确认'
            : 'Web 审核：确认候选'
      const result = await api.appendDecision({
        candidate: candidate.raw,
        decision: intent === 'IGNORE' ? 'IGNORE' : intent === 'RESOLVE_CONFLICT' ? 'RESOLVE_CONFLICT' : corrections ? 'CORRECT_AND_CONFIRM' : 'CONFIRM',
        reason: reviewNote?.trim() || defaultReason,
        corrections,
        conflictResolution: conflictResolution?.trim(),
        csrfToken: session.csrf_token,
      })
      let rereadVerified = false
      let persistedCandidate = result.candidate
      try {
        const reread = await api.getCandidate(candidate.id)
        if (
          reread.id !== result.candidate.id
          || reread.revision !== result.candidate.revision
          || reread.status !== result.candidate.status
        ) {
          throw new Error('Core 返回了未提交的事项版本')
        }
        persistedCandidate = reread
        rereadVerified = true
      } catch (error) {
        setNotice({
          tone: 'info',
          message: error instanceof Error
            ? `${candidate.shortId} 已保存，但重读验证失败：${error.message}`
            : `${candidate.shortId} 已保存，但重读验证失败`,
        })
      }
      const updated = toCandidate(persistedCandidate)
      setCandidates((items) => items.map((item) => (item.id === updated.id ? updated : item)))
      setReviewEvents((items) => [result.event, ...items.filter((item) => item.id !== result.event.id)])
      setSelectedCandidate(null)
      try {
        const refreshedReconciliation = await api.getReconciliation(selectedMonth)
        setReconciliation(refreshedReconciliation)
        setNotice({
          tone: rereadVerified ? 'success' : 'info',
          message: !rereadVerified
            ? `${candidate.shortId} 已保存，重新打开前请刷新确认`
            : intent === 'IGNORE'
            ? `${candidate.shortId} 已忽略，原始证据仍保留`
            : intent === 'RESOLVE_CONFLICT'
              ? `${candidate.shortId} 冲突已解决并进入本月草稿数据`
              : `${candidate.shortId} 已确认并进入本月草稿数据`,
        })
      } catch {
        setNotice({
          tone: 'info',
          message: rereadVerified
            ? `${candidate.shortId} 已保存并重读确认，对账状态需刷新`
            : `${candidate.shortId} 已保存，事项和对账状态均需刷新确认`,
        })
      }
    } catch (error) {
      setNotice({ tone: 'error', message: error instanceof Error ? error.message : '提交审核决定失败，请重试' })
    } finally {
      setDecisionBusyId(null)
    }
  }

  const bulkConfirmCandidates = async (eligible: Candidate[]) => {
    if (!session || !classificationGroupsAvailable || batchBusy || eligible.length === 0) return
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

  const applyClassificationGroup = async (
    candidate: Candidate,
    group: ClassificationGroup,
    target: ClassificationTarget,
    reviewNote?: string,
  ) => {
    if (!session || !classificationGroupsAvailable || decisionBusyId) return
    setDecisionBusyId(candidate.id)
    try {
      const receipt = await api.applyClassificationBatch({
        group,
        sourceCandidate: candidate.raw,
        target,
        reason: reviewNote?.trim() || 'Web 审核：明确预览并确认相似交易组分类',
        csrfToken: session.csrf_token,
      })
      const updated = new Map(
        receipt.results.map((result) => [result.candidate_ref, toCandidate(result.candidate)]),
      )
      setCandidates((items) => items.map((item) => updated.get(item.id) ?? item))
      const events = receipt.results.flatMap((result) => result.events)
      setReviewEvents((items) => [
        ...events,
        ...items.filter((item) => !events.some((event) => event.id === item.id)),
      ])
      setSelectedCandidate(null)
      const [groupResult, reconciliationResult] = await Promise.allSettled([
        api.listClassificationGroups(),
        api.getReconciliation(selectedMonth),
      ])
      if (groupResult.status === 'fulfilled') {
        setClassificationGroups(groupResult.value.items)
        setClassificationGroupsAvailable(true)
      } else {
        setClassificationGroups([])
        setClassificationGroupsAvailable(false)
      }
      if (reconciliationResult.status === 'fulfilled') {
        setReconciliation(reconciliationResult.value)
      }
      setNotice({
        tone: groupResult.status === 'fulfilled' && reconciliationResult.status === 'fulfilled'
          ? 'success'
          : 'info',
        message: `已原子确认同组 ${receipt.results.length} 笔交易；全部成员均已写入审核记录`,
      })
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof ApiError && error.status === 409
          ? '同组成员或版本已变化，本次没有处理任何交易；请刷新后重新预览'
          : error instanceof Error
            ? error.message
            : '相似交易组提交失败，本次没有处理任何交易',
      })
    } finally {
      setDecisionBusyId(null)
    }
  }

  const openCandidate = async (candidate: Candidate) => {
    const requestId = ++candidateDetailRequestRef.current
    const readOnly = ['CONFIRMED', 'IGNORED', 'SUPERSEDED'].includes(candidate.status)
    setAccountingDimensionsError(null)
    setCandidateDetailReadyId(null)
    setSelectedCandidate(candidate)
    setCandidateDetailLoadingId(candidate.id)
    const [detailResult, dimensionsResult] = await Promise.allSettled([
      api.getCandidate(candidate.id),
      readOnly ? Promise.resolve(null) : api.getAccountingDimensions(),
    ])
    if (candidateDetailRequestRef.current !== requestId) return
    if (detailResult.status === 'rejected') {
      const error = detailResult.reason
      setAccountingDimensionsError('候选详情读取失败，不能提交更正')
      setNotice({ tone: 'error', message: error instanceof Error ? `证据详情读取失败：${error.message}` : '证据详情读取失败' })
      setCandidateDetailLoadingId(null)
      return
    }
    setSelectedCandidate((current) => current?.id === candidate.id ? toCandidate(detailResult.value) : current)
    setCandidateDetailReadyId(candidate.id)
    if (!readOnly) {
      if (dimensionsResult.status === 'fulfilled') {
        setAccountingDimensions(dimensionsResult.value)
      } else {
        setAccountingDimensionsError('会计维度目录暂时不可用；可按现有分类继续确认，修改分类请在目录恢复后重试')
      }
    }
    if (candidateDetailRequestRef.current === requestId) setCandidateDetailLoadingId(null)
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
      setClassificationGroups([])
      setClassificationGroupsAvailable(false)
      setReconciliation(null)
      setConnections([])
      setReviewEvents([])
      setAuditCandidates([])
      setReviewEventCursor(null)
      setReviewEventsError(null)
      setSelectedCandidate(null)
      setCandidateDetailLoadingId(null)
      setCandidateDetailReadyId(null)
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
        <>
          <section className="overview-section" id="overview-summary" aria-label="概览摘要">
            <Overview
              pending={pendingCandidates}
              monthPending={overviewMonthPending}
              monthConfirmed={overviewMonthConfirmed}
              accountingMonth={selectedMonth}
              reconciliation={reconciliation}
              connections={connections}
              onNavigate={navigate}
              onOpenCandidate={openCandidate}
            />
          </section>
          <section className="overview-section" id="review" aria-label="待审核">
            <CompanyBankStatementReviewPanel csrfToken={session?.csrf_token ?? ''} />
            <CompanyTransactionClassificationPanel csrfToken={session?.csrf_token ?? ''} />
            <ReviewQueue
              candidates={pendingCandidates}
              bankStatements={pendingBankStatements}
              csrfToken={session?.csrf_token ?? ''}
              onBankStatementReviewed={async () => {
                const refreshed = await api.getPersonalBankTransactions()
                setPersonalBankData(refreshed)
              }}
              classificationGroups={classificationGroups}
              classificationGroupsAvailable={classificationGroupsAvailable}
              onOpenCandidate={openCandidate}
              onUpdate={updateCandidate}
              onRefresh={loadData}
              busyId={decisionBusyId}
              batchBusy={batchBusy}
              onBatchConfirm={bulkConfirmCandidates}
            />
          </section>
          <section className="overview-section" id="files" aria-label="文件与连接">
            <FilesAndConnections candidates={candidates} connections={connections} csrfToken={session?.csrf_token ?? null} onOpenCandidate={openCandidate} onRefresh={loadData} onNotice={setNotice} />
          </section>
        </>
      )
    }
    if (page === 'personal-finance') {
      return <PersonalFinanceOverview onNavigate={navigate} onOpenCandidate={openCandidate} csrfToken={session?.csrf_token ?? ''} />
    }
    if (page === 'reconciliation') {
      return (
        <OriginalReconciliationPage onNavigate={navigate} />
      )
    }
    if (page === 'company-reports') {
      return <CompanyReportsPage />
    }
    if (page === 'payroll') {
      return <PayrollWorkspacePage />
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
    return null
  }

  if (authLoading) return <AuthFrame><LoadingState title="正在检查访问状态" description="正在确认此设备的单用户会话。" /></AuthFrame>
  if (authError) return <AuthFrame><ErrorState message={authError} onRetry={loadAuthStatus} /></AuthFrame>
  if (!authStatus?.authenticated || authStatus.recovery_setup_required) {
    return <AuthScreen status={authStatus} onAuthenticated={completeAuthentication} onRecoveryCancelled={loadAuthStatus} />
  }

  const isCoreBacked = session?.runtime_mode === 'core-backed'
  const pageLoadsIndependently = page === 'payroll' || page === 'reconciliation' || page === 'company-reports'

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
                {item.id === 'review' && pendingReviewCount > 0 ? (
                  <span className="nav-count">{pendingReviewCount}</span>
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

        <main className={`content${page === 'reconciliation' ? ' content-reconciliation' : ''}`}>
          <Suspense fallback={<LoadingState title="正在加载功能模块" description="只加载当前打开的功能区。" />}>
            {pageLoadsIndependently
              ? renderPage()
              : loading
                ? <LoadingState />
                : loadError
                  ? <ErrorState message={loadError} onRetry={loadData} />
                  : renderPage()}
          </Suspense>
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
                {item.id === 'review' && pendingReviewCount > 0 ? <i>{pendingReviewCount}</i> : null}
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
          key={`${selectedCandidate.id}:${selectedCandidate.revision}:${selectedCandidate.businessUnitRef}:${selectedCandidate.categoryCode}:${selectedCandidate.amountMinor}:${selectedCandidate.accountingMonth ?? ''}`}
          candidate={selectedCandidate}
          classificationGroup={classificationGroupsAvailable
            ? classificationGroups.find((group) => (
                group.accounting_month === selectedCandidate.accountingMonth
                && group.members.some((member) => member.candidate_ref === selectedCandidate.id)
              )) ?? null
            : null}
          onClose={() => setSelectedCandidate(null)}
          onUpdate={updateCandidate}
          onApplyGroup={applyClassificationGroup}
          busy={selectedCandidate.id === decisionBusyId}
          detailLoading={candidateDetailLoadingId === selectedCandidate.id}
          detailReady={candidateDetailReadyId === selectedCandidate.id}
          accountingDimensions={accountingDimensions}
          accountingDimensionsError={accountingDimensionsError}
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


function Overview({
  pending,
  monthPending,
  monthConfirmed,
  accountingMonth,
  reconciliation,
  connections,
  onNavigate,
  onOpenCandidate,
}: {
  pending: Candidate[]
  monthPending: Candidate[]
  monthConfirmed: Candidate[]
  accountingMonth: string
  reconciliation: ReconciliationData | null
  connections: ConnectionStatus[]
  onNavigate: (page: Page) => void
  onOpenCandidate: (candidate: Candidate) => void
}) {
  const confirmedTotal = monthConfirmed.reduce((total, candidate) => total + candidate.amount, 0)
  const monthCandidates = [...monthPending, ...monthConfirmed]
  const conflictCount = pending.filter((candidate) => candidate.conflict).length
  const businessUnits = Array.from(new Set(monthCandidates.map((candidate) => candidate.businessUnit)))
    .sort((left, right) => left.localeCompare(right, 'zh-CN'))
  const connectedCount = connections.filter((connection) => connection.state === 'CONNECTED').length
  const candidateTotal = monthCandidates.length
  const reviewProgress = candidateTotal > 0 ? Math.round((monthConfirmed.length / candidateTotal) * 100) : 0
  const reconciliationBlockers = reconciliation?.blockers ?? []
  const reconciliationDetail = reconciliation === null
    ? '对账状态尚未返回'
    : reconciliationBlockers.length > 0
      ? reconciliationBlockers.map((blocker) => blocker.message).join('；')
      : reconciliation.ready
        ? '已满足草稿生成条件'
        : '尚未满足草稿生成条件'
  const unitStates = businessUnits.map((unit) => {
    const unresolved = monthPending.filter((candidate) => candidate.businessUnit === unit)
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
        eyebrow={`${accountingMonthLabel(accountingMonth)}对账`}
        title={pending.length > 0 ? '早上好，今天有几项需要确认' : '当前没有待审核事项'}
        description="待审核队列覆盖全部月份；金额、营业单元与对账状态按上方月份统计。"
        action={pending.length > 0 ? <Button onClick={() => onNavigate('review')}><ListChecks size={17} />开始审核</Button> : undefined}
      />

      <section className="metric-grid" aria-label="本月概览">
        <Metric primary label="全部待审核" value={`${pending.length} 条`} detail={conflictCount > 0 ? `优先处理 ${conflictCount} 条冲突候选` : '包含待归属月份的候选'} tone={pending.length > 0 ? 'attention' : undefined} icon={<ListChecks size={20} />} />
        <Metric label="本月已确认" value={currency.format(confirmedTotal)} detail={`${monthConfirmed.length} 条可用于本月草稿`} icon={<CheckCircle size={20} />} />
        <Metric label="本月候选营业单元" value={`${businessUnits.length} 家`} detail={businessUnits.length > 0 ? businessUnits.join('、') : '本月暂无候选营业单元'} icon={<Database size={20} />} />
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
            {pending.length === 0 ? <StatusLine icon={<Check size={17} />} label="待审核队列" detail="当前没有待审核候选" tone="ok" /> : null}
          </div>
        </section>

        <section className="panel readiness-panel">
          <div className="panel-heading">
            <div>
              <h2>本月候选审核</h2>
              <p>{candidateTotal > 0 ? `${monthConfirmed.length} / ${candidateTotal} 条已确认` : '暂无本月候选'}</p>
            </div>
            <span className="readiness-score">{candidateTotal > 0 ? `${reviewProgress}%` : '—'}</span>
          </div>
          <div className="progress-track" role="progressbar" aria-label="本月候选审核完成度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={reviewProgress} aria-valuetext={candidateTotal > 0 ? `${reviewProgress}%` : '暂无本月候选'}><span style={{ width: `${reviewProgress}%` }} /></div>
          <div className="readiness-list">
            <StatusLine
              icon={reconciliationBlockers.length > 0 ? <Warning size={17} /> : reconciliation?.ready ? <Check size={17} /> : <Info size={17} />}
              label="月度对账"
              detail={reconciliationDetail}
              tone={reconciliationBlockers.length > 0 ? 'warn' : reconciliation?.ready ? 'ok' : 'info'}
            />
            {unitStates.map((state) => <StatusLine key={state.unit} icon={state.icon} label={state.unit} detail={state.detail} tone={state.tone} />)}
            {unitStates.length === 0 ? <StatusLine icon={<Check size={17} />} label="本月候选" detail="暂无本月候选" tone="ok" /> : null}
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

function ReviewQueue({ candidates, bankStatements, csrfToken, onBankStatementReviewed, classificationGroups, classificationGroupsAvailable, onOpenCandidate, onUpdate, onRefresh, busyId, batchBusy, onBatchConfirm }: {
  candidates: Candidate[]
  bankStatements: PersonalBankStatement[]
  csrfToken: string
  onBankStatementReviewed: () => Promise<void>
  classificationGroups: ClassificationGroup[]
  classificationGroupsAvailable: boolean
  onOpenCandidate: (candidate: Candidate) => void
  onUpdate: (candidate: Candidate, intent: CandidateUpdateIntent, corrections?: CandidateCorrections, conflictResolution?: string) => void
  onRefresh: () => void
  busyId: string | null
  batchBusy: boolean
  onBatchConfirm: (candidates: Candidate[]) => void
}) {
  const [bankReviewBusy, setBankReviewBusy] = useState<string | null>(null)
  const [sourceFilter, setSourceFilter] = useState('all')
  const [statusFilter, setStatusFilter] = useState<'all' | 'conflict' | 'incomplete' | 'ready'>('all')
  const [transferObjectFilter, setTransferObjectFilter] = useState('all')
  const [query, setQuery] = useState('')
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const bulkEligible = classificationGroupsAvailable
    ? candidates.filter((candidate) => isBulkEligible(candidate, classificationGroups))
    : []
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
      || (statusFilter === 'ready' && classificationGroupsAvailable && isBulkEligible(candidate, classificationGroups))
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
      {bankStatements.length > 0 ? (
        <section className="panel" aria-label={`银行账单待确认 ${bankStatements.length}`}>
          <div className="panel-heading">
            <div><h2>银行账单待确认</h2><p>这些正式流水已经入库，确认只终结账单审核，不会自动生成或过账会计凭证。</p></div>
            <Badge color="amber">{bankStatements.length} 份</Badge>
          </div>
          <div className="personal-bank-facts-review">
            {bankStatements.map((statement) => (
              <div className="personal-bank-statement-review" key={statement.statement_ref}>
                <span>{({ abc: '中国农业银行', boc: '中国银行', ccb: '中国建设银行', mybank: '网商银行' } as Record<string, string>)[statement.institution_code] ?? '银行账户'} · 尾号 {statement.account_suffix}</span>
                <span>{statement.period_start} 至 {statement.period_end}</span>
                <span>{statement.transaction_count} 笔 · 审核版本 {statement.review_revision}</span>
                <Button disabled={bankReviewBusy !== null || !csrfToken} size="1" onClick={async () => {
                  setBankReviewBusy(statement.statement_ref)
                  try {
                    await api.reviewPersonalBankStatement({ statement, decision: 'CONFIRMED', reason: 'Web 审核：确认银行账单', csrfToken })
                    await onBankStatementReviewed()
                  } finally {
                    setBankReviewBusy(null)
                  }
                }}>{bankReviewBusy === statement.statement_ref ? '正在确认…' : '确认账单'}</Button>
              </div>
            ))}
          </div>
        </section>
      ) : null}
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
            <strong>{filtered.length + bankStatements.length} 条待处理</strong>
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
            <h2>{bankStatements.length > 0 ? '候选交易已处理完' : '当前筛选下没有待审核项'}</h2>
            <p>{bankStatements.length > 0 ? '上方仍有银行账单需要确认。' : '新的财务候选会在这里出现。'}</p>
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

export default App
