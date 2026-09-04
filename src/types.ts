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
  source_system?: string
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

export type CompanyReportCategorySlice = {
  category_code: string | null
  category_label: string | null
  amount_minor: number
  fact_count: number
}

export type CashReconciliation = {
  contract_version: 'ledgerbridge.cash-reconciliation.v2'
  accounting_month: string
  rules: Array<{
    rule_key: string
    source_kind: 'BANK_TRANSACTION' | 'CANDIDATE'
    source_ref: string
    flow_kind: 'INCOME' | 'EXPENSE' | 'CURRENT'
    business_unit_label: string
    item_label: string
    match_pattern: string
    amount_direction: 'CREDIT' | 'DEBIT' | 'ANY'
    effective_from: string
    effective_to: string | null
  }>
  rows: Array<{
    rule_key: string
    flow_kind: 'INCOME' | 'EXPENSE' | 'CURRENT'
    business_unit_label: string
    item_label: string
    source_kind: 'BANK_TRANSACTION' | 'CANDIDATE' | 'ADJUSTMENT'
    source_ref: string
    transaction_count: number
    amount_minor: number
    facts: Array<{ fact_ref: string; occurred_on: string; amount_minor: number }>
  }>
  issues: Array<{
    issue_kind: 'UNMATCHED' | 'MULTIPLE_RULES'
    source_kind: 'BANK_TRANSACTION' | 'CANDIDATE'
    fact_ref: string
    occurred_on: string
    amount_minor: number
    matched_rule_keys: string[]
  }>
  eligible_fact_count: number
  matched_fact_count: number
  unmatched_fact_count: number
  conflicted_fact_count: number
  issue_count: number
  issues_truncated: boolean
  totals: { income_minor: number; expense_minor: number; current_minor: number }
}

export type CompanyReportCategoryComposition = {
  total_minor: number
  fact_count: number
  items: CompanyReportCategorySlice[]
}

export type CompanyReportCompositionItem = {
  company_ref: string
  company_name: string
  currency: string
} & (
  | {
    basis: 'CONFIRMED_CANDIDATE'
    positive: CompanyReportCategoryComposition
    negative: CompanyReportCategoryComposition
  }
  | {
    basis: 'POSTED_LEDGER'
    revenue: CompanyReportCategoryComposition
    expense: CompanyReportCategoryComposition
  }
)

export type CompanyReportCompositionLayer = {
  contract_version: 'ledgerbridge.company-report-composition.v1'
  basis: 'CONFIRMED_CANDIDATE' | 'POSTED_LEDGER'
  from_month: string
  to_month: string
  items: CompanyReportCompositionItem[]
}

export type CompanyReportsResponse = {
  contract_version: 'ledgerbridge.company-reports-bff.v1' | 'ledgerbridge.company-reports-bff.v2' | 'ledgerbridge.company-reports-bff.v3'
  from_month: string
  to_month: string
  posted_ledger_status: 'AVAILABLE' | 'UNAVAILABLE'
  layers: CompanyReportLayer[]
  compositions?: CompanyReportCompositionLayer[]
  transaction_classifications?: CompanyTransactionClassificationSummaryPage
}

export type CompanyTransactionCategory =
  | 'PLATFORM_ROOM_REVENUE'
  | 'RELATED_PARTY_CURRENT'
  | 'PAYROLL'
  | 'FINANCING'
  | 'BOTTLED_WATER'
  | 'INTERNAL_TRANSFER'
  | 'RENT'
  | 'RENTAL_INCOME'
  | 'BANK_INTEREST'
  | 'LINEN_LAUNDRY'
  | 'OPERATING_FEE'

export type CompanyOperatingFeeReportingItem =
  | 'BANK_FEES'
  | 'TAX'
  | 'INSURANCE'
  | 'DISINFECTION'
  | 'ELEVATOR'
  | 'FIRE_SAFETY'
  | 'FRESH_FOOD'
  | 'MOONCAKE'
  | 'HOTEL_TECH'
  | 'HOTEL_SUPPLIES'
  | 'OPERATING_FEE'

export type CompanyTransactionCashflowRole =
  | 'OPERATING_INCOME'
  | 'OPERATING_EXPENSE'
  | 'NON_OPERATING'

