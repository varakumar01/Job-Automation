import { useEffect, useState } from 'react'
import { FloppyDisk, Gear } from '@phosphor-icons/react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { api, type EnvEntry, type PromptEntry } from '@/lib/api'
import * as v from '@/lib/validate'

export function AdvancedOptions({
  forceApply,
  onForceApplyChange,
}: {
  forceApply: boolean
  onForceApplyChange: (value: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger render={<Button variant="outline" size="lg" />}>
        <Gear className="size-4" />
        Advanced
      </DialogTrigger>
      {/* DialogContent's own default className includes `sm:max-w-sm` — must
          override at the same `sm:` breakpoint or tailwind-merge keeps both
          and the responsive one wins on any real screen (found live: dialog
          rendered at 384px wide regardless of an unprefixed max-w-4xl here). */}
      <DialogContent className="max-h-[85vh] max-w-4xl overflow-hidden sm:max-w-4xl">
        <DialogHeader>
          <DialogTitle>Advanced options</DialogTitle>
        </DialogHeader>
        <label className="flex items-start gap-2 rounded-md border bg-muted/30 p-2.5 text-xs">
          <input
            type="checkbox"
            checked={forceApply}
            onChange={(e) => onForceApplyChange(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            <span className="font-medium">Force-enable Applied/Skip/Failed on any job</span>
            <br />
            <span className="text-muted-foreground">
              Normally those buttons only work once a job is "tailored" (résumé decided).
              Turning this on lets you record an outcome on any job — e.g. one you applied to
              outside this tool. Still pure status-recording: it never opens a browser or
              submits anything, it only changes which jobs the record button is allowed to
              target.
            </span>
          </span>
        </label>
        <Tabs defaultValue="env" className="min-h-0">
          <TabsList>
            <TabsTrigger value="env">Env</TabsTrigger>
            <TabsTrigger value="prompts">Prompts</TabsTrigger>
            <TabsTrigger value="profile">Profile</TabsTrigger>
            <TabsTrigger value="resume">Résumé</TabsTrigger>
          </TabsList>
          <TabsContent value="env" className="max-h-[60vh] overflow-y-auto">
            {open && <EnvTab />}
          </TabsContent>
          <TabsContent value="prompts" className="max-h-[60vh] overflow-y-auto">
            {open && <PromptsTab />}
          </TabsContent>
          <TabsContent value="profile" className="max-h-[60vh] overflow-y-auto">
            {open && <ProfileTab />}
          </TabsContent>
          <TabsContent value="resume" className="max-h-[60vh] overflow-y-auto">
            {open && <ResumeTab />}
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}

// ── Env ──────────────────────────────────────────────────────────────────

function EnvTab() {
  const [env, setEnv] = useState<Record<string, EnvEntry>>({})
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [persist, setPersist] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.env().then((e) => {
      setEnv(e)
      setLoading(false)
    })
  }, [])

  async function save(key: string) {
    const value = edits[key]
    if (value === undefined || v.envValue(value)) return
    await api.setEnv(key, value, persist[key] ?? false)
    toast.success(`${key} updated${persist[key] ? ' (saved to .env)' : ' (this session only)'}`)
    const e = await api.env()
    setEnv(e)
    setEdits((prev) => ({ ...prev, [key]: '' }))
  }

  if (loading) return <p className="p-4 text-sm text-muted-foreground">Loading…</p>

  return (
    <div className="flex flex-col gap-3 p-1">
      <p className="text-xs text-muted-foreground">
        Values are session-only by default (this server process + its subprocesses only —
        gone on restart). Check "save to .env" to persist a value permanently.
      </p>
      {Object.entries(env).map(([key, entry]) => {
        const value = edits[key] ?? ''
        const error = value ? v.envValue(value) : null
        return (
          <div key={key} className="grid grid-cols-[1fr_auto] items-center gap-2">
            <div>
              <Label htmlFor={`env-${key}`} className="font-mono text-xs">
                {key}
              </Label>
              <div className="flex items-center gap-2">
                <Input
                  id={`env-${key}`}
                  placeholder={!entry.set ? '(not set)' : entry.value || '(set, but empty)'}
                  value={value}
                  onChange={(e) => setEdits((prev) => ({ ...prev, [key]: e.target.value }))}
                  className="h-8 font-mono text-xs"
                  aria-invalid={!!error}
                />
                <Button size="sm" variant="secondary" onClick={() => save(key)} disabled={!!error}>
                  Set
                </Button>
              </div>
              {error && <p className="mt-0.5 text-[11px] text-destructive">{error}</p>}
              <label className="mt-1 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <input
                  type="checkbox"
                  checked={persist[key] ?? false}
                  onChange={(e) => setPersist((prev) => ({ ...prev, [key]: e.target.checked }))}
                />
                save to .env (persists across restarts)
              </label>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── Prompts ──────────────────────────────────────────────────────────────

function PromptsTab() {
  const [prompts, setPrompts] = useState<Record<string, PromptEntry>>({})
  const [active, setActive] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.prompts().then((p) => {
      setPrompts(p)
      const first = Object.keys(p)[0] ?? null
      setActive(first)
      if (first) setText(p[first].text)
      setLoading(false)
    })
  }, [])

  function selectPrompt(name: string) {
    setActive(name)
    setText(prompts[name]?.text ?? '')
  }

  async function save() {
    if (!active) return
    await api.savePrompt(active, text)
    toast.success(`${active} prompt saved`)
    const p = await api.prompts()
    setPrompts(p)
  }

  if (loading) return <p className="p-4 text-sm text-muted-foreground">Loading…</p>

  return (
    <div className="flex flex-col gap-2 p-1">
      <div className="flex gap-1.5">
        {Object.keys(prompts).map((name) => (
          <Button
            key={name}
            size="sm"
            variant={active === name ? 'default' : 'outline'}
            onClick={() => selectPrompt(name)}
          >
            {name}
            {prompts[name].is_default && (
              <Badge variant="secondary" className="ml-1.5 text-[9px]">
                default
              </Badge>
            )}
          </Button>
        ))}
      </div>
      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        className="min-h-72 font-mono text-xs"
        spellCheck={false}
      />
      <Button size="sm" onClick={save} className="self-end">
        <FloppyDisk className="size-4" />
        Save prompt
      </Button>
    </div>
  )
}

// ── Profile ──────────────────────────────────────────────────────────────

const PROFILE_FIELDS: Array<{ key: string; label: string; type?: 'text' | 'checkbox' }> = [
  { key: 'full_name', label: 'Full name' },
  { key: 'email', label: 'Email' },
  { key: 'phone', label: 'Phone' },
  { key: 'location', label: 'Location' },
  { key: 'linkedin', label: 'LinkedIn' },
  { key: 'github', label: 'GitHub' },
  { key: 'current_company', label: 'Current company' },
  { key: 'current_title', label: 'Current title' },
  { key: 'total_experience', label: 'Total experience' },
  { key: 'notice_period', label: 'Notice period' },
  { key: 'current_ctc', label: 'Current CTC' },
  { key: 'expected_ctc', label: 'Expected CTC' },
  { key: 'availability_to_start', label: 'Availability to start' },
  { key: 'work_authorization', label: 'Work authorization' },
  { key: 'willing_to_relocate', label: 'Willing to relocate', type: 'checkbox' },
  { key: 'visa_sponsorship_needed', label: 'Visa sponsorship needed', type: 'checkbox' },
]

function ProfileTab() {
  const [profile, setProfile] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(true)
  const [touched, setTouched] = useState(false)

  useEffect(() => {
    api.profile().then((p) => {
      setProfile(p)
      setLoading(false)
    })
  }, [])

  function setField(key: string, value: unknown) {
    setProfile((prev) => ({ ...prev, [key]: value }))
  }

  const fieldErrors: Record<string, string | null> = {
    full_name: v.required((profile.full_name as string) ?? '', 'Full name'),
    email: v.email((profile.email as string) ?? ''),
  }
  const hasErrors = Object.values(fieldErrors).some(Boolean)

  async function save() {
    setTouched(true)
    if (hasErrors) return
    await api.saveProfile(profile)
    toast.success('Profile saved to candidate.json')
  }

  if (loading) return <p className="p-4 text-sm text-muted-foreground">Loading…</p>

  return (
    <div className="flex flex-col gap-3 p-1">
      <div className="grid grid-cols-2 gap-3">
        {PROFILE_FIELDS.map(({ key, label, type }) => {
          const error = touched ? fieldErrors[key] : null
          const fieldId = `profile-${key}`
          return (
            <div key={key} className={type === 'checkbox' ? 'flex items-center gap-2' : ''}>
              <Label htmlFor={fieldId} className="text-xs">
                {label}
              </Label>
              {type === 'checkbox' ? (
                <input
                  id={fieldId}
                  type="checkbox"
                  checked={Boolean(profile[key])}
                  onChange={(e) => setField(key, e.target.checked)}
                />
              ) : (
                <>
                  <Input
                    id={fieldId}
                    value={(profile[key] as string) ?? ''}
                    onChange={(e) => setField(key, e.target.value)}
                    className="h-8 text-xs"
                    aria-invalid={!!error}
                  />
                  {error && <p className="mt-0.5 text-[11px] text-destructive">{error}</p>}
                </>
              )}
            </div>
          )
        })}
      </div>
      <div>
        <Label htmlFor="profile-preferred_locations" className="text-xs">
          Preferred locations (comma-separated)
        </Label>
        <Input
          id="profile-preferred_locations"
          value={
            Array.isArray(profile.preferred_locations)
              ? (profile.preferred_locations as string[]).join(', ')
              : ''
          }
          onChange={(e) =>
            setField(
              'preferred_locations',
              e.target.value.split(',').map((s) => s.trim()).filter(Boolean),
            )
          }
          className="h-8 text-xs"
        />
      </div>
      <Button size="sm" onClick={save} className="self-end">
        <FloppyDisk className="size-4" />
        Save profile
      </Button>
    </div>
  )
}

// ── Résumé ───────────────────────────────────────────────────────────────

function ResumeTab() {
  const [tex, setTex] = useState('')
  const [pdfExists, setPdfExists] = useState(false)
  const [loading, setLoading] = useState(true)
  const [compiling, setCompiling] = useState(false)
  const [compileError, setCompileError] = useState<string | null>(null)
  const [pdfNonce, setPdfNonce] = useState(0)

  useEffect(() => {
    api.resume().then((r) => {
      setTex(r.tex)
      setPdfExists(r.pdf_exists)
      setLoading(false)
    })
  }, [])

  async function compile() {
    if (!tex.trim()) {
      setCompileError('résumé content cannot be empty')
      return
    }
    setCompiling(true)
    setCompileError(null)
    try {
      const res = await api.saveResume(tex)
      setPdfExists(res.pdf_exists)
      if (!res.ok) {
        setCompileError(res.stderr || 'compile failed')
        toast.error('Résumé compile failed — see error below')
      } else {
        toast.success('Résumé compiled')
        setPdfNonce((n) => n + 1)
      }
    } finally {
      setCompiling(false)
    }
  }

  if (loading) return <p className="p-4 text-sm text-muted-foreground">Loading…</p>

  return (
    <div className="flex flex-col gap-2 p-1">
      {/* Compile button pinned above the editor, not buried below a
          96-row textarea (owner request 2026-08-23: "move the button to
          the top ... Ill be just pasting the tex code anyway" — pasting a
          whole master résumé and compiling immediately is the primary flow
          here, not a bottom-of-scroll afterthought). */}
      <div className="flex items-center justify-between gap-2">
        <Button size="sm" onClick={compile} disabled={compiling}>
          <FloppyDisk className="size-4" />
          {compiling ? 'Compiling…' : 'Save + compile'}
        </Button>
        {compileError && (
          <span role="status" aria-live="polite" className="text-[11px] text-destructive">
            compile failed — see error below
          </span>
        )}
      </div>
      {/* min-w-0 is load-bearing: a flex-1 child's default min-width is its
          content's intrinsic size, and the .tex source has long unbroken
          comment-divider lines — without this, the editor column claims
          nearly all the row and squeezes the PDF preview to a couple of
          pixels (found live: iframe width=2 vs textarea width=668). */}
      <div className="flex min-w-0 flex-1 flex-col gap-2 lg:flex-row">
        <div className="min-w-0 flex-1">
          <Textarea
            value={tex}
            onChange={(e) => setTex(e.target.value)}
            className="min-h-96 font-mono text-xs"
            spellCheck={false}
          />
          {compileError && (
            <pre className="mt-2 max-h-32 overflow-y-auto rounded border border-destructive/40 bg-destructive/5 p-2 text-[11px] text-destructive">
              {compileError}
            </pre>
          )}
        </div>
        <Separator orientation="vertical" className="hidden lg:block" />
        <div className="min-w-0 flex-1">
          {pdfExists ? (
            <iframe
              key={pdfNonce}
              title="Résumé preview"
              src={`${api.resumePdfUrl()}?v=${pdfNonce}`}
              className="h-96 w-full rounded border"
            />
          ) : (
            <div className="flex h-96 items-center justify-center rounded border border-dashed text-sm text-muted-foreground">
              No compiled PDF yet — save to compile.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
