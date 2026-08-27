---
name: profile-matcher
description: >
  Rank/prioritize scraped jobs by how well they fit the master résumé with the
  fewest edits, and tag each with a best-fit role-profile. Trigger on "rank the
  jobs", "prioritize scraped jobs", "which jobs fit my résumé", "score matches",
  "best jobs to apply to". Reads status `scraped`, writes `match_score` +
  `role_profile`, advances to `matched`.
model: sonnet
---

# profile-matcher

Pipeline stage **2** (PLAN.md §5). Scores each scraped job 0–100 on *how few edits
the master résumé needs to fit it*, picks the best-fit **role-profile**, and advances
`scraped → matched`. Reads: master `varakumar_resume.tex` + `jobs` at `scraped`.
Writes: `match_score`, `role_profile`, a compact rationale in `notes`.

**Deterministic & free.** Pure stdlib — no LLM, no API key, no network. Scores are
reproducible and explainable, which is why this stage does not use the
session/api LLM provider (that's for jd-understander / humanise-responder).

## Scoring (0–100, transparent)

- **Skill overlap (≤60)** — recognized skills in the JD that the résumé already has,
  weighted mostly by *coverage* (fraction of the JD's recognized skills you hold) plus
  a small bonus for absolute overlap. This is the core "fewest edits" signal.
- **Role fit (≤25)** — overlap with the strongest matching role-profile's keywords.
- **Title/seniority fit (−10…+15)** — a security role term in the title adds points;
  senior/lead/principal/staff/manager titles subtract (the résumé is ~2 yrs experience).

The chosen **role-profile** — the India-market six from PLAN.md §9 (VAPT / Pentest ·
Cloud Security · ICS/OT Security · Application Security · Detection Engineering ·
Vulnerability Management), plus a General Security fallback when no sector clears the
win margin — is stored in `role_profile` to steer resume-tailor, which now picks the
matching committed sector base under `resumes/*.tex` (see PLAN §9 2026-08-24). `notes`
holds a JSON rationale: `{role, breakdown, matched[], missing[]}` — `missing` = skills
the JD wants that the résumé lacks (useful to resume-tailor).

## Run it

```bash
.venv/bin/python .claude/skills/profile-matcher/scripts/match.py            # score all scraped → matched
.venv/bin/python .claude/skills/profile-matcher/scripts/match.py --dry-run  # preview, no DB writes
.venv/bin/python .claude/skills/profile-matcher/scripts/match.py --show     # ranked matched jobs
.venv/bin/python .claude/skills/profile-matcher/scripts/match.py --rescore  # recompute matched rows in place (no status change)
.venv/bin/python .claude/skills/profile-matcher/scripts/match.py --resume <path>.tex
```

Use `--rescore` after editing `varakumar_resume.tex` or the scoring logic to refresh
already-`matched` jobs without re-scraping; it leaves `status` (and any `jd_brief`)
untouched.

Processes per job and persists immediately (resumable). Re-running re-reads only rows
still at `scraped`; use `--show` to review the current ranking any time.

## Self-annealing

The role-profiles and skill vocabulary live at the top of `scripts/match.py`
(`ROLE_PROFILES`, `EXTRA_VOCAB`). The résumé's own skills are parsed live from its
`\techrow{}` rows, so editing `varakumar_resume.tex` updates the corpus automatically.
If matches look off for a new role family, add its keywords to `ROLE_PROFILES` /
`EXTRA_VOCAB` and note the change here + in PLAN.md §9.
