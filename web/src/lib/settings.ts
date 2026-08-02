import { useCallback, useState } from 'react'

// Persisted UI-only preferences — not backend state, just a local override of
// how the control panel presents itself in this browser.
const FORCE_APPLY_KEY = 'job-search:force-apply-any-status'

/** Whether the Applied/Skip/Failed buttons are clickable on a job that isn't
 * at status 'tailored' (normally they render but stay disabled/dimmed there).
 * Lives in localStorage so it survives a refresh but never touches the
 * backend by itself — it only changes whether `api.log()` is called with
 * `force: true`, which the backend still validates against `--force` before
 * writing anything. */
export function useForceApply(): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(() => localStorage.getItem(FORCE_APPLY_KEY) === '1')
  const set = useCallback((next: boolean) => {
    setValue(next)
    localStorage.setItem(FORCE_APPLY_KEY, next ? '1' : '0')
  }, [])
  return [value, set]
}
