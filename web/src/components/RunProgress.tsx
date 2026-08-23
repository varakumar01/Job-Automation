import { progressPercent, type RunProgress as RunProgressState } from '@/lib/runProgress'

// Determinate progress for a running pipeline op (PLAN.md §9 2026-08-23 UI
// audit, finding 9) — replaces the raw stdout tail as the only progress
// signal. `role="status"`/`aria-live` so screen readers hear stage changes
// without needing to poll the log panel.
export function RunProgress({ progress }: { progress: RunProgressState }) {
  const percent = progressPercent(progress)
  return (
    <span role="status" aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
      <span>{progress.stage}</span>
      {progress.current != null && progress.total != null && (
        <span className="font-mono tabular-nums">
          {progress.current}/{progress.total}
        </span>
      )}
      {percent != null && (
        <span className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
          <span className="block h-full rounded-full bg-brand transition-[width]" style={{ width: `${percent}%` }} />
        </span>
      )}
    </span>
  )
}
