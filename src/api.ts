import type {
  ApiCandidate,
  AuthenticationOptionsJson,
  AuthResult,
  AuthStatus,
  CandidateCorrections,
  CandidateDecision,
  CandidateDetail,
  CandidateListResponse,
  ConnectionStatus,
  EvidencePreview,
  PasskeyAdditionResult,
  Problem,
  Reconciliation,
  ReviewEvent,
  ReviewEventListResponse,
  Session,
  RegistrationOptionsJson,
  WorkbookDraft,
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

  listCandidates: ({ status, cursor }: { status?: string; cursor?: string } = {}) => {
    const query = new URLSearchParams()
    if (status) query.set('status', status)
    if (cursor) query.set('cursor', cursor)
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    return requestJson<CandidateListResponse>(`/api/v1/candidates${suffix}`)
  },

  getCandidate: (candidateId: string) =>
    requestJson<CandidateDetail>(`/api/v1/candidates/${encodeURIComponent(candidateId)}`),

  getEvidencePreview: (evidenceId: string, reference: string) => {
    const query = new URLSearchParams({ reference })
    return requestJson<EvidencePreview>(
      `/api/v1/evidence/${encodeURIComponent(evidenceId)}/preview?${query.toString()}`,
    )
  },

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

  createWorkbookDraft: ({ accountingMonth, expectedRevision, csrfToken }: {
    accountingMonth: string
    expectedRevision: number
    csrfToken: string
  }) => requestJson<WorkbookDraft>(`/api/v1/reconciliations/${encodeURIComponent(accountingMonth)}/drafts`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': createOperationId(),
      'X-CSRF-Token': csrfToken,
    },
    body: JSON.stringify({ expected_revision: expectedRevision }),
  }),

  listConnections: async () => {
    const response = await requestJson<{ items: ConnectionStatus[] }>('/api/v1/connections')
    return response.items
  },
}

export const minorToMajor = (amountMinor: number) => amountMinor / 100
export const majorToMinor = (amount: number) => Math.round(amount * 100)
