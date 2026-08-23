import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { CmdPanel } from '@/components/CmdPanel'
import { SourceReport } from '@/components/SourceReport'
import { ResultsTiers } from '@/components/ResultsTiers'
import { SearchPanel } from '@/components/SearchPanel'
import { PrepPanel } from '@/components/PrepPanel'
import { RankPanel } from '@/components/RankPanel'
import { ResetMenu } from '@/components/ResetMenu'
import { AdvancedOptions } from '@/components/AdvancedOptions'
import { ThemeToggle } from '@/components/theme-toggle'
import { RUN_TOAST_ID, usePipelineRunner } from '@/lib/usePipelineRunner'
import { useForceApply } from '@/lib/settings'
import { api, streamPrep, type JobLists, type Source, type Stats } from '@/lib/api'

function App() {
  const [sources, setSources] = useState<Source[]>([])
  const [lists, setLists] = useState<JobLists | null>(null)
  const [stats, setStats] = useState<Stats>({})
  const [forceApply, setForceApply] = useForceApply()
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Two different world-states — "backend is down" and "you haven't searched
  // yet" — used to render the same pixel-identical empty page (finding 10).
  // `loadError` distinguishes them so the UI can say what's actually wrong
  // instead of quietly failing. Sources rides in the same Promise.all as
  // lists/stats (not a separate silently-swallowed fetch) so an outage that
  // hits mid-run doesn't leave SourceReport permanently blank after a
  // successful Retry — code-tester defect 2026-08-23, see PLAN.md §9.
  const refreshResults = useCallback(() => {
    Promise.all([api.lists(), api.stats(), api.sources()])
      .then(([l, s, src]) => {
        setLists(l)
        setStats(s)
        setSources(src)
        setLoadError(null)
      })
      .catch((err) => setLoadError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false))
  }, [])

  const onFinished = useCallback(() => {
    refreshResults()
    toast.success('Done', { id: RUN_TOAST_ID })
  }, [refreshResults])

  const { cmdLines, running, progress, run, stop } = usePipelineRunner(onFinished)

  useEffect(() => {
    refreshResults()
  }, [refreshResults])

  // Per-job "tailor this" action on the card (finding 6) — the largest
  // bucket in the app ("needs mod") was presented as uniformly inert, with
  // the only recovery path being a batch run from PrepPanel above. This
  // reuses prep's existing single-job selection mode.
  //
  // `llm: 'auto'` (not 'claude') — this is the button whose tooltip promises
  // "tailors the résumé", but `cmd_prep` short-circuits to a no-op session-mode
  // message under `--llm claude` before it even reads `--jobs`. Hardcoding
  // 'claude' here would make that promise false for the exact tier prep now
  // defaults to (code-reviewer MAJOR, 2026-08-23 — see PLAN.md §9).
  const prepJob = useCallback(
    (jobId: number) => {
      run((onLine, onDone, onError) =>
        streamPrep(
          { llm: 'auto', selection: 'jobs', jobs: String(jobId), modify_resume: true },
          onLine,
          onDone,
          onError,
        ),
      )
    },
    [run],
  )

  return (
    <div className="min-h-screen bg-background">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b px-4 py-4 sm:px-8">
        <span className="text-base font-semibold tracking-tight">job-search</span>
        <div className="flex items-center gap-2 sm:gap-3">
          <ResetMenu onReset={refreshResults} />
          <AdvancedOptions forceApply={forceApply} onForceApplyChange={setForceApply} />
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto flex max-w-5xl flex-col items-center gap-8 px-4 pb-16 sm:px-6">
        {/* Search sits above the vertical middle of the page, not at the very top. */}
        <div className="flex w-full flex-col items-center gap-4 pt-[8vh]">
          <SearchPanel sources={sources} running={running} run={run} />
          <div className="flex flex-wrap justify-center gap-4 text-xs text-muted-foreground">
            {Object.entries(stats).map(([status, count]) => (
              <span key={status}>
                <span className="font-mono font-medium text-foreground">{count}</span> {status}
              </span>
            ))}
          </div>
        </div>

        {loadError && (
          <div className="flex w-full flex-wrap items-center justify-between gap-3 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm">
            <span>
              Can't reach the backend — is <code className="font-mono">dev.sh</code> running? ({loadError})
            </span>
            <Button size="sm" variant="outline" onClick={refreshResults}>
              Retry
            </Button>
          </div>
        )}

        <div className="flex w-full flex-col gap-6">
          <RankPanel running={running} run={run} />
          <PrepPanel running={running} run={run} />
          <CmdPanel lines={cmdLines} running={running} progress={progress} onStop={stop} />
          <SourceReport sources={sources} />
          {loading ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : (
            <ResultsTiers
              lists={lists}
              forceApply={forceApply}
              onChanged={refreshResults}
              onPrepJob={prepJob}
              running={running}
            />
          )}
        </div>
      </main>
    </div>
  )
}

export default App
