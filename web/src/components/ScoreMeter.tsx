import { cn } from '@/lib/utils'

// The score is the one number that should drive the scan (PLAN.md §9
// 2026-08-23 UI audit, finding 4) — it gets the brand accent for top-decile
// jobs in whatever list it's rendered in, and a plain neutral bar otherwise.
// `isTopTier` is computed by the caller against the list's own distribution
// (this component has no notion of "top" in isolation).
export function ScoreMeter({ score, isTopTier }: { score: number | null; isTopTier: boolean }) {
  if (score == null) return null
  const pct = Math.max(0, Math.min(100, score))
  return (
    <div className="flex w-16 shrink-0 flex-col items-end gap-1" title={`Match score ${pct.toFixed(1)} / 100`}>
      <span className={cn('font-mono text-sm font-semibold tabular-nums', isTopTier ? 'text-brand' : 'text-foreground')}>
        {pct.toFixed(1)}
      </span>
      <div className="h-1 w-16 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full', isTopTier ? 'bg-brand' : 'bg-foreground/30')}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
