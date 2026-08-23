// Pure reducer that turns the raw SSE line stream from search/prep/rank into
// a small progress summary the UI can render as a stepper + bar, instead of
// forcing the user to read raw stdout to guess how far along a run is (see
// PLAN.md §9 2026-08-23 UI audit, finding 9). Parses the exact text these
// scripts already print — scrape.py's per-source line
// (`.claude/skills/job-scraper/scripts/scrape.py:379`) and the stage markers
// streamed by server/app.py. Presentational only: an unrecognized line never
// blocks or changes anything, it just keeps the last known stage instead of
// flickering to "unknown".

export interface RunProgress {
  stage: string
  current: number | null
  total: number | null
}

export const INITIAL_PROGRESS: RunProgress = { stage: 'starting…', current: null, total: null }

// `  [3/37] ✓ naukri done — saved 4 new / 1 updated (of 5 found), 0 error(s)`
const SOURCE_DONE_RE = /^\s*\[(\d+)\/(\d+)\]\s+\S+\s+\S+\s+done/

const STAGE_MARKERS: Array<[RegExp, string]> = [
  [/^\$\s*scrape\.py\b/, 'scraping job sources'],
  [/^\$\s*match\.py\b/, 'matching against your profile'],
  [/^\$\s*(prep|resume_tailor)\.py\b/, 'tailoring résumés'],
  [/^\$\s*rank\.py\b/, 'ranking'],
  [/probing LLM providers/i, 'probing LLM providers'],
  [/LLM rerank via/i, 'ranking with LLM'],
  [/auto-rejected/i, 'applying eligibility rules'],
]

export function nextProgress(prev: RunProgress, line: string): RunProgress {
  const sourceMatch = line.match(SOURCE_DONE_RE)
  if (sourceMatch) {
    return { stage: 'scraping job sources', current: Number(sourceMatch[1]), total: Number(sourceMatch[2]) }
  }
  for (const [re, label] of STAGE_MARKERS) {
    if (re.test(line)) return { stage: label, current: null, total: null }
  }
  if (/^\[exit /.test(line)) return { ...prev, stage: 'finishing up…' }
  return prev
}

export function progressPercent(p: RunProgress): number | null {
  if (p.current == null || p.total == null || p.total <= 0) return null
  return Math.min(100, Math.round((p.current / p.total) * 100))
}
