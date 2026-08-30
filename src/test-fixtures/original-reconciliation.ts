import type { OriginalReconciliation } from '../types'

const columns: OriginalReconciliation['columns'] = Array.from({ length: 13 }, (_, offset) => ({
  column: String.fromCharCode('A'.charCodeAt(0) + offset),
  ordinal: offset + 1,
  role: offset === 5 || offset === 6 ? 'SPACER' : offset <= 4 ? 'MAIN' : 'DETAIL',
}))

const rows: OriginalReconciliation['rows'] = Array.from({ length: 40 }, (_, rowOffset) => {
  const rowNumber = rowOffset + 1
  return {
    row_number: rowNumber,
    cells: columns.map(({ column }) => {
      const base = {
        coordinate: `${column}${rowNumber}`,
        column,
        row_number: rowNumber,
        kind: 'BLANK' as const,
        label: null,
        amount_minor: null,
        currency: null,
        gap_code: null,
        source_fact_refs: [],
      }
      if (rowNumber === 1 && column === 'A') {
        return { ...base, kind: 'LABEL' as const, label: '示例科目' }
      }
      if (rowNumber === 2 && column === 'H') {
        return {
          ...base,
          kind: 'AMOUNT' as const,
          amount_minor: 12345,
          currency: 'CNY' as const,
          source_fact_refs: ['fact-confirmed-1'],
        }
      }
      if (rowNumber === 2 && column === 'I') {
        return {
          ...base,
          kind: 'AMOUNT' as const,
          amount_minor: -2345,
          currency: 'CNY' as const,
          source_fact_refs: ['fact-posted-1'],
        }
      }
      if (rowNumber === 2 && column === 'J') {
        return {
          ...base,
          kind: 'GAP' as const,
          label: null,
          gap_code: 'MISSING_ECONOMIC_EFFECT' as const,
        }
      }
      if (rowNumber === 2 && column === 'K') {
        return {
          ...base,
          kind: 'AMOUNT' as const,
          amount_minor: 10000,
          currency: 'CNY' as const,
        }
      }
      return base
    }),
  }
})

export const originalReconciliationFixture: OriginalReconciliation = {
  contract_version: 'ledgerbridge.original-reconciliation.v1',
  taxonomy_version: 'ledgerbridge.financial-foundation-blocker-taxonomy.v1',
  layout_version: 'ledgerbridge.original-reconciliation-layout.v1',
  mapping_version: 'ledgerbridge.original-reconciliation-mapping.v1',
  is_complete: false,
  posted_ledger_complete: true,
  projection_gaps: ['MISSING_TIME_GRANULARITY'],
  month: '2026-08',
  scope: {
    entity_ref: '10000000-0000-4000-8000-000000000001',
    business_unit_ref: 'unit-demo-a',
  },
  columns,
  rows,
  totals: {
    posted_income_minor: 12345,
    posted_expense_minor: 2345,
    posted_profit_minor: 10000,
    opening_balance_minor: null,
    closing_balance_minor: null,
    mapped_cell_count: 2,
    confirmed_candidate_amount_minor: 12345,
    posted_amount_minor: -2345,
    currency: 'CNY',
  },
  pending_review_count: 3,
  confirmed_pending_posting_count: 2,
  missing_material_count: 1,
  unmapped_confirmed_count: 1,
  sources: [
    {
      source_kind: 'CONFIRMED_CANDIDATE',
      source_system: 'synthetic_confirmed',
      source_label: '已确认候选（脱敏）',
      fact_count: 2,
      mapped_fact_count: 1,
      amount_minor: 12345,
    },
    {
      source_kind: 'POSTED_LEDGER',
      source_system: 'synthetic_posted',
      source_label: '正式账簿（脱敏）',
      fact_count: 1,
      mapped_fact_count: 1,
      amount_minor: -2345,
    },
    {
      source_kind: 'ACCOUNT_STATEMENT',
      source_system: 'synthetic_statement',
      source_label: null,
      fact_count: 1,
      mapped_fact_count: 0,
      amount_minor: 0,
    },
  ],
}
