import type { Candidate, EvidenceReference } from '../types'

export function evidenceLookupReference(candidate: Candidate): string {
  return candidate.summary.match(/\bTX-[0-9]{4,8}\b/)?.[0] ?? candidate.shortId
}

const CONTROLLED_PHOTO_EVIDENCE: Array<{ summary: string; digestPrefix: string }> = [
  { summary: '薇旭美团', digestPrefix: '920f69115b96' },
  { summary: '薇旭携程', digestPrefix: '29f7c422799c' },
  { summary: '景怡美团', digestPrefix: 'd9a2e8132642' },
]

export function evidenceForBillConfirmation(candidate: Candidate): EvidenceReference[] {
  if (candidate.source === '中行账单（复核材料）') {
    const manualReview = candidate.evidence.find((item) => item.original_filename === 'boc-manual-review.xlsx')
    const spreadsheet = candidate.evidence.find((item) => item.media_type.includes('spreadsheet'))
    return manualReview ? [manualReview] : spreadsheet ? [spreadsheet] : []
  }
  if (candidate.source === '照片凭证') {
    const mapping = CONTROLLED_PHOTO_EVIDENCE.find((item) => candidate.summary.includes(item.summary))
    const matchingImage = mapping
      ? candidate.evidence.find((item) => item.sha256?.startsWith(mapping.digestPrefix))
      : undefined
    const firstImage = candidate.evidence.find((item) => item.media_type.startsWith('image/'))
    return matchingImage ? [matchingImage] : firstImage ? [firstImage] : []
  }
  return candidate.evidence.slice(0, 1)
}
