import type { Candidate, OriginalReconciliation } from '../types'

export type OriginalWorkflowFlowKind = 'income' | 'expense' | 'current' | 'unclassified'

export type OriginalWorkflowItem = {
  candidate: Candidate
  flowKind: OriginalWorkflowFlowKind
  signedAmountMinor: number
}

export type OriginalWorkflowStageState = 'COMPLETE' | 'BLOCKED' | 'PAUSED'

export type OriginalWorkflowStage = {
  id: 'source' | 'classification' | 'account-match' | 'evidence' | 'review' | 'close'
  label: string
  state: OriginalWorkflowStageState
  detail: string
}

export type OriginalWorkflowSummary = {
  itemCount: number
  classifiedCount: number
  evidenceLinkedCount: number
  pendingReviewCount: number
  closeReady: boolean
  stages: OriginalWorkflowStage[]
  blockers: string[]
}

const actionableStatuses = new Set(['PENDING', 'INCOMPLETE', 'CONFLICTED'])

function unique(values: string[]) {
  return [...new Set(values)]
}

export function buildOriginalWorkflowSummary(
  items: OriginalWorkflowItem[],
  projection: OriginalReconciliation | null,
): OriginalWorkflowSummary {
  const itemCount = items.length
  const unclassifiedCount = items.filter((item) => item.flowKind === 'unclassified').length
  const classifiedCount = itemCount - unclassifiedCount
  const evidenceLinkedCount = items.filter((item) => item.candidate.evidence.length > 0).length
  const missingEvidenceCount = itemCount - evidenceLinkedCount
  const pendingReviewCount = items.filter((item) => actionableStatuses.has(item.candidate.status)).length
  const hasStableAccountMatchContract = false
  const hasFormalCloseCommand = false

  const blockers = unique([
    ...(itemCount === 0 ? ['本月尚无 Core 已识别的旧表事项'] : []),
    ...(unclassifiedCount > 0 ? [`${unclassifiedCount} 笔事项的业务性质待归类`] : []),
    ...(missingEvidenceCount > 0 ? [`${missingEvidenceCount} 笔事项没有关联凭证`] : []),
    ...(pendingReviewCount > 0 ? [`${pendingReviewCount} 笔事项仍待审核或补录`] : []),
    ...(!hasStableAccountMatchContract && itemCount > 0
      ? ['逐笔事项缺少稳定旧表项目编号与账户引用，不能自动绑定账单账户']
      : []),
    ...(!hasFormalCloseCommand && itemCount > 0
      ? ['正式关账仍需 Core 月结命令；审核清单导出不能代替月结']
      : []),
    ...(!projection ? ['Core 月度完整性状态暂不可用'] : []),
    ...(projection && !projection.is_complete ? ['Core 月度投影尚未满足完整性条件'] : []),
    ...(projection?.confirmed_pending_posting_count
      ? [`${projection.confirmed_pending_posting_count} 条已确认事项尚未进入正式账簿`]
      : []),
    ...(projection?.missing_material_count
      ? [`${projection.missing_material_count} 份月度材料仍待补充`]
      : []),
    ...(projection?.unmapped_confirmed_count
      ? [`${projection.unmapped_confirmed_count} 条已确认事项仍未映射到旧表项目`]
      : []),
  ])

  const closeReady = Boolean(
    itemCount > 0
    && unclassifiedCount === 0
    && missingEvidenceCount === 0
    && pendingReviewCount === 0
    && hasStableAccountMatchContract
    && hasFormalCloseCommand
    && projection?.is_complete,
  )

  const stages: OriginalWorkflowStage[] = [
    {
      id: 'source',
      label: '事项来源',
      state: 'PAUSED',
      detail: itemCount > 0
        ? `已读取 ${itemCount} 笔 Core 旧表事项；旧截图和历史表格提交继续暂停`
        : '旧截图和历史表格提交继续暂停；等待账单导出形成受控事项',
    },
    {
      id: 'classification',
      label: '业务归类',
      state: itemCount > 0 && unclassifiedCount === 0 ? 'COMPLETE' : 'BLOCKED',
      detail: itemCount === 0 ? '尚无可归类事项' : `${classifiedCount}/${itemCount} 笔已区分收入、支出或往来款`,
    },
    {
      id: 'account-match',
      label: '账单账户匹配',
      state: 'BLOCKED',
      detail: itemCount === 0 ? '等待旧表事项' : '待 Core 提供稳定事项编号与账户引用；不按摘要猜账户',
    },
    {
      id: 'evidence',
      label: '凭证关联',
      state: itemCount > 0 && missingEvidenceCount === 0 ? 'COMPLETE' : 'BLOCKED',
      detail: itemCount === 0 ? '尚无可关联事项' : `${evidenceLinkedCount}/${itemCount} 笔已关联至少一份凭证`,
    },
    {
      id: 'review',
      label: '逐项审核',
      state: itemCount > 0 && pendingReviewCount === 0 ? 'COMPLETE' : 'BLOCKED',
      detail: itemCount === 0 ? '尚无可审核事项' : pendingReviewCount === 0 ? '本月事项均已进入终态' : `${pendingReviewCount} 笔仍待处理`,
    },
    {
      id: 'close',
      label: '月度闭环',
      state: closeReady ? 'COMPLETE' : 'BLOCKED',
      detail: closeReady ? '本月已完成正式闭环' : '只展示可验证阻断；正式关账仍需 Core 月结命令',
    },
  ]

  return {
    itemCount,
    classifiedCount,
    evidenceLinkedCount,
    pendingReviewCount,
    closeReady,
    stages,
    blockers,
  }
}

const flowLabels: Record<OriginalWorkflowFlowKind, string> = {
  income: '收入',
  expense: '支出',
  current: '往来款',
  unclassified: '待归类',
}

const statusLabels: Record<Candidate['status'], string> = {
  INCOMPLETE: '待补录',
  PENDING: '待审核',
  CONFLICTED: '有冲突',
  CONFIRMED: '已确认',
  IGNORED: '已忽略',
  SUPERSEDED: '已取代',
}

function csvCell(value: string | number) {
  return `"${String(value).replaceAll('"', '""')}"`
}

export function buildOriginalReviewCsv(month: string, items: OriginalWorkflowItem[]) {
  const rows: Array<Array<string | number>> = [
    ['月份', '事项编号', '业务性质', '公司/门店', '种类', '金额（分）', '审核状态', '凭证数', '凭证文件', '取数账户对应'],
    ...items.map((item) => [
      month,
      item.candidate.shortId,
      flowLabels[item.flowKind],
      item.candidate.businessUnit || '待补',
      item.candidate.category || '待补',
      item.signedAmountMinor,
      statusLabels[item.candidate.status],
      item.candidate.evidence.length,
      item.candidate.evidence.map((evidence) => evidence.original_filename ?? evidence.id).join(' | '),
      '待 Core 稳定事项编号与账户引用',
    ]),
  ]
  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n')
}
