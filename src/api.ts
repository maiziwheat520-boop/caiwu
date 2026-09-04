import type {
  AccountingDimensions,
  ApiCandidate,
  AuthenticationOptionsJson,
  AuthResult,
  AuthStatus,
  CandidateCorrections,
  CandidateDecision,
  CandidateDetail,
  CandidateListResponse,
  ClassificationBatchReceipt,
  ClassificationGroup,
  ClassificationGroupPage,
  ClassificationTarget,
  CompanyReportsResponse,
  ConnectionStatus,
  EvidenceUnlockResult,
  EvidencePreview,
  OriginalReconciliation,
  PasskeyAdditionResult,
  PersonalBankTransactionsResponse,
  PersonalBankStatement,
  PersonalBankStatementReviewReceipt,
  CompanyBankStatement,
  CompanyBankStatementsResponse,
  CompanyTransactionCategory,
  CompanyTransactionClassification,
  CompanyTransactionClassificationReviewReceipt,
  CompanyTransactionClassificationsResponse,
  CompanyOperatingFeeReportingItem,
  PayrollBatchListData,
  PayrollCommandResult,
  PayrollDashboardData,
  PayrollMaterialListData,
  PayrollReadResponse,
  PayrollStatusData,
  PayrollVerificationListData,
  PayrollTestWorkspaceReadResponse,
  PayrollTestWorkspaceCommandResult,
  PayrollTestMaterialOrganizeResult,
  PayrollTestBatchValidationResult,
  PayrollTestMaterialType,
  PayrollTestMaterialPreviewResponse,
  PayrollInputMaterialPreviewResponse,
  PayrollSummaryAuthoritativePreviewResponse,
  PayrollLegacyAction,
  PayrollLegacyCommandResult,
  PayrollLegacyWorkspaceReadResponse,
  Problem,
  Reconciliation,
  ReviewEvent,
  ReviewEventListResponse,
  Session,
  RegistrationOptionsJson,
} from './types'

export class ApiError extends Error {
  status: number
  code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export function createOperationId(): string {
  if (typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'))
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-')
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      Accept: 'application/json',
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let problem: Problem | undefined
    try {
      problem = await response.json() as Problem
    } catch {
      problem = undefined
    }
    throw new ApiError(problem?.detail || problem?.title || `请求失败（${response.status}）`, response.status, problem?.code)
  }

  return response.json() as Promise<T>
}

async function requestVoid(path: string, init: RequestInit): Promise<void> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (!response.ok) {
    let problem: Problem | undefined
    try {
      problem = await response.json() as Problem
    } catch {
      problem = undefined
    }
    throw new ApiError(problem?.detail || problem?.title || `请求失败（${response.status}）`, response.status, problem?.code)
  }
}

async function requestEvidenceUnlock(path: string, init: RequestInit): Promise<EvidenceUnlockResult> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
  })
  if (!response.ok) {
    await response.body?.cancel()
    throw new ApiError(
      response.status >= 500 ? '账单解锁服务暂不可用，请稍后重试' : '账单解锁失败，请检查密码后重试',
      response.status,
    )
  }
  try {
    const result = await response.json() as Partial<EvidenceUnlockResult>
    if (result.unlocked !== true) throw new Error('invalid unlock response')
    return { unlocked: true }
  } catch {
    throw new ApiError('账单解锁服务暂不可用，请稍后重试', 503)
  }
}

export function base64UrlToArrayBuffer(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')
  const binary = atob(padded)
  return Uint8Array.from(binary, (character) => character.charCodeAt(0)).buffer as ArrayBuffer
}

export function arrayBufferToBase64Url(value: ArrayBuffer | ArrayBufferView): string {
  const bytes = value instanceof ArrayBuffer
    ? new Uint8Array(value)
    : new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  let binary = ''
  bytes.forEach((byte) => { binary += String.fromCharCode(byte) })
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '')
}

function registrationOptionsFromJson(options: RegistrationOptionsJson): PublicKeyCredentialCreationOptions {
  return {
    ...options,
    challenge: base64UrlToArrayBuffer(options.challenge),
    user: { ...options.user, id: base64UrlToArrayBuffer(options.user.id) },
    excludeCredentials: options.excludeCredentials?.map((credential) => ({
      ...credential,
      id: base64UrlToArrayBuffer(credential.id),
    })),
  }
}

