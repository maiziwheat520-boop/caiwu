import type { Candidate } from '../types'

export function SourceIcon({ source }: { source: Candidate['source'] }) {
  const initials: Record<string, string> = {
    Telegram: 'T',
    钉钉: '钉',
    微信: '微',
    支付宝: '支',
    Hermes: 'H',
    '中行账单（复核材料）': '银',
    照片凭证: '照',
    合成数据: '合',
  }
  return <span className={`source-icon source-${source}`}>{initials[source] || '?'}</span>
}
