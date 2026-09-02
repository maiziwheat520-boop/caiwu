import { describe, expect, it } from 'vitest'
import { originalReconciliationFixture } from '../test-fixtures/original-reconciliation'
import type { Candidate } from '../types'
import {
  buildOriginalReviewCsv,
  buildOriginalWorkflowSummary,
  type OriginalWorkflowItem,
} from './workflowState'

function candidate(overrides: Partial<Candidate> = {}): Candidate {
  const raw = {
    id: '10000000-0000-4000-8000-000000000001',
    short_id: 'C-001',
    revision: 1,
    status: 'CONFIRMED' as const,
    source_channel: 'controlled_upload' as const,
    source_system: 'original_reconciliation_xlsx',
    source_message_id: 'source-001',
    received_at: '2026-08-01T00:00:00Z',
    business_unit: '薇旭',
    business_unit_ref: 'unit-weixu',
    category: '房费收入',
    category_code: 'ROOM_INCOME',
    amount_minor: 12_345,
    currency: 'CNY' as const,
    accounting_month: '2026-08',
    summary: '旧表事项',
    confidence_basis_points: 10_000,
    evidence: [{
      id: 'evidence-001',
      kind: 'attachment' as const,
      media_type: 'application/pdf',
      sha256: null,
      original_filename: '账单.pdf',
    }],
    blockers: [],
    review_risks: [],
  }
  return {
    id: raw.id,
    shortId: raw.short_id,
    revision: raw.revision,
    source: '照片凭证',
    sourceChannel: raw.source_channel,
    receivedAt: '8月1日',
    businessUnit: raw.business_unit,
    businessUnitRef: raw.business_unit_ref,
    category: raw.category,
    categoryCode: raw.category_code,
    amount: 123.45,
    amountMinor: raw.amount_minor,
    accountingMonth: raw.accounting_month,
    summary: raw.summary,
    evidence: raw.evidence,
    confidence: 1,
    status: raw.status,
    blockers: [],
    reviewRisks: [],
    reviewEvents: [],
    incomplete: false,
    conflict: false,
    raw,
    ...overrides,
  }
}

function item(value: Candidate, flowKind: OriginalWorkflowItem['flowKind']): OriginalWorkflowItem {
  return { candidate: value, flowKind, signedAmountMinor: value.amountMinor }
}

describe('original reconciliation workflow state', () => {
  it('reports classification, evidence and review blockers without inventing account matches', () => {
    const pending = candidate({
      id: '10000000-0000-4000-8000-000000000002',
      shortId: 'C-002',
      status: 'PENDING',
      evidence: [],
      raw: {
        ...candidate().raw,
        id: '10000000-0000-4000-8000-000000000002',
        short_id: 'C-002',
        status: 'PENDING',
        evidence: [],
      },
    })
    const summary = buildOriginalWorkflowSummary(
      [item(candidate(), 'income'), item(pending, 'unclassified')],
      originalReconciliationFixture,
    )

    expect(summary).toMatchObject({
      itemCount: 2,
      classifiedCount: 1,
      evidenceLinkedCount: 1,
      pendingReviewCount: 1,
      closeReady: false,
    })
    expect(summary.blockers).toContain('1 笔事项的业务性质待归类')
    expect(summary.blockers).toContain('1 笔事项没有关联凭证')
    expect(summary.blockers).toContain('逐笔事项缺少稳定旧表项目编号与账户引用，不能自动绑定账单账户')
    expect(summary.stages.find((stage) => stage.id === 'source')?.state).toBe('PAUSED')
    expect(summary.stages.find((stage) => stage.id === 'account-match')?.state).toBe('BLOCKED')
  })

  it('does not call a complete projection closed while the account-link and close commands are absent', () => {
    const summary = buildOriginalWorkflowSummary(
      [item(candidate(), 'income')],
      { ...originalReconciliationFixture, is_complete: true, projection_gaps: [], pending_review_count: 0, confirmed_pending_posting_count: 0, missing_material_count: 0, unmapped_confirmed_count: 0 },
    )

    expect(summary.closeReady).toBe(false)
    expect(summary.stages.find((stage) => stage.id === 'close')).toMatchObject({
      state: 'BLOCKED',
      detail: '只展示可验证阻断；正式关账仍需 Core 月结命令',
    })
  })

  it('exports an inspectable CSV review list and preserves exact integer minor units', () => {
    const value = candidate({
      businessUnit: '薇旭,酒店',
      evidence: [{
        id: 'evidence-quoted',
        kind: 'attachment',
        media_type: 'application/pdf',
        sha256: null,
        original_filename: '账单,"复核".pdf',
      }],
    })
    const csv = buildOriginalReviewCsv('2026-08', [item(value, 'income')])

    expect(csv).toContain('"金额（分）"')
    expect(csv).toContain('"12345"')
    expect(csv).toContain('"薇旭,酒店"')
    expect(csv).toContain('"账单,""复核"".pdf"')
    expect(csv).toContain('"待 Core 稳定事项编号与账户引用"')
  })
})
