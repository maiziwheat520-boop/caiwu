export type Page = 'overview' | 'personal-finance' | 'review' | 'reconciliation' | 'original-reconciliation' | 'company-reports' | 'payroll' | 'files' | 'audit'

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
  | 'controlled_upload'
  | 'synthetic'

export type SourceLabel =
  | 'Telegram'
  | '钉钉'
  | '微信'
  | '支付宝'
  | 'Hermes'
  | '中行账单（复核材料）'
  | '照片凭证'
  | '合成数据'

export type EvidenceReference = {
  id: string
  kind: 'message' | 'attachment'
  media_type: string
  sha256: string | null
  original_filename: string | null
  unlock_status?: 'NOT_REQUIRED' | 'PASSWORD_REQUIRED' | 'UNLOCKED'
  source_ref?: string | null
}

export type EvidenceUnlockResult = {
  unlocked: true
}

export type Blocker = {
  code: string
  message: string
}

export type EvidencePreviewField = {
  label: string
  value: string
}

export type EvidencePreview =
  | {
      kind: 'image'
      filename: string
      media_type: string
      data_url: string
    }
  | {
      kind: 'text'
      filename: string
      text: string
    }
  | {
      kind: 'spreadsheet'
      filename: string
      reference: string | null
      matched: boolean
      records: Array<{
        sheet: string
        row_number: number
        header_row_number: number | null
        fields: EvidencePreviewField[]
      }>
      fallback: {
        sheet: string
        rows: Array<{ row_number: number; cells: string[] }>
      } | null
    }
  | {
      kind: 'unsupported'
      filename: string
      reason: string
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
  review_risks: ReviewRisk[]
}

export type ReviewRisk = {
  code: string
  message: string
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
  reviewRisks: ReviewRisk[]
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
  runtime_mode: 'synthetic-preview' | 'authenticated-preview' | 'core-backed'
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

export type PasskeyAdditionResult = {
  added: true
  passkey_count: number
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

export type OriginalReconciliationColumn = {
  column: string
  ordinal: number
  role: 'MAIN' | 'SPACER' | 'DETAIL'
}

export type OriginalReconciliationCell = {
  coordinate: string
  column: string
  row_number: number
  kind: 'BLANK' | 'LABEL' | 'AMOUNT' | 'GAP'
  label: string | null
  amount_minor: number | null
  currency: 'CNY' | null
  gap_code: 'MISSING_LEGACY_SLOT_MAPPING' | 'MISSING_BALANCE_MAPPING' | 'MISSING_ECONOMIC_EFFECT' | 'POSTED_LEDGER_UNAVAILABLE' | null
  source_fact_refs: string[]
}

export type OriginalReconciliation = {
  contract_version: 'ledgerbridge.original-reconciliation.v1'
  taxonomy_version: 'ledgerbridge.financial-foundation-blocker-taxonomy.v1'
  layout_version: string
  mapping_version: string
  is_complete: boolean
  posted_ledger_complete: boolean
  projection_gaps: Array<'MISSING_TIME_GRANULARITY' | 'MISSING_BUSINESS_UNIT_ATTRIBUTION'>
  month: string
  scope: {
    entity_ref: string
    business_unit_ref: string
  }
  columns: OriginalReconciliationColumn[]
  rows: Array<{
    row_number: number
    cells: OriginalReconciliationCell[]
  }>
  totals: {
    posted_income_minor: number | null
    posted_expense_minor: number | null
    posted_profit_minor: number | null
    opening_balance_minor: number | null
    closing_balance_minor: number | null
    mapped_cell_count: number
    confirmed_candidate_amount_minor: number
    posted_amount_minor: number | null
    currency: 'CNY'
  }
  pending_review_count: number
  confirmed_pending_posting_count: number
  missing_material_count: number
  unmapped_confirmed_count: number
  sources: Array<{
    source_kind: 'POSTED_LEDGER' | 'CONFIRMED_CANDIDATE' | 'ACCOUNT_STATEMENT'
    source_system: string
    source_label: string | null
    fact_count: number
    mapped_fact_count: number
    amount_minor: number
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
