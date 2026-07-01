---
name: jd-understander
description: >
  Turn each matched job's description into a structured JSON brief (company, role,
  must-haves, ATS keywords, fit angle) that steers résumé-tailoring and answer-drafting.
  Trigger on "understand this job/company", "summarize the JD", "what does this role
  want", "build job briefs", "analyze the postings". Reads status `matched` (jobs
  lacking a `jd_brief`), writes `jd_brief`; the row stays at `matched`.
model: sonnet
---

# jd-understander

Pipeline stage **3** (PLAN.md §5). For every job at `matched` that has no `jd_brief`
yet, it produces a compact **structured brief** and stores it in `jd_brief`. The row
**stays at `matched`** — resume-tailor (stage 4) is what advances it to `tailored`.
The brief is the work queue: a job is "done" here once `jd_brief` is set, so runs are
resumable.

## The brief (strict JSON stored in `jd_brief`)

`company_summary` · `role_summary` · `key_tools[]` · `must_have[]` · `nice_to_have[]`
· `keywords[]` (ATS terms to mirror) · `seniority` · `red_flags[]` · `fit_notes`
(how this ~2-yr candidate should angle the résumé/answers). `company_summary` and
`role_summary` are required; the rest default to empty. Downstream: resume-tailor
reads `keywords`/`must_have`/`nice_to_have`; humanise-responder reads
`fit_notes`/`role_summary`.

## Runs in all three LLM modes (PLAN §9 · `execution/llm.py`)

The mode comes from `LLM_PROVIDER` in `.env`. Same deterministic prep/save either way;
only **who answers** the prompt differs.

**session (default, free — orchestrator-in-the-loop):**
```bash
.venv/bin/python .claude/skills/jd-understander/scripts/understand.py prepare   # → .tmp/jd-understander/prompts.json (+ printout)
# the orchestrator (this Claude session) reads each prompt, writes a JSON object
#   { "<job_id>": { …brief… }, … }  →  .tmp/jd-understander/answers.json
.venv/bin/python .claude/skills/jd-understander/scripts/understand.py save      # validates + stores
```

**api (Anthropic) / grok (xAI) — one-shot loop:**
```bash
# set LLM_PROVIDER=api (ANTHROPIC_API_KEY) or grok (XAI_API_KEY) in .env, then:
.venv/bin/python .claude/skills/jd-understander/scripts/understand.py run [--limit N]
```

**any mode:**
```bash
.venv/bin/python .claude/skills/jd-understander/scripts/understand.py        # auto: prepare (session) | run (api/grok)
.venv/bin/python .claude/skills/jd-understander/scripts/understand.py show   # which matched jobs have briefs
```

`--limit N` caps jobs per run (cost control in api/grok mode). Each brief is written
the moment it's produced, so interrupting and re-running only re-processes the rest.

## Orchestrator procedure (session mode)

1. Run `prepare`. Read `.tmp/jd-understander/prompts.json` (or the printout).
2. For each job, apply the SYSTEM INSTRUCTION and produce the strict-JSON brief —
   extract only what the JD states; never invent requirements.
3. Write all briefs as one object `{ "<job_id>": {brief}, … }` to
   `.tmp/jd-understander/answers.json`.
4. Run `save`. It rejects briefs missing `company_summary`/`role_summary` and any
   job no longer at `matched`.

## Self-annealing

The brief schema lives in `scripts/understand.py` (`BRIEF_KEYS`, `REQUIRED_KEYS`,
`SYSTEM_PROMPT`). If resume-tailor/humanise-responder need another field, add it there
(and to `_normalize_brief`), then note it here + in PLAN.md §9. Model JSON wrapped in
``` fences or stray prose is tolerated by `_extract_json`; if a new provider returns a
shape that slips past it, harden that helper and record the case.
