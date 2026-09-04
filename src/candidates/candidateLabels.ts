export type CandidateUpdateIntent = 'CONFIRM' | 'IGNORE' | 'RESOLVE_CONFLICT'

export function accountingMonthLabel(month: string | null): string {
  if (!month) return '期间待确认'
  const [year, monthNumber] = month.split('-')
  return `${year} 年 ${Number(monthNumber)} 月`
}
