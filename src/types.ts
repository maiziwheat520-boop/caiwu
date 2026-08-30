export type Page = 'overview' | 'personal-finance' | 'review' | 'reconciliation' | 'company-reports' | 'payroll' | 'files' | 'audit'

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
  business_unit_ref?: string | null
  category: string
  category_code?: string | null
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
  businessUnitRef: string
  category: string
  categoryCode: string
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
  business_unit_ref: string
  category_code: string
  amount_minor: number
  accounting_month: string
}>

export type AccountingDimensions = {
  contract_version: 'ledgerbridge.accounting-dimensions.v1'
  business_units: Array<{
    ref: string
    label: string
  }>
  categories: Array<{
    code: string
    label: string
  }>
}

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
    identity_changed: boolean
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

export type PayrollReadResponse<T> = {
  contract_version: 'ledgerbridge.payroll-read.v1'
  entity_ref: string
  company_id: string
  data: T
}

export type PayrollStatusData = {
  schema_version: 'ledgerbridge.payroll-status.v1'
  projection_revision: string
  etag: string
  live_data_ready: boolean
  live_projection_schema: 'payroll-ledgerbridge-live-projection/v1'
  payment_operations_exposed: false
  capabilities: {
    commands_enabled: boolean
    allowed_actions: string[]
  }
  setup_summary: {
    provider_connected: true
    runtime_mode: 'live-provider'
    unassigned_material_count: number
    ready_material_count: number
    company_mapped_material_count: number
    blocking_reason_codes: string[]
  }
}

export type PayrollDashboardData = {
  schema_version: 'ledgerbridge.payroll-dashboard.v1'
  projection_revision: string
  etag: string
  generated_at: string
  live_data_ready: boolean
  setup_summary: PayrollStatusData['setup_summary']
  dashboard?: {
    batch_count: number
    material_count: number
    materials_needing_review_count: number
    verification_attention_count: number
    unassigned_material_count: number
    net_pay_minor: number
  }
}

export type PayrollMaterial = {
  company_id: string
  material_id: string
  period: string | null
  material_type: string | null
  status: string
  review_revision: number
  payable: false
  submission_supported: false
}

export type PayrollMaterialListData = {
  schema_version: 'ledgerbridge.payroll-material-list.v1'
  projection_revision: string
  etag: string
  generated_at: string
  items: PayrollMaterial[]
}

export type PayrollBatch = {
  company_id: string
  batch_id: string
  pay_period: string
  revision: number
  status: string
  payable: false
  submission_supported: false
  payment_submission_supported: false
  lines: PayrollBatchLine[]
  audit_closure?: {
    audit_event_id: string
    audit_hash: string
  }
}

export type PayrollBatchLine = {
  company_id: string
  employee_id: string
  employee_display: string
  account_id: string
  account_display: string
  net_pay_minor: number
}

export type PayrollBatchListData = {
  schema_version: 'ledgerbridge.payroll-batch-list.v1'
  projection_revision: string
  etag: string
  generated_at: string
  items: PayrollBatch[]
}

export type PayrollEmployeeVerificationResult = {
  company_id: string
  employee_id: string
  employee_display: string
  account_id: string
  account_display: string
  status: string
}

export type PayrollVerificationResult = {
  verification_id: string
  company_id: string
  batch_id: string
  source_artifact_ids: string[]
  status: string
  results: PayrollEmployeeVerificationResult[]
  payable: false
  submission_supported: false
  payment_submission_supported: false
}

export type PayrollAvailableEvidence = {
  company_id: string
  artifact_id: string
  period: string
  evidence_type: string
  status: 'READY_FOR_MATCHING'
  display_label: string
}

export type PayrollVerificationListData = {
  schema_version: 'ledgerbridge.payroll-verification-list.v1'
  projection_revision: string
  etag: string
  generated_at: string
  items: PayrollVerificationResult[]
  available_evidence: PayrollAvailableEvidence[]
}

export type PayrollCommandResult = {
  contract_version: 'ledgerbridge.payroll-command-result.v1'
  entity_ref: string
  company_id: string
  action: string
  resource_ref: string
  replayed: boolean
  data: {
    schema_version: 'payroll-ledgerbridge-command-receipt/v1'
    company_id: string
    resource_id: string
    action: 'payroll.receipts.verify'
    audit_event_id: string
    audit_hash: string
    occurred_at: string
    idempotency_key: string
    replayed: boolean
    audit_closure: {
      company_id: string
      resource_id: string
      action: 'payroll.receipts.verify'
      actor_subject: string
      actor_id: string
      audit_event_id: string
      audit_hash: string
      occurred_at: string
    }
  }
}
