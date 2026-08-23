import { useCallback, useEffect, useState } from 'react'
import { api, type LlmProviders } from './api'

// Module-scoped so every mounted <LlmSelect> (Search/Rank/Prep — 3 as of
// 2026-08-23) shares ONE in-flight request instead of each firing its own.
// Without this, 3 concurrent GET /api/llm-providers on a cold/stale cache each
// ran execution/llm_health.pick_provider() in its own thread, and _probe()
// patches the *process-wide* os.environ for the duration of its network call —
// concurrent probes under different providers interleaved and could send one
// provider's API key to another provider's base URL (code-reviewer MAJOR,
// 2026-08-23). Deduping the request client-side is the fix here; a
// threading.Lock in execution/llm_health.py is the server-side belt-and-braces.
let sharedData: LlmProviders | null = null
let inFlight: Promise<LlmProviders> | null = null
const listeners = new Set<(data: LlmProviders) => void>()

function fetchShared(force: boolean): Promise<LlmProviders> {
  if (inFlight) return inFlight
  const p = api
    .llmProviders(force)
    .then((data) => {
      sharedData = data
      listeners.forEach((fn) => fn(data))
      return data
    })
    .finally(() => {
      inFlight = null
    })
  inFlight = p
  return p
}

/** Live LLM provider health (nvidia/grok/deepseek/api) for the Search/Rank/Prep
 * dropdowns: probes once across all mounted pickers (server-side cached ~10
 * min — see execution/llm_health.py's TTL_SECS), plus an on-demand
 * `refresh(force)` that updates every mounted instance, not just the caller's.
 * Fetch failure is non-fatal: callers fall back to their static option list. */
export function useLlmProviders() {
  const [data, setData] = useState<LlmProviders | null>(sharedData)
  const [loading, setLoading] = useState(false)

  const refresh = useCallback((force = false) => {
    setLoading(true)
    fetchShared(force)
      .then(setData)
      .catch(() => {
        /* non-fatal — panels fall back to the static option list */
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    listeners.add(setData)
    return () => {
      listeners.delete(setData)
    }
  }, [])

  useEffect(() => {
    if (!sharedData) refresh(false)
  }, [refresh])

  return { data, loading, refresh }
}
