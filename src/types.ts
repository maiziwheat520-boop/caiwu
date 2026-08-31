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

export type ClassificationRiskCode =
  | 'FUNDING_STATEMENT_REQUIRED'
  | 'HOTEL_PAYOUT_STATEMENT_REQUIRED'
  | 'RELATED_ACCOUNT_STATEMENT_REQUIRED'
  | 'REVERSAL_MATCH_REQUIRED'
  | 'TRANSFER_REVIEW_REQUIRED'
  | 'UNSETTLED_TRANSACTION'

export type SimilarityConditions = {
  key_version: 'ledgerbridge.classification-key.v1'
  entity_ref: string
  source_system: string
  source_kind: string
  platform: string
  direction: 'INFLOW' | 'OUTFLOW' | 'NEUTRAL'
  transaction_type: string
  counterparty_key: string
  counterparty_label: string
  counterparty_basis: 'REGISTRY_COUNTERPARTY' | 'EXACT_PLATFORM_SUMMARY_V1'
  funding_instrument: string
  transaction_status: string
  currency: 'CNY'
  risk_signature: ClassificationRiskCode[]
}

export type ClassificationGroupMember = {
  candidate_ref: string
  short_id: string
  revision: number
  status: CandidateStatus
  amount_minor: number
  accounting_month: string
  confidence_basis_points: number
  review_risk_codes: ClassificationRiskCode[]
  amount_outlier: boolean
  batch_eligible: boolean
  one_click_eligible: boolean
  exclusion_codes: Array<
    'NOT_PENDING' | 'LOW_CONFIDENCE' | 'BLOCKED' | 'STRUCTURAL_RISK' | 'AMOUNT_OUTLIER'
  >
}

export type ClassificationGroup = {
  contract_version: 'ledgerbridge.classification-group.v1'
  group_ref: string
  accounting_month: string
  conditions: SimilarityConditions
  members: ClassificationGroupMember[]
  batch_member_count: number
  one_click_member_count: number
  terminal_statuses: CandidateStatus[]
  terminal_classifications: string[]
  rule_learning_eligible: boolean
  rule_learning_blocks: Array<
    | 'PROVISIONAL_BASIS'
    | 'TERMINAL_DECISION_CONFLICT'
    | 'REVIEW_RISK_PRESENT'
    | 'AMOUNT_OUTLIER'
    | 'NO_CONFIRMED_SOURCE'
  >
  active_rule: null
}

export type ClassificationGroupPage = {
  contract_version: 'ledgerbridge.classification-groups.v1'
  items: ClassificationGroup[]
  next_cursor: null
}

export type ClassificationTarget = {
  business_unit_ref: string
  category_code: string
}

export type ClassificationBatchReceipt = {
  contract_version: 'ledgerbridge.classification-batch.v1'
  operation_id: string
  replayed: boolean
  group_ref: string
  accounting_month: string
  source_candidate_ref: string
  target: ClassificationTarget
  acknowledged_risk_codes: ClassificationRiskCode[]
  results: Array<{
    candidate_ref: string
    operation_id: string
    status: 'APPLIED' | 'REPLAYED'
    candidate: ApiCandidate
    events: ReviewEvent[]
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

export type CompanyReportBasis = 'CONFIRMED_CANDIDATE' | 'ACCOUNT_STATEMENT' | 'POSTED_LEDGER'

export type ConfirmedCandidateMetrics = {
  basis: 'CONFIRMED_CANDIDATE'
  confirmed_positive_minor: number
  confirmed_negative_minor: number
  confirmed_net_minor: number
  confirmed_count: number
  source_count: number
}

export type AccountStatementMetrics = {
  basis: 'ACCOUNT_STATEMENT'
  cash_inflow_minor: number
  cash_outflow_minor: number
  net_cash_flow_minor: number
  confirmed_transaction_count: number
  statement_count: number
}

export type PostedLedgerMetrics = {
  basis: 'POSTED_LEDGER'
  revenue_minor: number
  expense_minor: number
  profit_minor: number
  posted_entry_count: number
  source_count: number
}

export type CompanyReportMetrics =
  | ConfirmedCandidateMetrics
  | AccountStatementMetrics
  | PostedLedgerMetrics

export type CompanyReportBalance = {
  balance_basis: 'UNAVAILABLE'
  opening_balance_minor: null
  closing_balance_minor: null
  gap: 'AUTHORITATIVE_BALANCE_UNAVAILABLE'
}

export type CompanyReportAggregate = {
  metrics: CompanyReportMetrics
  pending_review_count: number
  attribution_pending_count: number
  missing_material_count: number | null
  taxonomy_version: string | null
  balance: CompanyReportBalance
}

export type CompanyReportBusinessUnit = CompanyReportAggregate & {
  business_unit_ref: string
  business_unit_label: string
}

export type CompanyReportBusinessUnitBreakdownStatus =
  | 'AVAILABLE'
  | 'EMPTY'
  | 'UNAVAILABLE_ATTRIBUTION_PENDING'
  | 'UNAVAILABLE_MISSING_SNAPSHOT'

type CompanyReportMonthIdentity = CompanyReportAggregate & {
  month: string
}

export type CompanyReportMonth = CompanyReportMonthIdentity & (
  | {
    business_unit_breakdown_status: 'AVAILABLE'
    business_units: CompanyReportBusinessUnit[]
  }
  | {
    business_unit_breakdown_status: 'EMPTY'
    business_units: []
  }
  | {
    business_unit_breakdown_status: 'UNAVAILABLE_ATTRIBUTION_PENDING' | 'UNAVAILABLE_MISSING_SNAPSHOT'
    business_units: null
  }
)

export type CompanyReportCompany = CompanyReportAggregate & {
  company_ref: string
  company_name: string
  currency: string
  business_unit_breakdown_status: CompanyReportBusinessUnitBreakdownStatus
  months: CompanyReportMonth[]
}

export type CompanyReportLayer = {
  contract_version: 'ledgerbridge.company-report.v1'
  basis: CompanyReportBasis
  from_month: string
  to_month: string
  items: CompanyReportCompany[]
}

export type CompanyReportsResponse = {
  contract_version: 'ledgerbridge.company-reports-bff.v1'
  from_month: string
  to_month: string
  posted_ledger_status: 'AVAILABLE' | 'UNAVAILABLE'
  layers: CompanyReportLayer[]
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

export type PayrollReadResponse<T> = {
  contract_version: 'ledgerbridge.payroll-read.v1'
  entity_ref: string
  company_id: string
  data: T
}

export type PayrollTestRoutingStatus = 'AUTO_TEST' | 'REVIEW_REQUIRED' | 'DATE_UNKNOWN'

export type PayrollTestWorkspaceMaterial = {
  company_id: string
  material_id: string
  routing_status: PayrollTestRoutingStatus
  period: string | null
  material_type: string | null
  payable: false
  submission_supported: false
}

export type PayrollTestWorkspaceProjection = {
  contract_version: '1.0.0'
  schema_version: 'payroll-ledgerbridge-test-projection/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  cutoff_date: '2026-08-31'
  workspace_revision: number
  projection_revision: string
  etag: string
  generated_at: string
  auto_test_ready: boolean
  payment_submission_supported: false
  payable: false
  submission_supported: false
  routing_counts: {
    auto_test: number
    review_required: number
    date_unknown: number
  }
  materials: PayrollTestWorkspaceMaterial[]
}

export type PayrollTestWorkspaceReadResponse = {
  contract_version: 'ledgerbridge.payroll-test-workspace-read.v1'
  entity_ref: string
  company_id: string
  data: PayrollTestWorkspaceProjection
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
