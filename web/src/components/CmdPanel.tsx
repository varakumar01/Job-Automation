import { useEffect, useRef } from 'react'
import { Terminal } from '@phosphor-icons/react'

interface CmdPanelProps {
  lines: string[]
  running: boolean
}

export function CmdPanel({ lines, running }: CmdPanelProps) {
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
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <Terminal className="size-5 text-muted-foreground" weight="bold" />
        <span className="text-base font-medium">Backend commands</span>
        {running && (
          <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />
            running
          </span>
        )}
      </div>
      <div ref={viewportRef} className="h-80 overflow-y-auto">
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
