import { useEffect, useRef } from 'react'
import { Terminal } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { RunProgress } from '@/components/RunProgress'
import type { RunProgress as RunProgressState } from '@/lib/runProgress'

interface CmdPanelProps {
  lines: string[]
  running: boolean
  progress: RunProgressState
  onStop: () => void
}

export function CmdPanel({ lines, running, progress, onStop }: CmdPanelProps) {
  const viewportRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Scroll only this panel's own container to its bottom as new lines
    // arrive — NOT `scrollIntoView`, which bubbles up and scrolls the whole
    // page too (surfaced once this panel moved below the fold: mounting it
    // was yanking the entire page down on load to "reveal" an empty panel).
    const el = viewportRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  return (
    <div className="rounded-xl border bg-card">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
        <Terminal className="size-5 text-muted-foreground" weight="bold" />
        <span className="text-base font-medium">Backend commands</span>
        {running && (
          <div className="ml-auto flex items-center gap-3">
            <span className="size-1.5 animate-pulse rounded-full bg-brand" aria-hidden="true" />
            <RunProgress progress={progress} />
            {/* The mechanism this wires to (aborting the SSE fetch) already
                existed (usePipelineRunner's stopRef) — it was just never
                connected to a control (finding 9). A run that used to trap
                the user until reload/kill now has an exit. */}
            <Button size="xs" variant="outline" onClick={onStop}>
              Stop
            </Button>
          </div>
        )}
      </div>
      <div ref={viewportRef} className="h-80 overflow-y-auto" role="log" aria-live="polite">
        <pre className="whitespace-pre-wrap break-words p-4 font-mono text-sm leading-relaxed text-foreground/80">
          {lines.length === 0 ? (
            <span className="text-muted-foreground">
              Nothing running yet — start a search to see live output here.
            </span>
          ) : (
            lines.join('\n')
          )}
        </pre>
      </div>
    </div>
  )
}
