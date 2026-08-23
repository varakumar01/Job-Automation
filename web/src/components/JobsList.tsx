import { memo, useMemo, useState } from 'react'
import { ArrowSquareOut, Check, DownloadSimple, FileText, SkipForward, Sparkle, WarningOctagon } from '@phosphor-icons/react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ScoreMeter } from '@/components/ScoreMeter'
import { api, type Job, type JobLists } from '@/lib/api'
import { dedupeJobs, formatAge, isStale, type JobGroup } from '@/lib/jobView'
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
// (matched/tailored) keeps the card's normal, untinted styling. Routed
// through --success/--warning instead of hardcoded emerald/red (PLAN.md §9
// 2026-08-23 UI audit) — these are outcome-only encodings, not general color.
const CARD_TONE: Record<string, string> = {
  applied: 'border-success/40 bg-success/5',
  failed: 'border-destructive/40 bg-destructive/5',
  skipped: 'border-border bg-muted/40',
}

// An eligible job never needs a real tailoring pass — prep's only move for it
// is copying the master résumé path verbatim — so it's apply-gate-ready the
// moment it's classified 'eligible', even while still sitting at 'matched'.
// Mirrors apply.py's `_apply_gate_ready()` on the backend.
function computeGate(job: Job, tier?: keyof JobLists) {
  const isEligibleAsIs = tier === 'eligible' && job.status === 'matched'
  const gateReady = job.status === 'tailored' || isEligibleAsIs
  const badge = gateReady && job.status !== 'tailored' ? STATUS_BADGE.tailored : STATUS_BADGE[job.status]
  return { gateReady, badge }
}

// A tier switch that renders every card at once was measured live at 1.4s /
// 342 cards / 9,237 DOM nodes (finding 3) — cap the initial render and let
// `content-visibility: auto` skip layout/paint for offscreen cards instead of
// pulling in a virtualization library (which would cost a dep and native
// in-page Ctrl+F).
const PAGE_SIZE = 40

function JobsListImpl({
  jobs,
  tier,
  forceApply = false,
  dedupe = true,
  readOnly = false,
  onChanged,
  onPrepJob,
  running = false,
}: {
  jobs: Job[]
  tier?: keyof JobLists
  forceApply?: boolean
  dedupe?: boolean
  /** Public dashboard: no backend, no secrets — suppresses the action
   * cluster and résumé tags entirely instead of rendering them disabled with
   * an instruction ("enable Advanced options") that page doesn't have. */
  readOnly?: boolean
  onChanged?: () => void
  /** Per-card "tailor this job" action (finding 6) — omitted entirely when
   * not provided, e.g. on the public build. */
  onPrepJob?: (jobId: number) => void
  running?: boolean
}) {
  const [visible, setVisible] = useState(PAGE_SIZE)

  const groups = useMemo(() => (dedupe ? dedupeJobs(jobs) : jobs.map((job) => ({ job, duplicates: [] }))), [jobs, dedupe])

  // "ready to apply" on 100% of cards in a tab carries zero information
  // (finding 4) — suppress the badge in that case rather than repeat it on
  // every single card.
  const allReady = useMemo(
    () => groups.length > 0 && groups.every(({ job }) => computeGate(job, tier).badge?.label === 'ready to apply'),
    [groups, tier],
  )

  // Accent hue is reserved for genuinely top-decile fit within *this* list
  // (finding 4) — computed against the list's own score distribution, not an
  // absolute constant that would mean something different in "Stretch" vs
  // "Eligible".
  const topTierThreshold = useMemo(() => {
    const scores = groups.map(({ job }) => job.match_score).filter((s): s is number => s != null).sort((a, b) => b - a)
    if (scores.length === 0) return Infinity
    const threshold = scores[Math.max(0, Math.floor(scores.length * 0.1) - 1)]
    // When scores are bunched (or identical), the 90th-percentile cut can
    // land on more than half the list — an accent applied to most cards
    // encodes nothing, same failure mode as the "ready to apply" badge this
    // was meant to fix. Suppress it rather than let it go uniform.
    const aboveCount = scores.filter((s) => s >= threshold).length
    return aboveCount > scores.length / 2 ? Infinity : threshold
  }, [groups])

  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        {tier === 'unarranged'
          ? "Nothing waiting to be arranged — run Search to find more, then Arrange to sort them."
          : 'No jobs yet — run a search to populate results.'}
      </div>
    )
  }

  const shown = groups.slice(0, visible)

  return (
    <div>
      <div className="grid grid-cols-1 gap-x-8 gap-y-4 md:grid-cols-2">
        {shown.map((group) => (
          <div key={group.job.id} className="[content-visibility:auto] [contain-intrinsic-size:0_180px]">
            <JobCard
              group={group}
              tier={tier}
              forceApply={forceApply}
              readOnly={readOnly}
              onChanged={onChanged}
              onPrepJob={onPrepJob}
              running={running}
              suppressReadyBadge={allReady}
              isTopTier={group.job.match_score != null && group.job.match_score >= topTierThreshold}
            />
          </div>
        ))}
      </div>
      {visible < groups.length && (
        <div className="mt-4 flex justify-center">
          <Button variant="outline" size="sm" onClick={() => setVisible((v) => v + PAGE_SIZE)}>
            Show {Math.min(PAGE_SIZE, groups.length - visible)} more ({groups.length - visible} left)
          </Button>
        </div>
      )}
    </div>
  )
}

