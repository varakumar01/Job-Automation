import { useEffect, useRef } from 'react'
import { Terminal } from '@phosphor-icons/react'
import { ScrollArea } from '@/components/ui/scroll-area'

interface CmdPanelProps {
  lines: string[]
  running: boolean
}

export function CmdPanel({ lines, running }: CmdPanelProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [lines])

  return (
    <div className="rounded-lg border bg-card">
      <div className="flex items-center gap-2 border-b px-3 py-2">
        <Terminal className="size-4 text-muted-foreground" weight="bold" />
        <span className="text-sm font-medium">Backend commands</span>
        {running && (
          <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />
            running
          </span>
        )}
      </div>
      <ScrollArea className="h-64">
        <pre className="whitespace-pre-wrap break-words p-3 font-mono text-xs leading-relaxed text-foreground/80">
          {lines.length === 0 ? (
            <span className="text-muted-foreground">
              Nothing running yet — start a search to see live output here.
            </span>
          ) : (
            lines.join('\n')
          )}
          <div ref={bottomRef} />
        </pre>
      </ScrollArea>
    </div>
  )
}