export type CompanyTransactionClassification = {
  transaction_ref: string
  entity_ref: string
  company_name: string
  occurred_at: string
  amount_minor: number
  currency: 'CNY'
  counterparty_name: string | null
  transaction_name: string
  status: 'PENDING'
  category_code: null
  cashflow_role: null
  revision: number
  source: 'AUTO_RULE'
  rule_version: string
}

export type CompanyTransactionClassificationsResponse = {
  contract_version: 'ledgerbridge.company-transaction-classifications-bff.v1'
  items: CompanyTransactionClassification[]
}

export type CompanyTransactionCategorySummary = {
  category_code: CompanyTransactionCategory
  reporting_item_code: string | null
  reporting_item_label: string | null
  cashflow_role: CompanyTransactionCashflowRole
  transaction_count: number
  inflow_minor: number
  outflow_minor: number
  net_minor: number
  gross_minor: number
  transaction_share_ppm: number
  gross_share_ppm: number
}

export type CompanyTransactionClassificationSummary = {
  entity_ref: string
  company_name: string
  from_date: string
  to_date_exclusive: string
  confirmed_count: number
  pending_count: number
  confirmed_gross_minor: number
  categories: CompanyTransactionCategorySummary[]
}

export type CompanyTransactionClassificationSummaryPage = {
  contract_version: 'ledgerbridge.company-transaction-classification-summary.v2'
  items: CompanyTransactionClassificationSummary[]
}

export type CompanyTransactionClassificationReviewReceipt = {
  contract_version: 'ledgerbridge.company-transaction-classification-review.v1'
  transaction_ref: string
  status: 'CONFIRMED'
  category_code: CompanyTransactionCategory
  reporting_item_code: string | null
  reporting_item_revision: number | null
  revision: number
  created: boolean
}

export type PersonalBankTransaction = {
  statement_ref: string
  source_row_number: number
  occurred_at: string
  amount_minor: number
  balance_minor: number
  currency: 'CNY'
  counterparty_name: string | null
  counterparty_account_masked: string | null
  counterparty_institution: string | null
  transaction_name: string
}

export type PersonalBankStatement = {
  statement_ref: string
  managed_account_ref: string
  institution_code: string
  account_suffix: string
  period_start: string
  period_end: string
  transaction_count: number
  review_status: 'PENDING' | 'CONFIRMED' | 'REJECTED'
  review_revision: number
}

export type PersonalBankTransactionsResponse = {
  contract_version: 'ledgerbridge.personal-bank-transactions-bff.v2'
  snapshot_revision: string
  owner_kind: 'PERSON'
  statements: PersonalBankStatement[]
  summary: {
    currency: 'CNY'
    statement_count: number
    transaction_count: number
    cash_inflow_minor: number
    cash_outflow_minor: number
    net_cash_flow_minor: number
  }
  items: PersonalBankTransaction[]
}

export type PersonalBankStatementReviewReceipt = {
  contract_version: 'ledgerbridge.bank-statement-review.v1'
  statement_ref: string
  decision: 'CONFIRMED' | 'REJECTED'
  revision: number
  created: boolean
}

export type CompanyBankStatement = PersonalBankStatement & {
  company_name: string
}

