export type Page = 'overview' | 'review' | 'reconciliation' | 'files'

export type CandidateStatus = 'pending' | 'confirmed' | 'ignored'

export type Candidate = {
  id: string
  source: 'Telegram' | '钉钉' | '微信'
  receivedAt: string
  sender: string
  businessUnit: string
  category: string
  amount: number
  accountingMonth: string | null
  summary: string
  evidence: string
  attachment?: string
  confidence: number
  status: CandidateStatus
  incomplete?: boolean
  conflict?: boolean
}

export type Notice = {
  tone: 'success' | 'info'
  message: string
}
