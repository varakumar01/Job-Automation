import { useEffect, useMemo, useState } from 'react'
import { MagnifyingGlass } from '@phosphor-icons/react'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { JobsList } from '@/components/JobsList'
import { ThemeToggle } from '@/components/theme-toggle'
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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return jobs.filter((j) => {
      if (source !== 'all' && j.source !== source) return false
      if (!q) return true
      return (
        (j.title ?? '').toLowerCase().includes(q) ||
        (j.company ?? '').toLowerCase().includes(q) ||
        (j.location ?? '').toLowerCase().includes(q)
      )
    })
  }, [jobs, query, source])

  return (
    <div className="min-h-screen bg-background">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <span className="text-sm font-semibold tracking-tight">job-search — public dashboard</span>
        <ThemeToggle />
      </header>

      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 pb-16">
        <div className="flex flex-col items-center gap-3 pt-[10vh]">
          <div className="relative w-full max-w-xl">
            <MagnifyingGlass className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by title, company, or location…"
              className="h-11 pl-9"
            />
          </div>
          <Select value={source} onValueChange={(v) => setSource(v ?? 'all')}>
            <SelectTrigger className="h-8 w-40 text-xs">
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
          </p>
        </div>

        <JobsList jobs={filtered} />
      </main>
    </div>
  )
}

export default StaticApp
