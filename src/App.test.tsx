import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Theme } from '@radix-ui/themes'
import App from './App'
import type { AccountingDimensions, ApiCandidate, AuthStatus, CashReconciliation, ClassificationGroup, EvidencePreview, OriginalReconciliation, ReviewEvent } from './types'
import { originalReconciliationFixture } from './test-fixtures/original-reconciliation'

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
    business_unit_ref: 'unit-south', category: '布草', category_code: 'LINEN', amount_minor: 638000, currency: 'CNY', accounting_month: '2026-08',
    summary: '城南店 8 月布草清洗费用，供应商月结单', confidence_basis_points: 9600, evidence, blockers: [], review_risks: [],
  },
  {
    id: 'candidate-2', short_id: 'C-62D9', revision: 1, status: 'INCOMPLETE', source_channel: 'weixin',
    source_message_id: 'message-2', received_at: '2026-08-23T17:35:00+08:00', business_unit: '机场店',
    business_unit_ref: 'unit-airport', category: '水费', category_code: 'WATER', amount_minor: 483260, currency: 'CNY', accounting_month: null,
    summary: '机场店水费，原消息未说明归属月份', confidence_basis_points: 8800, evidence,
    blockers: [{ code: 'MISSING_ACCOUNTING_MONTH', message: '缺少归属月份' }], review_risks: [],
  },
  {
    id: 'candidate-3', short_id: 'C-5B17', revision: 2, status: 'CONFLICTED', source_channel: 'dingtalk',
    source_message_id: 'message-3', received_at: '2026-08-23T14:02:00+08:00', business_unit: '城南店',
    business_unit_ref: 'unit-south', category: '银行收款', category_code: 'SETTLEMENT', amount_minor: 1268000, currency: 'CNY', accounting_month: '2026-08',
    summary: '城南店银行收款，与另一条候选冲突', confidence_basis_points: 9400, evidence,
    blockers: [{ code: 'BUSINESS_KEY_CONFLICT', message: '相同凭证号金额不同' }], review_risks: [],
  },
  {
    id: 'candidate-4', short_id: 'C-49E3', revision: 4, status: 'CONFIRMED', source_channel: 'dingtalk',
    source_message_id: 'message-4', received_at: '2026-08-21T11:28:00+08:00', business_unit: '江景店',
    business_unit_ref: 'unit-river', category: '税费', category_code: 'TAX', amount_minor: 924050, currency: 'CNY', accounting_month: '2026-08',
    summary: '江景店本月税费缴款', confidence_basis_points: 9800, evidence, blockers: [], review_risks: [],
  },
]

const reconciliation = {
  accounting_month: '2026-08', revision: 7, ready: false,
  blockers: [{ code: 'BUSINESS_KEY_CONFLICT', message: '相同凭证号金额不同' }],
  business_units: [{ name: '城南店', amounts_minor: { water: 512080, linen: 638000, bank_receipts: 4286000 } }],
}

const cashReconciliation: CashReconciliation = {
  contract_version: 'ledgerbridge.cash-reconciliation.v2',
  accounting_month: '2026-09',
  rules: [
    { rule_key: 'income.synthetic', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.synthetic', flow_kind: 'INCOME', business_unit_label: '示例门店', item_label: '平台实收', match_pattern: 'synthetic-income', amount_direction: 'CREDIT', effective_from: '2026-01-01', effective_to: null },
    { rule_key: 'expense.synthetic', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.synthetic', flow_kind: 'EXPENSE', business_unit_label: '示例门店', item_label: '布草', match_pattern: 'synthetic-expense', amount_direction: 'DEBIT', effective_from: '2026-01-01', effective_to: null },
    { rule_key: 'current.synthetic', source_kind: 'CANDIDATE', source_ref: 'wechat.synthetic', flow_kind: 'CURRENT', business_unit_label: '示例门店', item_label: '往来款', match_pattern: 'synthetic-current', amount_direction: 'ANY', effective_from: '2026-01-01', effective_to: null },
  ],
  rows: [
    { rule_key: 'income.synthetic', flow_kind: 'INCOME', business_unit_label: '示例门店', item_label: '平台实收', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.synthetic', transaction_count: 1, amount_minor: 10_000, facts: [{ fact_ref: 'income-1', occurred_on: '2026-09-01', amount_minor: 10_000 }] },
    { rule_key: 'expense.synthetic', flow_kind: 'EXPENSE', business_unit_label: '示例门店', item_label: '布草', source_kind: 'BANK_TRANSACTION', source_ref: 'bank.synthetic', transaction_count: 1, amount_minor: 3_000, facts: [{ fact_ref: 'expense-1', occurred_on: '2026-09-02', amount_minor: -3_000 }] },
    { rule_key: 'current.synthetic', flow_kind: 'CURRENT', business_unit_label: '示例门店', item_label: '往来款', source_kind: 'CANDIDATE', source_ref: 'wechat.synthetic', transaction_count: 1, amount_minor: 2_000, facts: [{ fact_ref: 'current-1', occurred_on: '2026-09-01', amount_minor: 2_000 }] },
  ],
  issues: [],
  eligible_fact_count: 3,
  matched_fact_count: 3,
  unmatched_fact_count: 0,
  conflicted_fact_count: 0,
  issue_count: 0,
  issues_truncated: false,
  totals: { income_minor: 10_000, expense_minor: 3_000, current_minor: 2_000 },
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
  changes: [{ field: 'status', previous_value: 'PENDING', new_value: 'CONFIRMED', identity_changed: false }],
  conflict_resolution: null,
  created_at: '2026-08-21T11:35:00+08:00',
}]

const payrollEntityRef = '30000000-0000-4000-8000-000000000001'
const payrollCompanyId = 'company_hotel_001'
const payrollBatchId = 'batch_0123456789abcdef01234567'
const payrollArtifactId = 'artifact_0123456789abcdef01234567'
const payrollEvidence = [
  ...Array.from({ length: 5 }, (_, index) => ({
    artifact_id: index === 0 ? payrollArtifactId : `artifact_mybank_company_${index + 1}_2026_08`,
    evidence_type: 'MYBANK_STATEMENT',
  })),
  { artifact_id: 'artifact_boc_cash_2026_08', evidence_type: 'BOC_RECEIPT' },
  { artifact_id: 'artifact_wechat_separate_2026_08', evidence_type: 'WECHAT_RECEIPT' },
]
const payrollEvidenceIds = payrollEvidence.map((item) => item.artifact_id)

function payrollRead(data: unknown) {
  return {
    contract_version: 'ledgerbridge.payroll-read.v1',
    entity_ref: payrollEntityRef,
    company_id: payrollCompanyId,
    data,
  }
}

const livePayrollResponses: Record<string, unknown> = {
  '/api/v1/payroll/status': payrollRead({
    schema_version: 'ledgerbridge.payroll-status.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    live_data_ready: true,
    live_projection_schema: 'payroll-ledgerbridge-live-projection/v1',
    payment_operations_exposed: false,
    setup_summary: {
      provider_connected: true,
      runtime_mode: 'live-provider',
      unassigned_material_count: 3,
      ready_material_count: 0,
      company_mapped_material_count: 1,
      blocking_reason_codes: [],
    },
    capabilities: {
      commands_enabled: true,
      allowed_actions: ['VERIFY_RECEIPTS'],
    },
  }),
  '/api/v1/payroll/test-workspace': {
    contract_version: 'ledgerbridge.payroll-test-workspace-read.v1',
    entity_ref: payrollEntityRef,
    company_id: payrollCompanyId,
    data: {
      contract_version: '1.0.0',
      schema_version: 'payroll-ledgerbridge-test-projection/v1',
      data_scope: 'TEST_ONLY',
      test_batch_id: 'payroll_history_20260831',
      company_id: payrollCompanyId,
      cutoff_date: '2026-08-31',
      workspace_revision: 1,
      projection_revision: 'b'.repeat(64),
      etag: `"${'b'.repeat(64)}"`,
      generated_at: '2026-09-01T02:00:00.000Z',
      auto_test_ready: true,
      payment_submission_supported: false,
      payable: false,
      submission_supported: false,
      routing_counts: { auto_test: 1, review_required: 0, date_unknown: 0 },
      materials: [{
        company_id: payrollCompanyId,
        material_id: 'material_attendance_001',
        routing_status: 'AUTO_TEST',
        period: '2026-08',
        material_type: 'ATTENDANCE_SHEET',
        payable: false,
        submission_supported: false,
      }],
    },
  },
  '/api/v1/payroll/dashboard': payrollRead({
    schema_version: 'ledgerbridge.payroll-dashboard.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-08-30T08:00:00.000Z',
    live_data_ready: true,
    setup_summary: {
      provider_connected: true,
      runtime_mode: 'live-provider',
      unassigned_material_count: 3,
      ready_material_count: 0,
      company_mapped_material_count: 1,
      blocking_reason_codes: [],
    },
    dashboard: {
      batch_count: 1,
      material_count: 1,
      materials_needing_review_count: 1,
      verification_attention_count: 0,
      unassigned_material_count: 3,
      net_pay_minor: 524000,
    },
  }),
  '/api/v1/payroll/materials': payrollRead({
    schema_version: 'ledgerbridge.payroll-material-list.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-08-30T08:00:00.000Z',
    items: [{
      company_id: payrollCompanyId,
      material_id: 'material_0123456789abcdef01234567',
      period: '2026-08',
      material_type: 'PAYROLL_SHEET',
      status: 'NEEDS_REVIEW',
      review_revision: 0,
      payable: false,
      submission_supported: false,
    }],
  }),
  '/api/v1/payroll/batches': payrollRead({
    schema_version: 'ledgerbridge.payroll-batch-list.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-08-30T08:00:00.000Z',
    items: [{
      company_id: payrollCompanyId,
      batch_id: payrollBatchId,
      pay_period: '2026-08',
      revision: 4,
      status: 'DRAFT',
      payable: false,
      submission_supported: false,
      payment_submission_supported: false,
      lines: [{
        company_id: payrollCompanyId,
        employee_id: 'employee_live_001',
        employee_display: '员••0001',
        account_id: 'account_live_001',
        account_display: '**** 0123',
        net_pay_minor: 524000,
      }],
      audit_closure: {
        audit_event_id: 'audit_0123456789abcdef01234567',
        audit_hash: 'c'.repeat(64),
      },
    }],
  }),
  '/api/v1/payroll/verification': payrollRead({
    schema_version: 'ledgerbridge.payroll-verification-list.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    generated_at: '2026-08-30T08:00:00.000Z',
    items: [{
      verification_id: 'verification_0123456789abcdef01234567',
      company_id: payrollCompanyId,
      batch_id: payrollBatchId,
      source_artifact_ids: payrollEvidenceIds,
      status: 'MATCHED',
      results: [{
        company_id: payrollCompanyId,
        employee_id: 'employee_live_001',
        employee_display: '员••0001',
        account_id: 'account_live_001',
        account_display: '**** 0123',
        status: 'MATCHED',
      }],
      payable: false,
      submission_supported: false,
      payment_submission_supported: false,
    }],
    available_evidence: payrollEvidence.map((evidence) => ({
      company_id: payrollCompanyId,
      ...evidence,
      period: '2026-08',
      status: 'READY_FOR_MATCHING',
      display_label: `${evidence.evidence_type} · 2026-08`,
    })),
  }),
}

const notReadyPayrollResponses: Record<string, unknown> = {
  '/api/v1/payroll/status': payrollRead({
    schema_version: 'ledgerbridge.payroll-status.v1',
    projection_revision: 'a'.repeat(64),
    etag: `"${'a'.repeat(64)}"`,
    live_data_ready: false,
    live_projection_schema: 'payroll-ledgerbridge-live-projection/v1',
    payment_operations_exposed: false,
    capabilities: {
      commands_enabled: false,
      allowed_actions: [],
    },
    setup_summary: {
      provider_connected: true,
      runtime_mode: 'live-provider',
      unassigned_material_count: 3,
      ready_material_count: 0,
      company_mapped_material_count: 1,
      blocking_reason_codes: [
        'UNASSIGNED_MATERIALS',
        'MATERIAL_REVIEW_REQUIRED',
        'PAYROLL_BATCH_REQUIRED',
        'LIVE_DATA_NOT_READY',
      ],
    },
  }),
  '/api/v1/payroll/test-workspace': {
    contract_version: 'ledgerbridge.payroll-test-workspace-read.v1',
    entity_ref: payrollEntityRef,
    company_id: payrollCompanyId,
    data: {
      contract_version: '1.0.0',
      schema_version: 'payroll-ledgerbridge-test-projection/v1',
      data_scope: 'TEST_ONLY',
      test_batch_id: 'payroll_history_20260831',
      company_id: payrollCompanyId,
      cutoff_date: '2026-08-31',
      workspace_revision: 1,
      projection_revision: 'b'.repeat(64),
      etag: `"${'b'.repeat(64)}"`,
      generated_at: '2026-09-01T02:00:00.000Z',
      auto_test_ready: true,
      payment_submission_supported: false,
      payable: false,
      submission_supported: false,
      routing_counts: { auto_test: 1, review_required: 0, date_unknown: 0 },
      materials: [{
        company_id: payrollCompanyId,
        material_id: 'material_attendance_001',
        routing_status: 'AUTO_TEST',
        period: '2026-08',
        material_type: 'ATTENDANCE_SHEET',
        payable: false,
        submission_supported: false,
      }],
    },
  },
}

const reportBases = ['CONFIRMED_CANDIDATE', 'ACCOUNT_STATEMENT', 'POSTED_LEDGER'] as const
type TestReportBasis = typeof reportBases[number]

function reportMetrics(basis: TestReportBasis, posted = false) {
  if (basis === 'CONFIRMED_CANDIDATE') return {
    basis,
    confirmed_positive_minor: 0,
    confirmed_negative_minor: 0,
    confirmed_net_minor: 0,
    confirmed_count: 61,
    source_count: 61,
  }
  if (basis === 'ACCOUNT_STATEMENT') return {
    basis,
    cash_inflow_minor: 0,
    cash_outflow_minor: 0,
    net_cash_flow_minor: 0,
    confirmed_transaction_count: 0,
    statement_count: 0,
  }
  return {
    basis,
    revenue_minor: posted ? 800000 : 0,
    expense_minor: posted ? 235000 : 0,
    profit_minor: posted ? 565000 : 0,
    posted_entry_count: posted ? 3 : 0,
    source_count: posted ? 2 : 0,
  }
}

function reportAggregate(basis: TestReportBasis, posted = false) {
  return {
    metrics: reportMetrics(basis, posted),
    pending_review_count: basis === 'CONFIRMED_CANDIDATE' ? 146 : 0,
    attribution_pending_count: basis === 'CONFIRMED_CANDIDATE' ? 61 : 0,
    missing_material_count: null,
    taxonomy_version: null,
    balance: {
      balance_basis: 'UNAVAILABLE',
      opening_balance_minor: null,
      closing_balance_minor: null,
      gap: 'AUTHORITATIVE_BALANCE_UNAVAILABLE',
    },
  }
}

function reportCompany(basis: TestReportBasis, posted = false) {
  return {
    company_ref: '10000000-0000-4000-8000-000000000001',
    company_name: '演示公司',
    currency: 'CNY',
    business_unit_breakdown_status: 'AVAILABLE',
    ...reportAggregate(basis, posted),
    months: [{
      month: '2026-08',
      ...reportAggregate(basis, posted),
      business_unit_breakdown_status: 'AVAILABLE',
      business_units: [{
        business_unit_ref: 'unit-demo-a',
        business_unit_label: '演示门店',
        ...reportAggregate(basis, posted),
      }],
    }],
  }
}

function companyReports(withCompany = false, posted = false) {
  return {
    contract_version: 'ledgerbridge.company-reports-bff.v1',
    from_month: '2026-01',
    to_month: '2026-08',
    posted_ledger_status: 'AVAILABLE',
    layers: reportBases.map((basis) => ({
      contract_version: 'ledgerbridge.company-report.v1',
      basis,
      from_month: '2026-01',
      to_month: '2026-08',
      items: withCompany ? [reportCompany(basis, posted && basis === 'POSTED_LEDGER')] : [],
    })),
  }
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': status >= 400 ? 'application/problem+json' : 'application/json' },
  })
}

