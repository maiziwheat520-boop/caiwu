import { minorToMajor } from '../api'
import type { ApiCandidate, Candidate, CandidateDetail } from '../types'

const sourceLabels: Record<ApiCandidate['source_channel'], Candidate['source']> = {
  telegram: 'Telegram',
  dingtalk: '钉钉',
  weixin: '微信',
  hermes: 'Hermes',
  outlook: '中行账单（复核材料）',
  controlled_upload: '照片凭证',
  synthetic: '合成数据',
}

export function toCandidate(candidate: ApiCandidate | CandidateDetail): Candidate {
  const blockerCodes = new Set(candidate.blockers.map((blocker) => blocker.code))
  const reviewRisks = candidate.review_risks ?? []
  const source = candidate.summary.startsWith('微信 |')
    ? '微信'
    : candidate.summary.startsWith('支付宝 |')
      ? '支付宝'
      : sourceLabels[candidate.source_channel]
  return {
    id: candidate.id,
    shortId: candidate.short_id,
    revision: candidate.revision,
    source,
    sourceChannel: candidate.source_channel,
    receivedAt: new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(candidate.received_at)),
    businessUnit: candidate.business_unit,
    businessUnitRef: candidate.business_unit_ref ?? '',
    category: candidate.category,
    categoryCode: candidate.category_code ?? '',
    amount: minorToMajor(candidate.amount_minor),
    amountMinor: candidate.amount_minor,
    accountingMonth: candidate.accounting_month,
    summary: candidate.summary,
    evidence: candidate.evidence,
    confidence: candidate.confidence_basis_points / 10000,
    status: candidate.status,
    blockers: candidate.blockers,
    reviewRisks,
    reviewEvents: 'review_events' in candidate ? candidate.review_events : [],
    incomplete: candidate.status === 'INCOMPLETE' || blockerCodes.has('MISSING_ACCOUNTING_MONTH'),
    conflict: candidate.status === 'CONFLICTED' || blockerCodes.has('BUSINESS_KEY_CONFLICT') || blockerCodes.has('DUPLICATE_MESSAGE') || blockerCodes.has('DUPLICATE_ATTACHMENT'),
    raw: candidate,
  }
}
