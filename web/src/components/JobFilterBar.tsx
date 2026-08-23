import type { Ref } from 'react'
import { MagnifyingGlass } from '@phosphor-icons/react'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SORTS, type SortKey } from '@/lib/jobView'

// The control strip finding 2 asked for — text filter, sort, source facet —
// plus the dedupe toggle from finding 11. Pure controlled component: all
// state lives in the caller (ResultsTiers) so it survives tab switches and
// the sort/dedupe choices persist via `useJobViewPrefs`.
interface JobFilterBarProps {
  query: string
  onQueryChange: (v: string) => void
  sort: SortKey
  onSortChange: (v: SortKey) => void
  source: string
  onSourceChange: (v: string) => void
  sources: string[]
  dedupe: boolean
  onDedupeChange: (v: boolean) => void
  searchInputRef?: Ref<HTMLInputElement>
}

export function JobFilterBar({
  query,
  onQueryChange,
  sort,
  onSortChange,
  source,
  onSourceChange,
  sources,
  dedupe,
  onDedupeChange,
  searchInputRef,
}: JobFilterBarProps) {
  return (
    <div className="flex w-full flex-wrap items-center gap-2">
      <div className="relative min-w-48 flex-1">
        <MagnifyingGlass className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          ref={searchInputRef}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="Filter by title, company, or location… ( / )"
          aria-label="Filter jobs by title, company, or location"
          className="h-8 pl-8 text-sm"
        />
      </div>
      <Select value={sort} onValueChange={(v) => onSortChange((v ?? 'score-desc') as SortKey)}>
        <SelectTrigger className="h-8 w-44 text-xs" aria-label="Sort jobs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {SORTS.map((s) => (
            <SelectItem key={s.value} value={s.value}>
              {s.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select value={source} onValueChange={(v) => onSourceChange(v ?? 'all')}>
        <SelectTrigger className="h-8 w-36 text-xs" aria-label="Filter by source">
          <SelectValue>{(val: string) => (val === 'all' ? 'all sources' : val)}</SelectValue>
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
      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <input type="checkbox" checked={dedupe} onChange={(e) => onDedupeChange(e.target.checked)} />
        group duplicates
      </label>
    </div>
  )
}