function JobCard({
  group,
  tier,
  forceApply,
  readOnly,
  onChanged,
  onPrepJob,
  running,
  suppressReadyBadge,
  isTopTier,
}: {
  group: JobGroup
  tier?: keyof JobLists
  forceApply: boolean
  readOnly: boolean
  onChanged?: () => void
  onPrepJob?: (jobId: number) => void
  running: boolean
  suppressReadyBadge: boolean
  isTopTier: boolean
}) {
  const { job, duplicates } = group
  const [pending, setPending] = useState<'applied' | 'skipped' | 'failed' | null>(null)
  const [showDuplicates, setShowDuplicates] = useState(false)
  const { gateReady, badge } = computeGate(job, tier)
  const actionable = gateReady || forceApply
  const stale = isStale(job.posted_at)
  // 'scraped' (Unarranged) jobs are excluded: `cmd_prep` only ever selects from
  // `store.get_jobs(status="matched")`, even for an explicit --jobs id
  // (main.py:426-427) — an unarranged job would silently no-op, the same class
  // of dead-button bug fixed for the 'claude' llm default (PLAN §9, 2026-08-23).
  const canPrep = !readOnly && !gateReady && !!onPrepJob &&
    !['applied', 'skipped', 'failed', 'scraped'].includes(job.status)

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
    <Card className={cn('group/card relative p-4 transition-colors hover:bg-muted/40', CARD_TONE[job.status])}>
      <div className="relative z-10 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-balance">{job.title ?? '(untitled)'}</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {job.company ?? '—'} · {job.location ?? '—'} · via {job.source}
            {' · '}
            <span className={cn(stale && 'font-medium text-warning')} title={job.posted_at ?? undefined}>
              {formatAge(job.posted_at)}
            </span>
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {job.role_profile && (
              <Badge variant="secondary" className="shrink-0 text-[10px]">
                {job.role_profile}
              </Badge>
            )}
            {badge && !(suppressReadyBadge && badge.label === 'ready to apply') && (
              <Badge variant={badge.variant} className="shrink-0 text-[10px]">
                {badge.label}
              </Badge>
            )}
            {!readOnly && <ResumeTag job={job} tier={tier} />}
            {duplicates.length > 0 && (
              <button
                type="button"
                onClick={() => setShowDuplicates((v) => !v)}
                className="shrink-0 text-[10px] text-muted-foreground underline decoration-dotted hover:text-foreground"
              >
                {showDuplicates ? 'hide duplicates' : `+${duplicates.length} more posting${duplicates.length > 1 ? 's' : ''}`}
              </button>
            )}
          </div>
          {job.outcome && (
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              {job.outcome}
              {job.applied_at ? ` · ${job.applied_at}` : ''}
            </p>
          )}
          {showDuplicates && (
            <ul className="mt-1.5 space-y-0.5 border-l pl-2 text-[11px] text-muted-foreground">
              {duplicates.map((d) => (
                <li key={d.id}>
                  via {d.source} — {formatAge(d.posted_at)} · #{d.id}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <div className="flex items-center gap-2">
            <ScoreMeter score={job.match_score} isTopTier={isTopTier} />
            {job.url && (
              <a
                href={job.url}
                target="_blank"
                rel="noreferrer"
                className="relative z-10 text-muted-foreground hover:text-foreground"
                aria-label="Open posting"
              >
                <ArrowSquareOut className="size-4" />
              </a>
            )}
          </div>
          {canPrep && (
            <Button
              size="xs"
              variant="secondary"
              onClick={() => onPrepJob?.(job.id)}
              disabled={running}
              title="Run prep for just this job — tailors the résumé and moves it toward the apply gate"
            >
              <Sparkle className="size-3" />
              Prep this job
            </Button>
          )}
          {!readOnly && !['applied', 'skipped', 'failed'].includes(job.status) && (
            <div
              className={cn('flex items-center gap-2', !actionable && 'opacity-40')}
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
                size="xs"
                variant="ghost"
                disabled={!actionable || pending !== null}
                onClick={() => logOutcome('skipped')}
                title="Skip — decided not to apply"
              >
                <SkipForward className="size-3" />
                Skip
              </Button>
              <Button
                size="xs"
                variant="ghost"
                disabled={!actionable || pending !== null}
                onClick={() => logOutcome('failed')}
                title="Failed — application attempt didn't go through"
              >
                <WarningOctagon className="size-3" />
                Failed
              </Button>
            </div>
          )}
        </div>
      </div>
      {/* Whole-card-clickable (finding 7) — "open the posting" is the
          highest-frequency action on this screen and previously had the
          smallest target (a 16px icon) while ~600px of card did nothing.
          This anchor sits *behind* the interactive content above (which is
          lifted to z-10), so real buttons/links still win the click; any
          empty card surface now opens the posting too. */}
      {job.url && (
        <a
          href={job.url}
          target="_blank"
          rel="noreferrer"
          tabIndex={-1}
          className="absolute inset-0 z-0 rounded-xl"
          aria-hidden="true"
        />
      )}
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
        className="relative z-10 flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
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
        className="relative z-10 flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
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

export const JobsList = memo(JobsListImpl)
