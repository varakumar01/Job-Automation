@AGENTS.md
@SKILL.md

> **What this file is:** `CLAUDE.md` is **only the instruction file for you (the
> orchestrator)** — how to work, which agents/skills exist, and the operating rules.
> It is **not** the project's reference. The **main reference file is `.claude/PLAN.md`**
> — the source of truth for the product spec, architecture, decisions, build order,
> and the live build status (**PLAN.md §8 Build Tracker / TODO** — there is no
> separate `progress.md`). For planning anything, or any question about *what* to
> build, read `.claude/PLAN.md` first.
> # Research before coming to any conclusions!
> This file only tells you *how* to operate.

## What this project is

An **AI job-search & auto-apply system**. It scrapes jobs from portals (LinkedIn,
Naukri, Indeed in v1; extensible via a plugin system), tailors the master résumé
(`varakumar_resume.tex`) per job, understands each job/company, generates
human-sounding answers, and drives a browser to fill the application —
**human-in-the-loop: a person reviews and clicks submit; nothing is submitted
autonomously.** All work is Python scripts bundled inside Skills, orchestrated by
you, with the browser driven via the **Playwright MCP/CLI**.

Pipeline: `scrape → store → match/prioritize → understand JD → tailor résumé →
humanise answers → fill form → [HUMAN REVIEW + SUBMIT] → log outcome`. The local
SQLite store under `data/` is the spine — each job row carries a `status` so runs
resume after interruption. Full spec in `.claude/PLAN.md`.

## The Skills Architecture

**Layer 1: Skills (Intent + Execution bundled)**
Each Skill bundles SKILL.md instructions with its scripts. Claude auto-discovers
and invokes them based on task context. Shared rules for all skills live in
`.claude/SKILL.md`.

**Layer 2: Orchestration (Decision making)**
This is you. Your job: intelligent routing. Read SKILL.md, run bundled scripts in
the right order, handle errors, ask for clarification, update Skills with learnings.
You're the glue between intent and execution.

**Layer 3: Shared Utilities**
Common scripts and infrastructure code used across multiple Skills — notably the
`data/` store module and the job-source plugin base/registry.

**Why this works:** if you do everything yourself, errors compound. 90% accuracy
per step = 59% success over 5 steps. The solution is push complexity into
deterministic code. That way you just focus on decision-making.

## Codebase structure: use graphify

Do not document folder layouts, file purposes, or code structure in this file —
static explanations go stale. For any question about the codebase, its
architecture, or file relationships, use the **graphify** knowledge graph
(`/graphify`).

**The graph is built once real code exists** (after the foundation + first skill
land). If `graphify-out/graph.json` is present, **query it first** —
`graphify query "<question>"` — do NOT `find`/`grep` the tree blind. If it is not
yet present, build it with `/graphify`, then keep it updated with `/graphify --update`
whenever code changes.

**Key file locations (so you never have to ask):**
- Master spec / architecture / decisions / build order / **live status**:
  **`.claude/PLAN.md`** (note: capitalized, lives in `.claude/`, *not* repo root).
  The build status board is **PLAN.md §8** — there is **no `progress.md`**.
- Master résumé (single source of truth for all tailored variants):
  `varakumar_resume.tex` (compile via `make` / tectonic).
- Shared subagent rules: `.claude/AGENTS.md`. Shared skill rules: `.claude/SKILL.md`.
- Local job/pipeline store: `data/` (SQLite + JSON export).

## Subagents

Subagents are lightweight agents with self-contained contexts. They're cheaper,
unbiased (no parent context leakage), and keep the parent context clean. Shared
rules for all subagents live in `.claude/AGENTS.md`. All subagents run on the
latest Sonnet, except `code-reviewer`, which runs on Opus 4.6.

### Available Subagents
- `code-reviewer` - Unbiased code review with zero context. Returns issues by severity with a PASS/FAIL verdict.
- `code-tester` - Zero-context functionality tester. Derives a test plan from the spec, executes the code, and returns PASS or structured defect reports.
- `research` - Deep research via web search, file reads, and codebase exploration. Returns concise sourced findings.

### Design & Build Workflow

When building or modifying any non-trivial code (scripts, skills, scrapers, refactors), follow this loop:

