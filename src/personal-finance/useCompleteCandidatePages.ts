import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'
import type { ApiCandidate } from '../types'

const MAX_CANDIDATE_PAGES = 100

export async function loadCompleteCandidatePages(): Promise<ApiCandidate[]> {
  const candidates: ApiCandidate[] = []
  const seenCandidateIds = new Set<string>()
  const seenCursors = new Set<string>()
  let cursor: string | undefined

  for (let pageNumber = 0; pageNumber < MAX_CANDIDATE_PAGES; pageNumber += 1) {
    const page = await api.listCandidates({ cursor })
    for (const candidate of page.items) {
      if (seenCandidateIds.has(candidate.id)) {
        throw new Error('候选分页包含重复记录，已停止生成不可靠的个人财务汇总')
      }
      seenCandidateIds.add(candidate.id)
      candidates.push(candidate)
    }

    if (!page.next_cursor) return candidates
    if (seenCursors.has(page.next_cursor)) {
      throw new Error('候选分页游标重复，已停止生成不完整的个人财务汇总')
    }
    seenCursors.add(page.next_cursor)
    cursor = page.next_cursor
  }

  throw new Error('候选分页超过安全上限，已停止生成不完整的个人财务汇总')
}

export function useCompleteCandidatePages() {
  const [candidates, setCandidates] = useState<ApiCandidate[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setCandidates(await loadCompleteCandidatePages())
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '无法完整读取个人财务候选')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    void loadCompleteCandidatePages()
      .then((loadedCandidates) => {
        if (active) setCandidates(loadedCandidates)
      })
      .catch((loadError: unknown) => {
        if (active) setError(loadError instanceof Error ? loadError.message : '无法完整读取个人财务候选')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  return { candidates, loading, error, reload }
}
