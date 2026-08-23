import { useEffect, useMemo, useState } from 'react'
import { MagnifyingGlass } from '@phosphor-icons/react'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { JobsList } from '@/components/JobsList'
import { ThemeToggle } from '@/components/theme-toggle'
import { filterJobs } from '@/lib/jobView'
import type { Job } from '@/lib/api'

/** Read-only public dashboard — no backend, no live commands, no secrets.
 * Reads a privacy-trimmed snapshot published alongside the static build
 * (see `data/store.export_public_json` + the deploy workflow). */
function StaticApp() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loaded, setLoaded] = useState(false)
  const [query, setQuery] = useState('')
  const [source, setSource] = useState('all')

  useEffect(() => {
    fetch('/jobs.public.json')
      .then((res) => (res.ok ? res.json() : []))
      .then((data: Job[]) => setJobs(data))
      .finally(() => setLoaded(true))
  }, [])

  const sources = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.source))).sort(),
    [jobs],
  )

  // Same predicate the control panel uses — kept in one shared module
  // (jobView.ts) so the two surfaces never quietly drift apart.
  const filtered = useMemo(() => filterJobs(jobs, query, source), [jobs, query, source])

  // No separate export-time manifest field exists — `updated_at` on each row
  // is the only timestamp available, so the most recent one across the
  // snapshot is the best available proxy for "how fresh is this".
  const lastUpdated = useMemo(() => {
    const stamps = jobs.map((j) => j.updated_at).filter((s): s is string => !!s).sort()
    return stamps.length ? stamps[stamps.length - 1] : null
  }, [jobs])

  return (
    <div className="min-h-screen bg-background">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <span className="text-sm font-semibold tracking-tight">job-search — public dashboard</span>
        <ThemeToggle />
      </header>

      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 pb-16">
        <div className="flex flex-col items-center gap-3 pt-[10vh] text-center">
          {/* Context a stranger arriving from a GitHub Pages link has no way
              to infer otherwise (public-build section of the audit): whose
              jobs these are, how they're ranked, how fresh the data is. */}
          <p className="max-w-lg text-sm text-muted-foreground">
            Job postings scraped and scored for fit against one candidate's résumé — higher
            "match" means closer fit, not necessarily a better job. Read-only: this page has no
            way to apply, skip, or edit anything.
          </p>
          <div className="relative w-full max-w-xl">
            <MagnifyingGlass className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by title, company, or location…"
              aria-label="Filter jobs by title, company, or location"
              className="h-11 pl-9"
            />
          </div>
          <Select value={source} onValueChange={(v) => setSource(v ?? 'all')}>
            <SelectTrigger className="h-8 w-40 text-xs" aria-label="Filter by source">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">all sources</SelectItem>
              {sources.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            {loaded ? `${filtered.length} of ${jobs.length} jobs` : 'Loading…'}
            {lastUpdated && ` · updated ${lastUpdated}`}
          </p>
        </div>

        <JobsList jobs={filtered} readOnly />
      </main>
    </div>
  )
}

export default StaticApp
