export type Page = 'overview' | 'review' | 'reconciliation' | 'files' | 'audit'

export type CandidateStatus =
  | 'INCOMPLETE'
  | 'PENDING'
  | 'CONFLICTED'
  | 'CONFIRMED'
  | 'IGNORED'
  | 'SUPERSEDED'

export type SourceChannel =
  | 'telegram'
  | 'dingtalk'
  | 'weixin'
  | 'hermes'
  | 'outlook'
  | 'synthetic'

export type SourceLabel =
  | 'Telegram'
  | '钉钉'
  | '微信'
  | 'Hermes'
  | '中行邮箱'
  | '合成数据'

export type EvidenceReference = {
  id: string
  kind: 'message' | 'attachment'
  media_type: string
  sha256: string | null
  original_filename: string | null
}

export type Blocker = {
  code: string
  message: string
}

export type ApiCandidate = {
  id: string
  short_id: string
  revision: number
  status: CandidateStatus
  source_channel: SourceChannel
  source_message_id: string
  received_at: string
  business_unit: string
  category: string
  amount_minor: number
  currency: 'CNY'
  accounting_month: string | null
  summary: string
  confidence_basis_points: number
  evidence: EvidenceReference[]
  blockers: Blocker[]
}

export type CandidateDetail = ApiCandidate & {
  review_events: ReviewEvent[]
}

export type Candidate = {
  id: string
  shortId: string
  revision: number
  source: SourceLabel
  sourceChannel: SourceChannel
  receivedAt: string
  businessUnit: string
  category: string
  amount: number
  amountMinor: number
  accountingMonth: string | null
  summary: string
  evidence: EvidenceReference[]
  confidence: number
  status: CandidateStatus
  blockers: Blocker[]
  reviewEvents: ReviewEvent[]
  incomplete: boolean
  conflict: boolean
  raw: ApiCandidate
}

export type CandidateListResponse = {
  items: ApiCandidate[]
  next_cursor: string | null
}

export type CandidateDecision = 'CONFIRM' | 'CORRECT_AND_CONFIRM' | 'IGNORE' | 'RESOLVE_CONFLICT'

export type CandidateCorrections = Partial<{
  business_unit: string
  category: string
  amount_minor: number
  accounting_month: string
}>

export type ReviewEvent = {
  id: string
  candidate_id: string
  sequence: number
  from_revision: number
  to_revision: number
  decision: CandidateDecision
  actor: string
  reason: string
  changes: Array<{
    field: 'business_unit' | 'category' | 'amount_minor' | 'accounting_month' | 'status'
    previous_value: string | number | null
    new_value: string | number | null
  }>
  conflict_resolution: string | null
  created_at: string
}

export type ReviewEventListResponse = {
  items: ReviewEvent[]
  next_cursor: string | null
}

export type Session = {
  principal: string
  csrf_token: string
  expires_at: string
}

export type AuthStatus = {
  authenticated: boolean
  setup_required: boolean
  passkey_registered: boolean
  recovery_setup_required: boolean
  recovery_pending: boolean
  principal?: string
}

export type AuthResult = AuthStatus & {
  recovery_codes?: string[]
  csrf_token?: string
  expires_at?: string
}

export type RegistrationOptionsJson = Omit<PublicKeyCredentialCreationOptions, 'challenge' | 'user' | 'excludeCredentials'> & {
  challenge: string
  user: Omit<PublicKeyCredentialUserEntity, 'id'> & { id: string }
  excludeCredentials?: Array<Omit<PublicKeyCredentialDescriptor, 'id'> & { id: string }>
}

export type AuthenticationOptionsJson = Omit<PublicKeyCredentialRequestOptions, 'challenge' | 'allowCredentials'> & {
  challenge: string
  allowCredentials?: Array<Omit<PublicKeyCredentialDescriptor, 'id'> & { id: string }>
}

export type Reconciliation = {
  accounting_month: string
  revision: number
  ready: boolean
  blockers: Blocker[]
  business_units: Array<{
    name: string
    amounts_minor: Record<string, number>
  }>
}

export type WorkbookDraft = {
  id: string
  accounting_month: string
  input_revision: number
  status: 'QUEUED' | 'BUILDING' | 'NEEDS_REVIEW' | 'VERIFIED' | 'FAILED'
  verification: 'LIBREOFFICE_VERIFIED' | null
}

export type ConnectionId =
  | 'hermes_ingress'
  | 'ledgerbridge_core'
  | 'onedrive_appfolder'
  | 'libreoffice_worker'

export type ConnectionStatus = {
  id: ConnectionId
  state: 'CONNECTED' | 'DISCONNECTED' | 'DEGRADED' | 'NOT_CONFIGURED'
  checked_at: string
  detail?: string
}

export type Problem = {
  type?: string
  title?: string
  status?: number
  code?: string
  detail?: string
}

export type Notice = {
  tone: 'success' | 'info' | 'error'
  message: string
}
