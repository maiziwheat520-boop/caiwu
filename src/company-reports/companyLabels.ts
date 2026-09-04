const COMPANY_LEGAL_NAME_SUFFIXES = [
  '商务公寓管理有限公司',
  '公寓管理有限公司',
  '酒店公寓有限公司',
  '商务公寓有限公司',
  '酒店有限公司',
] as const

export function companyTabLabel(companyName: string) {
  const normalized = companyName.trim()
  const localName = normalized.startsWith('深圳市') ? normalized.slice(3) : normalized
  const brandName = localName.startsWith('创业') ? localName.slice(2) : localName
  const suffix = COMPANY_LEGAL_NAME_SUFFIXES.find((candidate) => brandName.endsWith(candidate))
  const shortName = suffix ? brandName.slice(0, -suffix.length) : ''
  return shortName || normalized
}
