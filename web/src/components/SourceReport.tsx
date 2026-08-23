import { useEffect, useState } from 'react'
import { CaretDown, WarningCircle } from '@phosphor-icons/react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { Source } from '@/lib/api'

// Uniform repetition destroys detectability (finding 8): 36 identical green
// checks trained the eye to skip the block, and the one real warning
// inherited that skip. Collapsed to a one-line summary by default; only
// auto-expands when something's actually wrong. Color is inverted too —
// nothing gets a green "OK" mark, only the sources that need attention get
// --warning, so the accent is reserved for signal, not noise.
export function SourceReport({ sources }: { sources: Source[] }) {
  const unavailable = sources.filter((s) => !s.available)
  const [expanded, setExpanded] = useState(unavailable.length > 0)

  // `sources` arrives async (empty on first mount), so the `useState`
  // initializer above almost never sees a real failure — auto-open whenever
  // one shows up later, without fighting a manual collapse by re-opening on
  // every render (only fires when the failure count actually changes).
  useEffect(() => {
    if (unavailable.length > 0) setExpanded(true)
  }, [unavailable.length])

  if (sources.length === 0) return null

  return (
    <div className="rounded-xl border bg-card p-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 text-left text-sm"
        aria-expanded={expanded}
      >
        <CaretDown className={cn('size-3.5 text-muted-foreground transition-transform', !expanded && '-rotate-90')} />
        <span className={cn('font-medium', unavailable.length > 0 ? 'text-warning' : 'text-muted-foreground')}>
          {sources.length - unavailable.length}/{sources.length} sources OK
          {unavailable.length > 0 && ` · ${unavailable.length} issue${unavailable.length > 1 ? 's' : ''}`}
        </span>
      </button>
      {expanded && (
        <div className="mt-3 grid max-h-56 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">
          {/* Failures first — the one row worth reading shouldn't be buried
              alphabetically among 36 rows that don't matter. */}
          {[...unavailable, ...sources.filter((s) => s.available)].map((s) => (
            <div
              key={s.name}
              className="flex items-center gap-1.5 rounded px-1.5 py-1 text-xs"
              title={s.reason ?? s.base_url ?? undefined}
            >
              {s.available ? (
                <span className="size-4 shrink-0" aria-hidden="true" />
              ) : (
                <WarningCircle className="size-4 shrink-0 text-warning" weight="fill" />
              )}
              <span className="truncate font-medium">{s.name}</span>
              {s.mechanism && (
                <Badge variant="outline" className="ml-auto shrink-0 text-[10px]">
                  {s.mechanism}
                </Badge>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