function authenticationOptionsFromJson(options: AuthenticationOptionsJson): PublicKeyCredentialRequestOptions {
  return {
    ...options,
    challenge: base64UrlToArrayBuffer(options.challenge),
    allowCredentials: options.allowCredentials?.map((credential) => ({
      ...credential,
      id: base64UrlToArrayBuffer(credential.id),
    })),
  }
}

function serializeRegistrationCredential(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAttestationResponse
  return {
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: arrayBufferToBase64Url(response.clientDataJSON),
      attestationObject: arrayBufferToBase64Url(response.attestationObject),
      transports: typeof response.getTransports === 'function' ? response.getTransports() : undefined,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  }
}

function serializeAuthenticationCredential(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: arrayBufferToBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: arrayBufferToBase64Url(response.clientDataJSON),
      authenticatorData: arrayBufferToBase64Url(response.authenticatorData),
      signature: arrayBufferToBase64Url(response.signature),
      userHandle: response.userHandle ? arrayBufferToBase64Url(response.userHandle) : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
  }
}

const jsonPost = (body: unknown, csrfToken?: string): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) },
  body: JSON.stringify(body),
})

export const api = {
  getAuthStatus: () => requestJson<AuthStatus>('/api/v1/auth/status'),

  registerPasskey: async (setupCode: string, csrfToken?: string) => {
    if (!navigator.credentials) throw new Error('当前浏览器不支持通行密钥')
    const options = await requestJson<RegistrationOptionsJson>('/api/v1/auth/passkey/register/options', jsonPost({ setup_code: setupCode }, csrfToken))
    const credential = await navigator.credentials.create({ publicKey: registrationOptionsFromJson(options) })
    if (!credential || credential.type !== 'public-key' || !('rawId' in credential)) throw new Error('通行密钥创建未完成')
    return requestJson<AuthResult>('/api/v1/auth/passkey/register/verify', jsonPost({
      setup_code: setupCode,
      credential: serializeRegistrationCredential(credential as PublicKeyCredential),
    }, csrfToken))
  },

  loginWithPasskey: async () => {
    if (!navigator.credentials) throw new Error('当前浏览器不支持通行密钥')
    const options = await requestJson<AuthenticationOptionsJson>('/api/v1/auth/passkey/login/options', jsonPost({}))
    const credential = await navigator.credentials.get({ publicKey: authenticationOptionsFromJson(options) })
    if (!credential || credential.type !== 'public-key' || !('rawId' in credential)) throw new Error('通行密钥验证未完成')
    return requestJson<AuthResult>('/api/v1/auth/passkey/login/verify', jsonPost({
      credential: serializeAuthenticationCredential(credential as PublicKeyCredential),
    }))
  },

  addPasskey: async (csrfToken: string) => {
    if (!navigator.credentials) throw new Error('当前浏览器不支持通行密钥')
    const authorizationOptions = await requestJson<AuthenticationOptionsJson>(
      '/api/v1/auth/passkey/add/authorize/options',
      jsonPost({}, csrfToken),
    )
    const authorization = await navigator.credentials.get({
      publicKey: authenticationOptionsFromJson(authorizationOptions),
    })
    if (!authorization || authorization.type !== 'public-key' || !('rawId' in authorization)) {
      throw new Error('现有通行密钥验证未完成')
    }
    const registrationOptions = await requestJson<RegistrationOptionsJson>(
      '/api/v1/auth/passkey/add/authorize/verify',
      jsonPost({
        credential: serializeAuthenticationCredential(authorization as PublicKeyCredential),
      }, csrfToken),
    )
    const credential = await navigator.credentials.create({
      publicKey: registrationOptionsFromJson(registrationOptions),
    })
    if (!credential || credential.type !== 'public-key' || !('rawId' in credential)) {
      throw new Error('新通行密钥创建未完成')
    }
    return requestJson<PasskeyAdditionResult>('/api/v1/auth/passkey/add/verify', jsonPost({
      credential: serializeRegistrationCredential(credential as PublicKeyCredential),
    }, csrfToken))
  },

  recoverSession: (recoveryCode: string) =>
    requestJson<AuthResult>('/api/v1/auth/recovery', jsonPost({ recovery_code: recoveryCode })),

  getRecoverySession: () => requestJson<Session>('/api/v1/auth/recovery/session'),

  logout: (csrfToken: string) => requestVoid('/api/v1/session/logout', {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrfToken },
  }),

  getSession: () => requestJson<Session>('/api/v1/session'),

  getCompanyReports: ({ fromMonth, toMonth }: { fromMonth?: string; toMonth?: string } = {}) => {
    const query = new URLSearchParams()
    if (fromMonth) query.set('from_month', fromMonth)
    if (toMonth) query.set('to_month', toMonth)
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    return requestJson<CompanyReportsResponse>(`/api/v1/company-reports${suffix}`)
  },

  getPersonalBankTransactions: () =>
    requestJson<PersonalBankTransactionsResponse>('/api/v1/personal-finance/bank-transactions'),

  reviewPersonalBankStatement: ({ statement, decision, reason, csrfToken }: {
    statement: PersonalBankStatement
    decision: 'CONFIRMED' | 'REJECTED'
    reason: string
    csrfToken: string
  }) => requestJson<PersonalBankStatementReviewReceipt>(
    `/api/v1/personal-finance/bank-statements/${encodeURIComponent(statement.statement_ref)}/reviews`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        expected_revision: statement.review_revision,
        decision,
        reason,
      }),
    },
  ),

  getCompanyBankStatements: () =>
    requestJson<CompanyBankStatementsResponse>('/api/v1/company-bank-statements'),

  reviewCompanyBankStatement: ({ statement, decision, reason, csrfToken }: {
    statement: CompanyBankStatement
    decision: 'CONFIRMED' | 'REJECTED'
    reason: string
    csrfToken: string
  }) => requestJson<PersonalBankStatementReviewReceipt>(
    `/api/v1/company-bank-statements/${encodeURIComponent(statement.statement_ref)}/reviews`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        expected_revision: statement.review_revision,
        decision,
        reason,
      }),
    },
  ),

  getCompanyTransactionClassifications: () =>
    requestJson<CompanyTransactionClassificationsResponse>(
      '/api/v1/company-transaction-classifications',
    ),

  reviewCompanyTransactionClassification: ({
    transaction,
    categoryCode,
    reportingItemCode,
    reason,
    csrfToken,
  }: {
    transaction: CompanyTransactionClassification
    categoryCode: CompanyTransactionCategory
    reportingItemCode: CompanyOperatingFeeReportingItem | null
    reason: string
    csrfToken: string
  }) => requestJson<CompanyTransactionClassificationReviewReceipt>(
    `/api/v1/company-transaction-classifications/${encodeURIComponent(transaction.transaction_ref)}/reviews`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        entity_ref: transaction.entity_ref,
        expected_revision: transaction.revision,
        category_code: categoryCode,
        reporting_item_code: reportingItemCode,
        reason,
      }),
    },
  ),

  listCandidates: ({ status, cursor }: { status?: string; cursor?: string } = {}) => {
    const query = new URLSearchParams()
    if (status) query.set('status', status)
    if (cursor) query.set('cursor', cursor)
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    return requestJson<CandidateListResponse>(`/api/v1/candidates${suffix}`)
  },

  getCandidate: (candidateId: string) =>
    requestJson<CandidateDetail>(`/api/v1/candidates/${encodeURIComponent(candidateId)}`),

  getAccountingDimensions: () =>
    requestJson<AccountingDimensions>('/api/v1/accounting-dimensions'),

  listClassificationGroups: () =>
    requestJson<ClassificationGroupPage>('/api/v1/candidate-classification-groups'),

  applyClassificationBatch: ({ group, sourceCandidate, target, reason, csrfToken }: {
    group: ClassificationGroup
    sourceCandidate: ApiCandidate
    target: ClassificationTarget
    reason: string
    csrfToken: string
  }) => requestJson<ClassificationBatchReceipt>(
    `/api/v1/candidate-classification-groups/${encodeURIComponent(group.group_ref)}/decisions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        source_candidate_ref: sourceCandidate.id,
        accounting_month: group.accounting_month,
        target,
        members: eligibleClassificationBatchMembers(group)
          .map((member) => ({
            candidate_ref: member.candidate_ref,
            expected_revision: member.revision,
          })),
        reason,
        acknowledged_risk_codes: group.conditions.risk_signature,
      }),
    },
  ),

  getEvidencePreview: (evidenceId: string, reference: string) => {
    const query = new URLSearchParams({ reference })
    return requestJson<EvidencePreview>(
      `/api/v1/evidence/${encodeURIComponent(evidenceId)}/preview?${query.toString()}`,
    )
  },

  unlockEvidence: ({ sourceRef, password, csrfToken }: {
    sourceRef: string
    password: string
    csrfToken: string
  }) => requestEvidenceUnlock('/api/v1/evidence/unlocks', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': createOperationId(),
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify({ source_ref: sourceRef, password }),
  }),

  listReviewEvents: (cursor?: string) => requestJson<ReviewEventListResponse>(
    `/api/v1/review-events${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`,
  ),

  appendDecision: ({ candidate, decision, reason, corrections, conflictResolution, csrfToken }: {
    candidate: ApiCandidate
    decision: CandidateDecision
    reason: string
    corrections?: CandidateCorrections
    conflictResolution?: string
    csrfToken: string
  }) => requestJson<{ candidate: ApiCandidate; event: ReviewEvent }>(
    `/api/v1/candidates/${encodeURIComponent(candidate.id)}/decisions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        decision,
        expected_revision: candidate.revision,
        reason,
        ...(corrections ? { corrections } : {}),
        ...(conflictResolution ? { conflict_resolution: conflictResolution } : {}),
      }),
    },
  ),

  getReconciliation: (accountingMonth: string) =>
    requestJson<Reconciliation>(`/api/v1/reconciliations/${encodeURIComponent(accountingMonth)}`),

  getOriginalReconciliation: ({ accountingMonth, entityRef, businessUnitRef }: {
    accountingMonth: string
    entityRef?: string
    businessUnitRef?: string
  }) => {
    const params = new URLSearchParams()
    if (entityRef) params.set('entity_ref', entityRef)
    if (businessUnitRef) params.set('business_unit', businessUnitRef)
    const query = params.size > 0 ? `?${params.toString()}` : ''
    return requestJson<OriginalReconciliation>(
      `/api/v1/original-reconciliations/${encodeURIComponent(accountingMonth)}${query}`,
    )
  },

  getCashReconciliation: (accountingMonth: string) =>
    requestJson<import('./types').CashReconciliation>(
      `/api/v1/cash-reconciliations/${encodeURIComponent(accountingMonth)}`,
    ),

  listConnections: async () => {
    const response = await requestJson<{ items: ConnectionStatus[] }>('/api/v1/connections')
    return response.items
  },

  getPayrollStatus: () =>
    requestJson<PayrollReadResponse<PayrollStatusData>>('/api/v1/payroll/status'),

  getPayrollTestWorkspace: () =>
    requestJson<PayrollTestWorkspaceReadResponse>('/api/v1/payroll/test-workspace'),

  getPayrollLegacyWorkspace: () =>
    requestJson<PayrollLegacyWorkspaceReadResponse>('/api/v1/payroll/legacy-workspace'),

  runPayrollLegacyCommand: ({ action, expectedRevision, payload, csrfToken }: {
    action: PayrollLegacyAction
    expectedRevision: number
    payload: Record<string, unknown>
    csrfToken: string
  }) => requestJson<PayrollLegacyCommandResult>('/api/v1/payroll/legacy-workspace/commands', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': createOperationId(),
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify({
      action,
      expected_revision: expectedRevision,
      payload,
    }),
  }),

  previewPayrollTestMaterial: (materialId: string) =>
    requestJson<PayrollTestMaterialPreviewResponse>(
      `/api/v1/payroll/test-workspace/materials/${encodeURIComponent(materialId)}/preview`,
    ),

  previewPayrollInputMaterial: (materialId: string) =>
    requestJson<PayrollInputMaterialPreviewResponse>(
      `/api/v1/payroll/test-workspace/materials/${encodeURIComponent(materialId)}/preview`,
    ),

  previewPayrollSummaryMaterial: (materialId: string) =>
    requestJson<PayrollSummaryAuthoritativePreviewResponse>(
      `/api/v1/payroll/test-workspace/materials/${encodeURIComponent(materialId)}/preview`,
    ),

  organizePayrollTestMaterial: ({
    materialId,
    expectedWorkspaceRevision,
    period,
    materialType,
    csrfToken,
  }: {
    materialId: string
    expectedWorkspaceRevision: number
    period: string
    materialType: PayrollTestMaterialType
    csrfToken: string
  }) => requestJson<PayrollTestWorkspaceCommandResult<PayrollTestMaterialOrganizeResult>>(
    `/api/v1/payroll/test-workspace/materials/${encodeURIComponent(materialId)}/organize`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        expected_workspace_revision: expectedWorkspaceRevision,
        period,
        material_type: materialType,
      }),
    },
  ),

  validatePayrollTestWorkspace: ({ expectedWorkspaceRevision, csrfToken }: {
    expectedWorkspaceRevision: number
    csrfToken: string
  }) => requestJson<PayrollTestWorkspaceCommandResult<PayrollTestBatchValidationResult>>(
    '/api/v1/payroll/test-workspace/validate',
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({ expected_workspace_revision: expectedWorkspaceRevision }),
    },
  ),

  getPayrollDashboard: () =>
    requestJson<PayrollReadResponse<PayrollDashboardData>>('/api/v1/payroll/dashboard'),

  listPayrollMaterials: () =>
    requestJson<PayrollReadResponse<PayrollMaterialListData>>('/api/v1/payroll/materials'),

  listPayrollBatches: () =>
    requestJson<PayrollReadResponse<PayrollBatchListData>>('/api/v1/payroll/batches'),

  listPayrollVerification: () =>
    requestJson<PayrollReadResponse<PayrollVerificationListData>>('/api/v1/payroll/verification'),

  verifyPayrollReceipts: ({ batchId, expectedRevision, sourceArtifactIds, csrfToken }: {
    batchId: string
    expectedRevision: number
    sourceArtifactIds: string[]
    csrfToken: string
  }) => requestJson<PayrollCommandResult>(
    `/api/v1/payroll/batches/${encodeURIComponent(batchId)}/verify-receipts`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': createOperationId(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        expected_revision: expectedRevision,
        reason_code: 'MANUAL_DISBURSEMENT_VERIFICATION',
        source_artifact_ids: sourceArtifactIds,
      }),
    },
  ),
}

export const minorToMajor = (amountMinor: number) => amountMinor / 100
export const majorToMinor = (amount: number) => Math.round(amount * 100)

export function minorToMajorInput(amountMinor: number): string {
  if (!Number.isSafeInteger(amountMinor)) throw new RangeError('amount_minor is not a safe integer')
  const value = BigInt(amountMinor)
  const absolute = value < 0n ? -value : value
  const sign = value < 0n ? '-' : ''
  return `${sign}${absolute / 100n}.${String(absolute % 100n).padStart(2, '0')}`
}

export function majorInputToMinor(amount: string): number | null {
  if (amount.length > 18) return null
  const match = /^(-?)([0-9]+)(?:\.([0-9]{1,2}))?$/.exec(amount)
  if (!match) return null
  const cents = BigInt(match[2]) * 100n + BigInt((match[3] ?? '').padEnd(2, '0') || '0')
  const signed = match[1] === '-' ? -cents : cents
  const limit = BigInt(Number.MAX_SAFE_INTEGER)
  return signed < -limit || signed > limit ? null : Number(signed)
}

export function eligibleClassificationBatchMembers(group: ClassificationGroup) {
  return group.members.filter(
    (member) => member.status === 'PENDING' && member.batch_eligible,
  )
}
