import type { Candidate } from '../types'

export const materialRiskCodes = new Set([
  'FUNDING_STATEMENT_REQUIRED',
  'RELATED_ACCOUNT_STATEMENT_REQUIRED',
  'HOTEL_PAYOUT_STATEMENT_REQUIRED',
])

export const platformInternalAccounts = new Set(['花呗', '余额宝', '账户余额', '零钱', '零钱通'])

export function summaryFields(candidate: Candidate): string[] {
  return candidate.summary.split('|').map((value) => value.trim())
}

export function candidateCashflowMinor(candidate: Candidate): number {
  const statedDirection = summaryFields(candidate)[2]
  if (statedDirection === '收入') return Math.abs(candidate.amountMinor)
  if (statedDirection === '支出') return -Math.abs(candidate.amountMinor)
  return candidate.amountMinor
}

export type PersonalTransaction = {
  candidate: Candidate
  cashflowMinor: number
  date: string
  transactionType: string
  counterparty: string
  sourceKind: 'PLATFORM' | 'BANK'
  scopeStatus: 'PERSONAL' | 'UNASSIGNED'
}

export type PersonalFinanceSelection = {
  entries: PersonalTransaction[]
  unassignedEntries: PersonalTransaction[]
  excludedCount: number
  deduplicatedCount: number
}

export function personalTransaction(candidate: Candidate): PersonalTransaction | null {
  if (candidate.status !== 'CONFIRMED') return null
  const fields = summaryFields(candidate)
  if (fields.length < 7 || !/^\d{4}-\d{2}-\d{2}$/.test(fields[1])) return null
  const sourceKind = fields[0] === '微信' || fields[0] === '支付宝'
    ? 'PLATFORM'
    : fields[0].includes('银行')
      ? 'BANK'
      : null
  if (!sourceKind) return null

  const scope = `${candidate.businessUnit} ${candidate.businessUnitRef}`.trim()
  if (/(公司|酒店|宾馆|门店|company|hotel)/i.test(scope)) return null

  return {
    candidate,
    cashflowMinor: candidateCashflowMinor(candidate),
    date: fields[1],
    transactionType: fields[3],
    counterparty: fields[4],
    sourceKind,
    scopeStatus: /(个人|本人|personal)/i.test(scope) ? 'PERSONAL' : 'UNASSIGNED',
  }
}

export function selectPersonalFinanceEntries(candidates: Candidate[]): PersonalFinanceSelection {
  const eligible = candidates.flatMap((candidate) => {
    const entry = personalTransaction(candidate)
    return entry ? [entry] : []
  })
  const excludedCount = candidates.length - eligible.length
  let deduplicatedCount = 0
  const groups = eligible.reduce((result, entry) => {
    const direction = entry.cashflowMinor < 0 ? 'OUT' : 'IN'
    const key = `${entry.date}|${direction}|${Math.abs(entry.cashflowMinor)}|${entry.counterparty}`
    const group = result.get(key) ?? []
    group.push(entry)
    result.set(key, group)
    return result
  }, new Map<string, PersonalTransaction[]>())

  const deduplicatedEntries = [...groups.values()].flatMap((group) => {
    const isTransfer = group.some((entry) => /(转账|提现)/.test(entry.transactionType))
    const preferredKind = isTransfer ? 'BANK' : 'PLATFORM'
    const preferred = group.filter((entry) => entry.sourceKind === preferredKind)
    const lowerPriority = group.filter((entry) => entry.sourceKind !== preferredKind)
    if (preferred.length === 0 || lowerPriority.length === 0) return group

    const pairedCount = Math.min(preferred.length, lowerPriority.length)
    deduplicatedCount += pairedCount
    return [...preferred, ...lowerPriority.slice(pairedCount)]
  })

  return {
    entries: deduplicatedEntries.filter((entry) => entry.scopeStatus === 'PERSONAL'),
    unassignedEntries: deduplicatedEntries.filter((entry) => entry.scopeStatus === 'UNASSIGNED'),
    excludedCount,
    deduplicatedCount,
  }
}

export function counterpartyFor(candidate: Candidate): string {
  return summaryFields(candidate)[4] ?? ''
}

export function paymentMethodFor(candidate: Candidate): string {
  return summaryFields(candidate)[5] ?? ''
}

export function isPlatformInternalAccount(value: string): boolean {
  const normalized = value.trim().replace(/^(支付宝|微信)[:：]?/, '')
  return platformInternalAccounts.has(normalized)
}

export function materialNameFor(candidate: Candidate, riskCode: string): string | null {
  if (!materialRiskCodes.has(riskCode)) return null
  if (riskCode === 'FUNDING_STATEMENT_REQUIRED') {
    const paymentMethod = paymentMethodFor(candidate)
    if (!paymentMethod) return '资金账户明细'
    return !isPlatformInternalAccount(paymentMethod) ? `${paymentMethod}明细` : null
  }
  if (riskCode === 'RELATED_ACCOUNT_STATEMENT_REQUIRED') {
    const counterparty = counterpartyFor(candidate)
    return isPlatformInternalAccount(counterparty) ? null : `${counterparty || '关联账户'}同期流水`
  }
  return '酒店平台收款银行流水'
}
