import { useId, useState } from 'react'
import { ChartBar } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { LLM_OPTIONS, LlmSelect } from '@/components/LlmSelect'
import { streamRank } from '@/lib/api'
import type { StreamFn } from '@/lib/usePipelineRunner'
import * as v from '@/lib/validate'

interface RankPanelProps {
  running: boolean
  run: (streamFn: StreamFn) => void
}

export function RankPanel({ running, run }: RankPanelProps) {
  const limitId = useId()
  const [llm, setLlm] = useState('auto')
  const [limit, setLimit] = useState(20)
  const [eligible, setEligible] = useState(true)
  // Arrange is now the durable sort action (2026-08-23, PLAN §9: it also runs
  // match.py + auto-reject) — default to persisting scores so Unarranged jobs
  // actually leave that tier and Prep's "LLM's best" selection isn't permanently
  // empty. Still a checkbox: a dry run to preview ordering is legitimate.
  const [save, setSave] = useState(true)
  const [touched, setTouched] = useState(false)

  const limitError = v.positiveInt(limit, 'Limit')

  function runArrange() {
    setTouched(true)
    if (limitError) return
    run((onLine, onDone, onError) => streamRank({ llm, limit, eligible, save }, onLine, onDone, onError))
  }

  return (
    <div className="rounded-xl border bg-card p-5">
      <div className="mb-3 flex items-center gap-2">
        <ChartBar className="size-5 text-muted-foreground" weight="bold" />
        <span className="text-base font-medium">Arrange — sort unarranged jobs into tiers</span>
      </div>
      <div className="flex flex-wrap items-end gap-4">
        <LlmSelect value={llm} onChange={setLlm} options={LLM_OPTIONS} pinnedFirst="auto" autoSelectPicked />
        <div>
          <Label htmlFor={limitId} className="text-xs">
            LLM refinement shortlist size
          </Label>
          <Input
            id={limitId}
            type="number"
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-9 w-20 text-sm"
            aria-invalid={touched && !!limitError}
          />
          {touched && limitError && <p className="mt-1 text-xs text-destructive">{limitError}</p>}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <input type="checkbox" checked={eligible} onChange={(e) => setEligible(e.target.checked)} />
          eligible only
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} />
          save scores (needed for prep's "LLM's best")
        </label>
        <Button size="lg" onClick={runArrange} disabled={running}>
          {running ? 'Running…' : 'Run arrange'}
        </Button>
      </div>
    </div>
  )
}
