import { ArrowSquareOut } from '@phosphor-icons/react'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import type { Job } from '@/lib/api'

export function JobsList({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No jobs yet — run a search to populate results.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {jobs.map((job) => (
        <Card key={job.id} className="p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h3 className="truncate text-sm font-semibold">{job.title ?? '(untitled)'}</h3>
                {job.role_profile && (
                  <Badge variant="secondary" className="shrink-0 text-[10px]">
                    {job.role_profile}
                  </Badge>
                )}
              </div>
              <p className="truncate text-xs text-muted-foreground">
                {job.company ?? '—'} · {job.location ?? '—'} · via {job.source}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {job.match_score != null && (
                <Badge variant="outline" className="font-mono">
                  {job.match_score.toFixed(1)}
                </Badge>
              )}
              {job.url && (
                <a
                  href={job.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-muted-foreground hover:text-foreground"
                  aria-label="Open posting"
                >
                  <ArrowSquareOut className="size-4" />
                </a>
              )}
            </div>
          </div>
        </Card>
      ))}
    </div>
  )
}
