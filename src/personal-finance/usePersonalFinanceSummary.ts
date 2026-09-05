import { useCallback, useEffect, useState } from 'react'

import { api } from '../api'
import type { PersonalFinanceSummary } from '../types'

/**
 * Read the summary Core builds.
 *
 * The page used to page the whole candidate collection into the browser and
 * derive these numbers here. Core owns those rules now, so one read replaces
 * nineteen and the browser never sees the collection.
 */
export function usePersonalFinanceSummary() {
  const [summary, setSummary] = useState<PersonalFinanceSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const read = useCallback(async (): Promise<PersonalFinanceSummary | null> => {
    setLoading(true)
    setError(null)
    try {
      const next = await api.getPersonalFinanceSummary()
      setSummary(next)
      return next
    } catch (readError) {
      setError(readError instanceof Error ? readError.message : '无法读取个人财务汇总')
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    void api
      .getPersonalFinanceSummary()
      .then((next) => {
        if (active) setSummary(next)
      })
      .catch((readError: unknown) => {
        if (active) {
          setError(readError instanceof Error ? readError.message : '无法读取个人财务汇总')
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [])

  return { summary, loading, error, reload: read }
}
