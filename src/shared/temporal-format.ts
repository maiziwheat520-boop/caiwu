export function formatMonthLabel(value: string) {
  const match = /^(\d{4})-(\d{2})$/.exec(value)
  return match ? `${match[1]} 年 ${Number(match[2])} 月` : value
}
