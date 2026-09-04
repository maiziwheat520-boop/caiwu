import type { ReviewEvent } from '../types'

export const decisionLabels: Record<ReviewEvent['decision'], string> = {
  CONFIRM: '确认候选',
  CORRECT_AND_CONFIRM: '更正并确认',
  IGNORE: '忽略候选',
  RESOLVE_CONFLICT: '解决冲突',
}

export const decisionColors: Record<ReviewEvent['decision'], 'green' | 'blue' | 'gray' | 'red'> = {
  CONFIRM: 'green',
  CORRECT_AND_CONFIRM: 'blue',
  IGNORE: 'gray',
  RESOLVE_CONFLICT: 'red',
}

export const auditFieldLabels: Record<ReviewEvent['changes'][number]['field'], string> = {
  business_unit: '营业单元',
  category: '科目',
  amount_minor: '金额',
  accounting_month: '归属月份',
  status: '状态',
}

export function auditChangeLabel(change: ReviewEvent['changes'][number]) {
  const label = auditFieldLabels[change.field]
  return change.identity_changed ? `${label}（标识已更新）` : label
}