function personalBankTransactions() {
  return {
    contract_version: 'ledgerbridge.personal-bank-transactions-bff.v2',
    snapshot_revision: 'a'.repeat(64),
    owner_kind: 'PERSON',
    statements: [{
      statement_ref: '70000000-0000-4000-8000-000000000007',
      managed_account_ref: '80000000-0000-4000-8000-000000000008',
      institution_code: 'mybank',
      account_suffix: '7968',
      period_start: '2026-07-01',
      period_end: '2026-07-02',
      transaction_count: 2,
      review_status: 'CONFIRMED',
      review_revision: 1,
    }],
    summary: {
      currency: 'CNY',
      statement_count: 1,
      transaction_count: 2,
      cash_inflow_minor: 10000,
      cash_outflow_minor: 2500,
      net_cash_flow_minor: 7500,
    },
    items: [
      {
        statement_ref: '70000000-0000-4000-8000-000000000007',
        source_row_number: 2,
        occurred_at: '2026-07-01T09:30:00+08:00',
        amount_minor: 10000,
        balance_minor: 20000,
        currency: 'CNY',
        counterparty_name: '正式对方甲',
        counterparty_account_masked: '******1234',
        counterparty_institution: '测试银行',
        transaction_name: '转入',
      },
      {
        statement_ref: '70000000-0000-4000-8000-000000000007',
        source_row_number: 3,
        occurred_at: '2026-07-02T10:30:00+08:00',
        amount_minor: -2500,
        balance_minor: 17500,
        currency: 'CNY',
        counterparty_name: '正式对方乙',
        counterparty_account_masked: null,
        counterparty_institution: null,
        transaction_name: '消费',
      },
    ],
  }
}

