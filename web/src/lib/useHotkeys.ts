import { useEffect, useRef } from 'react'

interface HotkeyOptions {
  onFocusSearch: () => void
  onSelectTab: (index: number) => void
  onClear: () => void
}

/** Keyboard shortcuts for the results view: `/` focuses the filter box, `1`/
 * `2`/`3` switch tiers, `Esc` clears the filter. `/` and the digit keys are
 * suppressed while focus is already inside a text field (so typing a job id
 * doesn't jump tabs); `Esc` is the one exception, since clearing the filter
 * from inside the filter box itself is the whole point of that binding. */
export function useHotkeys({ onFocusSearch, onSelectTab, onClear }: HotkeyOptions) {
  // Callers (e.g. ResultsTiers) pass fresh closures every render — stash the
  // latest in a ref rather than the effect's dep array, so the listener
  // isn't torn down and reattached on every keystroke in the filter box
  // (code-reviewer NIT, 2026-08-23).
  const optsRef = useRef({ onFocusSearch, onSelectTab, onClear })
  useEffect(() => {
    optsRef.current = { onFocusSearch, onSelectTab, onClear }
  })

  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'Escape') {
        optsRef.current.onClear()
        return
      }
      const target = e.target as HTMLElement | null
      const inField = !!target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      if (inField) return
      if (e.key === '/') {
        e.preventDefault()
        optsRef.current.onFocusSearch()
      } else if (e.key === '1' || e.key === '2' || e.key === '3') {
        optsRef.current.onSelectTab(Number(e.key) - 1)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])
}
