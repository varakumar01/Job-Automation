import { memo, useMemo, useRef, useState, useTransition } from 'react'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JobFilterBar } from '@/components/JobFilterBar'
import { JobsList } from '@/components/JobsList'
import { dedupeJobs, filterJobs, sortJobs } from '@/lib/jobView'
import { useJobViewPrefs } from '@/lib/settings'
import { useHotkeys } from '@/lib/useHotkeys'
import type { JobLists } from '@/lib/api'

// Plain labels, not emoji (finding 12) — the count is the useful part, and
// emoji render inconsistently per-OS on a page that's meant to be shared.
// 'unarranged' sits LAST (2026-08-24, owner: "keep the unarranged in the
// last" — revises the 2026-08-23 "sits first" placement). It's no longer the
// default landing tab either (owner: "Eligible should be default selected",
// same day) — its heading instead turns red while jobs are still waiting, so
// it stays impossible to miss without having to be the first thing you see.
const TIERS: Array<{ value: keyof JobLists; label: string }> = [
  { value: 'eligible', label: 'Eligible' },
  { value: 'needs_mod', label: 'Needs mod' },
  { value: 'stretch', label: 'Stretch' },
  { value: 'unarranged', label: 'Unarranged' },
]

const EMPTY_LISTS: JobLists = { unarranged: [], eligible: [], needs_mod: [], stretch: [], off_profile: [] }

export const ResultsTiers = memo(function ResultsTiers({
  lists,
  forceApply,
  onChanged,
  onPrepJob,
  running = false,
}: {
  lists: JobLists | null
  forceApply: boolean
  onChanged?: () => void
  /** Per-job "tailor this" action (finding 6) — threaded down to JobsList. */
  onPrepJob?: (jobId: number) => void
  running?: boolean
}) {
  const [tab, setTab] = useState<keyof JobLists>('eligible')
  // A first-time switch into the 342-card "Needs mod" tier measured
  // 300-445ms live (code-tester, 2026-08-23) — borderline against the
  // ~400ms target with no in-between visual acknowledgment. `useTransition`
  // doesn't shrink that work, but it keeps the click itself un-blocked (the
  // tab-trigger's `aria-selected` flips and `isPending` goes true well under
  // 100ms) and gives an explicit "switching…" affordance for the gap instead
  // of a frozen-looking pause — the actual failure mode finding 3 named.
  const [isPending, startTransition] = useTransition()
  // Free-text filter is deliberately ephemeral (not in useJobViewPrefs) — a
  // stale filter silently hiding jobs on next load is a bug shaped like a
  // feature (see settings.ts). Sort + dedupe persist; source facet resets
  // per session since which sources exist can change between runs.
  const [query, setQuery] = useState('')
  const [source, setSource] = useState('all')
  const { sort, setSort, dedupe, setDedupe } = useJobViewPrefs()
  const searchRef = useRef<HTMLInputElement>(null)

  useHotkeys({
    onFocusSearch: () => searchRef.current?.focus(),
    onSelectTab: (i) => TIERS[i] && startTransition(() => setTab(TIERS[i].value)),
    onClear: () => setQuery(''),
  })

  const safeLists = lists ?? EMPTY_LISTS

  const sources = useMemo(() => {
    const all = [...safeLists.unarranged, ...safeLists.eligible, ...safeLists.needs_mod, ...safeLists.stretch]
    return Array.from(new Set(all.map((j) => j.source))).sort()
  }, [safeLists])

  // Filter + sort once per tier per render, shared by the tab count badge and
  // the tab's JobsList — so the count on the tab always matches what's
  // actually rendered underneath it (finding 2 / finding 11).
  const tierViews = useMemo(() => {
    return TIERS.map((t) => {
      const filtered = filterJobs(safeLists[t.value], query, source)
      const sorted = sortJobs(filtered, sort)
      const count = dedupe ? dedupeJobs(filtered).length : filtered.length
      return { ...t, jobs: sorted, count }
    })
  }, [safeLists, query, source, sort, dedupe])

  if (!lists) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No jobs yet — run a search to populate results.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <JobFilterBar
        query={query}
        onQueryChange={setQuery}
        sort={sort}
        onSortChange={setSort}
        source={source}
        onSourceChange={setSource}
        sources={sources}
        dedupe={dedupe}
        onDedupeChange={setDedupe}
        searchInputRef={searchRef}
      />
      <Tabs
        value={tab}
        onValueChange={(v) => v && startTransition(() => setTab(v as keyof JobLists))}
      >
        <div className="flex items-center gap-2">
          <TabsList>
            {tierViews.map((t) => {
              // Unarranged is "unresolved" — i.e. still has jobs waiting to be
              // sorted — until Arrange clears it to 0 (owner request
              // 2026-08-24: "always show it in red until resolved"). The
              // color lives on an inner span, not the trigger's own classes,
              // so it overrides the trigger's active/hover text-color
              // utilities instead of losing to them.
              const unresolved = t.value === 'unarranged' && t.count > 0
              return (
                <TabsTrigger key={t.value} value={t.value}>
                  <span className={unresolved ? 'text-destructive' : undefined}>{t.label}</span>
                  <Badge variant="secondary" className="ml-1 text-[10px]">
                    {t.count}
                  </Badge>
                </TabsTrigger>
              )
            })}
          </TabsList>
          {/* Sibling of TabsList, not a child — a role="tablist" (what Base UI's
              TabsList renders as) may only contain role="tab" elements per the
              ARIA spec; a nested role="status" is invalid there
              (code-reviewer MINOR, 2026-08-23). */}
          {isPending && (
            <span role="status" className="text-[11px] text-muted-foreground">
              switching…
            </span>
          )}
        </div>
        {tierViews.map((t) => (
          <TabsContent key={t.value} value={t.value}>
            <JobsList
              jobs={t.jobs}
              tier={t.value}
              forceApply={forceApply}
              dedupe={dedupe}
              onChanged={onChanged}
              onPrepJob={onPrepJob}
              running={running}
            />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
})
