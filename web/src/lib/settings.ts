import { useCallback, useEffect, useRef, useState } from 'react'
import type { SortKey } from './jobView'

// Persisted UI-only preferences — not backend state, just a local override of
// how the control panel presents itself in this browser.
const FORCE_APPLY_KEY = 'job-search:force-apply-any-status'
const JOB_SORT_KEY = 'job-search:sort'
const JOB_DEDUPE_KEY = 'job-search:dedupe'

/** Generic localStorage-backed state. Lazy-init reads storage once; every
 * `set` writes straight through. `serialize`/`deserialize` default to
 * identity for strings and JSON for everything else via the overloads below. */
function usePersistedState<T>(key: string, initial: T, serialize: (v: T) => string, deserialize: (raw: string) => T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    const raw = localStorage.getItem(key)
    if (raw === null) return initial
    try {
      return deserialize(raw)
    } catch {
      return initial
    }
  })
  // `serialize` is a cheap inline closure recreated per render at each call
  // site below, but its behavior never varies (same key -> same transform
  // every time). Stash it in a ref rather than the dep array so `set`'s
  // identity only changes when `key` does, not on every render — and so the
  // callback always calls the *latest* serialize without lying to the
  // exhaustive-deps rule about it. Assigned in an effect, not inline during
  // render, so an interrupted/discarded render (React 19 concurrent features)
  // can never leave the ref pointing at a closure from a render that didn't
  // commit (code-reviewer finding, 2026-08-23).
  const serializeRef = useRef(serialize)
  useEffect(() => {
    serializeRef.current = serialize
  })
  const set = useCallback((next: T) => {
    setValue(next)
    localStorage.setItem(key, serializeRef.current(next))
  }, [key])
  return [value, set]
}

/** Whether the Applied/Skip/Failed buttons are clickable on a job that isn't
 * at status 'tailored' (normally they render but stay disabled/dimmed there).
 * Lives in localStorage so it survives a refresh but never touches the
 * backend by itself — it only changes whether `api.log()` is called with
 * `force: true`, which the backend still validates against `--force` before
 * writing anything. */
export function useForceApply(): [boolean, (value: boolean) => void] {
  return usePersistedState(
    FORCE_APPLY_KEY,
    false,
    (v) => (v ? '1' : '0'),
    (raw) => raw === '1',
  )
}

/** Sort + dedupe preferences for the job list (ResultsTiers). Persisted
 * because re-choosing "best match first" every page load is pure friction —
 * see PLAN.md §9 2026-08-23. The free-text filter query is deliberately
 * NOT persisted here: a stale filter silently hiding hundreds of jobs on next
 * load is a bug shaped like a feature. */
export function useJobViewPrefs() {
  const [sort, setSort] = usePersistedState<SortKey>(
    JOB_SORT_KEY,
    'score-desc',
    (v) => v,
    (raw) => raw as SortKey,
  )
  const [dedupe, setDedupe] = usePersistedState(
    JOB_DEDUPE_KEY,
    true,
    (v) => (v ? '1' : '0'),
    (raw) => raw === '1',
  )
  return { sort, setSort, dedupe, setDedupe }
}
