---
name: resume-tailor
description: >
  Build a per-job tailored copy of the master résumé (LaTeX → PDF) guided by the job's
  jd_brief, reusing one variant across similar postings. Trigger on "tailor my résumé",
  "tailor the resume for this job", "make a résumé for these jobs", "build tailored
  resumes". Reads `matched` jobs that have a `jd_brief`, writes `tailored_resume_path`,
  advances `matched → tailored`. Never modifies the master .tex/PDF.
model: sonnet
---

# resume-tailor

Pipeline stage **4** (PLAN.md §5). For each `matched` job that has a `jd_brief` and no
`tailored_resume_path`, it produces a **tailored copy** of `varakumar_resume.tex`,
compiles it to PDF with tectonic, records `tailored_resume_path`, and advances
`matched → tailored`.

**The master is sacred.** `varakumar_resume.tex` and its PDF are read-only — every job
gets its own copy under `tailored/<variant_id>/`. Tailoring is **LLM-driven but
fabrication-safe**: the model only returns *directives* — a rephrased Professional
Summary using the SAME facts, a tagline reordered from the candidate's REAL roles
(allowlist enforced), and a reordering of the EXISTING skill rows. Deterministic Python
splices those into the master copy, so the output always compiles and never invents
experience. Section order is preserved.

## Variant store + reuse (PLAN §9)

Each distinct **role_profile + keyword signature** becomes a variant
(`tailored/<id>/resume.tex` + `resume.pdf` + `meta.json`). A new job that's **similar**
to an existing variant — same `role_profile` and keyword overlap ≥ `--reuse-threshold`
(Jaccard, default 0.6) — **reuses** it instead of regenerating, so similar postings
share one résumé. `tailored/` is gitignored (generated copies).

## Runs in all three LLM modes (PLAN §9 · `execution/llm.py`)

**session (default, free):**
```bash
.venv/bin/python .claude/skills/resume-tailor/scripts/tailor.py prepare   # reuse + emit prompts for NEW variants → .tmp/resume-tailor/prompts.json
# orchestrator writes directives { "<job_id>": {tagline_roles, summary, skill_order}, … } → .tmp/resume-tailor/answers.json
.venv/bin/python .claude/skills/resume-tailor/scripts/tailor.py save       # apply directives + build PDFs
```

**api (Anthropic) / grok (xAI):**
```bash
.venv/bin/python .claude/skills/resume-tailor/scripts/tailor.py run [--limit N]
```

**any mode:**
```bash
.venv/bin/python .claude/skills/resume-tailor/scripts/tailor.py          # auto: prepare (session) | run (api/grok)
.venv/bin/python .claude/skills/resume-tailor/scripts/tailor.py show      # variants + which jobs use them
```

Flags: `--limit N` (cost cap in api/grok mode), `--master <tex>`, `--reuse-threshold`,
`--no-build` (skip tectonic, link the `.tex` — for environments without tectonic).
Builds with `tectonic` (see `Makefile`). Each job persists immediately → resumable.

## Orchestrator procedure (session mode)

1. Run `prepare` — it auto-resolves reuse jobs and prints directive prompts only for
   jobs needing a NEW variant.
2. For each printed job, return STRICT JSON directives: `tagline_roles` (only from the
   job's `allowed_tagline_roles`), `summary` (rephrase `current_summary` — SAME facts,
   numbers, tools, employers — re-emphasized for the job, ending with a "Seeking
   opportunities in …" line), `skill_order` (reorder the given `skill_labels`).
3. Write `{ "<job_id>": {directives}, … }` to `.tmp/resume-tailor/answers.json`.
4. Run `save`. Unknown tagline roles are dropped (anti-fabrication); a blank summary
   falls back to the master's; unknown skill labels are ignored and none are dropped.

## Self-annealing

Splice logic + the directive schema live in `scripts/tailor.py` (`SYSTEM_PROMPT`,
`apply_directives`, the `_TAGLINE_RE`/`_SUMMARY_RE`/`_TECHROW_RE` anchors). If the
master's structure changes (new section, renamed `\resumeSection`), update those
anchors and re-verify a build. Skill-label matching is normalized (`\&`, case,
whitespace) by `_norm_label`; if a new label form slips past, harden it and note the
case here + in PLAN.md §9. Reuse threshold is tunable via `--reuse-threshold`.
