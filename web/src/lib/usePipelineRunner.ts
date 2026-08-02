import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import type { Stats } from './api'

// One stable id so the "running" toast is replaced in place by the eventual
// success/error toast instead of stacking a second popup — a floating side
// notification, not a page reload, is the whole point of surfacing progress
// this way instead of requiring the user to scroll down to the CMD panel.
export const RUN_TOAST_ID = 'pipeline-run'

export type StreamFn = (
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
) => () => void

// Bounds memory/render cost on a very chatty run (e.g. `rank --llm python`
// prints one line per job — 200+ for a large store) — keep the tail, it's
// what you'd scroll to anyway.
const MAX_LINES = 2000

/** Shared state for the one CMD panel driving search/prep/rank — the backend
 * only allows one pipeline operation at a time (see server/app.py's
 * _pipeline_lock), so the frontend mirrors that with one `running` flag and
 * one line buffer regardless of which panel triggered the run. */
export function usePipelineRunner(onFinished?: (stats: Stats) => void) {
  const [cmdLines, setCmdLines] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const stopRef = useRef<(() => void) | null>(null)

  // Incoming SSE lines can arrive many-per-millisecond (a chatty run streams
  // hundreds of lines almost instantly). Committing each one straight to
  // React state was found live to make the browser janky for 20+ seconds on
  // ~220 lines — one state update + one full re-render per line. Buffer them
  // in a ref and flush at most once per animation frame instead.
  const pendingRef = useRef<string[]>([])
  const flushScheduledRef = useRef(false)

  const flush = useCallback(() => {
    flushScheduledRef.current = false
    if (pendingRef.current.length === 0) return
    const incoming = pendingRef.current
    pendingRef.current = []
    setCmdLines((prev) => {
      const next = prev.length ? prev.concat(incoming) : incoming
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next
    })
  }, [])

  const enqueueLine = useCallback(
    (line: string) => {
      pendingRef.current.push(line)
      if (!flushScheduledRef.current) {
        flushScheduledRef.current = true
        // setTimeout, not requestAnimationFrame — rAF is tied to paint
        // scheduling and gets heavily throttled for backgrounded/unfocused
        // tabs (found live: a run that completes in ~190ms server-side took
        // 15-20s to reflect in the UI under headless/backgrounded testing,
        // purely from rAF callbacks being deferred). A plain timer isn't
        // subject to that.
        setTimeout(flush, 16)
      }
    },
    [flush],
  )

  useEffect(() => () => stopRef.current?.(), [])

  const run = useCallback(
    (streamFn: StreamFn) => {
      if (running) return
      setRunning(true)
      pendingRef.current = []
      setCmdLines([])
      toast.loading('Running…', { id: RUN_TOAST_ID, duration: Infinity })
      stopRef.current = streamFn(
        enqueueLine,
        (stats) => {
          setRunning(false)
          stopRef.current = null
          onFinished?.(stats)
        },
        (err) => {
          setRunning(false)
          stopRef.current = null
          toast.error(err, { id: RUN_TOAST_ID })
          enqueueLine(`⚠ ${err}`)
        },
      )
    },
    [running, onFinished, enqueueLine],
  )

  return { cmdLines, running, run }
}
