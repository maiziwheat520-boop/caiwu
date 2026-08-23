import type {
  ApiCandidate,
  CandidateCorrections,
  CandidateDecision,
  CandidateDetail,
  CandidateListResponse,
  ConnectionStatus,
  Problem,
  Reconciliation,
  ReviewEvent,
  Session,
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

export const api = {
  getSession: () => requestJson<Session>('/api/v1/session'),

  listCandidates: (status?: string) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : ''
    return requestJson<CandidateListResponse>(`/api/v1/candidates${query}`)
  },

  getCandidate: (candidateId: string) =>
    requestJson<CandidateDetail>(`/api/v1/candidates/${encodeURIComponent(candidateId)}`),

  appendDecision: ({ candidate, decision, reason, corrections, csrfToken }: {
    candidate: ApiCandidate
    decision: CandidateDecision
    reason: string
    corrections?: CandidateCorrections
    csrfToken: string
  }) => requestJson<{ candidate: ApiCandidate; event: ReviewEvent }>(
    `/api/v1/candidates/${encodeURIComponent(candidate.id)}/decisions`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
        'X-CSRF-Token': csrfToken,
      },
      body: JSON.stringify({
        decision,
        expected_revision: candidate.revision,
        reason,
        ...(corrections ? { corrections } : {}),
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
      'Idempotency-Key': crypto.randomUUID(),
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
