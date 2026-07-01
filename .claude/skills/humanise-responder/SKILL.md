---
name: humanise-responder
description: >
  Draft honest, human-sounding application answers and a cover letter for each tailored
  job, grounded in the candidate's real résumé + the job's jd_brief. Trigger on "draft
  answers", "write a cover letter", "answer the application questions", "humanise
  responses", "prep application answers". Reads status `tailored` (jobs lacking
  `answers_json`), writes `answers_json`, advances `tailored → ready`.
model: sonnet
---

# humanise-responder

Pipeline stage **5** (PLAN.md §5) — the last skill before apply-agent. For each job at
`tailored` that lacks `answers_json`, it drafts a **cover letter** plus answers to the
common open-ended application questions, then advances `tailored → ready`.

**Honest by construction.** Every answer is grounded in the candidate's real résumé
(`candidate_profile` = summary + key achievements + skills, parsed from the master
`.tex`) and the job's `jd_brief`; the system prompt forbids inventing employers, tools,
certs, or experience. Facts only the candidate can supply — **notice period, current/
expected CTC, relocation/visa** — are NOT fabricated.

**Years/seniority rule (always on).** Every answer weighs the role's required
years/seniority (`jd_brief.seniority` / `must_have`) against the candidate's ~2 years: a
higher ask is named honestly and answered with transferable depth — never by claiming more
years or seniority than the résumé shows; an on-level role is stated plainly.

**Candidate details file (`candidate.json` via `execution/candidate.py`).** Facts the
candidate HAS filled in (relocation, work auth, total experience, contact, …) are passed
to the prompt as `candidate_facts` and may be cited honestly; facts STILL blank become
`screening_todo` **deterministically** (`candidate.screening_gaps()` — not the LLM's
guess), merged with any extra job-specific question the model surfaced. Edit
`candidate.json` to shrink the human-fill list (copy `candidate.example.json`). The
apply gate still owns whatever remains (PLAN §6 human-in-the-loop).

## answers_json (stored on the job)

`cover_letter` (required) · `answers` { `why_role`, `why_company`,
`relevant_experience` (mapped to `must_have`), `strengths`, `availability_note` } ·
`screening_todo[]` (fields the human must fill before submitting). apply-agent consumes
this to populate form fields and stops for review.

## Runs in all three LLM modes (PLAN §9 · `execution/llm.py`)

**session (default, free):**
```bash
.venv/bin/python .claude/skills/humanise-responder/scripts/respond.py prepare   # → .tmp/humanise-responder/prompts.json
# orchestrator writes { "<job_id>": {cover_letter, answers, screening_todo}, … } → answers.json
.venv/bin/python .claude/skills/humanise-responder/scripts/respond.py save
```

**api / grok:**
```bash
.venv/bin/python .claude/skills/humanise-responder/scripts/respond.py run [--limit N]
```

**any mode:**
```bash
.venv/bin/python .claude/skills/humanise-responder/scripts/respond.py        # auto: prepare (session) | run (api/grok)
.venv/bin/python .claude/skills/humanise-responder/scripts/respond.py show    # tailored (pending) + ready jobs
```

`--limit N` caps jobs (cost in api/grok). `--master <tex>` overrides the résumé. Each
job persists the moment its answers are saved → resumable.

## Orchestrator procedure (session mode)

1. Run `prepare`, read the per-job prompts.
2. For each job, return STRICT JSON: a `cover_letter` and `answers` grounded ONLY in
   the `candidate_profile`/`jd_brief`; put anything you'd otherwise guess (CTC, notice
   period, dates) into `screening_todo` — never invent it.
3. Write `{ "<job_id>": {answers}, … }` to `.tmp/humanise-responder/answers.json`.
4. Run `save`. Entries missing `cover_letter`, or whose job isn't at `tailored`, are
   skipped.

## Self-annealing

The answer schema + voice rules live in `scripts/respond.py` (`SYSTEM_PROMPT`,
`ANSWER_KEYS`, `_normalize_answers`). The candidate profile is parsed live from the
master `.tex` (`_candidate_profile`), so résumé edits flow through automatically — if
the master's section names change, update those anchors. Add new common-question keys
to `ANSWER_KEYS` (+ the prompt) and note it here + in PLAN.md §9.
