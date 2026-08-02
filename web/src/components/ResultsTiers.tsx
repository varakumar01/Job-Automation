import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JobsList } from '@/components/JobsList'
import type { JobLists } from '@/lib/api'

export function ResultsTiers({
  lists,
  forceApply,
  onChanged,
}: {
  lists: JobLists | null
  forceApply: boolean
  onChanged?: () => void
}) {
  if (!lists) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No jobs yet — run a search to populate results.
      </div>
    )
  }

  const tabs: Array<{ value: keyof JobLists; label: string }> = [
    { value: 'eligible', label: `✅ Eligible (${lists.eligible.length})` },
    { value: 'needs_mod', label: `✏️ Needs mod (${lists.needs_mod.length})` },
    { value: 'stretch', label: `🧗 Stretch (${lists.stretch.length})` },
  ]

  return (
    <Tabs defaultValue="eligible">
      <TabsList>
        {tabs.map((t) => (
          <TabsTrigger key={t.value} value={t.value}>
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {tabs.map((t) => (
        <TabsContent key={t.value} value={t.value}>
          <JobsList jobs={lists[t.value]} tier={t.value} forceApply={forceApply} onChanged={onChanged} />
        </TabsContent>
      ))}
    </Tabs>
  )
}
