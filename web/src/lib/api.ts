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

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${body}`)
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
  reset: (hard: boolean) =>
    jsonFetch<Record<string, unknown>>('/api/reset', {
      method: 'POST',
      body: JSON.stringify({ hard }),
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
}

export interface SearchParams {
  queries: string
  locations: string
  days: number
  source: string
  limit: number
  workers: number
}

/** Stream a search run via SSE, calling onLine for each output line and
 * onDone when the run finishes (with the final stats payload). */
export function streamSearch(
  params: SearchParams,
  onLine: (line: string) => void,
  onDone: (stats: Stats) => void,
  onError: (err: string) => void,
): () => void {
  const controller = new AbortController()
  ;(async () => {
    try {
      const res = await fetch('/api/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
        signal: controller.signal,
      })
      if (!res.ok || !res.body) {
        onError(`${res.status} ${res.statusText}`)
        return
      }
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const events = buf.split('\n\n')
        buf = events.pop() ?? ''
        for (const evt of events) {
          let eventType = 'message'
          let data = ''
          for (const line of evt.split('\n')) {
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
