import { useEffect, useRef, useState } from 'react'
import { MagnifyingGlass, ArrowClockwise, Trash } from '@phosphor-icons/react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CmdPanel } from '@/components/CmdPanel'
import { SourceReport } from '@/components/SourceReport'
import { JobsList } from '@/components/JobsList'
import { AdvancedOptions } from '@/components/AdvancedOptions'
import { ThemeToggle } from '@/components/theme-toggle'
import { api, streamSearch, type Job, type Source, type Stats } from '@/lib/api'

function App() {
  const [queries, setQueries] = useState('security engineer, detection engineer')
  const [locations, setLocations] = useState('Hyderabad, Bengaluru, India')
  const [days, setDays] = useState(7)
  const [limit, setLimit] = useState(30)
  const [workers, setWorkers] = useState(8)
  const [source, setSource] = useState('all')

  const [sources, setSources] = useState<Source[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [stats, setStats] = useState<Stats>({})
  const [cmdLines, setCmdLines] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const stopRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    api.sources().then(setSources).catch(() => {})
    refreshResults()
    // Abort any in-flight search stream if this component ever unmounts
    // mid-run (no routing today, but this keeps it safe if that changes).
    return () => stopRef.current?.()
  }, [])

  function refreshResults() {
    api.jobs({ status: 'matched', limit: 100 }).then(setJobs).catch(() => {})
    api.stats().then(setStats).catch(() => {})
  }

  function runSearch() {
    if (running) return
    setRunning(true)
    setCmdLines([])
    stopRef.current = streamSearch(
      { queries, locations, days, source, limit, workers },
      (line) => setCmdLines((prev) => [...prev, line]),
      () => {
        setRunning(false)
        stopRef.current = null
        refreshResults()
        api.sources().then(setSources).catch(() => {})
        toast.success('Search complete')
      },
      (err) => {
        setRunning(false)
        stopRef.current = null
        toast.error(`Search failed: ${err}`)
      },
    )
  }

  async function resetStore(hard: boolean) {
    if (!confirm(`Clear the job store${hard ? ' + tailored résumés + apply artifacts' : ''}?`)) {
      return
    }
    await api.reset(hard)
    toast.success('Store reset')
    refreshResults()
  }

  return (
    <div className="min-h-screen bg-background">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <span className="text-sm font-semibold tracking-tight">job-search</span>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => resetStore(false)}>
            <Trash className="size-4" />
            Reset
          </Button>
          <AdvancedOptions />
          <ThemeToggle />
        </div>
      </header>

      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 pb-16">
        {/* Search sits above the vertical middle of the page, not at the very top. */}
        <div className="flex flex-col items-center gap-4 pt-[10vh]">
          <div className="flex w-full max-w-xl items-center gap-2">
            <div className="relative flex-1">
              <MagnifyingGlass className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={queries}
                onChange={(e) => setQueries(e.target.value)}
                placeholder="security engineer, detection engineer, …"
                className="h-11 pl-9"
                onKeyDown={(e) => e.key === 'Enter' && runSearch()}
              />
            </div>
            <Button size="lg" onClick={runSearch} disabled={running}>
              {running ? <ArrowClockwise className="size-4 animate-spin" /> : <MagnifyingGlass className="size-4" />}
              {running ? 'Searching…' : 'Search'}
            </Button>
          </div>

          <div className="flex w-full max-w-xl flex-wrap items-center justify-center gap-2">
            <Input
              value={locations}
              onChange={(e) => setLocations(e.target.value)}
              placeholder="locations"
              className="h-8 w-48 text-xs"
            />
            <Select value={source} onValueChange={(v) => setSource(v ?? 'all')}>
              <SelectTrigger className="h-8 w-36 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">all sources</SelectItem>
                {sources.map((s) => (
                  <SelectItem key={s.name} value={s.name}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <NumberField label="days" value={days} onChange={setDays} />
            <NumberField label="limit" value={limit} onChange={setLimit} />
            <NumberField label="workers" value={workers} onChange={setWorkers} />
          </div>

          <div className="flex gap-4 text-xs text-muted-foreground">
            {Object.entries(stats).map(([status, count]) => (
              <span key={status}>
                <span className="font-mono font-medium text-foreground">{count}</span> {status}
              </span>
            ))}
          </div>
        </div>

        <CmdPanel lines={cmdLines} running={running} />
        <SourceReport sources={sources} />
        <JobsList jobs={jobs} />
      </main>
    </div>
  )
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string
  value: number
  onChange: (v: number) => void
}) {
  return (
    <label className="flex items-center gap-1 text-xs text-muted-foreground">
      {label}
      <Input
        type="number"
        value={value}
        onChange={(e) => onChange(Number(e.target.value) || 0)}
        className="h-8 w-16 text-xs"
      />
    </label>
  )
}

export default App
