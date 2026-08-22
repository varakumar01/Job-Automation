import { useCallback, useEffect, useState } from 'react'
import { api, type LlmProviders } from './api'

/** Live LLM provider health (nvidia/grok/deepseek/api) for the Rank/Prep panels'
 * dropdown: probes once on mount (server-side cached ~10 min — see
 * execution/llm_health.py's TTL_SECS), plus an on-demand `refresh(force)`.
 * Fetch failure is non-fatal: callers fall back to their static option list. */
export function useLlmProviders() {
  const [data, setData] = useState<LlmProviders | null>(null)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback((force = false) => {
    setLoading(true)
    api
      .llmProviders(force)
      .then(setData)
      .catch(() => {
        /* non-fatal — panels fall back to the static option list */
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh(false)
  }, [refresh])

  return { data, loading, refresh }
}
