---
name: apply-agent
description: >
  Apply to READY jobs — fill the application form (LinkedIn Easy Apply first) with the
  tailored résumé + drafted answers, screenshot it, and STOP at a human review gate;
  the human submits. Trigger on "apply to ready jobs", "fill the application", "start
  applying", "apply to <job>". Reads status `ready`, fills the form via the
  chrome-devtools MCP, and on `log` advances `ready → applied | skipped | failed`.
  NEVER auto-submits.
model: sonnet
---

# apply-agent

Pipeline stage **6** (final). The **highest-risk** surface — it touches real employer
forms — so it is **human-in-the-loop by design (PLAN §6, locked §9)**: the agent fills
the form and **stops**; a human reviews the screenshot and clicks Submit. There is **no
autonomous submit path**.

Two halves:
- **Deterministic spine** (`scripts/apply.py`) — builds the **apply packet** per `ready`
  job and, *after the human acts*, logs the outcome. It never opens a browser.
- **Browser driving** (the orchestrator, via the **chrome-devtools MCP**) — opens the
  posting, fills the form, and stops at the review gate.

## Prerequisites (apply-time)

- The **chrome-devtools MCP** must be configured (`.mcp.json`) — same server
  `chrome-screenshot-tester` uses. (Not installed in every environment; set it up before
  applying.)
- You must be **logged into LinkedIn** in that browser session (your own session — no
  credentials are stored by this app). If not logged in, log in first.
- Upstream done: the job is `ready` (has `tailored_resume_path` + `answers_json`).

## Procedure (LinkedIn Easy Apply — Option 1 gate: fill + screenshot, leave open)

1. **Build the packet.** `apply.py packet --limit 3` (or `--job N`). It prints, per job:
   the posting `url`, the tailored résumé path (`resume_abspath`), the `cover_letter`,
   each drafted `answers` field, `candidate_facts` (KNOWN personal facts from
   `candidate.json` — notice/CTC/relocation/work-auth — type these straight into matching
   screening questions), and `human_must_fill` (the facts STILL unknown — leave blank for
   the human, NEVER invent). `packet --source <portal>` filters to one portal; packets
   come **best-matched first**.
2. **Open the posting** with the chrome-devtools MCP; click **Easy Apply**.
3. **Fill each step** of the modal, reading the live page and mapping fields to the
   packet (`ANSWER_FIELD_HINTS` in `apply.py` suggests which answer fits which question).
   Attach/select the tailored résumé (`resume_abspath`); prefer LinkedIn's "use existing"
   if file upload isn't supported by the MCP. For a `human_must_fill` field, leave it
   blank/flagged for the human — do not guess.
4. **Screenshot** the filled review step with `chrome-screenshot-tester` so the human can
   verify exactly what will be submitted.
5. **STOP at the review gate.** Leave the browser on the final review step. Tell the human
   what you filled and what they must complete + submit. **Do NOT click Submit.**
6. **Human reviews + submits** (or decides to skip).
7. **Log the outcome:** `apply.py log --job N --outcome applied|skipped|failed [--note …]`
   → advances `ready → applied|skipped|failed` with `applied_at`. `log` refuses any job
   not at `ready` (guards against double-logging / logging an unreviewed job).

## Safety / rate limits (PLAN §6 — do not relax without a §9 decision)

- **Never auto-submit.** The orchestrator stops before Submit; the human clicks it.
- **Small, human-paced batches** (default 3 via `apply.py packet`); add delays between
  jobs; honor portal anti-bot signals. Don't bulk-apply.
- **No secrets stored** — uses your live logged-in browser session only.
- If a tailored résumé is missing or a job lacks answers, fix upstream (resume-tailor /
  humanise-responder) — don't apply with a generic résumé.

## Commands

```bash
.venv/bin/python .claude/skills/apply-agent/scripts/apply.py packet --limit 3   # packets for ready jobs
.venv/bin/python .claude/skills/apply-agent/scripts/apply.py show               # ready queue + apply log
.venv/bin/python .claude/skills/apply-agent/scripts/apply.py log --job 1 --outcome applied --note "Easy Apply, human submitted"
```

## Self-annealing

LinkedIn (and other portals) change their Easy-Apply DOM/flow; when a field mapping or
selector breaks, record the new pattern here + in PLAN.md §8/§9 with the date. Add new
common-question→answer mappings to `ANSWER_FIELD_HINTS`. New portals get their own apply
flow documented here (still behind the same human gate). The `log` status guard and the
no-submit rule are invariants — never remove them.
