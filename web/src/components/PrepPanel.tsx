import { useState } from 'react'
import { Sparkle } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { streamPrep, type PrepParams } from '@/lib/api'
import type { StreamFn } from '@/lib/usePipelineRunner'
import * as v from '@/lib/validate'

const LLM_OPTIONS = [
  { value: 'claude', label: 'Claude (this session — manual, no automation)' },
  { value: 'grok', label: 'Grok (Groq, free tier)' },
  { value: 'deepseek', label: 'DeepSeek (paid, cheap)' },
  { value: 'nvidia', label: 'NVIDIA NIM (free tier)' },
  { value: 'api', label: 'Anthropic API' },
]

const SELECTION_OPTIONS: Array<{ value: PrepParams['selection']; label: string }> = [
  { value: 'pending', label: 'All pending' },
  { value: 'eligible', label: '✅ Eligible (best match, master résumé as-is)' },
  { value: 'needs_mod', label: '✏️ Needs résumé modification' },
  { value: 'stretch', label: '🧗 Stretch (low-fit, heavy rewrite)' },
  { value: 'llm_best', label: "LLM reranker's best picks (needs Rank --save first)" },
  { value: 'jobs', label: 'Specific job ids' },
]

interface PrepPanelProps {
  running: boolean
  run: (streamFn: StreamFn) => void
}

export function PrepPanel({ running, run }: PrepPanelProps) {
  const [llm, setLlm] = useState('claude')
  const [selection, setSelection] = useState<PrepParams['selection']>('pending')
  const [jobs, setJobs] = useState('')
  const [modifyResume, setModifyResume] = useState(false)
  const [limit, setLimit] = useState('')
  const [touched, setTouched] = useState(false)

  const jobsError = selection === 'jobs' ? v.jobIdList(jobs) : null
  const limitError = limit.trim() ? v.positiveInt(Number(limit), 'Limit') : null
  const hasErrors = !!jobsError || !!limitError

  function runPrep() {
    setTouched(true)
    if (hasErrors) return
    run((onLine, onDone, onError) =>
      streamPrep(
        {
          llm,
          selection,
          jobs: selection === 'jobs' ? jobs : undefined,
          modify_resume: modifyResume,
          limit: limit.trim() ? Number(limit) : undefined,
        },
        onLine,
        onDone,
        onError,
      ),
    )
  }

  return (
    <div className="rounded-xl border bg-card p-5">
      <div className="mb-3 flex items-center gap-2">
        <Sparkle className="size-5 text-muted-foreground" weight="bold" />
        <span className="text-base font-medium">Prep — JD brief → tailor résumé</span>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <Label className="text-xs">LLM</Label>
          <Select value={llm} onValueChange={(val) => setLlm(val ?? 'claude')}>
            <SelectTrigger className="h-9 w-full text-sm">
              {/* base-ui's Select.Value shows the raw value by default, not the
                  matching item's label — map it explicitly. */}
              <SelectValue>{(val: string) => LLM_OPTIONS.find((o) => o.value === val)?.label ?? val}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {LLM_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label className="text-xs">Job selection</Label>
          <Select value={selection} onValueChange={(val) => setSelection((val ?? 'pending') as PrepParams['selection'])}>
            <SelectTrigger className="h-9 w-full text-sm">
              <SelectValue>
                {(val: string) => SELECTION_OPTIONS.find((o) => o.value === val)?.label ?? val}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {SELECTION_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {selection === 'jobs' && (
          <div>
            <Label className="text-xs">Job ids (comma-separated)</Label>
            <Input
              value={jobs}
              onChange={(e) => setJobs(e.target.value)}
              placeholder="12, 47, 103"
              className="h-9 text-sm"
              aria-invalid={touched && !!jobsError}
            />
            {touched && jobsError && <p className="mt-1 text-xs text-destructive">{jobsError}</p>}
          </div>
        )}
        <div>
          <Label className="text-xs">Limit (optional)</Label>
          <Input
            type="number"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            placeholder="no limit"
            className="h-9 text-sm"
            aria-invalid={touched && !!limitError}
          />
          {touched && limitError && <p className="mt-1 text-xs text-destructive">{limitError}</p>}
        </div>
      </div>
      <label className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
        <input type="checkbox" checked={modifyResume} onChange={(e) => setModifyResume(e.target.checked)} />
        Also tailor eligible jobs (they use the master résumé as-is by default)
      </label>
      <Button size="lg" onClick={runPrep} disabled={running} className="mt-4">
        {running ? 'Running…' : 'Run prep'}
      </Button>
    </div>
  )
}
