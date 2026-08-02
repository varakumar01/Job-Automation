import { useState } from 'react'
import { ArrowSquareOut, Check, DownloadSimple, FileText, SkipForward, X } from '@phosphor-icons/react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { api, type Job, type JobLists } from '@/lib/api'
import { cn } from '@/lib/utils'

// Pipeline status → a badge label/tone. 'matched' gets no badge (it's just
// the undecided queue). 'tailored' is the apply gate — there's no separate
// 'ready' stage (retired 2026-07-11: no direct-apply automation is planned,
// so pre-drafting screening answers had nothing to feed into) — everything
// else prep/apply touches gets a badge so its progress shows next to the job
// instead of the card just going quiet.
const STATUS_BADGE: Record<string, { label: string; variant: 'secondary' | 'outline' | 'default' | 'destructive' }> = {
  tailored: { label: 'ready to apply', variant: 'default' },
  applied: { label: 'applied', variant: 'secondary' },
  skipped: { label: 'skipped', variant: 'outline' },
  failed: { label: 'failed', variant: 'destructive' },
}

// Card tint by outcome status — a quick visual scan across a whole tab
// ("did this go well?") without reading every badge. Everything else
// (matched/tailored) keeps the card's normal, untinted styling.
const CARD_TONE: Record<string, string> = {
  applied: 'border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/30',
  failed: 'border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/30',
  skipped: 'border-border bg-muted/40',
}

export function JobsList({
  jobs,
  tier,
  forceApply = false,
  onChanged,
}: {
  jobs: Job[]
  tier?: keyof JobLists
  forceApply?: boolean
  onChanged?: () => void
}) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        No jobs yet — run a search to populate results.
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-x-8 gap-y-4 md:grid-cols-2">
      {jobs.map((job) => (
        <JobCard key={job.id} job={job} tier={tier} forceApply={forceApply} onChanged={onChanged} />
      ))}
    </div>
  )
}

function JobCard({
  job,
  tier,
  forceApply,
  onChanged,
}: {
  job: Job
  tier?: keyof JobLists
  forceApply: boolean
  onChanged?: () => void
}) {
  const [pending, setPending] = useState<'applied' | 'skipped' | 'failed' | null>(null)
  // An eligible job never needs a real tailoring pass — prep's only move for it
  // is copying the master résumé path verbatim — so it's apply-gate-ready the
  // moment it's classified 'eligible', even while still sitting at 'matched'.
  // Mirrors apply.py's `_apply_gate_ready()` on the backend.
  const isEligibleAsIs = tier === 'eligible' && job.status === 'matched'
  const gateReady = job.status === 'tailored' || isEligibleAsIs
  const badge = gateReady && job.status !== 'tailored' ? STATUS_BADGE.tailored : STATUS_BADGE[job.status]
  const actionable = gateReady || forceApply

  async function logOutcome(outcome: 'applied' | 'skipped' | 'failed') {
    if (!actionable) return
    setPending(outcome)
    try {
      await api.log(job.id, outcome, undefined, !gateReady)
      toast.success(`Job ${job.id} marked ${outcome}`)
      onChanged?.()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    } finally {
      setPending(null)
    }
  }

  return (
    <Card className={cn('p-4', CARD_TONE[job.status])}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-balance">{job.title ?? '(untitled)'}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {job.company ?? '—'} · {job.location ?? '—'} · via {job.source}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {job.role_profile && (
              <Badge variant="secondary" className="shrink-0 text-[10px]">
                {job.role_profile}
              </Badge>
            )}
            {badge && (
              <Badge variant={badge.variant} className="shrink-0 text-[10px]">
                {badge.label}
              </Badge>
            )}
            <ResumeTag job={job} tier={tier} />
          </div>
          {job.outcome && (
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              {job.outcome}
              {job.applied_at ? ` · ${job.applied_at}` : ''}
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <div className="flex items-center gap-2">
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
          {!['applied', 'skipped', 'failed'].includes(job.status) && (
            <div
              className={cn('flex items-center gap-1', !actionable && 'opacity-40')}
              title={
                actionable
                  ? undefined
                  : 'Only enabled once this job is "tailored" (résumé decided — eligible jobs ' +
                    'get this automatically) — or turn on "Force-enable Applied/Skip/Failed on ' +
                    'any job" in Advanced options'
              }
            >
              <Button
                size="xs"
                variant="outline"
                disabled={!actionable || pending !== null}
                onClick={() => logOutcome('applied')}
                title="Record that you reviewed + submitted this at the posting — does not submit anything itself"
              >
                <Check className="size-3" />
                Applied
              </Button>
              <Button
                size="icon-xs"
                variant="ghost"
                disabled={!actionable || pending !== null}
                onClick={() => logOutcome('skipped')}
                aria-label="Skip"
                title="Skip — decided not to apply"
              >
                <SkipForward className="size-3" />
              </Button>
              <Button
                size="icon-xs"
                variant="ghost"
                disabled={!actionable || pending !== null}
                onClick={() => logOutcome('failed')}
                aria-label="Failed"
                title="Failed — application attempt didn't go through"
              >
                <X className="size-3" />
              </Button>
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}

// resume-tailor writes the literal path "varakumar_resume.pdf" for as-is jobs
// (master, untouched) and "tailored/<variant_id>/resume.pdf" for a job that
// actually went through a rewrite pass (needs_mod/stretch always; eligible
// only if prep ran with "Also tailor eligible jobs" checked). That prefix is
// the only signal available for "was this genuinely tailored, or just a copy
// of the master" — surface it as a distinct tag either way.
function ResumeTag({ job, tier }: { job: Job; tier?: keyof JobLists }) {
  if (job.tailored_resume_path) {
    const genuinelyTailored = job.tailored_resume_path.startsWith('tailored/')
    return (
      <a
        href={api.jobResumePdfUrl(job.id)}
        target="_blank"
        rel="noreferrer"
        className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
        title={job.tailored_resume_path}
      >
        <DownloadSimple className="size-3" />
        {genuinelyTailored ? 'résumé tailored' : 'résumé (master, as-is)'}
      </a>
    )
  }
  if (tier === 'eligible') {
    return (
      <a
        href={api.resumePdfUrl()}
        target="_blank"
        rel="noreferrer"
        className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
        title="Uses the master résumé as-is — not yet run through prep"
      >
        <FileText className="size-3" />
        résumé (master)
      </a>
    )
  }
  if (tier === 'needs_mod' || tier === 'stretch') {
    return (
      <span className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground">
        <FileText className="size-3" />
        not tailored yet
      </span>
    )
  }
  return null
}