1. **Write/edit the code** — Make your changes.
2. **Code Review** — Spawn `code-reviewer` subagent with the changed file(s). It reports issues back — it does NOT fix anything itself.
3. **Test** — Spawn `code-tester` subagent with the code and spec. It executes the code and reports results back — it does NOT fix anything itself.
4. **Fix** — The parent agent (you) reads the review and test reports and applies all fixes.
5. **Ship** — Only after review passes and tests pass. Then mark the item done in PLAN.md §8.

**Important:** Subagents are read-only reporters. All code changes happen in the parent agent.

**Brief every subagent fully — they start zero-context.** When you spawn one, hand it
all the information it needs to do the job without guessing: the exact file paths /
directory under test, the spec or expected behavior (how it *should* work), how to run
or reach it (commands, arguments, env, demo creds), the scope (what changed and where
it sits in the hierarchy), and what to check. A subagent that has to infer the task
will test or review the wrong thing. The briefing is your responsibility, not theirs.

For research-heavy tasks (a new portal's page structure, an Apify actor's
input/output schema, anti-bot constraints), spawn `research` subagent first to gather
context without polluting the main conversation.

**Parallel execution:** When reviewing + testing independent files, spawn both subagents in parallel using `run_in_background: true`.

## Automation / Skill Build Workflow

When building a skill or scraper, follow this process:

1. **Spec first in `.claude/PLAN.md`** — every skill has an entry in **§5 Skills
   catalog** defining its trigger, inputs, outputs, and which `status` it reads and
   writes on the `jobs` row. Plan there first, then build. **Always refer to PLAN.md
   before acting** (including **§9 Decisions log** for locked decisions), and pass the
   relevant §-sections + the specific code/files to any subagent you brief.
2. **Respect the pipeline contract** — a skill consumes rows in one `status` and
   advances them to the next (`scraped → matched → tailored → ready → applied |
   skipped | failed`). Never skip a stage or write a status a downstream skill can't
   handle. Persist after every job so an interrupted run resumes from the store.
3. **Push complexity into deterministic Python** — keep the SKILL.md for intent and
   decision-making; put parsing, API calls, storage, and form-filling logic in
   `scripts/`. Run them from the skill folder:
   `python3 .claude/skills/<skill>/scripts/<script>.py`.
4. **Adding a portal = one plugin file** — new job sources are added by dropping a
   `<site>.py` into `plugins/` (repo root — not under `.claude/`; plain scraping code
   with no Claude-specific dependency) implementing the `JobSourcePlugin` interface;
   the registry auto-discovers it. Document the addition in PLAN.md §4 / §10. Never
   hardcode a portal anywhere else.

## Operating Principles

**0. Research before you respond — never assume**
Before answering or acting, gather the actual facts: query graphify (when built), read
`.claude/PLAN.md`, read the relevant code, or spawn the `research` subagent. Do not
answer from memory or assumption about how the codebase works — verify against the
graph and the files. State what you found, not what you guessed.

**0.1. Think downstream before every change**
When a change touches data that flows through the pipeline, always ask: *which other
skills and stages read or write this data?* Map every read and write path before
writing a single line. For example: adding a field to the `jobs` schema means
checking the store module, the scraper plugins that populate it, profile-matcher,
jd-understander, resume-tailor, humanise-responder, apply-agent, and any JSON
export/report — all may need updating. State the full map explicitly, note what is
already covered, and only then build the missing pieces. Never treat a change as done
until every stage that shows or collects that data has been audited.

**0.2. "Covered" means actually exercised, not code-present**
When auditing the pipeline, never mark a stage as covered because the field exists in
the schema. Ask: *will this field actually be populated and read when a real job flows
through?* Check for conditions that gate it (a plugin that never sets it, a status the
job never reaches, an Apify field absent in "standard" vs "detailed" mode, an
empty-result branch). A field written by a path no real job triggers is the same as a
missing field. For every stage marked "covered," state the exact condition under which
the field is populated/read — if you cannot state it, go read the code. Do not trust
plan history or prior session summaries — verify against the live file.

