/** Naming rules for the supporting material a candidate still needs.
 *
 * The personal finance aggregation that used to live here moved into Core, so
 * the browser holds only what it still decides on its own: how to name the
 * document a reviewer has to go and find.
 */
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
