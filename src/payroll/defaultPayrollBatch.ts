import { previousBusinessMonth } from '../shared/monthPolicy'

export function defaultPayrollBatch<T extends { pay_period: string }>(batches: T[], month = previousBusinessMonth()): T | null {
  const matches = batches.filter((batch) => batch.pay_period === month)
  return matches.length === 1 ? matches[0] : null
}