export type CompanyBankStatementsResponse = {
  contract_version: 'ledgerbridge.company-bank-statements-bff.v1'
  statements: CompanyBankStatement[]
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

export type PayrollDisbursementSourceRecord = {
  record_ref: string
  entity_ref: string
  company_name: string
  pay_period: string
  occurred_at: string
  actual_amount_minor: number
  direction: 'OUTFLOW' | 'INFLOW' | 'ZERO'
  currency: 'CNY'
  source_channel: 'MYBANK' | 'BOC' | 'BANK'
  source_system: string
  source_artifact_ref: string
  source_statement_ref: string
  source_row_number: number
  ingested_at: string
  managed_account_ref: string
  disbursement_account_masked: string
  counterparty_name: string | null
  counterparty_account_masked: string | null
  transaction_name: string
  classification_revision: number
  classification_source: 'AUTO_RULE' | 'HUMAN_REVIEW' | 'BACKFILL'
  classification_rule_version: string
  period_assignment_source: 'NEXT_MONTH_RULE'
  period_assignment_rule_version: 'payroll-next-month-disbursement.2026-09.v1'
  parse_status: 'PARSED'
  link_status: 'UNMATCHED' | 'UNSUPPORTED_DIRECTION'
  payable: false
  submission_supported: false
}

export type PayrollDisbursementRecordPage = {
  schema_version: 'ledgerbridge.payroll-disbursement-records.v1'
  pay_period: string
  source_artifact_count: number
  record_count: number
  unmatched_count: number
  records: PayrollDisbursementSourceRecord[]
  payable: false
  submission_supported: false
}

export type PayrollTestRoutingStatus = 'AUTO_TEST' | 'REVIEW_REQUIRED' | 'DATE_UNKNOWN'

export type PayrollTestMaterialType =
  | 'PAYROLL_SHEET'
  | 'RELEASE_LIST'
  | 'CASH_LIST'
  | 'ATTENDANCE_SHEET'
  | 'AUNT_ATTENDANCE_SHEET'
  | 'REVIEW_STATISTICS'
  | 'ADJUSTMENT_SOURCE'
  | 'PAYROLL_SUMMARY'
  | 'SUPPORTING_SCAN'
  | 'BACKUP'
  | 'OBSOLETE'

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

export type PayrollTestMaterialPreviewLine = {
  source_row: number
  company_id: string
  employee_id: string
  employee_name: string
  account_id: string
  account_masked: string
  payment_channel: string
  base_salary_cents: number
  allowance_cents: number
  bonus_cents: number
  deduction_cents: number
  social_insurance_cents: number
  housing_fund_cents: number
  individual_income_tax_cents: number
  gross_pay_cents: number
  net_pay_cents: number
  notes: string
}

export type PayrollTestMaterialPreview = {
  schema_version: 'payroll-test-material-preview/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  material_id: string
  period: string
  routing_status: 'AUTO_TEST' | 'REVIEW_REQUIRED'
  auto_batch_eligible: boolean
  status: 'READY_FOR_REVIEW' | 'NEEDS_HUMAN_REVIEW'
  line_count: number
  total_net_pay_cents: number
  lines: PayrollTestMaterialPreviewLine[]
  exceptions: Array<{
    code: string
    severity: string
    row: number
    field?: string
    calculated_cents?: number
    stated_cents?: number
  }>
  payment_submission_supported: false
  payable: false
  submission_supported: false
}

export type PayrollTestMaterialPreviewResponse = {
  contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1'
  entity_ref: string
  company_id: string
  material_id: string
  data: PayrollTestMaterialPreview
}

export type PayrollInputMaterialPreview = {
  schema_version: 'payroll-input-material-preview/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  material_id: string
  /** Any YYYY-MM the provider returns; the test window is a cutoff, not a fixed pair. */
  period: string
  material_type: 'ATTENDANCE_SHEET' | 'AUNT_ATTENDANCE_SHEET' | 'REVIEW_STATISTICS' | 'ADJUSTMENT_SOURCE'
  detected_material_type: 'ATTENDANCE_SHEET' | 'AUNT_ATTENDANCE_SHEET' | 'REVIEW_STATISTICS' | 'UNRECOGNIZED'
  canonical_name: string
  selected_sheet: string
  sheet_names: string[]
  columns: string[]
  record_count: number
  preview_rows: Array<{ source_row: number; values: string[] }>
  status: 'READY_FOR_REVIEW' | 'NEEDS_HUMAN_REVIEW'
  payment_submission_supported: false
  payable: false
  submission_supported: false
}

export type PayrollInputMaterialPreviewResponse = {
  contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1'
  entity_ref: string
  company_id: string
  material_id: string
  data: PayrollInputMaterialPreview
}

export type PayrollSummaryStoreTotal = {
  store_name: string
  net_pay_cents: number
}

export type PayrollSummaryPeriod = {
  period: string
  store_count: number
  stores: PayrollSummaryStoreTotal[]
  total_net_pay_cents: number
  total_source: 'SUMMARY_TOTAL_ROW' | 'SUM_OF_SUMMARY_STORE_ROWS'
  total_matches_stores: boolean
}

export type PayrollSummaryAuthoritativePreview = {
  schema_version: 'payroll-summary-authoritative-preview/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  material_id: string
  routing_status: PayrollTestRoutingStatus
  source_of_truth: 'PAYROLL_SUMMARY'
  authoritative: true
  period_count: number
  latest_period: string
  periods: PayrollSummaryPeriod[]
  payment_submission_supported: false
  payable: false
  submission_supported: false
}

export type PayrollSummaryAuthoritativePreviewResponse = {
  contract_version: 'ledgerbridge.payroll-test-material-preview-read.v1'
  entity_ref: string
  company_id: string
  material_id: string
  data: PayrollSummaryAuthoritativePreview
}

export type PayrollTestMaterialOrganizeResult = {
  schema_version: 'payroll-test-material-organize-result/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  workspace_revision: number
  projection_revision: string
  material: PayrollTestWorkspaceMaterial
  payment_submission_supported: false
  payable: false
  submission_supported: false
  replayed: boolean
}

export type PayrollTestBatchResult = {
  batch_id: string
  period: string
  material_count: number
  payroll_sheet_count: number
  supporting_material_count: number
  status: 'READY_FOR_TEST_REVIEW' | 'BLOCKED'
}

export type PayrollTestBatchValidationResult = {
  schema_version: 'payroll-test-batch-validation-result/v1'
  data_scope: 'TEST_ONLY'
  test_batch_id: string
  company_id: string
  workspace_revision: number
  ready_batch_count: number
  blocked_material_count: number
  batches: PayrollTestBatchResult[]
  payment_submission_supported: false
  payable: false
  submission_supported: false
  replayed: boolean
}

export type PayrollTestWorkspaceCommandResult<T> = {
  contract_version: 'ledgerbridge.payroll-test-workspace-command-result.v1'
  entity_ref: string
  company_id: string
  action: 'payroll.test_workspace.organize' | 'payroll.test_workspace.validate'
  resource_ref: string
  replayed: boolean
  data: T
}

export type PayrollLegacyAction =
  | 'FILL_MAIN'
  | 'GENERATE_MONTHLY_PAYROLL'
  | 'GENERATE_NORMAL_DRAFT'
  | 'GENERATE_SUPPLEMENTAL_DRAFT'
  | 'UPDATE_SUMMARY'
  | 'SAVE_RULES'
  | 'INITIALIZE_RULES'
  | 'CHECK_RULES_AND_HISTORY'
  | 'VERIFY_CURRENT_PAID'
  | 'VERIFY_AND_UPDATE_SUMMARY'
  | 'CHECK_PREVIOUS_PENDING'

export type PayrollLegacyLine = PayrollTestMaterialPreviewLine & {
  payment_kind?: 'NORMAL' | 'CASH' | 'SUPPLEMENT'
  night_shift_rate_cents?: number
  rest_days?: number
  job_group?: string
  location?: string
  disbursement_company?: string
}

export type PayrollLegacyAdjustment = {
  employee_id: string
  item_code: string
  kind: 'PERFORMANCE' | 'SPECIAL'
  amount_cents: number
  reason: string
  disposition: 'MAIN' | 'SUPPLEMENT'
  source_pending_id?: string
}

export type PayrollLegacyDraftLine = {
  employee_id: string
  account_id: string
  account_masked: string
  amount_cents: number
  payment_channel: string
  memo: string
}

export type PayrollLegacyDraft = {
  schema_version: 'payroll-bank-draft/v1'
  draft_id: string
  draft_type: 'normal_bank_payroll' | 'supplemental_bank_payroll'
  company_id: string
  batch_id: string
  pay_period: string
  version: number
  disbursement_company?: string
  lines: PayrollLegacyDraftLine[]
  total_amount_cents: number
  warning: string
  payable: false
  submission_supported: false
}

export type PayrollLegacyPendingItem = {
  pending_id: string
  source_batch_id: string
  source_period: string
  employee_id: string
  account_id: string
  amount_cents: number
  direction: 'ADD' | 'DEDUCT'
  reason: string
  status: 'OPEN' | 'RESOLVED' | 'IGNORED'
  decision?: 'ADD_TO_MAIN' | 'SUPPLEMENT' | 'IGNORE'
  resolution_reason?: string
  resolved_in_period?: string
}

export type PayrollLegacyEvidenceType =
  | 'MYBANK_STATEMENT'
  | 'BOC_RECEIPT'
  | 'WECHAT_RECEIPT'

export type PayrollLegacyEvidenceDocument = {
  evidence_type: PayrollLegacyEvidenceType
  evidence_ref: string
}

export type PayrollLegacyCurrentPaidVerification = {
  schema_version: 'payroll-current-paid-verification/v2'
  company_id: string
  batch_id: string
  period: string
  evidence_documents: PayrollLegacyEvidenceDocument[]
  evidence_summary: Array<{
    evidence_type: PayrollLegacyEvidenceType
    required_count: number
    received_count: number
  }>
  theoretical_total_cents: number
  actual_total_cents: number
  difference_cents: number
  totals_match: boolean
  by_payment_channel: Array<{
    payment_channel: 'MYBANK' | 'BOC' | 'WECHAT'
    expected_amount_cents: number
    actual_amount_cents: number
    difference_cents: number
    totals_match: boolean
  }>
  overall_status: 'MATCHED' | 'ATTENTION_REQUIRED'
  results: Array<{
    employee_id: string
    account_id: string
    payment_channel: 'MYBANK' | 'BOC' | 'WECHAT'
    expected_amount_cents: number
    actual_amount_cents: number
    difference_cents: number
    status: 'MATCHED' | 'MISSING_RECEIPT' | 'IDENTITY_MISMATCH' | 'PAYMENT_FAILED' | 'UNDERPAID' | 'OVERPAID'
  }>
  verified_at: string
  payable: false
  submission_supported: false
}

export type PayrollLegacyBatch = {
  batch_id: string
  period: string
  revision: number
  main_material_id?: string
  supporting_material_ids: Record<string, string>
  lines: PayrollLegacyLine[]
  adjustments: PayrollLegacyAdjustment[]
  source_exceptions: Array<Record<string, unknown>>
  drafts: PayrollLegacyDraft[]
  summary: null | {
    schema_version: 'payroll-monthly-summary/v1'
    company_id: string
    batch_id: string
    period: string
    employee_count: number
    gross_pay_cents: number
    net_pay_cents: number
    by_payment_channel: Array<{ payment_channel: string; amount_cents: number }>
    by_location?: Array<{
      location: string
      employee_count: number
      gross_pay_cents: number
      net_pay_cents: number
    }>
    payable: false
    submission_supported: false
  }
  verification: null | PayrollLegacyCurrentPaidVerification | {
    schema_version: 'payroll-current-paid-verification/v1'
    [key: string]: unknown
  }
  pending_items: PayrollLegacyPendingItem[]
  checks: null | {
    schema_version: 'payroll-rules-history-check/v1'
    current_issues: Array<Record<string, unknown>>
    history_issues: Array<Record<string, unknown>>
    [key: string]: unknown
  }
}

export type PayrollLegacyEmployeeRule = {
  employee_id: string
  employee_name: string
  account_id: string
  account_masked: string
  disbursement_company: string
  fixed_base_salary_cents: number
  fixed_allowance_cents: number
  fixed_adjustment_cents?: number
  night_shift_rate_cents: number
  rest_days: number
  payment_channel: 'MYBANK' | 'BOC' | 'WECHAT' | 'CASH'
  payment_kind: 'NORMAL' | 'CASH' | 'SUPPLEMENT'
  job_group: string
  location: string
}

export type PayrollLegacyReviewRuleType =
  | 'PAYMENT_CHANNEL_REQUIRED'
  | 'SUPPORTING_MATERIAL_REQUIRED'
  | 'HISTORY_CHANGE_REVIEW'

export type PayrollLegacyReviewRule = {
  rule_id: string
  name: string
  rule_type: PayrollLegacyReviewRuleType
  enabled: boolean
  severity: 'BLOCKING' | 'REVIEW'
  threshold_cents: number
}

export type PayrollLegacyWorkspace = {
  schema_version: 'payroll-legacy-feature-workspace/v1'
  data_scope: 'TEST_ONLY'
  company_id: string
  test_batch_id: string
  revision: number
  active_period: string
  rules: {
    revision: number
    employees: PayrollLegacyEmployeeRule[]
    review_rules?: PayrollLegacyReviewRule[]
  }
  batches: PayrollLegacyBatch[]
  audit_events: Array<{
    sequence: number
    action: string
    period: string
    occurred_at: string
    reason: string
  }>
  payment_submission_supported: false
  payable: false
  submission_supported: false
}

export type PayrollLegacyWorkspaceReadResponse = {
  contract_version: 'ledgerbridge.payroll-legacy-feature-read.v1'
  entity_ref: string
  company_id: string
  data: PayrollLegacyWorkspace
}

export type PayrollLegacyCommandResult = {
  contract_version: 'ledgerbridge.payroll-legacy-feature-command-result.v1'
  entity_ref: string
  company_id: string
  action: 'payroll.test_workspace.legacy.command'
  resource_ref: string
  replayed: boolean
  data: {
    action: PayrollLegacyAction
    replayed: boolean
    workspace: PayrollLegacyWorkspace
  }
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
