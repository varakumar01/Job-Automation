import { CheckCircle, WarningCircle } from '@phosphor-icons/react'
import { Badge } from '@/components/ui/badge'
import type { Source } from '@/lib/api'

function StatusIcon({ source }: { source: Source }) {
  if (!source.available) return <WarningCircle className="size-4 text-amber-500" weight="fill" />
  return <CheckCircle className="size-4 text-emerald-500" weight="fill" />
}

export function SourceReport({ sources }: { sources: Source[] }) {
  if (sources.length === 0) return null
  const availableCount = sources.filter((s) => s.available).length

  return (
    <div className="rounded-xl border bg-card p-5">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-base font-medium">
          Sources — {availableCount}/{sources.length} available
        </span>
      </div>
      <div className="grid max-h-56 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">
        {sources.map((s) => (
          <div
            key={s.name}
            className="flex items-center gap-1.5 rounded px-1.5 py-1 text-xs"
            title={s.reason ?? s.base_url ?? undefined}
          >
            <StatusIcon source={s} />
            <span className="truncate font-medium">{s.name}</span>
            {s.mechanism && (
              <Badge variant="outline" className="ml-auto shrink-0 text-[10px]">
                {s.mechanism}
              </Badge>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