const emptyPersonalBankTransactions = {
  contract_version: 'ledgerbridge.personal-bank-transactions-bff.v2',
  snapshot_revision: '0'.repeat(64),
  owner_kind: 'PERSON',
  statements: [],
  summary: {
    currency: 'CNY',
    statement_count: 0,
    transaction_count: 0,
    cash_inflow_minor: 0,
    cash_outflow_minor: 0,
    net_cash_flow_minor: 0,
  },
  items: [],
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
  payrollResponses?: Record<string, unknown>
  payrollVerifyResult?: unknown
  failAccountingDimensions?: boolean
  failAccountingDimensionsAfterFirst?: boolean
  failCandidateDetail?: boolean
  candidateDetails?: Record<string, ApiCandidate>
  accountingDimensions?: AccountingDimensions
  classificationGroups?: ClassificationGroup[]
  failClassificationGroupsOnce?: boolean
  failClassificationGroupsAfterFirst?: boolean
  failCandidatesAfterFirstOnce?: boolean
  batchFailureStatus?: number
  companyReportResponse?: unknown
  failCompanyReportsOnce?: boolean
  failOriginalReconciliation?: boolean
  failCashReconciliation?: boolean
  cashReconciliation?: CashReconciliation
  personalBankResponse?: unknown
  failPersonalBank?: boolean
  originalReconciliation?: OriginalReconciliation
  originalReconciliationGate?: Promise<void>
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
    payrollResponses = {},
    payrollVerifyResult = {
      contract_version: 'ledgerbridge.payroll-command-result.v1',
      entity_ref: payrollEntityRef,
      company_id: payrollCompanyId,
      action: 'payroll.batch.verify-receipts',
      resource_ref: payrollBatchId,
      replayed: false,
      data: {
        schema_version: 'payroll-ledgerbridge-command-receipt/v1',
        company_id: payrollCompanyId,
        resource_id: payrollBatchId,
        action: 'payroll.receipts.verify',
        audit_event_id: 'audit_0123456789abcdef01234567',
        audit_hash: 'd'.repeat(64),
        occurred_at: '2026-08-30T08:00:00.000Z',
        idempotency_key: '11111111-1111-4111-8111-111111111111',
        replayed: false,
        audit_closure: {
          company_id: payrollCompanyId,
          resource_id: payrollBatchId,
          action: 'payroll.receipts.verify',
          actor_subject: 'checker_live',
          actor_id: 'actor_checker_001',
          audit_event_id: 'audit_0123456789abcdef01234567',
          audit_hash: 'd'.repeat(64),
          occurred_at: '2026-08-30T08:00:00.000Z',
        },
      },
    },
    failAccountingDimensions = false,
    failAccountingDimensionsAfterFirst = false,
    failCandidateDetail = false,
    candidateDetails = {},
    accountingDimensions,
    classificationGroups = [],
    failClassificationGroupsOnce = false,
    failClassificationGroupsAfterFirst = false,
    failCandidatesAfterFirstOnce = false,
    batchFailureStatus,
    companyReportResponse = companyReports(),
    failCompanyReportsOnce = false,
    failOriginalReconciliation = false,
    failCashReconciliation = false,
    cashReconciliation: cashProjection = cashReconciliation,
    personalBankResponse = emptyPersonalBankTransactions,
    failPersonalBank = false,
    originalReconciliation = originalReconciliationFixture,
    originalReconciliationGate,
  } = options
  let shouldFailSession = failSessionOnce
  let shouldFailClassificationGroups = failClassificationGroupsOnce
  let shouldFailCandidatesAfterFirst = failCandidatesAfterFirstOnce
  let candidateListRequestCount = 0
  let classificationGroupRequestCount = 0
  let accountingDimensionsRequestCount = 0
  let decisionSaved = false
  let shouldFailCompanyReports = failCompanyReportsOnce
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
    if (url === `/api/v1/payroll/batches/${payrollBatchId}/verify-receipts` && init?.method === 'POST') {
      return response(payrollVerifyResult)
    }
    if (url in payrollResponses) return response(payrollResponses[url])
    if (url === '/api/v1/company-reports') {
      if (shouldFailCompanyReports) {
        shouldFailCompanyReports = false
        return response({ title: '公司报表暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
      return response(companyReportResponse)
    }
    if (url === '/api/v1/personal-finance/bank-transactions') {
      if (failPersonalBank) {
        return response({ title: '个人正式流水暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
      return response(personalBankResponse)
    }
    if (url === '/api/v1/candidates' || url.startsWith('/api/v1/candidates?')) {
      candidateListRequestCount += 1
      if (shouldFailCandidatesAfterFirst && candidateListRequestCount > 1) {
        shouldFailCandidatesAfterFirst = false
        return response({ title: '候选刷新暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      }
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
    if (url.startsWith('/api/v1/cash-reconciliations/')) {
      if (failCashReconciliation) return response({ title: '规则生成结果暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      const requestedMonth = new URL(url, 'http://ledgerbridge.local').pathname.split('/').at(-1)!
      return response({ ...cashProjection, accounting_month: requestedMonth })
    }
    if (url.startsWith('/api/v1/reconciliations/') && init?.method !== 'POST') {
      if (decisionSaved && failReconciliationAfterDecision) return response({ title: '对账投影暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      return response(reconciliation)
    }
    if (url.startsWith('/api/v1/original-reconciliations/')) {
      if (originalReconciliationGate) await originalReconciliationGate
      if (failOriginalReconciliation) return response({ title: '原口径投影暂不可用', status: 503, code: 'UNAVAILABLE' }, 503)
      const requestedMonth = new URL(url, 'http://ledgerbridge.local').pathname.split('/').at(-1)!
      return response({ ...originalReconciliation, month: requestedMonth })
    }
    if (url === '/api/v1/connections') return response({ items: [] })
    if (url === '/api/v1/accounting-dimensions') {
      accountingDimensionsRequestCount += 1
      if (failAccountingDimensions || (failAccountingDimensionsAfterFirst && accountingDimensionsRequestCount > 1)) {
        return response({ title: '会计维度暂不可用', status: 503, code: 'ACCOUNTING_DIMENSIONS_UNAVAILABLE' }, 503)
      }
      return response(accountingDimensions ?? {
        contract_version: 'ledgerbridge.accounting-dimensions.v1',
        business_units: [...new Map(items.flatMap((candidate) => {
          const stable = candidate.business_unit_ref
          return stable ? [[stable, {
            ref: stable,
            label: candidate.business_unit,
          }] as const] : []
        })).values()],
        categories: [...new Map(items.flatMap((candidate) => {
          const stable = candidate.category_code
          return stable ? [[stable, {
            code: stable,
            label: candidate.category,
          }] as const] : []
        })).values()],
      })
    }
    if (url === '/api/v1/candidate-classification-groups') {
      classificationGroupRequestCount += 1
      if (
        shouldFailClassificationGroups
        || (failClassificationGroupsAfterFirst && classificationGroupRequestCount > 1)
      ) {
        shouldFailClassificationGroups = false
        return response({
          title: 'LedgerBridge Core request failed',
          status: 503,
          code: 'CORE_CONTRACT_INVALID',
        }, 503)
      }
      return response({
        contract_version: 'ledgerbridge.classification-groups.v1',
        items: classificationGroups,
        next_cursor: null,
      })
    }
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
    if (url.includes('/candidate-classification-groups/') && url.endsWith('/decisions') && init?.method === 'POST') {
      decisionSaved = true
      if (batchFailureStatus) {
        return response({
          title: '相似交易组已变化',
          detail: '成员或版本与预览不一致',
          status: batchFailureStatus,
          code: 'CLASSIFICATION_GROUP_STALE',
        }, batchFailureStatus)
      }
      const body = JSON.parse(String(init.body)) as {
        source_candidate_ref: string
        target: { business_unit_ref: string; category_code: string }
        members: Array<{ candidate_ref: string; expected_revision: number }>
        acknowledged_risk_codes: string[]
      }
      return response({
        contract_version: 'ledgerbridge.classification-batch.v1',
        operation_id: '11111111-1111-4111-8111-111111111111',
        replayed: false,
        group_ref: classificationGroups[0]?.group_ref,
        accounting_month: classificationGroups[0]?.accounting_month,
        source_candidate_ref: body.source_candidate_ref,
        target: body.target,
        acknowledged_risk_codes: body.acknowledged_risk_codes,
        results: body.members.map((member, index) => {
          const original = items.find((candidate) => candidate.id === member.candidate_ref)!
          return {
            candidate_ref: member.candidate_ref,
            operation_id: `11111111-1111-4111-8111-11111111111${index}`,
            status: 'APPLIED',
            candidate: { ...original, revision: original.revision + 1, status: 'CONFIRMED' },
            events: [{
              id: `group-event-${index}`, candidate_id: original.id, sequence: 1,
              from_revision: original.revision, to_revision: original.revision + 1,
              decision: 'CONFIRM', actor: 'finance-admin', reason: 'group review',
              changes: [{ field: 'status', previous_value: original.status, new_value: 'CONFIRMED', identity_changed: false }],
              conflict_resolution: null, created_at: '2026-08-24T10:00:00+08:00',
            }],
          }
        }),
      })
    }
    if (url.includes('/decisions') && init?.method === 'POST') {
      decisionSaved = true
      const body = JSON.parse(String(init.body)) as { decision: string }
      const original = items.find((candidate) => url.includes(candidate.id))!
      const updatedCandidate = {
        ...original,
        revision: original.revision + 1,
        status: body.decision === 'IGNORE' ? 'IGNORED' as const : 'CONFIRMED' as const,
      }
      candidateDetails[original.id] = updatedCandidate
      return response({
        candidate: updatedCandidate,
        event: {
          id: 'event-1', candidate_id: original.id, sequence: 1,
          from_revision: original.revision, to_revision: original.revision + 1,
          decision: body.decision, actor: 'finance-admin', reason: 'review',
          changes: [{ field: 'status', previous_value: original.status, new_value: body.decision === 'IGNORE' ? 'IGNORED' : 'CONFIRMED', identity_changed: false }],
          conflict_resolution: null, created_at: '2026-08-24T10:00:00+08:00',
        },
      })
    }
    if (url.startsWith('/api/v1/candidates/')) {
      if (failCandidateDetail) {
        return response({ title: '候选详情暂不可用', status: 503, code: 'CANDIDATE_DETAIL_UNAVAILABLE' }, 503)
      }
      const candidate = candidatePages.flatMap((page) => page.items).find((item) => url.endsWith(item.id))!
      const candidateEvents = reviewEventPages.flatMap((page) => page.items).filter((event) => event.candidate_id === candidate.id)
      return response({ ...(candidateDetails[candidate.id] ?? candidate), review_events: candidateEvents })
    }
    throw new Error(`Unexpected request: ${url}`)
  })
}

function renderApp() {
  return render(<Theme><App /></Theme>)
}

function overviewSummary() {
  return screen.getByRole('region', { name: '概览摘要' })
}

function reviewWorkspace() {
  return screen.getByRole('region', { name: '待审核' })
}

function filesWorkspace() {
  return screen.getByRole('region', { name: '文件与连接' })
}

function similarClassificationFixture() {
  const source = {
    ...candidates[0],
    summary: '支付宝 | 2026-08-01 | 收入 | 投资理财 | 余额宝 | 余额宝 | 交易成功',
    review_risks: [{
      code: 'TRANSFER_REVIEW_REQUIRED',
      message: '转账类交易需人工确认收付款方及资金性质',
    }],
  } as ApiCandidate
  const peer = {
    ...source,
    id: 'candidate-5',
    short_id: 'C-8F22',
    revision: 2,
    amount_minor: 4288,
    summary: '支付宝 | 2026-08-02 | 收入 | 投资理财 | 余额宝 | 余额宝 | 交易成功',
  } as ApiCandidate
  const group: ClassificationGroup = {
    contract_version: 'ledgerbridge.classification-group.v1',
    group_ref: `cg_${'a'.repeat(32)}`,
    accounting_month: '2026-08',
    conditions: {
      key_version: 'ledgerbridge.classification-key.v1',
      entity_ref: '10000000-0000-4000-8000-000000000001',
      source_system: 'alipay',
      source_kind: 'controlled_upload',
      platform: '支付宝',
      direction: 'INFLOW',
      transaction_type: '投资理财',
      counterparty_key: 'exact:余额宝',
      counterparty_label: '余额宝',
      counterparty_basis: 'EXACT_PLATFORM_SUMMARY_V1',
      funding_instrument: '余额宝',
      transaction_status: '交易成功',
      currency: 'CNY',
      risk_signature: ['TRANSFER_REVIEW_REQUIRED'],
    },
    members: [source, peer].map((candidate) => ({
      candidate_ref: candidate.id,
      short_id: candidate.short_id,
      revision: candidate.revision,
      status: candidate.status,
      amount_minor: candidate.amount_minor,
      accounting_month: candidate.accounting_month!,
      confidence_basis_points: candidate.confidence_basis_points,
      review_risk_codes: ['TRANSFER_REVIEW_REQUIRED'],
      amount_outlier: false,
      batch_eligible: true,
      one_click_eligible: false,
      exclusion_codes: [],
    })),
    batch_member_count: 2,
    one_click_member_count: 0,
    terminal_statuses: [],
    terminal_classifications: [],
    rule_learning_eligible: false,
    rule_learning_blocks: ['PROVISIONAL_BASIS', 'REVIEW_RISK_PRESENT', 'NO_CONFIRMED_SOURCE'],
    active_rule: null,
  }
  return { source, peer, group }
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
    expect(await within(overviewSummary()).findByText('¥6,380.00')).toBeInTheDocument()
    expect(within(overviewSummary()).getByText('3 条')).toBeInTheDocument()
  })

  it('keeps overview metrics on the selected month and presents reconciliation blockers separately', async () => {
    const historicalConfirmed: ApiCandidate = {
      ...candidates[3],
      id: 'candidate-historical-confirmed',
      short_id: 'C-HIST',
      accounting_month: '2026-05',
      business_unit: '2026年5月对账复核',
      business_unit_ref: 'unit-historical-review',
      amount_minor: 54160537,
    }
    installFetch({ items: [historicalConfirmed] })
    renderApp()

    expect(await screen.findByRole('heading', { name: '当前没有待审核事项' })).toBeInTheDocument()
    expect(screen.getByText('2026 年 8 月对账')).toBeInTheDocument()
    const overview = screen.getByRole('region', { name: '本月概览' })
    expect(within(overview).getByText('¥0.00')).toBeInTheDocument()
    expect(within(overview).getByText('0 家')).toBeInTheDocument()
    expect(screen.queryByText('2026年5月对账复核')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始审核' })).not.toBeInTheDocument()
    const readiness = screen.getByRole('heading', { name: '本月候选审核' }).closest('section')!
    expect(screen.getAllByText('暂无本月候选')).toHaveLength(2)
    expect(within(readiness).getByText('月度对账')).toBeInTheDocument()
    expect(within(readiness).getByText('相同凭证号金额不同')).toBeInTheDocument()
    expect(screen.queryByText('100%')).not.toBeInTheDocument()
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

  it('keeps the Core-backed overview available when similar groups are temporarily unavailable', async () => {
    installFetch({
      runtimeMode: 'core-backed',
      failClassificationGroupsOnce: true,
    })
    renderApp()

    expect(await screen.findByText('正式环境 · Core 实时业务数据')).toBeInTheDocument()
    expect(await screen.findByText('早上好，今天有几项需要确认')).toBeInTheDocument()
    expect(screen.getByText('同类批量归类暂不可用，可继续逐笔审核')).toBeInTheDocument()
    expect(screen.queryByText('数据读取失败')).not.toBeInTheDocument()
    expect(screen.queryByText('演示环境 · 登录已启用 · 合成业务数据')).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByText('待审核')[0])
    expect(screen.getByRole('button', { name: '一键审批 0 条' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '可一键审批 0' })).toBeInTheDocument()
  })

  it('invalidates a loaded similar group before a refresh that also fails another projection', async () => {
    const { source, peer, group } = similarClassificationFixture()
    const fetchMock = installFetch({
      items: [source, peer],
      classificationGroups: [group],
      runtimeMode: 'core-backed',
      failClassificationGroupsAfterFirst: true,
      failCandidatesAfterFirstOnce: true,
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    const refreshButton = screen.getByRole('button', { name: '刷新' })
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText(/同时处理本组其余 1 笔/)).toBeInTheDocument()
    fireEvent.click(refreshButton)

    expect(await screen.findByText('数据读取失败')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes('/candidate-classification-groups/')
      && String(input).endsWith('/decisions')
    ))).toHaveLength(0)
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

  it('previews an exact similar group and submits one risk-acknowledged atomic batch', async () => {
    const { source, peer, group } = similarClassificationFixture()
    const fetchMock = installFetch({
      items: [source, peer],
      classificationGroups: [group],
      runtimeMode: 'core-backed',
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('严格平台摘要匹配')).toBeInTheDocument()
    expect(within(dialog).getByRole('combobox', { name: '营业单元' })).toBeEnabled()
    expect(within(dialog).getByRole('combobox', { name: '科目' })).toBeEnabled()
    fireEvent.change(within(dialog).getByRole('textbox', { name: '费用种类或说明' }), {
      target: { value: '余额宝收益，按月归入投资理财' },
    })
    fireEvent.click(within(dialog).getByLabelText(/同时处理本组其余 1 笔/))
    const matchingDetails = within(dialog).getByText('匹配依据详情').closest('details')
    expect(matchingDetails).not.toHaveAttribute('open')
    fireEvent.click(within(dialog).getByText('匹配依据详情'))
    expect(within(dialog).getByText('键版本：ledgerbridge.classification-key.v1')).toBeInTheDocument()
    expect(within(dialog).getByText('实体：10000000-0000-4000-8000-000000000001')).toBeInTheDocument()
    expect(within(dialog).getByText('来源：alipay / controlled_upload')).toBeInTheDocument()
    expect(within(dialog).getByText('平台：支付宝')).toBeInTheDocument()
    expect(within(dialog).getByText('方向：INFLOW')).toBeInTheDocument()
    expect(within(dialog).getByText('交易类型：投资理财')).toBeInTheDocument()
    expect(within(dialog).getByText('对方键：exact:余额宝')).toBeInTheDocument()
    expect(within(dialog).getByText('对方名称：余额宝')).toBeInTheDocument()
    expect(within(dialog).getByText('对方依据：EXACT_PLATFORM_SUMMARY_V1')).toBeInTheDocument()
    expect(within(dialog).getByText('币种：CNY')).toBeInTheDocument()
    expect(within(dialog).getByText('风险签名：TRANSFER_REVIEW_REQUIRED')).toBeInTheDocument()
    expect(within(dialog).getByText('C-8F22')).toBeInTheDocument()
    const submit = within(dialog).getByRole('button', { name: '确认本组 2 笔' })
    expect(submit).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText(/我已逐项核对并确认风险条件/))
    expect(submit).toBeEnabled()
    fireEvent.click(submit)

    expect(await screen.findByText(/已原子确认同组 2 笔交易/)).toBeInTheDocument()
    const batchCall = fetchMock.mock.calls.find(([input]) => (
      String(input).includes('/candidate-classification-groups/')
      && String(input).endsWith('/decisions')
    ))
    expect(batchCall).toBeDefined()
    expect((batchCall?.[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe(
      session.csrf_token,
    )
    expect(JSON.parse(String(batchCall?.[1]?.body))).toEqual({
      source_candidate_ref: source.id,
      accounting_month: '2026-08',
      target: {
        business_unit_ref: source.business_unit_ref,
        category_code: source.category_code,
      },
      members: [
        { candidate_ref: source.id, expected_revision: source.revision },
        { candidate_ref: peer.id, expected_revision: peer.revision },
      ],
      reason: '余额宝收益，按月归入投资理财',
      acknowledged_risk_codes: ['TRANSFER_REVIEW_REQUIRED'],
    })
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes('/candidate-classification-groups/')
      && String(input).endsWith('/decisions')
    ))).toHaveLength(1)
  })

  it('reports a stale similar group as an atomic zero-write conflict', async () => {
    const { source, peer, group } = similarClassificationFixture()
    const fetchMock = installFetch({
      items: [source, peer],
      classificationGroups: [group],
      runtimeMode: 'core-backed',
      batchFailureStatus: 409,
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByLabelText(/同时处理本组其余 1 笔/))
    fireEvent.click(within(dialog).getByLabelText(/我已逐项核对并确认风险条件/))
    fireEvent.click(within(dialog).getByRole('button', { name: '确认本组 2 笔' }))

    expect(await screen.findByText('同组成员或版本已变化，本次没有处理任何交易；请刷新后重新预览')).toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes('/candidate-classification-groups/')
      && String(input).endsWith('/decisions')
    ))).toHaveLength(1)
  })

  it('shows grouped scope status but blocks batches above the atomic limit', async () => {
    const { source, group } = similarClassificationFixture()
    group.members = Array.from({ length: 101 }, (_, index) => ({
      ...group.members[0],
      candidate_ref: index === 0 ? source.id : `candidate-limit-${index}`,
      short_id: `C-L${String(index).padStart(3, '0')}`,
    }))
    group.batch_member_count = 101
    installFetch({ items: [source], classificationGroups: [group], runtimeMode: 'core-backed' })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText(/101 笔可处理/)).toHaveLength(2)
    expect(within(dialog).getByText(/超过单次 100 笔上限/)).toBeInTheDocument()
    expect(within(dialog).queryByLabelText(/同时处理本组其余/)).not.toBeInTheDocument()
  })

  it('posts only changed amount and month without resubmitting display labels', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    const fetchMock = installFetch({ items: [stableCandidate] })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('金额'), { target: { value: '7000.00' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存更正并确认' }))

    await screen.findByText(/C-8F21 已确认/)
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body))).toMatchObject({
      decision: 'CORRECT_AND_CONFIRM',
      expected_revision: 3,
      corrections: {
        amount_minor: 700000,
      },
    })
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).not.toHaveProperty('business_unit')
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).not.toHaveProperty('category')
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).not.toHaveProperty('accounting_month')
    expect(fetchMock.mock.calls.filter(([input, init]) => (
      String(input).endsWith('/api/v1/candidates/candidate-1') && init?.method !== 'POST'
    ))).toHaveLength(2)
  })

  it('blocks amounts outside the JSON safe-integer wire range', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    installFetch({ items: [stableCandidate] })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('金额'), { target: { value: '90071992547410' } })
    expect(within(dialog).getByRole('button', { name: '保存更正并确认' })).toBeDisabled()
  })

  it('round-trips safe-integer boundary cents without an unintended amount correction', async () => {
    const boundaryCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
      amount_minor: 9_007_199_254_740_990,
    } as ApiCandidate
    const fetchMock = installFetch({ items: [boundaryCandidate] })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(boundaryCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByLabelText('金额')).toHaveValue('90071992547409.90')
    fireEvent.change(within(dialog).getByLabelText('归属月份'), { target: { value: '2026-09' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存更正并确认' }))

    await screen.findByText(/C-8F21 已确认/)
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).toEqual({ accounting_month: '2026-09' })
  })

  it('posts explicitly edited stable business-unit and category references', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    const alternativeDimensions = {
      ...candidates[3],
      id: 'candidate-dimension-option',
      business_unit: '机场店',
      business_unit_ref: 'unit-airport',
      category: '水费',
      category_code: 'WATER',
    } as ApiCandidate
    const fetchMock = installFetch({ items: [stableCandidate, alternativeDimensions], runtimeMode: 'core-backed' })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    const businessUnitSelect = await within(dialog).findByRole('combobox', { name: '营业单元' })
    const categorySelect = within(dialog).getByRole('combobox', { name: '科目' })
    expect(within(businessUnitSelect).getByRole('option', { name: '城南店' })).toHaveValue('unit-south')
    expect(within(categorySelect).getByRole('option', { name: '布草' })).toHaveValue('LINEN')
    fireEvent.change(businessUnitSelect, { target: { value: 'unit-airport' } })
    fireEvent.change(categorySelect, { target: { value: 'WATER' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存更正并确认' }))

    await screen.findByText(/C-8F21 已确认/)
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).toEqual({
      business_unit_ref: 'unit-airport',
      category_code: 'WATER',
    })
  })

  it('allows confirmation with unchanged stable dimensions when the catalog is temporarily unavailable', async () => {
    const fetchMock = installFetch({ failAccountingDimensions: true, runtimeMode: 'core-backed' })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(candidates[0].summary))

    const dialog = await screen.findByRole('dialog')
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('可按现有分类继续确认')
    expect(within(dialog).getByLabelText('营业单元')).toHaveValue(candidates[0].business_unit_ref)
    expect(within(dialog).getByLabelText('科目')).toHaveValue(candidates[0].category_code)
    const submit = within(dialog).getByRole('button', { name: '保存更正并确认' })
    expect(submit).toBeEnabled()
    expect(submit).toHaveAttribute('aria-describedby', alert.id)
    fireEvent.click(submit)

    await screen.findByText(/C-8F21 已确认/)
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body))).not.toHaveProperty('corrections')
  })

  it('keeps similar-group propagation closed when a cached dimension catalog becomes unavailable', async () => {
    const { source, peer, group } = similarClassificationFixture()
    const fetchMock = installFetch({
      items: [source, peer],
      classificationGroups: [group],
      failAccountingDimensionsAfterFirst: true,
      runtimeMode: 'core-backed',
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    let dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('combobox', { name: '营业单元' })).toBeEnabled()
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    fireEvent.click(within(reviewWorkspace()).getByText(source.summary))

    dialog = await screen.findByRole('dialog')
    await within(dialog).findByText(/可按现有分类继续确认/)
    expect(within(dialog).getByRole('combobox', { name: '营业单元' })).toBeDisabled()
    expect(within(dialog).getByRole('combobox', { name: '科目' })).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText(/同时处理本组其余 1 笔/))
    fireEvent.click(within(dialog).getByLabelText(/我已逐项核对并确认风险条件/))
    const submit = within(dialog).getByRole('button', { name: '确认本组 2 笔' })
    expect(submit).toBeDisabled()
    fireEvent.click(submit)

    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('/candidate-classification-groups/')
      && String(input).endsWith('/decisions')
    ))).toBe(false)
  })

  it('does not install accounting dimensions when candidate detail fails', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    installFetch({ items: [stableCandidate], failCandidateDetail: true, runtimeMode: 'core-backed' })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('候选详情读取失败')
    expect(within(dialog).getByRole('button', { name: '保存更正并确认' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: '忽略候选' })).toBeDisabled()
    expect(within(dialog).getByLabelText('营业单元')).toBeDisabled()
  })

  it('announces and describes a current dimension missing from the active catalog', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-retired',
      category_code: 'LINEN',
    } as ApiCandidate
    installFetch({
      items: [stableCandidate],
      accountingDimensions: {
        contract_version: 'ledgerbridge.accounting-dimensions.v1',
        business_units: [{ ref: 'unit-active', label: '机场店' }],
        categories: [{ code: 'LINEN', label: '布草' }],
      },
      runtimeMode: 'core-backed',
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    const message = await within(dialog).findByText('当前会计维度不在授权目录中')
    const status = message.closest('[role="status"]')
    expect(status).not.toBeNull()
    expect(within(dialog).getByLabelText('营业单元')).toHaveAttribute('aria-describedby', status?.id)
    expect(within(dialog).getByRole('button', { name: '保存更正并确认' })).toHaveAttribute('aria-describedby', status?.id)
  })

  it('synchronizes stable selections supplied by same-revision candidate detail', async () => {
    const summaryCandidate = {
      ...candidates[0],
      business_unit_ref: undefined,
      category_code: undefined,
    }
    const detailCandidate = {
      ...summaryCandidate,
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    installFetch({
      items: [summaryCandidate],
      candidateDetails: { [summaryCandidate.id]: detailCandidate },
      accountingDimensions: {
        contract_version: 'ledgerbridge.accounting-dimensions.v1',
        business_units: [{ ref: 'unit-south', label: '城南店' }],
        categories: [{ code: 'LINEN', label: '布草' }],
      },
      runtimeMode: 'core-backed',
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(summaryCandidate.summary))

    await screen.findByRole('dialog')
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByLabelText('营业单元')).toHaveValue('unit-south'))
    const currentDialog = screen.getByRole('dialog')
    expect(within(currentDialog).getByLabelText('科目')).toHaveValue('LINEN')
    expect(within(currentDialog).getByRole('button', { name: '保存更正并确认' })).toBeEnabled()
  })

  it('accepts any regex-valid accounting month instead of a hard-coded month list', async () => {
    const stableCandidate = {
      ...candidates[0],
      business_unit_ref: 'unit-south',
      category_code: 'LINEN',
    } as ApiCandidate
    const fetchMock = installFetch({ items: [stableCandidate], runtimeMode: 'core-backed' })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(stableCandidate.summary))

    const dialog = await screen.findByRole('dialog')
    const month = await within(dialog).findByLabelText('归属月份')
    expect(month).toHaveAttribute('type', 'month')
    fireEvent.change(month, { target: { value: '2025-12' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存更正并确认' }))

    await screen.findByText(/C-8F21 已确认/)
    const decisionCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/candidate-1/decisions'))
    expect(JSON.parse(String(decisionCall?.[1]?.body)).corrections).toEqual({ accounting_month: '2025-12' })
  })

  it('shows identity-only accounting dimension changes in audit history without identifiers', async () => {
    const identityEvent: ReviewEvent = {
      ...reviewEvents[0],
      id: 'event-identity-change',
      candidate_id: candidates[0].id,
      changes: [{
        field: 'business_unit',
        previous_value: '同名门店',
        new_value: '同名门店',
        identity_changed: true,
      }],
    }
    installFetch({ reviewEventPages: [{ items: [identityEvent], next_cursor: null }] })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(within(reviewWorkspace()).getByText(candidates[0].summary))

    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText(/营业单元（标识已更新）/)).toBeInTheDocument()
    expect(dialog).not.toHaveTextContent('unit-demo')
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

  it('renders summary, review, and files as one continuous overview while keeping anchor links', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')

    const summary = screen.getByRole('region', { name: '概览摘要' })
    const review = screen.getByRole('region', { name: '待审核' })
    const files = screen.getByRole('region', { name: '文件与连接' })
    expect(screen.queryByRole('navigation', { name: '概览工作区' })).not.toBeInTheDocument()
    expect(summary.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(review.compareDocumentPosition(files) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(within(summary).getByRole('button', { name: /开始审核/ }))
    expect(`${window.location.pathname}${window.location.hash}`).toBe('/overview#review')

    fireEvent.change(screen.getByLabelText('搜索候选编号、门店或科目'), { target: { value: '机场店' } })
    expect(within(review).getByText('机场店水费，原消息未说明归属月份')).toBeInTheDocument()
    expect(within(review).queryByText('城南店 8 月布草清洗费用，供应商月结单')).not.toBeInTheDocument()

    fireEvent.click(within(screen.getByLabelText('主导航')).getByRole('button', { name: '概览' }))
    expect(`${window.location.pathname}${window.location.hash}`).toBe('/overview')
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
    fireEvent.click(await within(reviewWorkspace()).findByText('中行邮箱账单待复核：TX-0139'))

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
    expect(within(reviewWorkspace()).getByText('仅用于审核上下文的较早候选')).toBeInTheDocument()
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
    fireEvent.click(within(reviewWorkspace()).getByText('城南店银行收款，与另一条候选冲突'))

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
    const { group } = similarClassificationFixture()
    const safeUngroupedCandidate: ApiCandidate = {
      ...candidates[0],
      id: 'candidate-safe-ungrouped',
      short_id: 'C-SAFE1',
      summary: '未纳入相似组的高置信度安全账单',
    }
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
    const fetchMock = installFetch({
      items: [candidates[0], safeUngroupedCandidate, hotelRiskCandidate, fundingRiskCandidate],
      classificationGroups: [group],
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])

    expect(screen.getByText('2 条需补关联单据')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '一键审批 1 条' }))
    expect(await screen.findByText('已确认 1 条安全候选；风险项仍保留人工审核')).toBeInTheDocument()

    const decisionCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes('/decisions'))
    expect(decisionCalls).toHaveLength(1)
    expect(String(decisionCalls[0][0])).toContain('/candidate-safe-ungrouped/decisions')
    expect(String(decisionCalls[0][0])).not.toContain('/candidate-1/decisions')
    expect(String(decisionCalls[0][0])).not.toContain('/candidate-risk/decisions')
    expect(String(decisionCalls[0][0])).not.toContain('/candidate-funding-risk/decisions')
  })

  it('filters the queue by blocker status and keeps the counts visible', async () => {
    installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    expect(screen.getByRole('button', { name: '冲突 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '风险审核 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '可一键审批 1' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '冲突 1' }))
    expect(within(reviewWorkspace()).getByText('城南店银行收款，与另一条候选冲突')).toBeInTheDocument()
    expect(within(reviewWorkspace()).queryByText('机场店水费，原消息未说明归属月份')).not.toBeInTheDocument()
  })

  it('does not report a saved decision as failed when reconciliation refresh fails', async () => {
    installFetch({ failReconciliationAfterDecision: true })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('待审核')[0])
    fireEvent.click(screen.getAllByRole('button', { name: '确认' })[0])
    expect(await screen.findByText('C-8F21 已保存并重读确认，对账状态需刷新')).toBeInTheDocument()
    expect(screen.queryByText(/提交审核决定失败/)).not.toBeInTheDocument()
  })

  it('renders a real empty state when the API returns no candidates', async () => {
    installFetch({ items: [] })
    renderApp()
    await screen.findByRole('heading', { name: '当前没有待审核事项' })
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
    expect(screen.getAllByText('may-bank-statement.xlsx')).toHaveLength(1)
    expect(within(filesWorkspace()).getByText('2026 年 5 月')).toBeInTheDocument()
    expect(within(filesWorkspace()).getByText('关联 2 条候选')).toBeInTheDocument()
    expect(within(filesWorkspace()).getByText('含待审核')).toBeInTheDocument()
    fireEvent.click(within(filesWorkspace()).getByText('关联 2 条候选'))
    fireEvent.click(within(filesWorkspace()).getByRole('button', { name: /C-EV01/ }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(await screen.findByText('原始消息内容已直接展示')).toBeInTheDocument()
  })

  it('opens a password-only dialog for locked evidence, prevents duplicate submit, and refreshes after success', async () => {
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

    expect(document.querySelectorAll('.evidence-unlock-button')).toHaveLength(2)
    expect(screen.getByText('ordinary-statement.xlsx')).toBeInTheDocument()
    const passwordInput = await screen.findByLabelText('解压密码')
    expect(passwordInput).toHaveAttribute('type', 'password')
    fireEvent.change(passwordInput, { target: { value: 'ephemeral-test-password' } })
    const submitButton = screen.getByRole('button', { name: '解锁账单' })
    fireEvent.click(submitButton)
    fireEvent.click(submitButton)

    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')).toHaveLength(1))
    releaseUnlock()
    expect(await screen.findByText('账单已解锁，数据已刷新')).toBeInTheDocument()
    await waitFor(() => expect(document.querySelectorAll('.evidence-unlock-button')).toHaveLength(1))
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/candidates').length).toBeGreaterThanOrEqual(2)

    const unlockCall = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')[0]
    expect(unlockCall[1]?.credentials).toBe('same-origin')
    expect((unlockCall[1]?.headers as Record<string, string>)['X-CSRF-Token']).toBe(session.csrf_token)
    expect((unlockCall[1]?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse(String(unlockCall[1]?.body))).toEqual({ source_ref: sourceRef, password: 'ephemeral-test-password' })
    expect(document.body).not.toHaveTextContent('ephemeral-test-password')

    const secondPasswordInput = await screen.findByLabelText('解压密码')
    fireEvent.change(secondPasswordInput, { target: { value: 'second-ephemeral-password' } })
    fireEvent.click(screen.getByRole('button', { name: '解锁账单' }))
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')).toHaveLength(2))
    await waitFor(() => expect(document.querySelector('.evidence-unlock-button')).toBeNull())
    const secondUnlockCall = fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/evidence/unlocks')[1]
    expect(JSON.parse(String(secondUnlockCall[1]?.body))).toEqual({ source_ref: secondSourceRef, password: 'second-ephemeral-password' })
    expect(document.body).not.toHaveTextContent('second-ephemeral-password')
  })

  it('keeps a dismissed automatic unlock prompt closed until the user reopens it', async () => {
    const lockedEvidence = {
      id: '21500000-0000-4000-8000-000000000002',
      kind: 'attachment' as const,
      media_type: 'application/zip',
      sha256: 'e'.repeat(64),
      original_filename: 'dismissed-encrypted-statement.zip',
      unlock_status: 'PASSWORD_REQUIRED' as const,
      source_ref: '21500000-0000-4000-8000-000000000001',
    }
    installFetch({
      items: [{ ...candidates[0], id: 'candidate-dismissed-unlock', short_id: 'C-LK05', evidence: [lockedEvidence] }],
    })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])

    expect(await screen.findByLabelText('解压密码')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByLabelText('解压密码')).not.toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: '输入解压密码' }))
    expect(await screen.findByLabelText('解压密码')).toBeInTheDocument()
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
    const passwordInput = await screen.findByLabelText('解压密码')
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
    fireEvent.change(await screen.findByLabelText('解压密码'), { target: { value: 'accepted-password' } })
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

  it('does not keep material gaps from confirmed or ignored candidates', async () => {
    const materialCandidates: ApiCandidate[] = [
      { ...candidates[0], id: 'candidate-gap-pending', short_id: 'C-GP01', summary: '微信 | 2026-08-02 | 支出 | 商户消费 | 通信商 | 中国建设银行储蓄卡(7564) | 支付成功', review_risks: [{ code: 'FUNDING_STATEMENT_REQUIRED', message: '需关联资金账户明细后再确认' }] },
      { ...candidates[3], id: 'candidate-gap-confirmed', short_id: 'C-GC01', summary: '微信 | 2026-08-03 | 支出 | 商户消费 | 商户 | 中国银行借记卡(2061) | 支付成功', review_risks: [{ code: 'FUNDING_STATEMENT_REQUIRED', message: '历史风险已终结' }] },
      { ...candidates[3], id: 'candidate-gap-ignored', short_id: 'C-GI01', status: 'IGNORED', summary: '微信 | 2026-08-04 | 支出 | 商户消费 | 商户 | 农业银行借记卡(1234) | 支付成功', review_risks: [{ code: 'FUNDING_STATEMENT_REQUIRED', message: '已忽略的历史风险' }] },
    ]
    installFetch({ items: materialCandidates })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByText('文件与连接')[0])

    expect(screen.getByText('中国建设银行储蓄卡(7564)明细')).toBeInTheDocument()
    expect(screen.queryByText('中国银行借记卡(2061)明细')).not.toBeInTheDocument()
    expect(screen.queryByText('农业银行借记卡(1234)明细')).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '待补账单清单' })).getByText('1 项')).toBeInTheDocument()
  })

  it('shows the personal overview before collapsed formal bank details', async () => {
    const testCandidate: ApiCandidate = {
      ...candidates[3],
      id: 'candidate-test-personal',
      short_id: 'C-TEST',
      business_unit: '个人',
      business_unit_ref: 'personal-main',
      amount_minor: 8800,
      accounting_month: '2026-08',
      category: '测试收入',
      category_code: 'TEST_INCOME',
      summary: '支付宝 | 2026-08-01 | 收入 | 测试收入 | 测试对象 | 余额 | 交易成功',
    }
    installFetch({
      items: [testCandidate],
      personalBankResponse: personalBankTransactions(),
    })
    renderApp()
    await screen.findByRole('heading', { name: '当前没有待审核事项' })
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const formal = await screen.findByRole('region', { name: '个人正式银行流水' })
    const testSummary = screen.getByRole('region', { name: '个人财务收支概览' })
    expect(await within(formal).findByText('2 笔')).toBeInTheDocument()
    expect(within(formal).getByText('网商银行 · 尾号 7968')).toBeInTheDocument()
    expect(within(formal).getByText('账单审核：已确认')).toBeInTheDocument()
    expect(within(formal).queryByText('正式对方甲')).not.toBeInTheDocument()
    expect(within(formal).queryByText('正式对方乙')).not.toBeInTheDocument()
    expect(within(formal).getByText('账户现金流，不是营业收入')).toBeInTheDocument()
    expect(within(testSummary).getByText('测试收入')).toBeInTheDocument()
    expect(testSummary.compareDocumentPosition(formal) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(within(formal).getByRole('button', { name: '查看流水明细（2 笔）' }))
    expect(within(formal).getByText('正式对方甲')).toBeInTheDocument()
    expect(within(formal).getByText('正式对方乙')).toBeInTheDocument()
  })

  it('shows multiple formal bank statements in one reconciled list', async () => {
    const formalFacts = structuredClone(personalBankTransactions())
    const secondStatementRef = '70000000-0000-4000-8000-000000000009'
    formalFacts.statements.push({
      ...formalFacts.statements[0],
      statement_ref: secondStatementRef,
      managed_account_ref: '80000000-0000-4000-8000-000000000010',
      institution_code: 'ccb',
      account_suffix: '7564',
      transaction_count: 1,
      review_status: 'PENDING',
    })
    formalFacts.items.push({
      ...formalFacts.items[0],
      statement_ref: secondStatementRef,
      source_row_number: 1,
      occurred_at: '2026-06-01T09:30:00+08:00',
      amount_minor: 5000,
      balance_minor: 5000,
      counterparty_name: '建行正式对方',
    })
    Object.assign(formalFacts.summary, {
      statement_count: 2,
      transaction_count: 3,
      cash_inflow_minor: 15000,
      net_cash_flow_minor: 12500,
    })
    installFetch({ personalBankResponse: formalFacts })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const formal = await screen.findByRole('region', { name: '个人正式银行流水' })
    expect(await within(formal).findByText('3 笔')).toBeInTheDocument()
    expect(within(formal).getByText(/2 份账单/)).toBeInTheDocument()
    expect(within(formal).getByText('网商银行 · 尾号 7968')).toBeInTheDocument()
    expect(within(formal).getByText('中国建设银行 · 尾号 7564')).toBeInTheDocument()
    expect(within(formal).queryByText('建行正式对方')).not.toBeInTheDocument()

    fireEvent.click(within(formal).getByRole('button', { name: '查看流水明细（3 笔）' }))
    expect(within(formal).getByText('建行正式对方')).toBeInTheDocument()
    fireEvent.change(within(formal).getByLabelText('银行账户筛选'), { target: { value: secondStatementRef } })
    expect(within(formal).getByText('符合条件 1 笔')).toBeInTheDocument()
    expect(within(formal).getByText('建行正式对方')).toBeInTheDocument()
    expect(within(formal).queryByText('正式对方甲')).not.toBeInTheDocument()
  })

  it('repairs fixed-column PDF layout noise without changing formal transaction facts', async () => {
    const formalFacts = structuredClone(personalBankTransactions())
    formalFacts.statements[0].institution_code = 'boc'
    Object.assign(formalFacts.items[0], {
      counterparty_name: '陈莹 6',
      counterparty_account_masked: '  ************7442  ',
      counterparty_institution: '中国工商银行 ---- | --------------- -- | --------',
      transaction_name: '跨行转账 手机 | 银行 ---- | --------------- -- | --------',
    })
    installFetch({ personalBankResponse: formalFacts })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const formal = await screen.findByRole('region', { name: '个人正式银行流水' })
    fireEvent.click(await within(formal).findByRole('button', { name: '查看流水明细（2 笔）' }))
    expect(await within(formal).findByText('陈莹')).toBeInTheDocument()
    expect(within(formal).queryByText('陈莹 6')).not.toBeInTheDocument()
    expect(within(formal).getByText('跨行转账 · 手机银行 · 中国工商银行 · 对方尾号 7442')).toBeInTheDocument()
    expect(formal).not.toHaveTextContent('----')
    expect(formal).not.toHaveTextContent('|')
  })

  it('keeps Candidate test data visible when formal bank facts are unavailable', async () => {
    const testCandidate: ApiCandidate = {
      ...candidates[3],
      id: 'candidate-test-fallback',
      short_id: 'C-FALL',
      business_unit: '个人',
      business_unit_ref: 'personal-main',
      amount_minor: 9900,
      accounting_month: '2026-08',
      category: '测试收入',
      category_code: 'TEST_INCOME',
      summary: '支付宝 | 2026-08-01 | 收入 | 测试收入 | 测试对象 | 余额 | 交易成功',
    }
    installFetch({ items: [testCandidate], failPersonalBank: true })
    renderApp()
    await screen.findByRole('heading', { name: '当前没有待审核事项' })
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    expect(await screen.findByRole('alert')).toHaveTextContent('正式银行流水暂不可用')
    expect(screen.getByRole('region', { name: '个人财务收支概览' })).toHaveTextContent('¥99.00')
  })

  it('puts pending review first and summarizes only confirmed personal facts with source-priority deduplication', async () => {
    const personalCandidates: ApiCandidate[] = [
      { ...candidates[3], id: 'candidate-income-aug', short_id: 'C-PF01', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: 10000, accounting_month: '2026-08', category: '工资', category_code: 'SALARY', summary: '支付宝 | 2026-08-01 | 收入 | 工资 | 公司 | 余额 | 交易成功' },
      { ...candidates[3], id: 'candidate-expense-aug', short_id: 'C-PF02', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: 2500, accounting_month: '2026-08', category: '餐饮', category_code: 'DINING', summary: '微信 | 2026-08-02 | 支出 | 商户消费 | 餐厅 | 零钱 | 支付成功' },
      { ...candidates[3], id: 'candidate-expense-bank-copy', short_id: 'C-PF03', source_channel: 'controlled_upload', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: -2500, accounting_month: '2026-08', category: '餐饮', category_code: 'DINING', summary: '建设银行 | 2026-08-02 | 支出 | 消费 | 餐厅 | 储蓄卡 | 交易成功' },
      { ...candidates[3], id: 'candidate-transfer-platform', short_id: 'C-PF04', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: -20000, accounting_month: '2026-08', category: '转账', category_code: 'TRANSFER', summary: '支付宝 | 2026-08-03 | 支出 | 转账 | 张三 | 建设银行 | 交易成功' },
      { ...candidates[3], id: 'candidate-transfer-bank', short_id: 'C-PF05', source_channel: 'controlled_upload', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: -20000, accounting_month: '2026-08', category: '转账', category_code: 'TRANSFER', summary: '建设银行 | 2026-08-03 | 支出 | 转账 | 张三 | 储蓄卡 | 交易成功' },
      { ...candidates[0], id: 'candidate-pending-expense', short_id: 'C-PEND', business_unit: '个人', business_unit_ref: 'personal-main', amount_minor: 999999, accounting_month: '2026-08', category: '待审核', category_code: 'PENDING', summary: '微信 | 2026-08-04 | 支出 | 商户消费 | 商户 | 零钱 | 支付成功' },
      { ...candidates[3], id: 'candidate-company-income', short_id: 'C-COMP', business_unit: '景怡公司', business_unit_ref: 'company-jingyi', amount_minor: 880000, accounting_month: '2026-08', category: '公司收入', category_code: 'COMPANY_INCOME', summary: '支付宝 | 2026-08-05 | 收入 | 酒店收款 | 平台 | 余额 | 交易成功' },
      { ...candidates[3], id: 'candidate-unscoped-bank', short_id: 'C-UNSC', source_channel: 'controlled_upload', business_unit: '待归属', business_unit_ref: '', amount_minor: 500000, accounting_month: '2026-08', category: '转账', category_code: 'TRANSFER', summary: '中国银行 | 2026-08-06 | 收入 | 转账 | 某人 | 借记卡 | 交易成功' },
    ]
    installFetch({ items: personalCandidates })
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const review = screen.getByRole('region', { name: '个人财务待审核' })
    const summary = screen.getByRole('region', { name: '个人财务收支概览' })
    const postingStatus = screen.getByRole('region', { name: '个人财务入账状态' })
    expect(review.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(postingStatus).getByText('正式过账未启用')).toBeInTheDocument()
    expect(within(postingStatus).getByText('4 条已确认、尚未过账')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '个人财务归属待校准' })).getByText('C-UNSC')).toBeInTheDocument()
    expect(screen.getByText('1 条归属待校准')).toBeInTheDocument()
    expect(within(review).getByText('C-PEND')).toBeInTheDocument()
    expect(within(summary).getByText('¥5,100.00')).toBeInTheDocument()
    expect(within(summary).getByText('¥225.00')).toBeInTheDocument()
    expect(within(summary).getByText('¥4,875.00')).toBeInTheDocument()
    expect(within(summary).getByText('2 条已确认收入，含归属待校准')).toBeInTheDocument()
    expect(within(summary).getByText('2 条已确认支出，含归属待校准')).toBeInTheDocument()
    expect(screen.getByText('2 条不属于个人范围或状态未确认，未计入汇总')).toBeInTheDocument()
    expect(screen.getByText('2 条跨来源重复记录已合并')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '测试分类占比' })).toBeInTheDocument()
    expect(screen.getByText('工资')).toBeInTheDocument()
    expect(screen.getByText('97.7%')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '测试月度趋势' })).toBeInTheDocument()
    expect(screen.getByText('2026 年 8 月')).toBeInTheDocument()
    expect(screen.queryByText('公司收入')).not.toBeInTheDocument()
    const categoryPanel = screen.getByRole('heading', { name: '测试分类占比' }).closest('section')
    expect(categoryPanel).not.toBeNull()
    expect(within(categoryPanel!).queryByText('待审核')).not.toBeInTheDocument()
  })

  it('keeps same-source repeats and removes only one lower-priority cross-source copy per match', async () => {
    const personalCandidates: ApiCandidate[] = [
      ...['candidate-platform-spend-1', 'candidate-platform-spend-2'].map((id, index): ApiCandidate => ({
        ...candidates[3],
        id,
        short_id: `C-PS0${index + 1}`,
        business_unit: '个人',
        business_unit_ref: 'personal-main',
        amount_minor: 2500,
        accounting_month: '2026-08',
        category: '餐饮',
        category_code: 'DINING',
        summary: '微信 | 2026-08-02 | 支出 | 商户消费 | 餐厅 | 零钱 | 支付成功',
      })),
      {
        ...candidates[3],
        id: 'candidate-bank-spend-copy',
        short_id: 'C-BS01',
        source_channel: 'controlled_upload',
        business_unit: '个人',
        business_unit_ref: 'personal-main',
        amount_minor: -2500,
        accounting_month: '2026-08',
        category: '餐饮',
        category_code: 'DINING',
        summary: '建设银行 | 2026-08-02 | 支出 | 消费 | 餐厅 | 储蓄卡 | 交易成功',
      },
      ...['candidate-bank-transfer-1', 'candidate-bank-transfer-2'].map((id, index): ApiCandidate => ({
        ...candidates[3],
        id,
        short_id: `C-BT0${index + 1}`,
        source_channel: 'controlled_upload',
        business_unit: '个人',
        business_unit_ref: 'personal-main',
        amount_minor: -20000,
        accounting_month: '2026-08',
        category: '转账',
        category_code: 'TRANSFER',
        summary: '建设银行 | 2026-08-03 | 支出 | 转账 | 张三 | 储蓄卡 | 交易成功',
      })),
      {
        ...candidates[3],
        id: 'candidate-platform-transfer-copy',
        short_id: 'C-PT01',
        business_unit: '个人',
        business_unit_ref: 'personal-main',
        amount_minor: -20000,
        accounting_month: '2026-08',
        category: '转账',
        category_code: 'TRANSFER',
        summary: '支付宝 | 2026-08-03 | 支出 | 转账 | 张三 | 建设银行 | 交易成功',
      },
    ]
    installFetch({ items: personalCandidates })
    renderApp()
    await screen.findByRole('heading', { name: '当前没有待审核事项' })
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const summary = screen.getByRole('region', { name: '个人财务收支概览' })
    expect(within(summary).getByText('¥450.00')).toBeInTheDocument()
    expect(within(summary).getByText('4 条已确认支出，含归属待校准')).toBeInTheDocument()
    expect(screen.getByText('2 条跨来源重复记录已合并')).toBeInTheDocument()
  })

  it('shows every confirmed unassigned record in the test view', async () => {
    const unassignedCandidates = Array.from({ length: 7 }, (_, index): ApiCandidate => ({
      ...candidates[3],
      id: `candidate-unassigned-${index + 1}`,
      short_id: `C-UA0${index + 1}`,
      business_unit: '待归属',
      business_unit_ref: '',
      amount_minor: (index + 1) * 100,
      accounting_month: '2026-08',
      category: '转账',
      category_code: 'TRANSFER',
      summary: `中国银行 | 2026-08-${String(index + 1).padStart(2, '0')} | 收入 | 转账 | 对方${index + 1} | 借记卡 | 交易成功`,
    }))
    installFetch({ items: unassignedCandidates })
    renderApp()
    await screen.findByRole('heading', { name: '当前没有待审核事项' })
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])

    const unassigned = screen.getByRole('region', { name: '个人财务归属待校准' })
    expect(within(unassigned).getByText('C-UA01')).toBeInTheDocument()
    expect(within(unassigned).getByText('C-UA07')).toBeInTheDocument()
    expect(screen.getByText('7 条归属待校准')).toBeInTheDocument()
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

  it('opens the single cash reconciliation workspace from the monthly entry', async () => {
    const fetchMock = installFetch()
    renderApp()
    await screen.findByText('早上好，今天有几项需要确认')
    fireEvent.click(screen.getAllByRole('button', { name: /完整个人财务对账/ })[0])
    expect(screen.getByRole('heading', { name: '完整个人财务对账' })).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: /月度对账/ })[0])
    expect(await screen.findByRole('heading', { name: '月度对账' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '月度对账' })).toHaveLength(2)
    expect(screen.queryByRole('button', { name: '原口径对账表' })).not.toBeInTheDocument()
    expect(window.location.pathname).toBe('/reconciliation')
    expect(window.location.search).toBe('')
    expect(screen.queryByRole('heading', { name: '2026 年 8 月对账草稿' })).not.toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).startsWith('/api/v1/original-reconciliations/'))).toHaveLength(1))
    expect(fetchMock.mock.calls.filter(([input]) => String(input).startsWith('/api/v1/cash-reconciliations/'))).toHaveLength(1)
    fireEvent.click(screen.getAllByRole('button', { name: /各公司报表/ })[0])
    expect(await screen.findByRole('heading', { name: '各公司报表' })).toBeInTheDocument()
    expect(await screen.findByText('当前期间没有可展示的公司报表')).toBeInTheDocument()
  })

  it('shows posted totals, source layers, months, and authoritative business units separately', async () => {
    window.history.replaceState({}, '', '/company-reports')
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: companyReports(true, true) })

    renderApp()

    expect(await screen.findByRole('region', { name: '演示公司 财务汇总' })).toBeInTheDocument()
    expect(screen.getAllByText('¥8,000.00').length).toBeGreaterThan(0)
    expect(screen.getAllByText('¥2,350.00').length).toBeGreaterThan(0)
    expect(screen.getAllByText('¥5,650.00').length).toBeGreaterThan(0)
    expect(screen.getByText('已确认来源 61 条')).toBeInTheDocument()
    expect(screen.getByText('账户流水 0 条')).toBeInTheDocument()
    expect(screen.getByText('正式入账 3 条')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '2026 年 8 月' })).toBeInTheDocument()
    expect(screen.getByText('演示门店')).toBeInTheDocument()
    expect(screen.getByText('余额基础尚未建立')).toBeInTheDocument()
  })

  it('keeps confirmed attribution and review queues outside zero posted totals', async () => {
    window.history.replaceState({}, '', '/company-reports')
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: companyReports(true) })

    renderApp()

    expect(await screen.findByRole('region', { name: '演示公司 财务汇总' })).toBeInTheDocument()
    expect(screen.getByText('61 条已确认来源待账户或经济性质归属')).toBeInTheDocument()
    expect(screen.getByText('146 条来源待审核')).toBeInTheDocument()
    expect(screen.getAllByText('¥0.00').length).toBeGreaterThanOrEqual(3)
    expect(screen.getByText('正式入账 0 条')).toBeInTheDocument()
  })

  it('keeps source layers visible without showing zero when the posted ledger is unavailable', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true)
    reports.posted_ledger_status = 'UNAVAILABLE'
    reports.layers = reports.layers.filter((layer) => layer.basis !== 'POSTED_LEDGER')
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    expect(await screen.findByRole('region', { name: '演示公司 财务汇总' })).toBeInTheDocument()
    expect(screen.getByText('已确认来源 61 条')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '演示公司 正式财务总额' })).not.toBeInTheDocument()
    expect(screen.getByText('会计账簿尚未接入')).toBeInTheDocument()
  })

  it('prioritizes confirmed account cash flow when the posted ledger is empty', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true)
    const candidateCompany = reports.layers[0].items[0]
    Object.assign(candidateCompany.metrics, {
      confirmed_positive_minor: 150000,
      confirmed_negative_minor: -60000,
      confirmed_net_minor: 90000,
    })
    Object.assign(candidateCompany.months[0].metrics, {
      confirmed_positive_minor: 150000,
      confirmed_negative_minor: -60000,
      confirmed_net_minor: 90000,
    })
    const statementCompany = reports.layers[1].items[0]
    Object.assign(statementCompany.metrics, {
      cash_inflow_minor: 200000,
      cash_outflow_minor: 80000,
      net_cash_flow_minor: 120000,
      confirmed_transaction_count: 12,
      statement_count: 2,
    })
    Object.assign(statementCompany.months[0].metrics, {
      cash_inflow_minor: 200000,
      cash_outflow_minor: 80000,
      net_cash_flow_minor: 120000,
      confirmed_transaction_count: 12,
      statement_count: 2,
    })
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    const statementSummary = await screen.findByRole('region', { name: '演示公司 账户流水汇总' })
    expect(within(statementSummary).getByText('正式银行流水')).toBeInTheDocument()
    expect(within(statementSummary).getByText('正式数据')).toBeInTheDocument()
    expect(within(statementSummary).getByText('¥2,000.00')).toBeInTheDocument()
    expect(within(statementSummary).getByText('¥800.00')).toBeInTheDocument()
    expect(within(statementSummary).getByText('¥1,200.00')).toBeInTheDocument()
    expect(within(statementSummary).getByText(/12 条/)).toBeInTheDocument()
    expect(within(statementSummary).getByText('2 份账单')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '演示公司 已确认事项汇总' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '演示公司 正式财务总额' })).not.toBeInTheDocument()
    expect(screen.getByText('正式数据已接入，尚无会计过账分录')).toBeInTheDocument()
  })

  it('warns when Core only returns the generic company placeholder', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true)
    reports.layers.forEach((layer) => {
      layer.items[0].company_name = 'LedgerBridge controlled reconciliation'
    })
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    expect(await screen.findByText('待完成公司归属')).toBeInTheDocument()
    expect(screen.getByText('当前 Core 只返回一个通用公司主体，已导入数据尚未分配到各家公司；下方汇总不代表公司报表已完整。')).toBeInTheDocument()
  })

  it('switches between every authoritative company without the generic attribution warning', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true)
    reports.layers.forEach((layer) => {
      layer.items[0].company_name = '薇旭公司'
      layer.items.push({
        ...structuredClone(layer.items[0]),
        company_ref: '20000000-0000-4000-8000-000000000002',
        company_name: '景怡公司',
      })
    })
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    expect(await screen.findByRole('region', { name: '薇旭公司 财务汇总' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '景怡公司' }))
    expect(screen.getByRole('region', { name: '景怡公司 财务汇总' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '薇旭公司 财务汇总' })).not.toBeInTheDocument()
    expect(screen.queryByText('待完成公司归属')).not.toBeInTheDocument()
  })

  it('distinguishes unavailable business-unit breakdowns without backfilling names', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true, true)
    const statementMonth = reports.layers[1].items[0].months[0] as unknown as {
      business_unit_breakdown_status: string
      business_units: unknown
    }
    statementMonth.business_unit_breakdown_status = 'UNAVAILABLE_ATTRIBUTION_PENDING'
    statementMonth.business_units = null
    reports.layers[1].items[0].business_unit_breakdown_status = 'UNAVAILABLE_ATTRIBUTION_PENDING'
    const postedMonth = reports.layers[2].items[0].months[0] as unknown as {
      business_unit_breakdown_status: string
      business_units: unknown
    }
    postedMonth.business_unit_breakdown_status = 'UNAVAILABLE_MISSING_SNAPSHOT'
    postedMonth.business_units = null
    reports.layers[2].items[0].business_unit_breakdown_status = 'UNAVAILABLE_MISSING_SNAPSHOT'
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    expect(await screen.findByRole('region', { name: '演示公司 财务汇总' })).toBeInTheDocument()
    expect(screen.getByText('账户流水的业务单元归属待补；公司级现金流仍保留。')).toBeInTheDocument()
    expect(screen.getByText('历史业务单元快照缺失；未使用当前维度名称回填。')).toBeInTheDocument()
    expect(screen.queryByText('演示门店')).not.toBeInTheDocument()
  })

  it('shows a completed empty business-unit breakdown as empty rather than unavailable', async () => {
    window.history.replaceState({}, '', '/company-reports')
    const reports = companyReports(true)
    reports.layers.forEach((layer) => {
      const month = layer.items[0].months[0]
      month.business_unit_breakdown_status = 'EMPTY'
      month.business_units = []
      layer.items[0].business_unit_breakdown_status = 'EMPTY'
    })
    installFetch({ runtimeMode: 'core-backed', companyReportResponse: reports })

    renderApp()

    expect(await screen.findByText('正式入账层的业务单元事实确认为空。')).toBeInTheDocument()
    expect(screen.queryByText(/历史业务单元快照缺失/)).not.toBeInTheDocument()
  })

  it('retries company reports after a BFF error', async () => {
    window.history.replaceState({}, '', '/company-reports')
    installFetch({ failCompanyReportsOnce: true })

    renderApp()

    expect(await screen.findByText('公司报表层暂不可用，未显示任何 0 值。公司报表暂不可用')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('当前期间没有可展示的公司报表')).toBeInTheDocument()
  })

  it('renders rule-generated income, expense and current-account work lanes', async () => {
    window.history.replaceState({}, '', '/original-reconciliation')
    installFetch()
    renderApp()

    expect(await screen.findByRole('heading', { name: '月度对账' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/reconciliation')
    expect(window.location.search).toBe('')
    expect(screen.queryByRole('table', { name: '原口径固定列对账表' })).not.toBeInTheDocument()
    const workflow = screen.getByRole('region', { name: '收支与往来事项' })
    const lanes = within(workflow).getByRole('tablist', { name: '业务性质' })
    expect(within(lanes).getByRole('tab', { name: /收入/ })).toBeInTheDocument()
    expect(within(lanes).getByRole('tab', { name: /支出/ })).toBeInTheDocument()
    expect(within(lanes).getByRole('tab', { name: /往来款/ })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '旧表项目取数来源' })).toBeInTheDocument()

    fireEvent.click(within(workflow).getByRole('button', { name: '处理待审核' }))
    expect(`${window.location.pathname}${window.location.hash}`).toBe('/overview#review')
  })

  it('keeps projection gaps as secondary todos without presenting them as financial totals', async () => {
    window.history.replaceState({}, '', '/original-reconciliation')
    const fetchMock = installFetch()
    renderApp()

    expect(await screen.findByRole('heading', { name: '月度对账' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '收支与往来事项' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '原口径合计' })).not.toBeInTheDocument()
    expect(await screen.findByText('3 条交易待审核')).toBeInTheDocument()
    expect(screen.getByText('2 条已确认待入账')).toBeInTheDocument()
    expect(screen.getByText('1 份对应账单待补')).toBeInTheDocument()
    expect(screen.getByText('1 条已确认事项待归类')).toBeInTheDocument()
    expect(screen.getByText('月内日期待补')).toBeInTheDocument()
    expect(screen.getByText('待补业务性质')).toBeInTheDocument()
    expect(screen.getByText('规则版本可追溯')).toHaveAttribute(
      'title',
      `${originalReconciliationFixture.taxonomy_version} | ${originalReconciliationFixture.layout_version} | ${originalReconciliationFixture.mapping_version}`,
    )

    fireEvent.change(screen.getByLabelText('选择对账月份'), { target: { value: '2026-07' } })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/original-reconciliations/2026-07?entity_ref=${originalReconciliationFixture.scope.entity_ref}&business_unit=${originalReconciliationFixture.scope.business_unit_ref}`,
      expect.objectContaining({ credentials: 'same-origin' }),
    ))
  })

  it('keeps statement browsing usable across loading, error, and empty projection states', async () => {
    window.history.replaceState({}, '', '/original-reconciliation')
    let releaseProjection: () => void = () => undefined
    const projectionGate = new Promise<void>((resolve) => { releaseProjection = resolve })
    installFetch({ originalReconciliationGate: projectionGate })
    const loadingView = renderApp()
    expect(await screen.findByRole('heading', { name: '月度对账' })).toBeInTheDocument()
    await act(async () => releaseProjection())
    expect(await screen.findByRole('region', { name: '收支与往来事项' })).toBeInTheDocument()
    loadingView.unmount()

    vi.restoreAllMocks()
    installFetch({ failOriginalReconciliation: true })
    const errorView = renderApp()
    expect(await screen.findByText('旧口径补充待办暂不可用')).toBeInTheDocument()
    expect(screen.getByText('平台实收')).toBeInTheDocument()
    errorView.unmount()

    vi.restoreAllMocks()
    const blankRows = originalReconciliationFixture.rows.map((row) => ({
      ...row,
      cells: row.cells.map((cell) => ({
        ...cell,
        kind: 'BLANK' as const,
        label: null,
        amount_minor: null,
        currency: null,
        gap_code: null,
        source_fact_refs: [],
      })),
    }))
    installFetch({
      items: [],
      cashReconciliation: {
        ...cashReconciliation,
        rows: [],
        issues: [],
        eligible_fact_count: 0,
        matched_fact_count: 0,
        unmatched_fact_count: 0,
        conflicted_fact_count: 0,
        issue_count: 0,
        issues_truncated: false,
        totals: { income_minor: 0, expense_minor: 0, current_minor: 0 },
      },
      originalReconciliation: {
        ...originalReconciliationFixture,
        rows: blankRows,
        totals: {
          ...originalReconciliationFixture.totals,
          posted_income_minor: 0,
          posted_expense_minor: 0,
          posted_profit_minor: 0,
          confirmed_candidate_amount_minor: 0,
          posted_amount_minor: 0,
          mapped_cell_count: 0,
        },
        pending_review_count: 0,
        confirmed_pending_posting_count: 0,
        missing_material_count: 0,
        unmapped_confirmed_count: 0,
        sources: [],
      },
    })
    renderApp()
    expect(await screen.findByRole('heading', { name: '本月没有收入事项' })).toBeInTheDocument()
    expect(screen.queryByText(/截图导入|受控导入/)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '原口径合计' })).not.toBeInTheDocument()
    expect(screen.queryByRole('table', { name: '原口径固定列对账表' })).not.toBeInTheDocument()
  })

  it('does not surface unavailable posted-ledger projections as statement totals', async () => {
    window.history.replaceState({}, '', '/original-reconciliation')
    installFetch({
      originalReconciliation: {
        ...originalReconciliationFixture,
        posted_ledger_complete: false,
        totals: {
          ...originalReconciliationFixture.totals,
          posted_income_minor: null,
          posted_expense_minor: null,
          posted_profit_minor: null,
          posted_amount_minor: null,
        },
      },
    })
    renderApp()

    expect(await screen.findByRole('region', { name: '收支与往来事项' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '原口径合计' })).not.toBeInTheDocument()
    expect(screen.queryByText('正式入账收入')).not.toBeInTheDocument()
    expect(screen.queryByText('正式入账支出')).not.toBeInTheDocument()
  })

  it('keeps the payroll integration status reachable from its route and both navigation surfaces', async () => {
    window.history.replaceState({}, '', '/payroll')
    const fetchMock = installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: notReadyPayrollResponses,
    })
    renderApp()

    expect(await screen.findByRole('heading', { name: '工资与发放验证' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '工资与发放验证' })).toHaveLength(2)
    expect(await screen.findByText('七、八月工资测试账本已就绪')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '全部 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '2026 年 7 月 0' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '2026 年 8 月 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '检查七八月素材' })).toBeInTheDocument()
    expect(screen.getByText('只读工资发布契约已部署')).toBeInTheDocument()
    expect(screen.getByText('服务已接通，待归属材料 3 份')).toBeInTheDocument()
    expect(screen.getByText('工资材料仍有待归属项')).toBeInTheDocument()
    expect(screen.getByText('真实发薪和银行提交不可用')).toBeInTheDocument()
    expect(screen.queryByText(/127\.0\.0\.1/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|银行提交/ })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.map(([input]) => String(input)).filter((url) => url.startsWith('/api/v1/payroll/')))
      .toEqual(['/api/v1/payroll/status', '/api/v1/payroll/test-workspace'])
  })

  it('does not claim historical auto-import when the test workspace has no eligible material', async () => {
    window.history.replaceState({}, '', '/payroll')
    const emptyWorkspace = structuredClone(notReadyPayrollResponses['/api/v1/payroll/test-workspace']) as {
      data: {
        auto_test_ready: boolean
        routing_counts: { auto_test: number; review_required: number; date_unknown: number }
        materials: unknown[]
      }
    }
    emptyWorkspace.data.auto_test_ready = false
    emptyWorkspace.data.routing_counts = { auto_test: 0, review_required: 0, date_unknown: 0 }
    emptyWorkspace.data.materials = []
    installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: {
        ...notReadyPayrollResponses,
        '/api/v1/payroll/test-workspace': emptyWorkspace,
      },
    })
    renderApp()

    expect(await screen.findByText('测试账本已创建，暂无七、八月工资素材')).toBeInTheDocument()
    expect(screen.queryByText('七、八月工资测试账本已就绪')).not.toBeInTheDocument()
  })

  it('reads the live payroll projection only through the same-origin BFF and renders its real summaries', async () => {
    window.history.replaceState({}, '', '/payroll')
    const fetchMock = installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: livePayrollResponses,
    })
    renderApp()

    expect(await screen.findByRole('heading', { name: '真实材料汇总' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '公司内工资批次' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '发放验证结果' })).toBeInTheDocument()
    expect(screen.getByText('材料 1 份')).toBeInTheDocument()
    expect(screen.getByText('待人工审核 1 份')).toBeInTheDocument()
    expect(screen.getByText('待归属 3 份')).toBeInTheDocument()
    expect(screen.getByText('批次 1 个')).toBeInTheDocument()
    expect(screen.getByText('验证结果 1 条')).toBeInTheDocument()
    expect(screen.getAllByText('2026-08').length).toBeGreaterThan(0)
    expect(screen.getByText('草稿')).toBeInTheDocument()
    expect(screen.getAllByText('已匹配').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/员••0001/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/\*\*\*\* 0123/).length).toBeGreaterThan(0)

    await waitFor(() => {
      const payrollCalls = fetchMock.mock.calls
        .map(([input]) => String(input))
        .filter((url) => url.startsWith('/api/v1/payroll/'))
      expect(payrollCalls).toEqual([
        '/api/v1/payroll/status',
        '/api/v1/payroll/test-workspace',
        '/api/v1/payroll/dashboard',
        '/api/v1/payroll/materials',
        '/api/v1/payroll/batches',
        '/api/v1/payroll/verification',
      ])
      expect(fetchMock.mock.calls.some(([input]) => /^https?:\/\//.test(String(input)))).toBe(false)
    })
  })

  it('submits receipt verification with only selected READY_FOR_MATCHING evidence and controlled command fields', async () => {
    window.history.replaceState({}, '', '/payroll')
    const fetchMock = installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: livePayrollResponses,
    })
    renderApp()

    const evidenceOptions = await screen.findAllByRole('checkbox')
    expect(evidenceOptions).toHaveLength(7)
    for (const evidenceOption of evidenceOptions) {
      expect(evidenceOption).not.toBeChecked()
      fireEvent.click(evidenceOption)
    }
    fireEvent.click(screen.getByRole('button', { name: '提交发放验证' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.filter(([input, init]) =>
        String(input) === `/api/v1/payroll/batches/${payrollBatchId}/verify-receipts`
        && init?.method === 'POST')).toHaveLength(1)
    })
    const [, init] = fetchMock.mock.calls.find(([input, request]) =>
      String(input) === `/api/v1/payroll/batches/${payrollBatchId}/verify-receipts`
      && request?.method === 'POST')!
    expect(init?.credentials).toBe('same-origin')
    expect(init?.headers).toMatchObject({
      'Content-Type': 'application/json',
      'X-CSRF-Token': session.csrf_token,
    })
    expect((init?.headers as Record<string, string>)['Idempotency-Key']).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i)
    expect(JSON.parse(String(init?.body))).toEqual({
      expected_revision: 4,
      reason_code: 'MANUAL_DISBURSEMENT_VERIFICATION',
      source_artifact_ids: payrollEvidenceIds,
    })
  })

  it('keeps receipt verification fail-closed when the server does not grant the action', async () => {
    window.history.replaceState({}, '', '/payroll')
    const liveStatus = livePayrollResponses['/api/v1/payroll/status'] as { data: Record<string, unknown> }
    const fetchMock = installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: {
        ...livePayrollResponses,
        '/api/v1/payroll/status': payrollRead({
          ...liveStatus.data,
          capabilities: { commands_enabled: true, allowed_actions: [] },
        }),
      },
    })
    renderApp()

    expect((await screen.findAllByText('网商银行代发表 · 2026-08')).length).toBe(5)
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '提交发放验证' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).endsWith('/verify-receipts') && init?.method === 'POST')).toBe(false)
  })

  it('retries a mixed payroll snapshot once and refuses to render it when it stays inconsistent', async () => {
    window.history.replaceState({}, '', '/payroll')
    const liveMaterials = livePayrollResponses['/api/v1/payroll/materials'] as { data: Record<string, unknown> }
    const fetchMock = installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: {
        ...livePayrollResponses,
        '/api/v1/payroll/materials': payrollRead({
          ...liveMaterials.data,
          projection_revision: 'b'.repeat(64),
          etag: `"${'b'.repeat(64)}"`,
        }),
      },
    })
    renderApp()

    expect(await screen.findByText('数据正在刷新，请稍后重试')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '真实材料汇总' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.filter(([input]) => String(input) === '/api/v1/payroll/materials')).toHaveLength(2)
  })

  it('requires imported receipt evidence and never invents a verification success or payment action', async () => {
    window.history.replaceState({}, '', '/payroll')
    const verificationWithoutEvidence = payrollRead({
      schema_version: 'ledgerbridge.payroll-verification-list.v1',
      projection_revision: 'a'.repeat(64),
      etag: `"${'a'.repeat(64)}"`,
      generated_at: '2026-08-30T08:00:00.000Z',
      items: [],
      available_evidence: [],
    })
    installFetch({
      runtimeMode: 'core-backed',
      payrollResponses: {
        ...livePayrollResponses,
        '/api/v1/payroll/verification': verificationWithoutEvidence,
      },
    })
    renderApp()

    expect(await screen.findByText('请先导入发放回单/流水')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '提交发放验证' })).not.toBeInTheDocument()
    expect(screen.queryByText(/验证成功/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /付款|发薪|银行提交/ })).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/demo|payable|payment_submission/i)
  })
})
