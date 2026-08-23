// Pure list-view logic for job cards: recency parsing, sort, filter, dedupe.
// No React import — kept trivially testable and shared between the control
// panel (ResultsTiers) and the public dashboard (StaticApp) so the two never
// drift (see PLAN.md §9 2026-08-23 UI audit).

import type { Job } from './api'

// ── Recency ──────────────────────────────────────────────────────────────
// `posted_at` arrives from ~37 scraper plugins in at least five shapes:
// '2026-08-02', '2026-08-10T04:20:08+00:00', '30+ days ago', 'August 4, 2026',
// 'Wed, 19 Aug 2026 16:17:54 +0000'. None of it is normalized at ingest, so
// this parses defensively at display time rather than assuming one format.

const RELATIVE_DAYS_RE = /^(\d+)\+?\s*days?\s*ago$/i

/** Best-effort age in days for a raw `posted_at` string. Returns null when it
 * can't be parsed at all (rendered as "date unknown", not hidden or zeroed —
 * an unknown age must never silently sort as "newest"). */
export function ageInDays(postedAt: string | null, now: Date = new Date()): number | null {
  if (!postedAt) return null
  const rel = postedAt.match(RELATIVE_DAYS_RE)
  if (rel) return Number(rel[1])
  const ts = Date.parse(postedAt)
  if (Number.isNaN(ts)) return null
  const days = Math.floor((now.getTime() - ts) / 86_400_000)
  return days < 0 ? 0 : days // future-dated postings (clock skew) read as "today"
}

/** Short human label for a card — "today", "3d ago", "5w ago", "30+d ago". */
export function formatAge(postedAt: string | null, now: Date = new Date()): string {
  if (postedAt && RELATIVE_DAYS_RE.test(postedAt)) {
    const n = Number(postedAt.match(RELATIVE_DAYS_RE)![1])
    return `${n}+d ago`
  }
  const days = ageInDays(postedAt, now)
  if (days === null) return 'date unknown'
  if (days === 0) return 'today'
  if (days === 1) return '1d ago'
  if (days < 14) return `${days}d ago`
  if (days < 60) return `${Math.round(days / 7)}w ago`
  return `${Math.round(days / 30)}mo ago`
}

// A posting past this age is flagged distinctly (finding #1 in the audit —
// recency was invisible and let old postings visually outrank fresh ones).
export const STALE_DAYS = 30

export function isStale(postedAt: string | null, now: Date = new Date()): boolean {
  const days = ageInDays(postedAt, now)
  // "30+ days ago" parses to exactly 30 (RELATIVE_DAYS_RE), but it means
  // "more than 30" — >= so that sentinel value is caught too, not just
  // genuinely-computed ages past the threshold.
  return days === null ? false : days >= STALE_DAYS
}

// ── Sort ─────────────────────────────────────────────────────────────────

export type SortKey = 'score-desc' | 'score-asc' | 'newest' | 'oldest' | 'company' | 'title'

export const SORTS: Array<{ value: SortKey; label: string }> = [
  { value: 'score-desc', label: 'Best match first' },
  { value: 'score-asc', label: 'Worst match first' },
  { value: 'newest', label: 'Newest first' },
  { value: 'oldest', label: 'Oldest first' },
  { value: 'company', label: 'Company (A–Z)' },
  { value: 'title', label: 'Title (A–Z)' },
]

const AGE_UNKNOWN_SORT_VALUE = 1e9 // sorts unknown-date jobs to the end either direction

function compareBy(sort: SortKey, now: Date) {
  return (a: Job, b: Job): number => {
    switch (sort) {
      case 'score-desc':
        return (b.match_score ?? -1) - (a.match_score ?? -1)
      case 'score-asc':
        return (a.match_score ?? -1) - (b.match_score ?? -1)
      case 'newest': {
        const ad = ageInDays(a.posted_at, now) ?? AGE_UNKNOWN_SORT_VALUE
        const bd = ageInDays(b.posted_at, now) ?? AGE_UNKNOWN_SORT_VALUE
        return ad - bd
      }
      case 'oldest': {
        const ad = ageInDays(a.posted_at, now) ?? -1
        const bd = ageInDays(b.posted_at, now) ?? -1
        return bd - ad
      }
      case 'company':
        return (a.company ?? '').localeCompare(b.company ?? '')
      case 'title':
        return (a.title ?? '').localeCompare(b.title ?? '')
      default:
        return 0
    }
  }
}

export function sortJobs(jobs: Job[], sort: SortKey, now: Date = new Date()): Job[] {
  return [...jobs].sort(compareBy(sort, now))
}

// ── Filter ───────────────────────────────────────────────────────────────

/** Same substring predicate the public dashboard already used ad hoc
 * (previously only in StaticApp) — now the one place both surfaces call. */
export function matchesQuery(job: Job, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  return (
    (job.title ?? '').toLowerCase().includes(q) ||
    (job.company ?? '').toLowerCase().includes(q) ||
    (job.location ?? '').toLowerCase().includes(q)
  )
}

export function filterJobs(jobs: Job[], query: string, source: string): Job[] {
  return jobs.filter((j) => (source === 'all' || j.source === source) && matchesQuery(j, query))
}

// ── Dedupe ───────────────────────────────────────────────────────────────
// Uniqueness in the store is (source, ext_id) — two postings of the same role
// on the same portal are legitimate distinct rows there. This groups them for
// *display only*; nothing is deleted or merged server-side, so per-row
// applied/skipped/failed history is never lost. See PLAN.md §9 2026-08-23.

export interface JobGroup {
  job: Job // the representative shown on the card
  duplicates: Job[] // the other rows folded into this group (may be empty)
}

function normKey(job: Job): string {
  return [job.title, job.company, job.location].map((s) => (s ?? '').trim().toLowerCase()).join('|')
}

const STATUS_PRIORITY: Record<string, number> = {
  applied: 0,
  tailored: 1,
  matched: 2,
  skipped: 3,
  failed: 4,
}

function representativeRank(job: Job): [number, number, number] {
  return [
    STATUS_PRIORITY[job.status] ?? 5,
    -(job.match_score ?? -1), // higher score wins, so negate for ascending sort
    job.id,
  ]
}

function cmpTuple(a: [number, number, number], b: [number, number, number]): number {
  return a[0] - b[0] || a[1] - b[1] || a[2] - b[2]
}

/** Group jobs that are almost certainly the same posting (same normalized
 * title+company+location). Order of the returned groups follows the order
 * the representative jobs appeared in `jobs`. */
export function dedupeJobs(jobs: Job[]): JobGroup[] {
  const order: string[] = []
  const buckets = new Map<string, Job[]>()
  for (const job of jobs) {
    const key = normKey(job)
    if (!buckets.has(key)) {
      buckets.set(key, [])
      order.push(key)
    }
    buckets.get(key)!.push(job)
  }
  return order.map((key) => {
    const group = buckets.get(key)!
    const sorted = [...group].sort((a, b) => cmpTuple(representativeRank(a), representativeRank(b)))
    const [job, ...duplicates] = sorted
    return { job, duplicates }
  })
}
