import { useEffect, useId, useMemo, useState } from 'react'
import { ArrowClockwise } from '@phosphor-icons/react'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useLlmProviders } from '@/lib/useLlmProviders'

export interface LlmOption {
  value: string
  label: string
}

// 'python' (deterministic keyword sort) was removed as a ranking choice — every
// remaining option is a real LLM, and 'auto' already covers "whichever works".
// Shared by Search and Rank, which both only ever run a real model.
export const LLM_OPTIONS: LlmOption[] = [
  { value: 'auto', label: 'Auto (use whichever LLM works right now)' },
  { value: 'grok', label: 'Grok (Groq, free tier)' },
  { value: 'deepseek', label: 'DeepSeek (paid, cheap)' },
  { value: 'nvidia', label: 'NVIDIA NIM (free tier)' },
  { value: 'api', label: 'Anthropic API' },
]

interface LlmSelectProps {
  value: string
  onChange: (value: string) => void
  options: LlmOption[]
  /** Kept at index 0 regardless of live health (e.g. 'auto'). */
  pinnedFirst: string
  /** Other values with no live health signal to decorate — e.g. Prep's 'claude'
   * (session mode, always available, never probed). Rendered right after
   * `pinnedFirst`, in the order given, before the health-sorted providers;
   * no ✓/✗/? glyph and excluded from the working-first sort. */
  staticValues?: string[]
  /** Once the live probe lands, default the selection to the actual working
   * provider instead of leaving it on `pinnedFirst` — but only before the user
   * has touched the control. Only Rank wants this: Search/Prep default to the
   * literal pinned value ('auto'), which resolves server-side at run time and
   * so can't go stale between page load and clicking Run. */
  autoSelectPicked?: boolean
  triggerClassName?: string
}

/** Shared LLM-provider picker for Search/Rank/Prep: live health probe (nvidia/
 * grok/deepseek/api via useLlmProviders), ✓/✗/? status glyphs, working-first
 * ordering, and a manual re-probe button. Extracted 2026-08-23 — this exact
 * markup/ordering logic used to be duplicated verbatim in RankPanel and
 * PrepPanel; a third copy for Search would have made it three. */
export function LlmSelect({
  value,
  onChange,
  options,
  pinnedFirst,
  staticValues,
  autoSelectPicked = false,
  triggerClassName,
}: LlmSelectProps) {
  const selectId = useId()
  const [touched, setTouched] = useState(false)
  const { data: health, loading: healthLoading, refresh: refreshHealth } = useLlmProviders()

  useEffect(() => {
    if (autoSelectPicked && !touched && health?.picked) {
      onChange(health.picked)
    }
    // onChange intentionally excluded from deps — this effect must fire exactly
    // once, when the live health probe first resolves (or when autoSelectPicked/
    // touched change), not on every render. Both current callers pass a stable
    // useState setter so it wouldn't matter in practice, but a future caller
    // passing an inline/unstable callback would otherwise refire this on every
    // render and fight the user's own selection. (oxlint does honor this
    // eslint-compatible disable directive — confirmed via `npm run lint`,
    // 2026-08-23; a prior review comment claiming it was inert was wrong.)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [health, touched, autoSelectPicked])

  const staticSet = useMemo(
    () => new Set([pinnedFirst, ...(staticValues ?? [])]),
    [pinnedFirst, staticValues],
  )

  const orderedOptions = useMemo(() => {
    const pinned = options.find((o) => o.value === pinnedFirst)
    if (!pinned) return options
    const staticRest = options.filter((o) => o.value !== pinnedFirst && staticSet.has(o.value))
    const dynamic = options.filter((o) => !staticSet.has(o.value))
    if (!health) return [pinned, ...staticRest, ...dynamic]
    const rank = (val: string) => {
      const p = health.providers.find((row) => row.provider === val)
      if (!p || p.ok === null) return 1
      return p.ok ? 0 : 2
    }
    return [pinned, ...staticRest, ...[...dynamic].sort((a, b) => rank(a.value) - rank(b.value))]
  }, [health, options, pinnedFirst, staticSet])

  function statusGlyph(val: string): string {
    if (staticSet.has(val)) return ''
    const p = health?.providers.find((row) => row.provider === val)
    if (!p || p.ok === null) return '? '
    return p.ok ? '✓ ' : '✗ '
  }

  function statusDetail(val: string): string | undefined {
    if (staticSet.has(val)) return undefined
    return health?.providers.find((row) => row.provider === val)?.detail
  }

  return (
    <div>
      <div className="flex items-center gap-1.5">
        <Label htmlFor={selectId} className="text-xs">
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
        value={value}
        onValueChange={(val) => {
          setTouched(true)
          onChange(val ?? pinnedFirst)
        }}
      >
        <SelectTrigger id={selectId} className={triggerClassName ?? 'h-9 w-64 text-sm'}>
          {/* base-ui's Select.Value shows the raw value by default, not the
              matching item's label — map it explicitly. */}
          <SelectValue>
            {(val: string) => `${statusGlyph(val)}${options.find((o) => o.value === val)?.label ?? val}`}
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
  )
}