**0.3. Every user-requested change is a durable spec entry — capture it immediately**
Whenever the user requests *any* change — a portal to add, a résumé-tailoring rule, a
matching weight, a form-field mapping, a default, a validation, a safety/rate-limit
rule, a flow behavior — treat it as a durable spec decision, not a one-off edit. The
full loop:

1. **Apply** the change in code.
2. **Persist** — before marking the task done, append the decision to **`.claude/PLAN.md` §9
   (Decisions log)** at element-level granularity. Use the format:
   `YYYY-MM-DD — <skill/surface> — <element> — <decision> [revises §X]`.
3. **Consult §9 before every build** — re-read the decisions log and apply every entry
   that touches the skill being built. The log overrides the original §1–§7 spec.
4. **Brief subagents with relevant §9 entries** so they build to the latest decision.

**0.4. Safety gate — never auto-submit, never leak secrets**
The apply flow is **human-in-the-loop by design**: `apply-agent` fills the form and
stops at a review gate; a human inspects (use `chrome-screenshot-tester` to verify the
filled form) and clicks submit. Do **not** add a fully-autonomous submit path, and do
not raise scrape/apply rates past the conservative limits in PLAN.md §6 — these
mitigate portal ToS / account-flag risk. Secrets (`APIFY_TOKEN`, model keys, portal
sessions) come from `.env` and are never hardcoded or committed.

**1. The parent agent owns execution**
You (the parent agent) always edit the code yourself — never delegate code changes.
You manage the subagents: spawn them, read their reports, and act on the findings.
You invoke the necessary Skills for the task at hand rather than improvising the
work they already cover.

**2. Track every task in `.claude/PLAN.md` §8**
PLAN.md §8 (Build Tracker / TODO) is the resume file and the single status board —
**there is no `progress.md`.** As you work, **mark tasks done / in-progress there** —
flip an item to in-progress when you start it and to done (`[x]`) only after the
review + test loop passes. Keep it current so a fresh session can continue without
re-deriving state.

**3. Skills auto-activate**
Claude picks the right Skill based on your request. Each Skill's description tells Claude when to use it.

**4. Self-anneal when things break**
Read the error, fix the script, test it again, then update the respective
SKILL.md or agent file (and the shared `.claude/SKILL.md` / `.claude/AGENTS.md`
when the lesson applies broadly) with what you learned. Scrapers and form-fillers
break when portals change their markup — when you fix one, record the new
selector/schema and the date in the plugin and in PLAN.md. If something is broken
and you fix it, updating the respective file with the corrected rule is not
optional — that is how the system gets stronger.

**5. Update Skills as you learn**
Skills are living documents. When you discover API constraints (Apify actor quirks,
rate limits), better approaches, or edge cases — update the SKILL.md. But don't create
new Skills without asking. If a rule, skill, or agent causes repeated mistakes, update
its file.

## Order of Work

The authoritative build order is **`.claude/PLAN.md` §7 (Build Phases)**; the live
done/pending breakdown is in **`.claude/PLAN.md` §8 (Build Tracker / TODO)**. Always
read both before picking up work — don't infer order from memory. Sequencing rules
that override the raw phase list:

1. **One workflow at a time.** Finish a skill end-to-end (build → review → test →
   fix → mark done in §8) before starting the next. Don't leave half-wired skills.
2. **`apply-agent` is built LAST** — it depends on every upstream skill (matcher,
   jd-understander, resume-tailor, humanise-responder) being done, and it's the
   highest-risk surface (it touches real employer forms). This is a locked decision
   (PLAN §9).
3. **Remaining order** (per PLAN §7, owner can re-prioritize): docs/repurpose →
   foundation (store + plugin base + env + requirements, then build graphify) →
   `job-scraper` → `profile-matcher` → `jd-understander` → `resume-tailor` →
   `humanise-responder` → `apply-agent` (last) → extensibility pass (new-portal plugin).
4. Before each task, **confirm the target with the user** if priority is ambiguous,
   then mark it in PLAN.md §8 and brief subagents with the matching PLAN.md section.

## Summary

You work with Skills that bundle intent with execution. Research first (graphify when
built + PLAN.md), make decisions, edit the code yourself, run the review→test loop
with subagents, track status in PLAN.md §8, and continuously improve the system. For
anything about project structure, ask graphify.

Be pragmatic. Be reliable. Self-anneal.
