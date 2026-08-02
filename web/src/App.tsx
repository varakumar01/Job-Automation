import { useEffect, useState } from 'react'
import { toast } from 'sonner'
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
import { api, type JobLists, type Source, type Stats } from '@/lib/api'

function App() {
  const [sources, setSources] = useState<Source[]>([])
  const [lists, setLists] = useState<JobLists | null>(null)
  const [stats, setStats] = useState<Stats>({})
  const [forceApply, setForceApply] = useForceApply()

  const { cmdLines, running, run } = usePipelineRunner(() => {
    refreshResults()
    api.sources().then(setSources).catch(() => {})
    toast.success('Done', { id: RUN_TOAST_ID })
  })

  useEffect(() => {
    api.sources().then(setSources).catch(() => {})
    refreshResults()
  }, [])

  function refreshResults() {
    api.lists().then(setLists).catch(() => {})
    api.stats().then(setStats).catch(() => {})
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="flex items-center justify-between border-b px-8 py-4">
        <span className="text-base font-semibold tracking-tight">job-search</span>
        <div className="flex items-center gap-3">
          <ResetMenu onReset={refreshResults} />
          <AdvancedOptions forceApply={forceApply} onForceApplyChange={setForceApply} />
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto flex max-w-5xl flex-col items-center gap-8 px-6 pb-16">
        {/* Search sits above the vertical middle of the page, not at the very top. */}
        <div className="flex w-full flex-col items-center gap-4 pt-[8vh]">
          <SearchPanel sources={sources} running={running} run={run} />
          <div className="flex gap-4 text-xs text-muted-foreground">
            {Object.entries(stats).map(([status, count]) => (
              <span key={status}>
                <span className="font-mono font-medium text-foreground">{count}</span> {status}
              </span>
            ))}
          </div>
        </div>

        <div className="flex w-full flex-col gap-6">
          <RankPanel running={running} run={run} />
          <PrepPanel running={running} run={run} />
          <CmdPanel lines={cmdLines} running={running} />
          <SourceReport sources={sources} />
          <ResultsTiers lists={lists} forceApply={forceApply} onChanged={refreshResults} />
        </div>
      </main>
    </div>
  )
}

export default App
