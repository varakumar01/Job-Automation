import { useState } from 'react'
import { ChartBar } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { streamRank } from '@/lib/api'
import type { StreamFn } from '@/lib/usePipelineRunner'
import * as v from '@/lib/validate'

const LLM_OPTIONS = [
  { value: 'python', label: 'Python (deterministic, free)' },
  { value: 'grok', label: 'Grok (Groq, free tier)' },
  { value: 'deepseek', label: 'DeepSeek (paid, cheap)' },
  { value: 'nvidia', label: 'NVIDIA NIM (free tier)' },
  { value: 'api', label: 'Anthropic API' },
]

interface RankPanelProps {
  running: boolean
  run: (streamFn: StreamFn) => void
}

export function RankPanel({ running, run }: RankPanelProps) {
  const [llm, setLlm] = useState('python')
  const [limit, setLimit] = useState(20)
  const [eligible, setEligible] = useState(true)
  const [save, setSave] = useState(false)
  const [touched, setTouched] = useState(false)

  const limitError = llm !== 'python' ? v.positiveInt(limit, 'Limit') : null

  function runRank() {
    setTouched(true)
    if (limitError) return
    run((onLine, onDone, onError) => streamRank({ llm, limit, eligible, save }, onLine, onDone, onError))
  }

  return (
    <div className="rounded-xl border bg-card p-5">
      <div className="mb-3 flex items-center gap-2">
        <ChartBar className="size-5 text-muted-foreground" weight="bold" />
        <span className="text-base font-medium">Rank — LLM rerank by résumé fit</span>
      </div>
      <div className="flex flex-wrap items-end gap-4">
        <div>
          <Label className="text-xs">LLM</Label>
          <Select value={llm} onValueChange={(val) => setLlm(val ?? 'python')}>
            <SelectTrigger className="h-9 w-60 text-sm">
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
        {llm !== 'python' && (
          <div>
            <Label className="text-xs">Shortlist size</Label>
            <Input
              type="number"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              className="h-9 w-20 text-sm"
              aria-invalid={touched && !!limitError}
            />
            {touched && limitError && <p className="mt-1 text-xs text-destructive">{limitError}</p>}
          </div>
        )}
        {llm !== 'python' && (
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={eligible} onChange={(e) => setEligible(e.target.checked)} />
            eligible only
          </label>
        )}
        {llm !== 'python' && (
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} />
            save scores (needed for prep's "LLM's best")
          </label>
        )}
        <Button size="lg" onClick={runRank} disabled={running}>
          {running ? 'Running…' : 'Run rank'}
        </Button>
      </div>
    </div>
  )
}
