import { useState } from 'react'
import { MagnifyingGlass, ArrowClockwise } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { streamSearch, type Source } from '@/lib/api'
import type { StreamFn } from '@/lib/usePipelineRunner'
import * as v from '@/lib/validate'

interface SearchPanelProps {
  sources: Source[]
  running: boolean
  run: (streamFn: StreamFn) => void
}

export function SearchPanel({ sources, running, run }: SearchPanelProps) {
  const [queries, setQueries] = useState('security engineer, detection engineer')
  const [locations, setLocations] = useState('Hyderabad, Bengaluru, India')
  const [days, setDays] = useState(7)
  const [limit, setLimit] = useState(30)
  const [workers, setWorkers] = useState(8)
  const [source, setSource] = useState('all')
  const [touched, setTouched] = useState(false)

  const errors = {
    queries: v.required(queries, 'Queries'),
    days: v.nonNegativeInt(days, 'Days'),
    limit: v.positiveInt(limit, 'Limit'),
    workers: v.positiveInt(workers, 'Workers'),
  }
  const hasErrors = Object.values(errors).some(Boolean)

  function runSearch() {
    setTouched(true)
    if (hasErrors) return
    run((onLine, onDone, onError) =>
      streamSearch({ queries, locations, days, source, limit, workers }, onLine, onDone, onError),
    )
  }

  return (
    <div className="flex w-full flex-col items-center gap-4">
      <div className="flex w-full max-w-3xl items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="relative">
            <MagnifyingGlass className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={queries}
              onChange={(e) => setQueries(e.target.value)}
              placeholder="security engineer, detection engineer, …"
              className="h-14 w-full pl-11 text-base"
              aria-invalid={touched && !!errors.queries}
              onKeyDown={(e) => e.key === 'Enter' && runSearch()}
            />
          </div>
          {touched && errors.queries && <p className="mt-1 text-xs text-destructive">{errors.queries}</p>}
        </div>
        <Button size="lg" className="h-14 shrink-0 px-6 text-base" onClick={runSearch} disabled={running}>
          {running ? <ArrowClockwise className="size-5 animate-spin" /> : <MagnifyingGlass className="size-5" />}
          {running ? 'Running…' : 'Search'}
        </Button>
      </div>

      <div className="flex w-full max-w-3xl flex-wrap items-start justify-center gap-2.5">
        <Input
          value={locations}
          onChange={(e) => setLocations(e.target.value)}
          placeholder="locations (blank = no filter)"
          className="h-9 w-60 text-sm"
        />
        <Select value={source} onValueChange={(val) => setSource(val ?? 'all')}>
          <SelectTrigger className="h-9 w-40 text-sm">
            {/* base-ui's Select.Value shows the raw value by default, not the
                matching item's label ("all" vs "all sources") — map it explicitly. */}
            <SelectValue>{(val: string) => (val === 'all' ? 'all sources' : val)}</SelectValue>
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
        <NumberField label="days" value={days} onChange={setDays} error={touched ? errors.days : null} />
        <NumberField label="limit" value={limit} onChange={setLimit} error={touched ? errors.limit : null} />
        <NumberField
          label="workers"
          title="Number of job sources fetched in parallel (multi-threaded — higher = faster, but more load at once)"
          value={workers}
          onChange={setWorkers}
          error={touched ? errors.workers : null}
        />
      </div>
    </div>
  )
}

export function NumberField({
  label,
  value,
  onChange,
  error,
  title,
  inputClassName,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  error?: string | null
  title?: string
  inputClassName?: string
}) {
  return (
    <label className="flex flex-col gap-0.5 text-xs text-muted-foreground" title={title}>
      <span className="flex items-center gap-1">
        {label}
        <Input
          type="number"
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className={inputClassName ?? 'h-8 w-16 text-xs'}
          aria-invalid={!!error}
        />
      </span>
      {error && <span className="text-destructive">{error}</span>}
    </label>
  )
}
