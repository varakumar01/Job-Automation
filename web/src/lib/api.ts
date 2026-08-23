// Typed client for the local FastAPI backend (server/app.py, via `main.py serve`).
// The Vite dev server proxies /api/* to http://127.0.0.1:8000 (see vite.config.ts).

export interface Job {
  id: number
  source: string
  title: string | null
  company: string | null
  location: string | null
  url: string | null
  posted_at: string | null
  match_score: number | null
  llm_score: number | null
  status: string
  role_profile: string | null
  tailored_resume_path: string | null
  applied_at: string | null
  outcome: string | null
  // Present on every row (bumped on every write — see data/store.py's
  // `update_job`) but only actually useful on the public snapshot, where
  // it's the one available proxy for "how fresh is this dataset" (no
  // separate export-time manifest field exists — see
  // `data/store.export_public_json`).
  updated_at: string | null
}

export interface Source {
  name: string
  base_url: string | null
  mechanism: string | null
  available: boolean
  reason: string | null
}

export interface Stats {
  [status: string]: number
}

export interface PromptEntry {
  text: string
  is_default: boolean
}

export interface EnvEntry {
  set: boolean
  value: string
}

export interface JobLists {
  unarranged: Job[]
  eligible: Job[]
  needs_mod: Job[]
  stretch: Job[]
  off_profile: Job[]
}

export interface LlmProvider {
  provider: string
  ok: boolean | null // null = never probed
  detail: string
  checked_at: string
}

export interface LlmProviders {
  picked: string | null
  providers: LlmProvider[]
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    let detail: string | undefined
    try {
      detail = (JSON.parse(body) as { detail?: string }).detail
    } catch {
      /* body wasn't JSON — fall through to the raw-body message below */
    }
    throw new Error(detail ?? `${res.status} ${res.statusText}: ${body}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => jsonFetch<{ ok: boolean }>('/api/health'),
  jobs: (params: { status?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.limit) qs.set('limit', String(params.limit))
    return jsonFetch<Job[]>(`/api/jobs?${qs}`)
  },
  stats: () => jsonFetch<Stats>('/api/stats'),
  sources: () => jsonFetch<Source[]>('/api/sources'),
  llmProviders: (force = false) => jsonFetch<LlmProviders>(`/api/llm-providers?force=${force}`),
  lists: () => jsonFetch<JobLists>('/api/lists'),
  reset: (hard: boolean) =>
    jsonFetch<Record<string, unknown>>('/api/reset', {
      method: 'POST',
      body: JSON.stringify({ hard }),
    }),
  // Pure status-recording — never opens a browser or submits anything. The
  // backend refuses unless the job is already at status 'tailored' (the
  // apply gate — résumé decided) UNLESS `force` is set, so this can only
  // ever record an outcome that already happened, not cause one.
  log: (job: number, outcome: 'applied' | 'skipped' | 'failed', note?: string, force?: boolean) =>
    jsonFetch<{ ok: boolean; job: Job }>('/api/log', {
      method: 'POST',
      body: JSON.stringify({ job, outcome, note, force: force ?? false }),
    }),
  env: () => jsonFetch<Record<string, EnvEntry>>('/api/env'),
  setEnv: (key: string, value: string, persist: boolean) =>
    jsonFetch<{ ok: boolean }>('/api/env', {
      method: 'POST',
      body: JSON.stringify({ key, value, persist }),
    }),
  prompts: () => jsonFetch<Record<string, PromptEntry>>('/api/prompts'),
  savePrompt: (name: string, text: string) =>
    jsonFetch<{ ok: boolean }>(`/api/prompts/${encodeURIComponent(name)}`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),
  profile: () => jsonFetch<Record<string, unknown>>('/api/profile'),
  saveProfile: (body: Record<string, unknown>) =>
    jsonFetch<{ ok: boolean }>('/api/profile', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  resume: () => jsonFetch<{ tex: string; pdf_exists: boolean }>('/api/resume'),
  saveResume: (tex: string) =>
    jsonFetch<{ ok: boolean; stderr: string; pdf_exists: boolean }>('/api/resume', {
      method: 'POST',
      body: JSON.stringify({ tex }),
    }),
  resumePdfUrl: () => '/api/resume/pdf',
  jobResumePdfUrl: (jobId: number) => `/api/jobs/${jobId}/resume/pdf`,
}

export interface SearchParams {
  queries: string
  locations: string
  days: number
  source: string
  limit: number
  workers: number
  recheck?: boolean
}

export interface PrepParams {
  llm: string
  selection: 'pending' | 'eligible' | 'needs_mod' | 'stretch' | 'llm_best' | 'jobs'
  jobs?: string
  modify_resume: boolean
  limit?: number
}

export interface RankParams {
  llm: string
  limit: number
  eligible: boolean
  jobs?: string
  save: boolean
}

/** Stream any of the backend's SSE pipeline endpoints (search/prep/rank —
 * they share one server-side lock, so only one can run at a time), calling
 * onLine for each output line and onDone when the run finishes (with the
 * final stats payload). Returns an abort function. */
function streamSSE<P>(
  url: string,
  params: P,
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
): () => void {
  const controller = new AbortController()
  ;(async () => {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        let detail = `${res.status} ${res.statusText}`
        try {
          const body = (await res.json()) as { detail?: string }
          if (body.detail) detail = body.detail
        } catch {
          /* body wasn't JSON — keep the status line */
        }
        onError(detail)
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        // sse-starlette (the server's SSE library) terminates each event
        // with CRLF CRLF ("\r\n\r\n"), not bare "\n\n" — splitting on LF only
        // never matched a single event boundary (found live: `events=0` on
        // every run, so `onDone` never fired and the UI stayed stuck showing
        // "Running…" forever, even though the server responded in ~190ms).
        const events = buf.split(/\r?\n\r?\n/)
        buf = events.pop() ?? ''
        for (const evt of events) {
          let eventType = 'message'
          let data = ''
          for (const line of evt.split(/\r?\n/)) {
            if (line.startsWith('event:')) eventType = line.slice(6).trim()
            else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).trim()
          }
          if (eventType === 'line') onLine(data)
          else if (eventType === 'exit') onLine(`[exit ${data}]`)
          else if (eventType === 'done') {
            try {
              onDone((JSON.parse(data).stats ?? {}) as Stats)
            } catch {
              onDone({})
            }
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        onError(err instanceof Error ? err.message : String(err))
      }
    }
  })()
  return () => controller.abort()
}

export function streamSearch(
  params: SearchParams,
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
): () => void {
  return streamSSE('/api/search', params, onLine, onDone, onError)
}

export function streamPrep(
  params: PrepParams,
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
): () => void {
  return streamSSE('/api/prep', params, onLine, onDone, onError)
}

export function streamRank(
  params: RankParams,
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
): () => void {
  return streamSSE('/api/rank', params, onLine, onDone, onError)
}
