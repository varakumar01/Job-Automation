import { useEffect, useId, useMemo, useState } from 'react'
import { ArrowClockwise, ChartBar } from '@phosphor-icons/react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { streamRank } from '@/lib/api'
import { useLlmProviders } from '@/lib/useLlmProviders'
import type { StreamFn } from '@/lib/usePipelineRunner'
import * as v from '@/lib/validate'

// 'python' (deterministic keyword sort) was removed as a ranking choice — every
// remaining option is a real LLM, and 'auto' already covers "whichever works".
const LLM_OPTIONS = [
  { value: 'auto', label: 'Auto (use whichever LLM works right now)' },
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
  const limitId = useId()
  const llmSelectId = useId()
  const [llm, setLlm] = useState('auto')
  const [llmTouched, setLlmTouched] = useState(false)
  const [limit, setLimit] = useState(20)
  const [eligible, setEligible] = useState(true)
  const [save, setSave] = useState(false)
  const [touched, setTouched] = useState(false)

  const { data: health, loading: healthLoading, refresh: refreshHealth } = useLlmProviders()

  // Once the live probe lands, default the selection to the actual working
  // provider (e.g. nvidia) instead of leaving it on the generic 'auto' — but
  // only before the user has picked something themselves.
  useEffect(() => {
    if (!llmTouched && health?.picked) {
      setLlm(health.picked)
    }
  }, [health, llmTouched])

  // 'auto' always stays pinned first; the rest are sorted working-first by the
  // live probe so whatever actually answers right now floats to the top.
  const orderedOptions = useMemo(() => {
    const rest = LLM_OPTIONS.filter((o) => o.value !== 'auto')
    if (!health) return LLM_OPTIONS
    const rank = (value: string) => {
      const p = health.providers.find((row) => row.provider === value)
      if (!p || p.ok === null) return 1
      return p.ok ? 0 : 2
    }
    return [LLM_OPTIONS[0], ...[...rest].sort((a, b) => rank(a.value) - rank(b.value))]
  }, [health])

  function statusGlyph(value: string): string {
    if (value === 'auto') return ''
    const p = health?.providers.find((row) => row.provider === value)
    if (!p || p.ok === null) return '? '
    return p.ok ? '✓ ' : '✗ '
  }

  function statusDetail(value: string): string | undefined {
    if (value === 'auto') return undefined
    return health?.providers.find((row) => row.provider === value)?.detail
  }

  const limitError = v.positiveInt(limit, 'Limit')

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
          <div className="flex items-center gap-1.5">
            <Label htmlFor={llmSelectId} className="text-xs">
              LLM
            </Label>
            <button
              type="button"
              onClick={() => refreshHealth(true)}
              disabled={healthLoading}
              title="re-probe provider health now"
              className="text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              <ArrowClockwise className={healthLoading ? 'size-3 animate-spin' : 'size-3'} />
            </button>
          </div>
          <Select
            value={llm}
            onValueChange={(val) => {
              setLlmTouched(true)
              setLlm(val ?? 'auto')
            }}
          >
            <SelectTrigger id={llmSelectId} className="h-9 w-64 text-sm">
              <SelectValue>
                {(val: string) => `${statusGlyph(val)}${LLM_OPTIONS.find((o) => o.value === val)?.label ?? val}`}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {orderedOptions.map((o) => (
                <SelectItem key={o.value} value={o.value} title={statusDetail(o.value)}>
                  {statusGlyph(o.value)}
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div>
          <Label htmlFor={limitId} className="text-xs">
            Shortlist size
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
        <Button size="lg" onClick={runRank} disabled={running}>
          {running ? 'Running…' : 'Run rank'}
        </Button>
      </div>
    </div>
  )
}
