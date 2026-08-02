# PLAN.md — AI Job-Search & Auto-Apply System

> **This is the master reference.** `CLAUDE.md` says *how to operate*; this says
> *what to build*. The live status board is **§8 (Build Tracker / TODO)** — there is
> **no `progress.md`**. Locked decisions live in **§9** and override the spec above
> them. Read §8 + §9 before picking up any work.

---

## §0 Context & Goals

This repo began as a résumé/LaTeX workspace (master `varakumar_resume.tex`, a tectonic
`Makefile`, `LINKEDIN_SUGGESTIONS.md`). It is being built into an **AI system that
finds and applies to jobs**.

**Goals**
1. **Source** jobs from multiple portals (v1: LinkedIn, Naukri, Indeed), extensible to
   more via a plugin system.
2. **Prioritize** which jobs/role-profiles best fit the master résumé with the *fewest*
   changes.
3. **Tailor** the résumé per job from the master `.tex`, compiled to PDF.
4. **Understand** each job/company (what they do, what they want, likely tools) from the
   JD + web/Glassdoor/AmbitionBox.
5. **Answer** screening questions / cover letters in a natural, human voice.
6. **Apply** by driving a browser to fill the form — **human reviews & submits**.
7. **Track** every job through the pipeline in a durable store so runs resume after
   interruption and output is saved after each job.

**Non-goals (v1):** fully-autonomous submission; a web dashboard; portals beyond the
v1 three (added later via plugins).

---

## §1 Architecture & Data Flow

```
                ┌─────────────┐
   portals ──▶  │ job-scraper │  (Apify actors + custom plugins)
                └─────┬───────┘
                      ▼  normalize
                 ┌─────────┐
                 │  data/  │  SQLite store (jobs table = pipeline spine) + JSON export
                 └────┬────┘
   per job, advancing `status`:
   scraped → [profile-matcher] → matched → [jd-understander] (writes jd_brief)
           → [resume-tailor] → tailored (the apply gate — résumé decided)
           → [apply-agent] → fills form → ⏸ HUMAN REVIEW + SUBMIT
           → applied | skipped | failed  (outcome logged immediately, per job)

   (No separate `ready` stage — retired 2026-07-11, §9. humanise-responder
   pre-drafted screening answers before that; still runnable manually, but no
   direct-apply automation is ever planned, so nothing consumes pre-drafted
   answers automatically anymore.)
```

The orchestrator (Claude) routes between skills; the **browser is driven via the
Playwright MCP/CLI** (chosen for accessibility-tree, token-efficient, auto-wait
behavior). `chrome-screenshot-tester` verifies the filled form before the human approves.

---

## §2 Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | **Python 3** | Matches SKILL.md `python3` convention; great for AI + scraping. |
| Browser automation | **Playwright (MCP/CLI)** | Top pick (research). Used by `apply-agent` and any custom scraper plugin. |
| Managed scraping | **Apify** actors via REST/`apify-client` | LinkedIn / Naukri / Indeed actors exist; paid per-listing; `APIFY_TOKEN` in `.env`. |
| Résumé build | **tectonic** via existing `Makefile` | `make` compiles `<resume>.tex → .pdf`. |
| Store | **SQLite** (stdlib `sqlite3`) + JSON export | Single-file, zero-dep, resumable. |
| Secrets/config | **`.env`** (`python-dotenv`) | `APIFY_TOKEN`, model API keys, portal session paths. Never committed. |
| Deps | `requirements.txt` | apify-client, playwright, python-dotenv, etc. |

---

## §3 Data Model & Store

Single SQLite DB at `data/jobs.db` (+ a `data/jobs.json` export for inspection). One
`jobs` table is the pipeline spine; a small `runs` table logs scrape/apply sessions.

**`jobs`** (representative columns):

| Column | Meaning |
|---|---|
| `id` | internal PK |
| `source` | portal name (`linkedin`/`naukri`/`indeed`/…) |
| `ext_id` | portal's job id (dedupe key with `source`) |
| `url` | job posting URL |
| `title`, `company`, `location`, `posted_at` | normalized fields |
| `jd_text` | raw job description |
| `jd_brief` | jd-understander output (company/role/tools summary) |
| `match_score` | profile-matcher score (0–100) + chosen role-profile |
| `status` | `scraped → matched → tailored → applied \| skipped \| failed` (no separate `ready` stage — retired 2026-07-11, §9) |
| `tailored_resume_path` | path to the per-job compiled PDF |
| `answers_json` | humanise-responder answers (cover letter, screening Qs) — optional now; the skill is no longer part of the active pipeline |
| `applied_at`, `outcome`, `notes` | apply-agent result, written per job |

**Dedup:** unique `(source, ext_id)`. **Resumability:** every skill selects rows by
`status`, processes, and commits per row — kill mid-run and re-run to continue.

Store module (shared utility) exposes: `init_db()`, `upsert_jobs()`,
`get_jobs(status=…)`, `update_job(id, **fields)`, `export_json()`.

---

## §4 Plugin System (Job Sources) — how to add a portal

Location: `plugins/` (repo root — moved out of `.claude/` 2026-07-07, see §9; plain
scraping code with no Claude-specific dependency, sibling to `data/`/`execution/`).

- **`base.py`** defines a `Job` dataclass (normalized schema matching §3) and an
  abstract `JobSourcePlugin`:
  - `name: str`
  - `is_available() -> bool` — credentials/actor reachable?
  - `fetch(query: str, limit: int) -> list[Job]` — search + normalize.
- **`registry.py`** auto-discovers every `JobSourcePlugin` subclass in the folder, so
  **adding a portal = dropping one `<site>.py` file** — no other code changes.
- **Apify-backed plugins** (`linkedin.py`, `naukri.py`, `indeed.py`) call the Apify
  *Run Actor* endpoint with `APIFY_TOKEN` and map actor output → `Job`.
- **Custom plugins** (for portals Apify doesn't cover) implement the same interface
  using a Playwright logged-in session.
- Each plugin **checks availability / "are there jobs for this query"** before a full
  scrape, then returns normalized rows the scraper stores.

To add a portal: create `plugins/<site>.py`, implement the three members, list it
here and in §10, and smoke-test that the registry picks it up.

---

## §5 Skills Catalog

Each skill is `.claude/skills/<name>/` (SKILL.md + Python `scripts/`). The contract is
**read rows in one `status`, advance to the next**.

| # | Skill | Trigger (intent) | Reads | Writes | Status in → out |
|---|---|---|---|---|---|
| 1 | **job-scraper** | "find/scrape jobs for <query>" | portals (Apify + plugins) | normalized `jobs` rows | — → `scraped` |
| 2 | **profile-matcher** | "rank/prioritize scraped jobs" | master `.tex`, `jobs` | `match_score`, role-profile | `scraped` → `matched` |
| 3 | **jd-understander** | "understand this job/company" | `jd_text`, web/Glassdoor/AmbitionBox | `jd_brief` | `matched` → (brief set) |
| 4 | **resume-tailor** | "tailor résumé for this job" | master `.tex`, `jd_brief` | tailored `.tex`+PDF, `tailored_resume_path` | → `tailored` |
| 5 | **humanise-responder** *(retired from the active pipeline 2026-07-11, §9 — manual-only)* | "draft answers / cover letter" (explicit manual request only) | `jd_brief`, résumé | `answers_json` | `tailored` → `tailored` (no status change) |
| 6 | **apply-agent** | "apply to tailored jobs" | `tailored` rows + artifacts | fills form, `outcome`, `applied_at` | `tailored` → `applied\|skipped\|failed` |

**Kept existing skill:** `chrome-screenshot-tester` — used by `apply-agent` to verify
the filled form before the human approves. It stays (generic + directly useful); no
existing skill needs removing.

**profile-matcher detail:** scores each job by *how few edits the master résumé needs*
to fit it (keyword overlap, required skills present vs missing, seniority/role fit),
emits a ranked apply-list, and records the best-fit role-profile (e.g. Red Team,
Detection Eng, Cloud Sec — see README variants) to guide resume-tailor.

---

## §6 Apply Flow & Safety / ToS

- **Human-in-the-loop is mandatory.** `apply-agent` opens the posting, fills the form
  with the tailored PDF (+ `answers_json` if any is on file — usually none now that
  humanise-responder is retired from the active pipeline, §9), screenshots it
  (`chrome-screenshot-tester`), then **stops at a review gate**. A human inspects and
  clicks submit. No fully-autonomous submit path exists in v1.
- **Per-job logging:** the moment the human acts (or skips), the `outcome`/`applied_at`
  is written to the store — so progress is never lost.
- **ToS / account-flag risk:** automated interaction with LinkedIn/Naukri can violate
  ToS. Mitigations: human gate, **conservative rate limits** (small batches, delays
  between actions), use of the **user's own logged-in browser session** (no credential
  storage beyond the session), and honoring portal robots/anti-bot signals. Do not
  raise these limits without a §9 decision.
- **Secrets:** `APIFY_TOKEN`, model keys, and session paths from `.env` only.

---

## §7 Build Phases (one skill finished end-to-end before the next)

1. **Docs / repurpose** — rewrite `CLAUDE.md`, author this `PLAN.md` (incl. §8). *(no code)*
2. **Foundation** — `.env.example`, `requirements.txt`, `data/` store + schema, plugin
   `base.py` + `registry.py`. Then **run `/graphify`** (graph is meaningful once code exists).
3. **job-scraper** — Apify LinkedIn/Naukri/Indeed + custom-plugin scaffold → review → test (live small scrape) → fix.
4. **profile-matcher** — ranked apply-list.
5. **jd-understander** — JD briefs.
6. **resume-tailor** — LaTeX → PDF per job.
7. **humanise-responder** — answers.
8. **apply-agent** — Playwright + human-in-the-loop + per-job logging. **Last.**
9. **Extensibility pass** — document + smoke-test adding a new portal plugin.

---

## §8 Build Tracker / TODO  *(replaces progress.md — the single status board)*

Legend: `[ ]` pending · `[~]` in progress · `[x]` done (review + test passed).

**Phase 1 — Docs / repurpose**
- [x] Rewrite `.claude/CLAUDE.md` for the job-search domain
- [x] Author `.claude/PLAN.md` from scratch (this file)
- [x] Decision: no `progress.md` — status lives here in §8

**Phase 2 — Foundation**
- [x] `.env.example` (`APIFY_TOKEN`, model keys, session path)
- [x] `requirements.txt` (apify-client, playwright, python-dotenv, …)
- [x] `data/` store module + schema (`jobs`, `runs`) + JSON export — self-tested (dedup, guarded updates, export)
- [x] plugin `base.py` (`Job`, `JobSourcePlugin`) + `registry.py` — auto-discovery verified (script + package modes)
- [~] Run `/graphify` to build the codebase graph — **DEFERRED by owner 2026-06-30** (not a priority now). graphify isn't installed; treat as optional/skipped until the owner asks. Do not pick this up as pending work.

**Phase 3 — job-scraper**  *(token added 2026-06-30; venv at `.venv`, deps installed)*
- [x] Apify adapter: LinkedIn (`curious_coder/linkedin-jobs-scraper`) — live-verified, 100% field coverage incl. jd_text
- [x] Apify adapter: Naukri (`muhammetakkurtt/naukri-job-scraper`) — live-verified; nested `jobDetails`, searchUrl for location
- [x] Apify adapter: Indeed (`borderline/indeed-scraper`) — live-verified; dict location flattened, `datePublished`
- [x] Custom-plugin scaffold (`plugins/_custom_template.py`, Playwright-session template)
- [x] Multi-key Apify rotation + health indicator (`plugins/_apify_keys.py`; `run_actor` rotates by health, `scrape.py --keys`/`--reset-keys`). Review+test loop PASSED. code-tester 25/25 (incl. secret-safety); code-reviewer PASS, 2 MAJOR fixed (tightened `classify_error` so rate-limit/timeout/`token_count` no longer dead-key all keys; 429→transient; scrub token from persisted `last_error`) + 403-permission→invalid + redundant-guard comments.
- [x] SKILL.md + normalize/store wiring (`scripts/scrape.py`)
- [x] Review + test loop — code-tester 89/89 PASS; code-reviewer FAIL→fixed (3 MAJOR: `--source all` error isolation, `derive_ext_id` non-string-url guard, corrected cost comments; +2 MINOR/3 NIT). Re-validated. Live scrape passed — 16 rows in `data/`.

**LLM groundwork (shared)**
- [x] `execution/llm.py` multi-mode provider (`LLM_PROVIDER=session|api|grok`); `LLM_PROVIDER`/`LLM_MODEL`/`ANTHROPIC_API_KEY`/`XAI_API_KEY` in `.env`; `anthropic` optional dep (api only), grok via stdlib urllib (no dep). Provider dispatch + missing-key/unknown-provider paths smoke-tested.

**Phase 4–7 — Pipeline skills**
- [x] profile-matcher (ranked apply-list) — deterministic; review+test loop PASSED. code-tester 15/15; code-reviewer PASS, 2 MAJOR fixed (seniority substring→word-boundary `_present`; LaTeX `\&` unescape so ATT&CK is recognized) + MINOR (comment-line filter in techrow parse) + `--rescore` added. Re-scored 19 matched rows in place; jd_briefs preserved.
- [x] jd-understander (jd_brief) — first consumer of the session/api/grok LLM provider. Review+test loop PASSED. code-tester 14/14 (session prepare/save/show/run-guard, resumability, error paths, `_extract_json`/`_normalize_brief` units); code-reviewer PASS, MINOR fixed (list-format malformed entry skipped not crashed) + NIT comment. Status stays `matched` (resume-tailor advances).
- [x] resume-tailor (.tex → PDF via tectonic) — LLM-driven directive splice into a master COPY (master untouched), per-job variant store with reuse, auto-build via tectonic. code-reviewer PASS; 1 MAJOR fixed (build errors `FileNotFoundError`/`TimeoutExpired` now caught per-job in save/run via `OSError`+`TimeoutExpired`, run splits LLM-error→break vs build-error→continue) + MINOR (summary newline-collapse, tagline-role escaping, removed dead `_apply_one`/redundant `pending_ids`). code-tester cut off by a platform session-limit (infra, not code); behaviors independently verified by parent: real tectonic compile (22KB), session prepare→save on real DB (jobs 1&2 tailored, master PDF unchanged), reuse, resumability, run-guard, build-missing resilience, LaTeX-escape.
- [x] humanise-responder (answers_json) — LLM-driven (session/api/grok), honest answers grounded in candidate profile + jd_brief; CTC/notice/relocation → `screening_todo` (never fabricated). Review+test loop PASSED: code-tester 21/21, code-reviewer PASS. Fixed: 2 MAJOR (save guards `JSONDecodeError`; `_candidate_profile` techrow `re.match` None-guard) + MINOR (string-aware brace scan in `_extract_json` for trailing prose; LaTeX `_clean` keeps `3{,}200`/textbf/emdash; strip `%` comment lines; `--limit 0`) + programmatic anti-fabrication flag (pay/notice/date mentions → REVIEW note in screening_todo). Job 1 reached `ready` on real DB.

**Phase 8 — apply-agent (last)**
- [x] Form-fill + review gate (no auto-submit) — deterministic spine
  (`scripts/apply.py`: `packet`/`show`/`log`) never opens a browser/submits; browser
  driving is orchestrator-via-chrome-devtools-MCP per SKILL.md (Engine: chrome-devtools
  MCP, not Playwright — more accurate for the dynamic LinkedIn Easy Apply modal). Review
  gate = fill + screenshot, leave open; HUMAN submits. Review+test loop PASSED:
  code-reviewer PASS (safety invariants upheld: zero browser/network/submit code; `log`
  guard unbypassable), code-tester 12/12. Fixed 3 MINOR (`--limit 0` semantics;
  `applied_at` only for `applied`; added `store.get_job` to replace full-table scan) +
  2 NIT (ANSWER_FIELD_HINTS comment; `show` displays notes).
- [x] Per-job outcome logging — `log --job N --outcome applied|skipped|failed` writes
  `outcome`+`applied_at`, advances `ready → applied|skipped|failed`. Verified.
- [x] No-auto-submit verification — spine has no browser/submit path; `log` REFUSES any
  non-`ready` job (no double-log / no logging an unreviewed job); invalid outcome
  rejected; tested on a temp DB (job 1 applied; real DB untouched). Review+test running.

**Phase 10 — Orchestrator + control panel (`main.py`)**  *(owner request 2026-06-30)*
- [x] **Search transparency + `--reset` + parallel scraping** *(owner request 2026-07-10:
  "I never seen any stats about other aggregators getting scrapped" / "lets use multi
  threading... wait for all the processes to complete" / "lets have options --reset" /
  "always make a proper help page!")*: root cause of the missing-aggregator-stats
  complaint — `main.py search`'s `--source` defaulted to `linkedin` (only one portal),
  and `cmd_search` spawned one `scrape.py` subprocess per query×location combo, each
  scoped to that single source; the ~35 other plugins never ran unless `--source all`
  was passed explicitly, and even then unavailable/errored plugins vanished from output
  with no reason shown. Fixed:
  - `plugins/base.py`: `JobSourcePlugin` gained `base_url`, `mechanism`, and
    `availability_detail()` (default `"check creds"`, overridden on the 6 gated plugins —
    `linkedin`/`naukri`/`indeed`/`wellfound`/`careerhound`/`nextgenenergyjobs` — to name
    the exact missing dependency). Populated on all ~39 discovered plugins.
  - `scrape.py` rewritten: single-process plugin×query×location matrix (new
    `--queries`/`--locations`, comma lists), fetched via `ThreadPoolExecutor`
    (`--workers`, default 8) — one thread per plugin, each plugin's own combos still
    sequential (politeness, never hit one domain concurrently). DB upserts serialized
    on the main thread only after every fetch thread finishes. Ends every run with a
    SOURCE REPORT covering every discovered plugin (available or not) — domain,
    mechanism, outcome (✓ ok/◐ partial/∅ empty/✗ error/⚠ unavailable+reason). Live
    per-plugin progress prints by default (not `-v`-gated) since a full run can take
    minutes.
  - `main.py`: `search --source` default `linkedin` → `all`; `cmd_search` now makes ONE
    `scrape.py` call instead of the old per-combo subprocess loop; new `--workers`
    passthrough. New `reset` command (`store.reset(hard)`): clears `jobs`+`runs` tables +
    `jobs.json`, confirms interactively unless `--yes`; `--hard` also empties `tailored/`
    + `applications/`. Every subparser + the hand-written `HELP_DESC`/`EXAMPLES` block
    reviewed and corrected (stale `--days` doc, invalid positional-arg EXAMPLES that never
    matched the real argparse flags, undocumented commands).
  - `data/store.py`: `connect()` gained WAL mode + 30s busy timeout; new `reset()`.
  - **Bugs found live during this work** (not pre-existing findings, discovered by
    actually running `--source all` end-to-end for the first time): (1) `wellfound.py`
    and `careerhound.py` both launch a Playwright *persistent* context on the same
    `PLAYWRIGHT_USER_DATA_DIR` profile — running them concurrently (as the new threading
    does) hung on the profile lock; fixed with a `uses_persistent_profile` flag +
    `scrape.py`-level lock that only engages when that profile dir actually exists
    (narrowed further after review — see below). (2) `workday.py` had NO cap on
    detail-page fetches (every other multi-fetch plugin, e.g. `successfactors.py`, caps
    at `_MAX_DETAIL_FETCHES=20`) — for a broad query against a large tenant (e.g. Cisco,
    live-verified: `searchText` server-side matching is loose and the shared `matches()`
    helper is substring-OR, so "Engineering" trivially matches "engineer"), this did an
    unbounded number of sequential detail HTTP calls per company; fixed with the same
    `_MAX_DETAIL_FETCHES=20` cap. Even fixed, `workday`/`greenhouse`/`lever` (39/75/44
    configured companies, each a sequential per-company HTTP round trip) remain the
    slowest plugins in a `--source all` run — inherent to the many-company ATS-plugin
    design (not a threading bug), flagged in §10 as a possible follow-up (parallelize
    company iteration WITHIN a plugin).
  - Review+test loop PASSED. code-tester 13/13 (reset confirm/abort/`--yes`/`--hard`,
    post-reset empty stats, help pages, `--list` reasons, unknown-source error,
    missing-query error, multi-query×location matrix) — real DB backed up before
    destructive tests, restored and verified after. code-reviewer PASS; 1 MAJOR fixed
    (`store.reset()` crashed if `sqlite_sequence` didn't exist yet — fresh/never-inserted
    DB; guarded with an existence check) + 3 MINOR fixed (`connect()` docstring wrongly
    implied worker threads write to the DB; `_PROFILE_LOCK` coarsely serialized
    wellfound's entire fetch including its Apify-only path that never touches the shared
    profile — narrowed to only lock when `PLAYWRIGHT_USER_DATA_DIR` actually exists;
    `successfactors.py`'s `base_url` named the wrong domain, `*.successfactors.com`
    instead of the real `*.jobs.hr.cloud.sap`) + 1 NIT (pointless f-string in
    `careerhound.py`). Live-reverified end-to-end: `main.py search --source arbeitnow
    --queries "security engineer" --locations Remote --limit 2` → single scrape.py call →
    SOURCE REPORT → matcher → auto-reject → classified lists, all correctly chained.
- [x] **Local control-panel web app + static public dashboard + CI/hosting**
  *(owner request 2026-07-10: "make a proper front end!" — shadcn/ui + Phosphor icons,
  minimal search bar, session-only env, advanced-options editors, cmd panel; "make it
  hostable via github pages and auto compile into master branch" — pivoted to Netlify,
  see decision below)*:
  - **`execution/prompts.py`** (new) — `load_prompt(name, default)` reads
    `prompts/<name>.txt` if present/non-empty else the skill's own inline default
    (zero behavior change until a user edits one); `get_or_seed`/`save_prompt` back the
    API's edit UI. Wired into `jd-understander/understand.py`, `humanise-responder/
    respond.py`, `profile-matcher/llm_rank.py` — each's `SYSTEM_PROMPT` constant renamed
    to `_DEFAULT_SYSTEM_PROMPT`, with `SYSTEM_PROMPT = load_prompt(...)` added.
  - **`server/app.py`** (new) — FastAPI backend, `main.py serve [--host] [--port]
    [--reload]` launches it via uvicorn (binds `127.0.0.1` only — it runs subprocesses,
    edits `.env`, compiles the résumé, so it must never be exposed beyond localhost).
    Thin wrapper, no logic duplication: `GET /api/jobs|stats|sources|health`; `POST
    /api/search` streams `scrape.py`→`match.py`→`_auto_reject()` live over SSE (one
    `line` event per subprocess stdout line, `exit` per subprocess, final `done` with
    stats — guarded by an `asyncio.Lock` so two concurrent searches can't interleave
    output or race the store); `POST /api/reset` wraps the existing `store.reset`;
    `GET/POST /api/env` — session-only by default (this process's `os.environ`, inherited
    by its subprocess children, gone on restart), `persist: true` atomically rewrites
    `.env` (temp file + `os.replace`) preserving every other line, secret-looking keys
    (`TOKEN|KEY|SECRET|PASSWORD`) always masked on GET (verified live: zero full-secret
    leaks against the real `.env`); `GET/POST /api/prompts` (defaults cached per
    process — see review fixes below) and `/api/profile` (`candidate.json`) and
    `/api/resume` (+`/api/resume/pdf`, real `tectonic` recompile).
  - **`web/`** (new) — Vite + React 19 + TS, shadcn/ui on **base-ui** (not Radix — a
    2026-07 shadcn CLI default; primitives use base-ui's `render=` prop, not `asChild`),
    Tailwind v4, `@phosphor-icons/react`. Search bar centered above the vertical middle
    (not the very top, per owner request) with locations/source/days/limit/workers
    controls; live CMD panel (SSE-driven, per-line as it's produced — a full
    `--source all` run can take minutes, so this is the difference between "running"
    and "looks hung"); per-source status grid; job results list; an Advanced-options
    dialog with Env/Prompts/Profile/Résumé tabs (résumé tab is a `.tex` editor + compile
    + live PDF preview via an iframe). `web/src/lib/api.ts` includes a hand-rolled SSE
    parser (native `EventSource` can't do a POST-with-JSON-body request).
  - **Static public dashboard (Phase 3)** — same `web/` codebase, second build mode:
    `npm run build:static` (Vite `--mode static`, outputs `dist-static/`) renders
    `StaticApp.tsx` instead of `App.tsx` (branched on `import.meta.env.MODE` in
    `main.tsx`) — read-only, no backend/secrets/live commands, just a client-side
    filter over a bundled JSON snapshot. New `data/store.export_public_json()` /
    `python3 data/store.py export-public` produces that snapshot: only
    `matched|tailored|ready|applied` rows, and only display-safe columns (drops
    `jd_text`/`jd_brief`/`answers_json`/`notes`/local file paths — the full `jobs.json`
    is 4.4 MB with full JD prose per row, not fit for public publishing). The snapshot
    is committed at `web/public/jobs.public.json` (tracked, unlike `data/jobs.json`) —
    the deploy workflow builds from whatever is checked in, never a live DB.
  - **CI/hosting (Phase 4)** — checked the plan's own "purge committed secrets" blocking
    prerequisite against real git history (`git log --all` for `.env`/`candidate.json`/
    `data/apify_keys.json`/`data/grok_keys.json`) and found **none were ever
    committed, at any point, on any branch** — the blocker was a false alarm (raised
    defensively from file *existence* during planning, never confirmed against actual
    git tracking); no history rewrite needed. `.github/workflows/deploy.yml` — on push
    to `master` (paths: `web/**`), builds the static dashboard from the committed
    snapshot and deploys to **Netlify** (`nwtgck/actions-netlify@v3`; needs
    `NETLIFY_AUTH_TOKEN`+`NETLIFY_SITE_ID` repo secrets, not yet configured — owner
    chose not to deploy live yet). `.github/workflows/scrape.yml` — owner-requested
    scheduled scrape (daily cron + `workflow_dispatch`), runs `main.py search` (all ~39
    plugins; Apify-backed ones only bill if `APIFY_TOKEN` secret is added — left unset by
    default, workflow is otherwise free), exports + commits the refreshed public
    snapshot, whose push auto-triggers `deploy.yml`.
  - Review+test loop PASSED. code-tester 13/13 on `server/app.py` (health/stats/sources
    with specific unavailability reasons/jobs incl. 400-on-bad-status/SSE search
    completing in 1.2s with correct line+exit+done events/env masking verified via a
    real secret-leak scan — zero leaks/env persist appending exactly one line, zero
    other changes/prompts+profile+resume GET-POST round trips incl. real PDF
    recompile/reset — every destructive test backed up beforehand and restored+verified
    byte-identical after). code-reviewer PASS (first attempt hit the session API rate
    limit mid-review and was retried): 3 MAJOR fixed (`_stream_search` had no
    concurrency guard — two overlapping searches could interleave SSE output and race
    `_auto_reject`/store writes — added the `asyncio.Lock`/409 above; `_persist_env_var`
    truncate-then-write could corrupt `.env` on a crash mid-write — made atomic via
    temp-file + `os.replace` (caught my own follow-up bug here: `Path.with_suffix()` on
    a dotfile like `.env` produces `.env.env.tmp`, not `.env.tmp` — fixed to plain string
    concat); `/api/prompts` GET re-executed all 3 skill scripts' full module top level
    (imports, dotenv load, sys.path mutation) on every single request just to read a
    constant — added a process-lifetime cache) + 4 MINOR fixed (SSE client parser
    joined multiple `data:` lines with no separator, violating the SSE spec's
    newline-join rule — latent, server never emits multi-line data today, fixed anyway;
    `get_or_seed` in `prompts.py` skipped the name validation `save_prompt` had, a
    defense-in-depth gap; `_known_env_keys`'s comment-stripping was correct but
    undocumented — annotated; `"set": bool(os.environ.get(k))` misreported a
    deliberately-empty-string env var as unset — changed to `k in os.environ`) + 1 NIT
    (redundant generator wrapper in `post_search`) + 2 noted-but-left (multi-tab abort
    cleanup in `App.tsx` — added defensively; `EnvTab`'s persist-checkbox not resetting
    after save — reviewer judged current behavior reasonable). All fixes re-verified
    live: 409 confirmed on concurrent `POST /api/search`, atomic `.env` write confirmed
    (no leftover `.tmp`, diff-clean restore), frontend typecheck+build clean after every
    fix. **Visual/browser testing of `web/` is NOT yet done** — `chrome-devtools` MCP
    (added via a new root `.mcp.json`) needs a session reconnect to pick up its
    `--executablePath` (pointed at Playwright's bundled Chromium, since no system
    Chrome/Chromium exists in this environment) after being set; deferred rather than
    repeatedly blocking on restarts mid-session. Local pipeline is otherwise fully
    live-verified: `main.py serve` (FastAPI on :8000), `web/` dev server (:5173, proxies
    `/api`), and the static preview (:4173) all confirmed serving real data via curl.
- [~] `main.py` single entrypoint: `search` (multi-area/query/`--days`, newest-first),
  `lists` (eligible-as-is vs needs-mod), `prep --llm claude|grok|api [--modify-resume]`,
  `apply`, `log --screenshot`, `report` (dashboard + `applications/<id>/` artifacts), plus
  control panel `keys`/`sources`/`stats`/`rank`. Schema `screenshot_path` + migration;
  resume-tailor `--no-modify` passthrough; Groq provider live-validated (UA header fix).
  Deterministic commands tested on real data; Groq automation validated on a temp DB.
  Review+test loop pending.
- [x] **Candidate details + apply ergonomics** *(owner request 2026-06-30)*:
  `execution/candidate.py` + `candidate.json` (gitignored; `candidate.example.json`
  template) supply personal facts → humanise-responder auto-fills known facts &
  deterministically lists only the unknown ones in `screening_todo`; apply packet
  carries `candidate_facts` and drops now-answered items from `human_must_fill`
  (`covered_labels`). `apply --source <portal>` + best-matched-first ordering; new
  `main.py applied` log; `lists` now three lists (scraped/best/needs-mod, `--raw`).
  Review+test loop PASSED: code-tester 21/21, code-reviewer PASS (no BLOCKER/CRITICAL);
  fixed the stale-`human_must_fill` MINOR + 2 NITs (`args.raw`, §9 "three lists").
- [x] **`--tags` form + clean help + job-selection** *(owner request 2026-07-01)*:
  commands accept `--<cmd>`; `-h` de-duplicated (single reference, no auto-list);
  `store.parse_ids`; `--jobs`/`--query` on apply, `--jobs`/`--eligible` on prep threaded
  through all prep skills (grok preps only the best). Years-honesty rule in responder.
  Review+test loop PASSED: code-tester 28/28, code-reviewer PASS (no BLOCKER/CRITICAL);
  fixed 2 MINOR (`--limit 0` truthy checks in main.py + understand/tailor `_pending_jobs`)
  + 2 NITs (explicit usage line, `parse_ids` de-dupe). Reverted 4 stray `tailored` rows a
  test left on the real DB back to `matched`.
- [x] **NVIDIA NIM provider** *(owner request 2026-07-01)*: `--llm nvidia` added to `rank` and `prep` commands. Routes through the existing `grok` OpenAI-compatible backend (same pattern as DeepSeek). Default model `nvidia/llama-3.3-nemotron-super-49b-v1`, backup `meta/llama-3.3-70b-instruct` (auto-used via `LLM_BACKUP_MODEL` on model-specific failures, not auth/rate errors). `NVIDIA_API_KEY` in `.env`/`.env.example` (gitignored). `GROK_API_KEY` explicitly cleared in both DeepSeek and NVIDIA branches to prevent key pool leak. `<think>` stripping added to all four `_extract_json` functions (closed and unclosed tag cases). System prompts updated to suppress thinking traces. Review+test loop PASSED (6/6 live incl. API ping).
- [x] **Verbose flags `-v`/`-vv`** *(owner request 2026-07-01)*: New `execution/log.py` — `verbosity()` reads `JOBSEARCH_VERBOSITY` env (0/1/2), `vprint(level, *a)` gates stderr output, `add_verbose_arg(parser)` / `apply_verbosity(args)` wired into all 7 skill scripts (`scrape.py`, `match.py`, `understand.py`, `tailor.py`, `respond.py`, `llm_rank.py`, `apply.py`). `main.py` top-level `-v`/`-vv` (`action="count"`) sets `os.environ["JOBSEARCH_VERBOSITY"]`; `_run()` threads it via `{**os.environ, ...}` to every subprocess. `_normalize_argv` broadened to find `--<command>` at any argv position (not just index 0) so `-v --lists` works. Level 0 = today's output unchanged; level 1 = per-job scoring details / brief field summary; level 2 = raw LLM prompt + reply. Review+test loop PASSED (2 MINOR fixed: match.py "résumé skills recognized" restored to level 0, apply.py wired; 2 NITs cleaned up).
- [x] **Score-first ordering** *(owner request 2026-07-01)*: `store.get_jobs()` gains `order="score"` (SQL `COALESCE(llm_score, match_score) DESC, id DESC`; default `"id"` preserved). `_pending_jobs()` in jd-understander, resume-tailor, humanise-responder sort best-first before `[:limit]` so a limited run preps the highest-rated jobs. `main.py _ordered()` switches to score-first (llm_score preferred over match_score via `is not None` guard, matching `apply.py`). Dead helpers `_loc_rank`/`_posted_key` removed. Review+test loop PASSED.
- [x] **Reliability + efficiency batch** *(owner 2026-07-01)*: apify `logger=None` (search
  traceback), grok 429 retry/backoff, `--llm deepseek` provider (no TPM wall, key-leak-safe),
  removed dev-only ranker `--compare/--vs/--emit` (Grok tuned to ρ≈0.85 vs Claude), and the
  `rejected` status + `_auto_reject`/`reject`/`rejected` (off-profile parked, excluded from
  rank/prep; re-search is incremental via upsert dedup). Review+test loop PASSED: code-tester
  29/29, code-reviewer PASS; fixed 1 MAJOR (DeepSeek key-leak → `XAI_API_KEY=key or ""`) +
  MINOR (schema comment) + NIT (reject output formatting).

**Phase 10 — NVIDIA model + criteria improvements (2026-07-04)**
- [x] **NVIDIA model swap + reasoning-off prefix** *(owner request 2026-07-04)*: Primary model changed to `moonshotai/kimi-k2.6` (best seniority-rule calibration, 6/6 rubric, 1.6/4.7 s); backup to `mistralai/mistral-large-3-675b-instruct-2512` (fastest 1.3/2.8 s). Previous primary (`nemotron-super-49b-v1`) and backup (`llama-3.3-70b-instruct`) removed — both llama-3.3-70b and deepseek-v4-pro timed out at 150 s on free tier (queue-starved). `NVIDIA_SYSTEM_PREFIX="detailed thinking off"` added as a constant and wired into `_llm_env("nvidia")` via `LLM_SYSTEM_PREFIX` env var; `_complete_grok` in `execution/llm.py` prepends it to every system prompt (Nemotron reasoning-mode prevention). Updated `main.py` constants + `.env.example`. Re-runnable benchmark at `scripts/bench_nvidia.py`. Review+test loop PASSED (18/18 unit tests, PASS verdict).
- [x] **STRETCH eligibility tier** *(owner request 2026-07-04)*: New `stretch` classification between `needs_mod` and `off_profile` in `execution/eligibility.py`. `stretch` = security-adjacent, 20 ≤ score < 45, no scope gap → kept visible for heavy résumé rewrite (human opts in). New `has_scope_gap()` checks title against `SCOPE_TITLE_KEYWORDS` + word-boundary VP regex; JD against management/6+yr/strategy regex patterns with gerund/past-tense forms. `STRETCH_FLOOR = 20.0`. `off_profile` now means HARD-NOs only: non-security, has_scope_gap(), or score < 20. `_print_lists()` adds LIST 4 "🧗 STRETCH"; `_auto_reject()` preserves stretch jobs; `cmd_prep --stretch` selector added. `classify()` 4x-call NIT fixed (single-pass dict bucketing). Review: PASS (3 MINORs applied: stale comment, gerund regex, VP word-boundary; 1 NIT applied: classify compute-once).
- [x] **Joblister + job-portal lists** *(owner request 2026-07-04)*: `docs/joblisters.md` — aggregator sites with verified public JSON endpoints (Remotive, Arbeitnow, Jobicy, Himalayas, The Muse; all HTTP 200 verified); Apify-backed listers noted. `docs/job_portals.md` — individual company career sites grouped by ATS (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Recruitee, Workable, Eightfold, Zoho Recruit); live-verified ATS JSON patterns; owner's target companies mapped to ATS slugs. No plugins built this pass — lists feed a future ATSPlugin pass (§10).
- [x] **Help text + .env.example cleanups** *(owner request 2026-07-04)*: Help text restructured into sectioned blocks (SCRAPE / TRIAGE / PREP / APPLY / UTILITIES) with `━━━` separators; EXAMPLES rewritten to show the full tier workflow. `.env.example` LLM header updated to list all 5 providers (session/api/grok/deepseek/nvidia); NVIDIA block comments reflect new models + `LLM_SYSTEM_PREFIX`.
- [x] **Joblister plugins built** *(owner request 2026-07-05)*: The 5 public-JSON aggregators researched in the previous item are now live plugins in `.claude/skills/job-scraper/plugins/`: `remotive.py`, `arbeitnow.py`, `jobicy.py`, `himalayas.py`, `themuse.py`, following the `remoteok.py` template (stdlib `urllib`, no auth). Shared HTML-stripping/keyword-matching/epoch→ISO helpers factored into `_joblister_util.py` (registry-skipped via leading underscore). Live field shapes re-verified 2026-07-05 (differed from the 2026-07-04 research pass in places — see `docs/joblisters.md`). Review+test loop: code-reviewer found 1 MAJOR (The Muse pagination was 1-indexed against a 0-indexed API, silently skipping the first 20 results/query) + 3 MINOR + 2 NIT, all fixed; code-tester found 1 MINOR (`limit=0` returned 1 job instead of 0, duplicated across all 5 files), fixed. Re-verified: registry discovery, live fetch per source, HTML-free `jd_text`, ISO `posted_at`, `ext_id` uniqueness, dedup-stable re-run, clean failure isolation, `--source all` regression — all pass.
- [x] **ATS jobportal plugins built** *(owner request 2026-07-05)*: `greenhouse.py`, `lever.py`, `ashby.py` — one plugin per platform, configured via `GREENHOUSE_COMPANIES`/`LEVER_COMPANIES`/`ASHBY_COMPANIES` in `.env` (`slug` or `slug:Display Name`), per the locked §9/§10 design. Shared `_ats_util.py` (`parse_companies`, `epoch_ms_to_iso`, `round_robin`) re-exports HTML/keyword helpers from `_joblister_util.py`. Field-shape facts nailed down live: Greenhouse `content` is HTML-entity-escaped TWICE (needs an extra `html.unescape` pass); Lever's `createdAt` is epoch **milliseconds**, not seconds; Lever/Ashby expose no company-name field at all (display name comes from the `.env` config); Ashby's job-detail link is `jobUrl`, not the `applyUrl` apply-button link (PLAN §10 rule); Greenhouse/Lever/Ashby ids are unique only per-company, so every `ext_id` is prefixed `<slug>:<id>`. Review+test loop: code-reviewer PASS (2 NITs, one applied: `_ats_util.py` self-bootstraps `sys.path`). code-tester FAIL→fixed: MAJOR — a multi-company `.env` list silently starved every company after the first once it alone had ≥`limit` matches (defeats the entire point of a multi-company list); fixed via `round_robin()` merging each company's matches one-at-a-time instead of concatenating, verified live on all 3 plugins with 2-company configs (gitlab+duolingo, linear+ramp, leverdemo+palantir all now interleave fairly). Re-verified: availability gating, HTML-clean `jd_text` (incl. double-escape), correct epoch-ms dates, `ext_id` uniqueness across companies, bad-slug isolation, `limit=0`, dedup-stable re-run.
- [x] **5 more ATS jobportal plugins built + cybersecurity company list** *(owner request 2026-07-05: "gather all the individual company portals... create the plugins", then "get cybersecurity companies and give priority to companies who has my career jobs")*: `smartrecruiters.py`, `recruitee.py`, `bamboohr.py`, `workday.py`, `workable.py` added alongside the existing 3, all following the same `parse_companies()`/`round_robin()`/per-company-and-per-item-try/except pattern. `_ats_util.py` gained `post_json()` (shared POST-JSON helper for Workday/Workable) and `parse_workday_companies()` (Workday has no single slug — needs `tenant:wdN:site`, parsed from a `WORKDAY_COMPANIES` var). SmartRecruiters/BambooHR/Workday use a 2-call list+detail pattern (list has no JD text); Recruitee is single-call. Field-shape facts nailed live: Workday's server-enforced page-size max is **20** (21+ → HTTP 400, paginated via `offset` up to 5 pages instead); Workable's POST body must be `{"query":...}` ONLY (an added `"limit"` key errors), and its payload has no description/URL field at all (`jd_text` always `None`, `Job.url` constructed as `apply.workable.com/<co>/j/<shortcode>/`); BambooHR has no date field anywhere (`posted_at` always `None`). **Eightfold explicitly NOT built** — its documented public endpoint returns `403 "Not authorized for PCSX"` for every company/method/header combination tried (Qualcomm, NVIDIA both live-tested), a CSRF/session gate rather than a bare public API; deferred to a future Playwright-based custom plugin (PLAN §4's other plugin category). Companies researched + live-verified (job counts, role-keyword matches) across all 8 working platforms — see `docs/job_portals.md` for the full list; owner's 7 named targets resolved: Qualys→Workday (✓ configured, strong match), Mattel→SmartRecruiters (✓ configured, weak IT-security match), Qualcomm→Eightfold (blocked, deferred), Simbian→Zoho Recruit (confirmed, deferred/needs OAuth), Sibros→Rippling ATS (not one of the 9 target platforms, flagged for a possible future plugin), EY→SAP SuccessFactors (out of scope), cyber-times.in→skipped (offline). Review+test loop: code-reviewer FAIL→fixed: CRITICAL — Workday's detail URL template doubled the `/job/` path segment (`externalPath` already starts with `/job/...`), 404ing every detail call and silently leaving every Workday job without `jd_text`/real `posted_at`; fixed by removing the literal `job` from the template. code-tester FAIL→fixed: CRITICAL — Workday's `_PAGE_SIZE=50` exceeded the real server-enforced max of 20, causing every list call to 400; fixed via pagination in 20-item pages up to 5 pages/company. Both re-verified live (real Workday postings now return full JD text + real ISO `startDate` + correct `externalUrl`). MINOR (unbounded detail-call volume on SmartRecruiters/BambooHR under a broad query) fixed via a `_MAX_DETAIL_CALLS=100` cap on both. `.env` populated with the full live-verified company list across all 8 platforms; `.env.example` and `docs/job_portals.md` updated to match.
- [x] **Phase 2: Hyderabad/Bengaluru company research + custom-site plugins, wave 1** *(owner request 2026-07-06: "research companies from hyderabad and bengaluru which has career sites... make a manager which has all the code and a simple plugin for each website"; scope locked: no batch cap, security-related companies first then broaden to all IT)*: Built `_career_util.py` — shared manager for CUSTOM (non-ATS) company career-site plugins, modeled on `_ats_util.py`. Centralizes, in increasing cost order: `fetch_html`/`fetch_json` (stdlib urllib), `extract_next_data`/`extract_ld_json`/`extract_window_var` (JSON-blob-in-HTML extraction — covers most modern career pages without needing a browser), `job_id_from_url` (deterministic `ext_id` fallback), and `playwright_available`/`render_html` (Tier-3 fallback: fresh HEADLESS chromium, NO persistent profile/login — corrects `_custom_template.py`'s wrong assumption that a logged-in `PLAYWRIGHT_USER_DATA_DIR` session is needed for public career pages). Ran `playwright install chromium` (one-time; package was already installed but the browser binary was not). Research wave 1 (via `research` subagent + live network-tab capture during Playwright rendering) found several companies initially mis-classified as "custom/blocked" by static-HTML-only research were actually on already-supported ATS platforms once rendered: **Zscaler** (Greenhouse, `zscaler` slug directly resolves — research's "abandoned" conclusion was wrong), **Saviynt** (Lever, `saviynt` — research's "HubSpot CMS custom" conclusion was wrong, HubSpot is just the page shell), **InstaSafe** (Zoho Recruit's public endpoint, not a Gatsby-SPA dead end). Also added straightforward Bucket-A Workday tenants: F5 (`ffive:5:f5jobs`), Trellix (`trellix:1:EnterpriseCareers`), CrowdStrike (`crowdstrike:5:crowdstrikecareers`), and Greenhouse additions Netskope/CloudSEK. **Major discovery, corrects a Phase 1 finding:** Zoho Recruit has an UNAUTHENTICATED public `.../recruit/v2/public/Job_Openings` endpoint on every org with a published career page — the earlier "needs OAuth" note was only true of Zoho's authenticated CRUD API. Built `zoho_recruit.py` (new ATS-platform plugin, `ZOHORECRUIT_COMPANIES=subdomain.tld:Display Name` format since orgs use different TLDs) — this **unblocks Simbian**, a Phase 1 owner-priority target previously marked deferred; also covers InstaSafe and Astra Security. Built `synopsys.py` — the first Tier-3 (real Playwright browser render) plugin: Synopsys's `/search-jobs` page is a search form only (no server-rendered data), but results are directly URL-navigable once JS runs (`/search-jobs/<kw>/44408/<page>`), discovered via live network-tab capture rather than blind DOM-scraping; regex-parsed (matching this codebase's existing `strip_html`-style approach, no new HTML-parsing dependency). Investigated but NOT built this wave (flagged for a future pass): Check Point (403s even to a real browser, likely bot/WAF-protected), Seclore/Darwinbox (genuine client-side SPA, no JSON blob found), Appknox/Cutshort (Cutshort's own `__NEXT_DATA__` has `dehydratedState: null` — real data source not yet found; Cutshort is itself a multi-company Indian aggregator worth its own investigation pass like Zoho Recruit was). Several large companies (Radware, Fortinet, Sophos, Wells Fargo, Deutsche Bank, Deepfence, Imperva, WeSecureApp/Strobes) gave no static or bot-accessible signal — flagged for a dedicated follow-up, not chased further this wave. Full research findings, bucket classifications, and flagged companies in `docs/job_portals.md`. Review+test loop: code-reviewer FAIL→fixed: 2 MAJOR — (1) `zoho_recruit.py`'s host-construction used `rsplit('.', 1)`, silently breaking on Zoho's multi-part regional TLDs (`.co.in`, `.com.au`) by splitting at the wrong dot; fixed to split on the FIRST dot instead (Zoho subdomains never contain one, TLDs can). (2) `_career_util.py`'s `job_id_from_url` docstring promised a "guaranteed non-empty" `ext_id` fallback but silently returned `""` for a falsy url, which would `ValueError` two frames away in `Job.__post_init__`; fixed to raise immediately instead, matching the documented contract. Plus 2 MINOR fixed: `extract_window_var`'s regex required a trailing `;`, wrongly rejecting valid JS lacking one (ASI) and forcing an unnecessary Tier-3 fallback — made optional; `_mmddyyyy_to_iso` was duplicated byte-for-byte in both new plugins — moved to `_career_util.py` as `mmddyyyy_to_iso` (with month/day range validation added per a NIT), re-exported from `_ats_util.py` (matching the existing `epoch_to_iso` re-export convention), both call sites updated. code-tester PASS (9/9) with 1 MINOR found and fixed: Synopsys's `_TRAILING_MARKERS` cut didn't match the site's actual current markup (none of the 4 anticipated marker strings appear on the live page), leaking ~11% boilerplate (a location-map teaser + "Hiring Journey" process blurb) into every `jd_text`; added the two real marker strings, re-verified live — `jd_text` now cuts cleanly. Full re-verification after all fixes: registry listing unchanged (19 plugins, no import errors), `zoho_recruit`/`synopsys` both re-tested live with correct output, real `scrape.py --source zoho_recruit`/`--source synopsys` runs wrote clean rows to the store, and `profile-matcher` scored + ranked the new rows correctly (Synopsys top match 82.3; Zoho Recruit jobs from all 3 orgs represented in the ranked list).
- [x] **Bulk company discovery via search-engine indexing** *(owner request 2026-07-06: "lets build" the discovery idea raised in the previous session about a directory of ATS customers)*: no third-party directory was needed — every ATS platform's public job-board pages are ordinary search-engine-indexed webpages, so a plain `site:<platform-domain> <role keyword>` search surfaces many companies' slugs directly from the result URLs, no API/directory required. Ran this across all 8 ATS platforms with varied security-role keywords, extracted candidate slugs from result URLs, then live-verified every candidate (curl the real API, confirm it resolves with a real job count) before adding — same discipline as every company added so far, discovery ≠ verification. **Result: 41 → 96 companies in one pass** — full list auto-generated at `docs/supported_companies.md`. Notable finds: Anthropic, Palantir, HackerOne, 1Password, Adobe, Red Hat (major names manual research had missed), plus strong candidate-fit companies Nozomi Networks (ICS/OT), Horizon3.ai (offensive security), ON2IT (pure cybersecurity), Unit21/CDIT (security fintech). Correctly REJECTED during verification (not guessed, actually checked): `jobgether` (Lever) resolved with 4,803 postings but inspecting real items showed unrelated roles across many different employers — it's a recruiting/job-marketplace platform, not a single company, so it was excluded to avoid mislabeling every posting's `company` field; `certifyos`/`dbtlabs`/`dbtlabsinc`/`rhino-security-labs`/`firstdue.com` all resolved with 0 jobs (wrong/stale slug) and were excluded; `1x.recruitee.com`/`helpag.recruitee.com` are valid orgs but had 0 open roles at verify time, left out this pass but flagged as worth rechecking. Also found (not introduced by this pass) a pre-existing data quirk: Corelight's Greenhouse `company_name` API field literally returns `"Job Board"` instead of `"Corelight"` — a data-quality issue on Corelight's own Greenhouse config, not a plugin bug; `source="corelight"` stays correct in the store so identification is unaffected, only the cosmetic `company` display value; not special-cased in code since `greenhouse.py`'s "prefer the API's real company_name over the configured fallback" behavior is correct for the other 34 companies. `docs/job_portals.md` has the full method + results; `docs/supported_companies.md` regenerated via `list_companies.py`. Live-verified post-batch: `scrape.py --list` shows all 19 plugins with no import errors; live fetches on greenhouse/lever/ashby confirm new companies appear correctly in round-robin-merged results.

**Phase 9 — Extensibility**
- [x] Document "add a portal" (job-scraper SKILL.md "Adding a portal" section: Apify
  plugin vs custom Playwright plugin, one file each); smoke-tested stub plugin
  auto-discovery — a dropped-in `zzsmoketest.py` was listed by `--list`, resolved by
  `get_plugin`, `fetch()` worked, and it de-registered cleanly on removal.

**New portal plugins (owner request 2026-07-01)**
- [x] **RemoteOK** (`plugins/remoteok.py`): Public JSON API (`remoteok.com/api?tag=`), pure stdlib `urllib`, no Apify. Server-side `?tag=` pre-filter + client-side keyword filter across HTML-stripped title/tags/description. `html.unescape` applied, `<script>`/`<style>` content stripped. `is_available=True` always. 5 live jobs fetched + stored in integration test. Review: FAIL→fixed (2 MAJOR: empty-word-list silent discard + HTML entities not decoded; 1 MINOR: `or ""` drops id=0; 1 MINOR: filter on raw HTML; 1 NIT: script/style content). Re-tested: 7/7 PASS.
- [ ] **We Work Remotely** — research pending
- [ ] **Google Jobs** (Apify)
- [x] **Wellfound** (`plugins/wellfound.py`, 2026-07-10): Apify actor `thirdwatch/wellfound-jobs-scraper`
  PRIMARY (no public API/RSS/sitemap — DataDome+Cloudflare gate `/jobs.rss` and `/sitemap.xml`
  to 403, verified live) + logged-in Playwright-session FALLBACK (generic href-pattern
  extraction — real DOM markup unverified, login-gated). See §9 2026-07-10.
- [ ] **Dice** (Apify)
- [ ] **Glassdoor** (Apify)
- [ ] **YC Work at a Startup** (Apify or Playwright)
- [ ] **Hirist** (Playwright/session)
- [ ] **Instahyre** (Playwright/session)
- [ ] **remote.co** — deferred (owner request 2026-07-10): Cloudflare + FlexJobs-paywall gated,
  every `fetch_html` attempt timed out live; no RSS/API/sitemap found. Low ROI vs. fragility;
  revisit only if a specific need arises.

**New joblister plugins (owner request 2026-07-10)** — all live-verified + smoke-tested via
`scrape.py --source <name> --limit 3`, real rows confirmed in `data/jobs.db`:
- [x] **Working Nomads** (`plugins/workingnomads.py`): public JSON API
  (`workingnomads.com/api/exposed_jobs/`), no token, no server search (client-filtered).
- [x] **Best PM Jobs** (`plugins/bestpmjobs.py`): Jobboardly RSS feed (`/jobs.rss`); title
  parsed as `"{Role} - {Company} - {Location}"`.
- [x] **SkipTheDrive** (`plugins/skipthedrive.py`): NO working RSS of any kind (feed disabled
  site-wide, verified live) — scrapes the server-rendered `?s=<query>` search-results HTML
  directly instead; `location` hardcoded `"Remote"` (board is remote-only by definition).
- [x] **NoDesk** (`plugins/nodesk.py`): RSS feed (`/remote-jobs/index.xml`); feed embeds raw
  (non-CDATA) HTML entities that are invalid strict XML — fixed generically in
  `_joblister_util.parse_feed` (see §9 2026-07-10), not special-cased here.
- [x] **Remote100k** (`plugins/remote100k.py`): sitemap (`/sitemap.xml`, flat `<urlset>`) →
  `/remote-job/<slug>` pages → `JobPosting` ld+json (Tier 2, no render needed). `jd_text` is a
  short teaser only (site doesn't host the full JD); `Job.url` points to the real employer
  posting (Greenhouse/Workday/...) found via the page's own external "Apply" link, not the
  remote100k teaser page.
- [x] **NextGen Energy Jobs** (`plugins/nextgenenergyjobs.py`): sitemap INDEX (`/sitemap.xml` →
  `job_openings_*.xml` children) → Next.js App-Router pages needing a Tier-3 headless render
  (no `__NEXT_DATA__`/ld+json — data streams via RSC payload); fields parsed from a bounded
  window after `<h1>` via `lucide-*` icon-class markers. Site's WAF blocked Playwright's
  default headless UA fingerprint — see §9 2026-07-10 `render_html` fix.
- [x] **Relocate.me** (`plugins/relocateme.py`): sitemap (`/sitemap.xml`) filtered to
  trailing-numeric-id URLs → `JobPosting` ld+json (Tier 2). ld+json failed strict `json.loads`
  (literal control chars in `description`) — fixed generically in `_career_util.extract_ld_json`
  (see §9 2026-07-10). Title/description are double-HTML-escaped; unescaped twice before
  `strip_html`.
- [x] **Jobs in Education** (`plugins/jobsineducation.py`): SmartJobBoard RSS
  (`/feeds/rss.xml`); company from `dc:creator`, location best-effort-extracted from the
  description's leading text when the company name appears near the start.
- [x] **Rejobs** (`plugins/rejobs.py`): Atom feed (`/en/rss/renewable-energy-jobs`); title
  parsed as `"{Role} - {Company}"`, `ext_id` from the Atom `<id>` (short numeric URL, distinct
  from `<link>`).
- [x] **We Love Product** (`plugins/weloveproduct.py`): public JSON API (`/api/jobs`);
  unauthenticated calls only return page 1 (~32 latest of the true total — deeper pages 401).
- [x] **CareerHound** (`plugins/careerhound.py`): session-gated Playwright (same
  `PLAYWRIGHT_USER_DATA_DIR` model as the custom-plugin template) — entirely login-gated site,
  no public preview/API/RSS/sitemap found; generic href-pattern card extraction since the real
  in-app markup could not be inspected without a live login. **Needs live-session verification
  on first real use** (self-anneal per SKILL.md) — see plugin docstring.
- [x] **Indeed reworked to browser-first** (`plugins/indeed.py`, owner request 2026-07-10):
  primary fetch is now a headless-browser scrape of the search-results page (parses the
  `providerData["mosaic-provider-jobcards"]` JS blob via `JSONDecoder.raw_decode`, no Apify
  cost); the original Apify actor (`borderline/indeed-scraper`) is kept as an automatic
  fallback when the browser path is unconfigured/blocked/empty. Toggles:
  `INDEED_USE_BROWSER`, `INDEED_APIFY_FALLBACK` (both default on). See §9 2026-07-10.
- [x] **Cutshort** — built 2026-07-07 as a Tier-1 JSON-embedded-in-HTML ATS-platform plugin
  (`cutshort.py`), NOT Playwright/session as originally predicted here — see Phase 11 below.

**Phase 11 — Wave 2: new ATS-provider plugins (owner request 2026-07-06, security-first)**
- [x] Oracle Fusion Cloud Recruiting (`oraclefusion.py`) — Tier-1 JSON, no auth. Review+test
  loop PASSED.
- [x] SAP SuccessFactors CSB2 (`successfactors.py`) — Tier-2 sitemap + JSON-LD/microdata.
  Review+test loop PASSED.
- [x] Rippling ATS (`rippling.py`) — Tier-1 JSON, no auth. Review+test loop PASSED.
- [x] iCIMS (`icims.py`) — Tier-2 HTML list + JSON-LD detail, reverses a Wave-1 "needs
  browser" assessment. Review+test loop: first pass FAILED (list-page regex never matched
  any real row due to intervening markup — BLOCKER), fixed and re-tested PASSED; a second
  review round then found + fixed 2 MINOR + 2 NIT (dict-vs-string address guard, relative-URL
  resolution, `validThrough` misused as posted_at, redundant unescape).
- [x] Cutshort (`cutshort.py`, unlocks Appknox) — Tier-2 Next.js embedded JSON + JSON-LD
  detail. Review+test loop PASSED first pass (code-tester), 1 MAJOR fixed on review
  (unsafe `.get(key, {})` chaining crashes on a documented live `null` value) + 1 MINOR
  (query-key alias mismatch guard).
- [x] Darwinbox (`darwinbox.py`, unlocks Seclore) — Tier-1 JSON (same-origin API found via
  the SPA shell's own JS bundle). Review+test loop PASSED first pass; 3 MINOR fixed on
  review (redundant duplicate page-1 fetch, pagination-failure discarding all prior
  candidates, docstring/URL mismatch).
- [x] Freshteam (`freshteam.py`) — Tier-2 server-rendered HTML + JSON-LD detail, reverses a
  Wave-1 "auth-gated" assessment. Review+test loop PASSED first pass; 2 MINOR fixed on
  review (regex attribute-order fragility, un-unescaped location entities).
- [x] Avature (`avature.py`, lowest priority) — Tier-2 server-rendered HTML, reverses a
  Wave-1 "needs headless browser" assumption (the existing bespoke `synopsys.py` remains
  separate/unaffected). Review+test loop PASSED first pass (single-tenant); adding a second
  real tenant during the populate pass exposed 2 template-variance bugs (regex attribute-
  order fragility identical to the Freshteam finding, plus a second card-layout generation
  needing new location/posted-date fallback extractors) — both fixed, re-review + re-test
  PASSED with both tenants live together.
- [x] Populate pass — cybersecurity companies added across all 8 providers, product→
  services→general-IT priority, Hyderabad/Bengaluru security-first scope (owner decision).
  96 → 119 companies, 9 → 17 platforms. `list_companies.py` updated to recognize the 8 new
  platforms (was silently omitting them from `docs/supported_companies.md`). Full company
  list + exclusion reasoning in `docs/job_portals.md`; full technical decision log in §9.

**Phase 12 — Wave 3: all-IT company expansion (owner request 2026-07-07)**
- [x] Scope broadened from security-first to all-IT across all 17 already-built ATS
  platforms, Hyderabad/Bengaluru unchanged (Pune/Delhi still deferred) — owner decision, see
  §9. No new platform-discovery work, config-only populate pass.
- [x] 4 parallel research batches covering all 17 platforms, each live-verifying every
  candidate before recommending it. 119 → 371 companies, 17 → 18 platforms/plugins.
- [x] Two real code bugs found + fixed during the pass (not the original build/review/test
  loop): `icims.py`'s pagination was 1-indexed but the platform's real pagination is
  0-indexed, silently skipping every tenant's entire first page since Wave 2 — fixed;
  `ashby.py` didn't URL-encode a company slug before building the request URL, which would
  have crashed on a genuine slug containing a literal space — fixed. Both live-reverified,
  no regression. Full detail in §9.
- [x] Docs finalized: `docs/supported_companies.md` regenerated (371 companies, 18
  platforms), `docs/job_portals.md` "Wave 3" section added (methodology + per-platform
  counts + notable exclusions), PLAN.md §9/§10 updated.

---

## §9 Decisions Log  *(append-only; overrides the spec above)*

Format: `YYYY-MM-DD — <skill/surface> — <element> — <decision> [revises §X]`

- 2026-06-30 — project — script language — **Python** for all skill scripts. [§2]
- 2026-06-30 — job-scraper — sourcing — **Hybrid**: Apify actors where they exist
  (LinkedIn/Naukri/Indeed) + custom plugin scrapers for the rest. [§4]
- 2026-06-30 — apply-agent — autonomy — **Human-in-the-loop**; AI fills, human reviews
  & submits. No fully-autonomous submit path in v1. [§6]
- 2026-06-30 — job-scraper — v1 portals — **LinkedIn, Naukri, Indeed**. [§4/§5]
- 2026-06-30 — project — status board — **no `progress.md`**; status tracked in
  **§8** of this file. [§8]
- 2026-06-30 — apply-agent — build order — built **last** (depends on all upstream
  skills; highest-risk surface). [§7]
- 2026-06-30 — job-scraper — actors — LinkedIn=`curious_coder/linkedin-jobs-scraper`
  ($0.001/result, min count 10), Indeed=`borderline/indeed-scraper` ($0.005/job,
  country default `in`), Naukri=`muhammetakkurtt/naukri-job-scraper` (min `maxJobs`
  50, output nested under `jobDetails`). Overridable via `APIFY_ACTOR_*`. [§4/§5]
- 2026-06-30 — job-scraper — cost safety — bound spend by passing `limit` into each
  actor's count field (floored to the actor minimum) + a hard
  `max_total_charge_usd` ceiling (`APIFY_MAX_CHARGE_USD`, default $0.50). Do NOT use
  `.call(max_items=…)` — batch-writing actors early-return before dataset flush,
  yielding zero rows; apply `limit` when reading the dataset instead. [§6]
- 2026-06-30 — job-scraper — Naukri location — free-text location is encoded as a
  `searchUrl` (`/<kw>-jobs-in-<loc>`); the actor's `cities` field needs internal
  numeric codes, so it is not used. [§4]
- 2026-06-30 — job-scraper — normalization — structured actor fields (dict/list,
  e.g. Indeed `location`, Naukri `locations`) are coerced to strings via
  `_apify.as_text` so the SQLite store never receives a dict/list. [§3]
- 2026-06-30 — job-scraper — Naukri jd_text — `fetchDetails=True` by default so each
  row carries the full JD (downstream jd-understander needs it); `APIFY_NAUKRI_DETAILS=0`
  falls back to cheaper standard data without jd_text. [§5]
- 2026-06-30 — project — env — Python venv at `.venv` (Python 3.14, no system pip);
  run scripts with `.venv/bin/python`. [§2]
- 2026-06-30 — job-scraper — robustness — `--source all` isolates per-plugin
  failures (one portal's error never aborts the others; failed sources logged to
  `runs`, run exits non-zero). Field mapping uses `_apify.first_text` (applies
  `as_text` across each candidate) so a structured field with no text falls through
  to the next candidate instead of silently nulling. [§5]
- 2026-06-30 — project — LLM access — **multi-mode** `LLM_PROVIDER` (env): `session`
  (default) = the current Claude Code session (orchestrator) does the reasoning — no
  API key, no network, no cost; `api` = Anthropic Messages API with
  `ANTHROPIC_API_KEY`; `grok` = xAI Grok chat-completions API with `XAI_API_KEY`
  (OpenAI-compatible; stdlib `urllib`, NO extra dep; endpoint overridable via
  `XAI_BASE_URL`). Per-provider default model via `LLM_MODEL` (api=`claude-sonnet-4-6`,
  grok=`grok-4`). Shared in `execution/llm.py`; `complete()` dispatches api→Anthropic,
  grok→xAI, and raises `SessionModeError` under session (or any unknown provider —
  unknown safely falls back to session so a typo never triggers a surprise charge).
  LLM skills (jd-understander, humanise-responder, apply-agent reasoning) run as
  prepare→[orchestrator answers]→save in session mode, or a one-shot `run` loop in
  api/grok mode; the store rows are the work queue. `anthropic` is an OPTIONAL dep
  (only for `api` mode); `grok` mode needs no package. [§2/§5]
- 2026-06-30 — profile-matcher — design — **deterministic, pure stdlib** (no LLM): score
  0–100 = skill overlap (≤60, mostly JD-skill *coverage*) + role fit (≤25) +
  title/seniority fit (−10…+15, senior/lead/staff/principal penalized for a ~2-yr
  résumé). Role-profiles = README variants (Red Team/Pentest, Detection Engineering,
  Cloud Security, ICS/OT, Vulnerability Research, Application Security) in
  `ROLE_PROFILES`. Résumé skills parsed live from `\techrow{}` rows so the corpus
  tracks the master `.tex`. Writes `match_score`, `role_profile`, JSON rationale
  (`matched`/`missing` skills) in `notes`; `scraped → matched`. [§5]
- 2026-06-30 — profile-matcher — fixes — seniority match uses word-boundary
  `_present` (not substring, which falsely flagged "headless" etc.); `_clean_tex`
  unescapes LaTeX `\&`/`\#`/`\_`/`\%`/`\$` before stripping backslashes so résumé
  skills like "mitre att&ck" are recognized; `_parse_techrow_skills` drops `%`
  comment lines. Added `--rescore` (recompute `matched` rows in place, no status
  change) so scoring fixes/résumé edits apply without re-scraping. [§5]
- 2026-06-30 — llm.py — grok/api robustness — grok path reads body then guards
  `json.JSONDecodeError` (200-but-not-JSON → clear RuntimeError); anthropic path
  wraps `anthropic.APIError` as RuntimeError (uniform error type across providers)
  and caches the client module-level (reuses the httpx pool). [§9 LLM access]
- 2026-06-30 — job-scraper — multi-key Apify — store **several** Apify tokens and
  **auto-rotate** by health. Keys from `APIFY_TOKEN` (comma/space/`;`-separated) and/or
  `APIFY_TOKEN_1/2/…`. `run_actor(token=None)` tries keys best-health-first
  (`healthy→unknown→exhausted→invalid`); a usage/credit-limit error → mark `exhausted`
  + rotate, an auth error → mark `invalid` + rotate, a non-key error (bad input/timeout)
  → **propagate** (don't swallow). Health persists in `data/apify_keys.json`
  (gitignored; only a masked `…last5` hint + sha256[:12] id, never the secret);
  `exhausted` auto-resets monthly (free credit renews). Logic in
  `plugins/_apify_keys.py`; indicator `scrape.py --keys`, clear with `--reset-keys`.
  `get_token()` now returns the best candidate (still used by `is_available()`).
  `APIFY_MAX_CHARGE_USD` is applied **per key**, so rotation can't multiply the ceiling
  within one attempt. [§4/§6]
- 2026-06-30 — job-scraper — multi-key Apify hardening — `classify_error` uses
  SPECIFIC phrases (not bare "limit"/"token"/"exceeded", which mis-flagged
  "rate limit exceeded"/"actor run time limit exceeded"/"token_count" and dead-keyed
  every key on a transient error); HTTP 429 / "rate limit" → None (transient, no
  rotation); message permission signals → `invalid` even on 403 (so a permission
  denial isn't auto-retried monthly as `exhausted`). `mark()` scrubs the key out of
  any persisted `last_error` (`error.replace(key, key_hint(key))`) so the secret can
  never reach `data/apify_keys.json`. [§4/§6]
- 2026-06-30 — resume-tailor — design — **LLM-driven** (session/api/grok), strictly
  reorders/rephrases TRUE content from the master `.tex` guided by `jd_brief`; never
  fabricates. **Master `varakumar_resume.tex` + its PDF are NEVER modified** — read-only
  source. Each job gets a tailored COPY in a variant store (`tailored/`), auto-compiled
  to PDF (tectonic/make). **Similar postings reuse an existing variant** (same
  `role_profile` + high keyword/must-have overlap) instead of regenerating; otherwise a
  new variant is created. Section/content ORDER is preserved. Writes
  `tailored_resume_path` (→ the per-job PDF), advances `matched`→`tailored`. Work queue
  = `matched` rows with a `jd_brief` and no `tailored_resume_path`. [§5/§6]
- 2026-06-30 — humanise-responder — design — reads `tailored` jobs lacking
  `answers_json`, drafts a `cover_letter` + `answers` (why_role, why_company,
  relevant_experience, strengths, availability_note) GROUNDED in the candidate profile
  (`_candidate_profile` parses summary + key achievements + skills from the master
  `.tex`) and `jd_brief`; advances `tailored → ready`. Facts only the candidate can
  supply (notice period, current/expected CTC, relocation/visa) are put in
  `screening_todo` for the human at the apply gate — NEVER fabricated. session
  (prepare/save) or api/grok (`run`) like jd-understander; `cover_letter` required.
  apply-agent consumes `answers_json` to fill the form. [§5/§6]
- 2026-06-30 — orchestrator — `main.py` — single terminal entrypoint wiring all stages:
  `search` (multi-location × multi-query × `--days`, newest-first, then match+classify),
  `lists` (THREE lists — 📥 scraped-pool summary + ✅ eligible-as-is vs ✏️ needs-résumé-mod,
  + off-profile skipped; `--raw` dumps the full scraped list — see 2026-06-30 main.py
  applied-log entry below),
  `prep --llm claude|grok|api [--modify-resume]` (chains briefs→résumé→answers; grok/api
  automate, claude=session=ask-Claude), `apply`, `log --screenshot`, `report`
  (applications dashboard + stores artifacts under `applications/<id>/`: resume.pdf,
  answers.json, screenshot, record.json + index.json). Calls the tested skill CLIs as
  subprocesses with per-stage LLM env. Eligibility: security-title + score≥45 to qualify;
  ≥70 & coverage≥0.6 = eligible-as-is, else needs_mod. Order = Hyderabad→Bengaluru→rest,
  newest-first. [§5/§6/§11]
- 2026-06-30 — store — schema — added `screenshot_path` column (apply review-gate
  screenshot) + an idempotent `init_db` migration (`ALTER TABLE … ADD COLUMN` when an
  older DB lacks it). [§3]
- 2026-06-30 — resume-tailor — passthrough — `--no-modify` sets `tailored_resume_path`
  to the master `varakumar_resume.pdf` and advances `matched→tailored` with NO LLM
  modification — the "apply with master résumé as-is" path for eligible jobs. [§5]
- 2026-06-30 — llm.py — Groq/Cloudflare — the grok provider works against **Groq**
  (`gsk_` key, base `https://api.groq.com/openai/v1`, a Groq model e.g.
  `llama-3.3-70b-versatile`), not just xAI. Fixed a Cloudflare 403 (err 1010) by sending
  `User-Agent`/`Accept` headers (default Python-urllib UA is blocked). Live-validated:
  briefs + answers generated via Groq, screening facts still flagged not fabricated. [§9 LLM access]
- 2026-06-30 — job-scraper — LinkedIn recency — `LINKEDIN_POSTED_DAYS` env (e.g. 7)
  adds LinkedIn's date-posted filter `f_TPR=r<days*86400>` to the search URL and sorts
  `sortBy=DD` (newest-first), so scraping (and downstream apply order) runs recent →
  older. Unset = no date filter. [§4/§5]
- 2026-06-30 — apply-agent/store — fixes — `applied_at` is set ONLY for outcome
  `applied` (NULL for skipped/failed — the field name implies submission time); `log`
  uses new `store.get_job(id)` (indexed single-row lookup) instead of scanning all
  jobs; `apply.py packet --limit 0` yields 0 (was: all); `show` prints the outcome note.
  [§5/§6]
- 2026-06-30 — apply-agent — engine — browser driving uses the **chrome-devtools MCP**
  (the server `chrome-screenshot-tester` already uses), driven INTERACTIVELY by the
  orchestrator — NOT a fixed Playwright script. Rationale: LinkedIn Easy Apply is a
  dynamic multi-step modal (per-job screening Qs, varied input types); an adaptive
  orchestrator is more accurate than a brittle script, and Playwright isn't installed.
  v1 surface = **LinkedIn Easy Apply**. [§5/§6]
- 2026-06-30 — apply-agent — review gate — **fill + screenshot, leave open** (Option 1):
  orchestrator fills every step, screenshots via chrome-screenshot-tester, STOPS on the
  review step; the HUMAN inspects the live page and clicks Submit. No autonomous submit
  path. `scripts/apply.py` is the deterministic spine only (packet builder + outcome
  logger); it never opens a browser. `log` refuses any job not at `ready` (guards
  double-logging / logging an unreviewed job). Default batch 3 (human-paced, §6). [§6]
- 2026-06-30 — LLM skills — `_extract_json` — all three LLM skills (jd-understander,
  resume-tailor, humanise-responder) use a string-aware brace-depth scan to extract the
  first complete JSON object, so trailing prose containing braces no longer breaks
  parsing (was a naive `find('{')`/`rfind('}')`). [§5]
- 2026-06-30 — humanise-responder — hardening — `save` guards `JSONDecodeError` on the
  answers file (friendly error, exit 1); `_candidate_profile` guards malformed `\techrow`
  lines (no crash), strips `%` comment lines, and preserves `3{,}200`/`\textbf`/emdash in
  `_clean`; `_extract_json` uses a string-aware brace scan so trailing prose with braces
  doesn't break parsing; `--limit 0` honored. Added a programmatic anti-fabrication
  guard: if a drafted answer mentions pay/notice/dates, a REVIEW note is appended to
  `screening_todo` for the human gate (prompt-level honesty + this code-level check). [§5/§6]
- 2026-06-30 — resume-tailor — hardening — build failures are per-job, not fatal:
  `save`/`run` catch `OSError` (tectonic missing) + `subprocess.TimeoutExpired`; `run`
  splits the try so an LLM/API error breaks the batch while a parse/build error only
  skips that job. Summary newlines collapsed to spaces (no stray paragraph break);
  tagline roles `_tex_escape`d on splice (future role with a LaTeX special can't break
  the build). `--no-build` links the `.tex` for tectonic-less environments. [§5]
- 2026-06-30 — jd-understander — robustness — `save` list-format parsing skips a
  malformed entry (missing/bad `job_id`) instead of aborting the whole batch
  (matches the dict-format/validation skip-and-continue pattern). [§5]
- 2026-06-30 — candidate-details — file — NEW `candidate.json` at repo root (GITIGNORED —
  holds CTC/notice/contact; `candidate.example.json` is the committed schema template)
  is the single source of personal application facts the résumé can't supply. Loader
  `execution/candidate.py`: `load_details()` (missing/invalid file → `{}`, never raises),
  `known_facts()` (only FILLED fields — bool counts as answered even when False; list
  needs ≥1 non-blank; text non-empty), `screening_gaps()` (human labels of the
  UNANSWERED `FIELDS`). Pure stdlib JSON (pyyaml not installed). [§3/§5]
- 2026-06-30 — humanise-responder — candidate facts — `_job_prompt` now passes a
  `candidate_facts` block (known facts the model MAY cite for availability/relocation —
  NEVER invent a value not present). `screening_todo` is now DETERMINISTIC =
  `candidate.screening_gaps()` (facts still unknown) MERGED with any extra job-specific
  question the LLM surfaced (case-insensitive substring dedup). The existing
  sensitive-mention REVIEW guardrail stays. [§5]
- 2026-06-30 — apply-agent — packet — `_packet` adds `candidate_facts` (from
  candidate.json) so the orchestrator types known screening answers directly instead of
  stopping for each; `human_must_fill` = the remaining gaps. `packet` gains
  `--source <portal>` filter and best-matched-first ordering (`-match_score, -id`). No
  browser/submit path added — spine stays deterministic. [§5/§6]
- 2026-06-30 — orchestrator main.py — applied log + 3 lists — NEW `applied` command
  (applied/skipped/failed log; applied jobs never re-enter apply lists because
  `store.upsert_jobs` preserves status on re-scrape). `lists` now prints THREE lists
  (📥 scraped summarized by source / ✅ best-match / ✏️ needs-mod; `--raw` dumps full
  scraped). `apply --source` passthrough. [§5]
- 2026-07-01 — orchestrator main.py — --tags + clean help — commands accept a flag form
  (`main.py --apply` == `main.py apply`) via leading-token argv normalization (bare words
  still work, nothing breaks). `-h` is the single source of truth: it documents every
  command + its flags + examples ONCE; the duplicate per-command auto-list is suppressed
  (`add_subparsers(help=SUPPRESS)`). [§11] *(owner request 2026-07-01)*
- 2026-07-01 — selection — job targeting — added `store.parse_ids` (shared). `--jobs "1,2"`
  id-filter threads through the prep skills (jd-understander / resume-tailor /
  humanise-responder: `_pending_jobs`/`prepare`/`run`/`passthrough`) and `apply`
  (`apply.py packet --jobs` + `--query <title/company text>`). `main.py prep --eligible`
  resolves eligible best-match `matched` ids and passes `--jobs` to every stage, so
  `prep --llm grok --eligible` automates ONLY the best matches (not all ~300 matched).
  `apply --query/--jobs/--source` select which ready jobs to package. [§5/§11]
  *(owner request 2026-07-01)*
- 2026-07-01 — job-scraper — apify logs — pass `logger=None` to `actor.call()` to disable
  apify-client's live log-stream thread, which could raise a non-fatal
  `impit.TimeoutException` (Request timeout) mid-run and print a scary traceback even though
  the scrape succeeds. [§4] *(owner bug report 2026-07-01)*
- 2026-07-01 — llm.py — 429 resilience — the grok / OpenAI-compatible path retries HTTP 429
  up to 5×, honoring the `Retry-After` header or the "try again in Xs" body hint (capped
  30s), so Groq's free-tier TPM wall no longer aborts a prep run mid-batch. [§9 LLM access]
  *(owner bug report 2026-07-01)*
- 2026-07-01 — LLM access — DeepSeek provider — `--llm deepseek` (prep + rank) reuses the
  OpenAI-compatible grok backend with DeepSeek's base (`https://api.deepseek.com`), model
  `deepseek-chat`, and `DEEPSEEK_API_KEY`. DeepSeek is paid but very cheap
  (~$0.14/1M in, $0.28/1M out) and has NO free-tier TPM wall (concurrency-based) → far more
  reliable than Groq-free for bulk prep. Env in `_llm_env`; key in `.env`/.env.example. [§2/§9]
  *(owner request 2026-07-01)*
- 2026-07-01 — profile-matcher/llm_rank — prompt tuning v2 — added the candidate's
  `preferred_locations` to the ranking profile and two prompt rules: (a) de-emphasize pure
  manual/network/red-team PENTEST roles (candidate is a detection/automation/cloud DEVELOPER,
  not a dedicated pentester; extra penalty for 4+ yr pentest asks), (b) LOCATION as a modest
  tiebreaker (preferred_locations/Remote rank above elsewhere; identical postings differing
  only by city ordered best-location-first), and added 'Vice President'/'VP'/'Manager' to the
  explicit senior-marker penalty. Re-tuned against the orchestrator's (Claude's) own ranking
  of the live eligible top-12: Spearman ρ went 0.685 → **0.979** (mean rank gap 0.50). The
  compare is a DEV/tuning step (scratchpad script), not a shipped command. [§5]
  *(owner request 2026-07-01)*
- 2026-07-01 — llm/keys — Grok multi-key rotation — generalized `_apify_keys.py` into a
  shared `execution/keypool.py` (`KeyPool`: env parsing, health states, secret-scrubbing,
  atomic state, config-driven recovery — monthly for Apify, short cooldown for Grok);
  `_apify_keys.py` is now a thin wrapper (public API unchanged, Apify rotation re-verified).
  Grok keys from `XAI_API_KEY`/`GROK_API_KEY` (one-or-many) + `_1/_2/…`; `_complete_grok`
  ROTATES to the next key on a 429 (a fresh key = a fresh TPM bucket) and only waits when
  ALL keys are throttled. `main.py keys --llm` shows/resets Grok key health
  (`data/grok_keys.json`, gitignored). main.py now loads `.env` so key checks see the keys. [§9/§4]
- 2026-07-01 — resume-tailor — prompt v2 — ATS-friendly (mirror `jd_brief.keywords`/
  `must_have` where the candidate GENUINELY has it), year-gap-aware (present ~2 yrs
  confidently, never imply seniority/lead scope), precise/tight; two hard rules: use ONLY
  tools/terms in `current_summary`/`skill_labels` (no invented CI/CD tool), and PRESERVE the
  strongest quantified metrics (3,200+/700%). Tuned vs the orchestrator's own FICO tailoring.
  [§5]
- 2026-07-01 — main.py — apply links in lists — `_print_lists` shows each job's id +
  posting URL per row (directly actionable: open it, or `apply --jobs <id>`). [§5]
- 2026-07-01 — apply — easy-apply finding — the LinkedIn actor
  `curious_coder/linkedin-jobs-scraper` does NOT expose an Easy-Apply vs external signal
  (verified LIVE: only `applyUrl`, empty for all 10 sampled). So NO `easy_apply` column/
  filter was built (it would be data-less — anti-pattern). To add it later: switch to an
  actor that outputs `applyType`, OR detect at apply-time via browser-driving. [§10]
- 2026-07-01 — main.py — reject notes fix — `_reject_by_llm` no longer overwrites the
  `notes` column (which holds the profile-matcher breakdown JSON needed by
  `coverage()`/`classify()`); the reason already lives in `llm_score`/`llm_reason`. [§5]
- 2026-07-01 — main.py — tab-completion — zero-dep bash completion
  `completions/main.py.bash` (commands bare + `--tag`, per-command flags, enum values);
  install via `source … && complete -F _jobsearch_main_py`. Documented in README. [§5]
- 2026-07-01 — selection — Grok-score filter — the tuned LLM reranker (`llm_score`) now
  DRIVES best-job selection, not just the keyword `match_score`. `execution/eligibility.py`:
  `LLM_BEST_SCORE=60` + `llm_best`/`llm_dud`/`llm_scored` helpers (unranked jobs are NOT
  duds). `main.py`: `prep --llm-best` (prep only Grok-scored-best matched jobs),
  `reject --by-llm` (park jobs Grok scored below the cutoff — the tuned ranking filters the
  duds that keyword-eligibility mis-selects, e.g. Virtusa patching / Saviynt VP). `apply`
  orders `ready` jobs by `llm_score` when present (else `match_score`). Recommended cycle:
  `search → rank --llm grok --eligible --save → reject --by-llm → prep --llm-best → apply`.
  Also: grok-prepped 18 Python-eligible jobs → `ready` (25 ready total, all with answers +
  résumés); pre-rejected the 2 keyword-false-positives Grok ranked worst. [§5]
  *(owner request 2026-07-01)*
- 2026-07-01 — session prep + jd-understander tuning — ran full SESSION-mode prep
  (orchestrator = gold standard) for the top-3 LLM-best jobs (FICO 211, Trintech 217,
  Whitefield 264): hand-written briefs → fabrication-safe **tailored** résumés (reordered
  tagline/skills + re-emphasized summary, built & verified as 22-23KB PDFs) → answers →
  `ready`. Then tested Grok on the same briefs vs mine: Grok's `fit_notes` was GENERIC
  because jd-understander's prompt gave it the JD but NOT the candidate. Fix: extracted a
  shared `execution/profile.py` (`candidate_profile` — résumé summary/skills/target-roles/
  experience, single source; llm_rank now imports it, removing its duplicate) and fed a slim
  profile into jd-understander's user prompt; `fit_notes` instruction now demands
  candidate-specific angling. Re-tested → Grok's fit_notes became candidate-specific
  (names the real Python/cloud/automation strengths). [§5] *(owner request 2026-07-01)*
- 2026-07-01 — env — LLM keys — rotated the active Groq key to a new `gsk_…` (verified live);
  kept the previous Groq key as a commented fallback in `.env`. Stored `DEEPSEEK_API_KEY`
  but it returns **"Insufficient Balance"** (billing not enabled) — `--llm deepseek` is
  unusable until credit is added; use `--llm grok` (Groq, with the 429 retry) meanwhile. [§9]
- 2026-07-01 — profile-matcher/llm_rank — compare is DEV-only — the Grok-vs-Claude
  comparison (`--compare/--vs/--emit`, agreement metric) was a prompt-TUNING aid done by the
  orchestrator (Claude), NOT a shipped user command — removed from the CLI. The tuned prompt
  is retained (judge JD DUTIES not the title; penalize explicit senior markers; flag fluff
  JDs); it matched Claude's own ranking at Spearman ρ≈0.85. The ranker now just outputs the
  ranking (+ `--save`). [§5] *(owner clarification 2026-07-01)*
- 2026-07-01 — store/main — rejected list + incremental efficiency — new terminal status
  `rejected`. `_auto_reject` moves off-profile `matched` jobs → `rejected` (automatically
  after `search`, or via `main.py reject`); `main.py rejected` lists them. Rejected jobs are
  excluded from `lists`/`rank`/`prep`, so the LLM only ever processes RELEVANT jobs (token
  saving). Re-search is naturally incremental: `upsert_jobs` dedups on `(source, ext_id)`
  keeping the existing status, and `match.py` only advances NEW `scraped` rows — so
  already-applied / ready / rejected jobs are never re-scored or re-prepped the next day. [§3/§5]
  *(owner request 2026-07-01)*
- 2026-07-01 — prep — needs-mod + note — `prep --needs-mod` targets non-best (needs_mod)
  jobs; `cmd_prep` prints a NOTE whenever non-best jobs will be applied with an
  LLM-MODIFIED résumé, and a second NOTE if `--modify-resume` also tailors eligible jobs
  the master already fits. `--modify-resume` stays OPTIONAL (needs-mod jobs are tailored
  by default; master résumé never changed). [§5] *(owner request 2026-07-01)*
- 2026-07-01 — profile-matcher — LLM rerank (Grok) — NEW `llm_rank.py`: reranks a
  shortlist (top Python matches / `--eligible` / `--jobs`) by résumé fit using the LLM
  (grok/api), weighing skill overlap + role alignment + required-years-vs-~2yrs (heavily
  penalizes senior/lead/manager + far-higher-year roles). `main.py rank --llm grok
  [--compare] [--save] [--eligible] [--jobs] [--limit N]`. Driven via the shared
  `execution/llm.py` with **temperature=0** (added to `complete`) → deterministic,
  repeatable order. JD snippet jumps past "About us" fluff to the requirements; profile =
  résumé summary + skills + tagline target-roles + experience level. Persists new
  `llm_score`/`llm_reason` columns (schema + idempotent migration). The deterministic
  Python matcher stays the free coarse ranker; the LLM is the reranker on top. Live result:
  Grok demotes senior/management + pure-pentest roles the Python score over-ranked (e.g.
  Virtusa "Security Management Engineer" Py#1→LLM#18) and elevates cloud/automation/appsec
  roles matching the candidate's real CloudSploit/Python work. [§5/§9 — augments the
  "profile-matcher deterministic" decision: LLM rerank is additive, not a replacement]
  *(owner request 2026-07-01)*
- 2026-07-01 — llm.py — temperature — `complete(..., temperature=None)` threaded to both
  the Anthropic and Groq backends (added to the request payload only when not None, so the
  provider default is unchanged otherwise). Used by llm_rank for temperature=0. [§9 LLM access]
- 2026-07-01 — humanise-responder — years rule — every drafted answer must WEIGH the
  role's required years/seniority (`jd_brief.seniority` / `must_have`) against the
  candidate's ~2 years: if the posting asks for more, name the gap honestly and lead with
  transferable depth; NEVER claim more years/seniority than the profile shows; if on-level,
  say so. Baked into `SYSTEM_PROMPT`. [§5] *(owner request 2026-07-01)*
- 2026-07-01 — LLM provider — nvidia — NVIDIA NIM added as `--llm nvidia` provider (integrate.api.nvidia.com/v1, OpenAI-compatible, reuses grok backend). Default model: `nvidia/llama-3.3-nemotron-super-49b-v1`; backup: `meta/llama-3.3-70b-instruct`. Both XAI_API_KEY and GROK_API_KEY cleared in DeepSeek+NVIDIA env dicts to prevent stale Groq key leak. Backup fires only on model-specific errors (not 429/401/403). [§9 LLM access]
- 2026-07-01 — LLM prompts — <think> suppression — all four `_extract_json` functions strip `<think>…</think>` traces (closed + unclosed tag fallback). All four SYSTEM_PROMPTs updated with explicit "no <think> tags" instruction. [§5]
- 2026-07-01 — job-scraper/remoteok — portal plugin — RemoteOK added via public JSON API (`remoteok.com/api`). Pure stdlib urllib (no Apify). `?tag=<first_query_word>` server-side pre-filter; client-side keyword filter on HTML-stripped title+tags+description. `html.unescape()` + `<script>`/`<style>` stripping in `_strip_html`. Empty query words → accept-all (no silent zero results). `id=None` guard (not `or ""`) to preserve falsy-but-valid IDs. `is_available=True` always. [§4/§10]
- 2026-07-01 — verbosity — all scripts — `JOBSEARCH_VERBOSITY` env var (0/1/2) is the shared verbosity contract. Set by `main.py` top-level `-v`/`-vv` before dispatch; threaded into subprocesses via `{**os.environ}` spread. Scripts call `apply_verbosity(args)` taking `max(cli, env)` so parent-set level can't be downgraded. Level 0 = unchanged default output (print); level 1 = `vprint(1)` per-job context (stderr); level 2 = `vprint(2)` raw LLM payloads (stderr). All 7 skill scripts wired via `execution/log.py`. [§3 Execution conventions]
- 2026-07-01 — ordering — all display lists & prep batches — sort by COALESCE(llm_score, match_score) DESC within each classified set (eligible / needs_mod); score is primary key, newest id tiebreak; `is not None` guard used (not `or`) to correctly honour an explicit llm_score=0.0. Classification thresholds in `eligibility.py` unchanged. [revises §9 2026-06-30 location-preference / apply ordering]
- 2026-07-04 — LLM provider — nvidia — model defaults updated: primary `moonshotai/kimi-k2.6` (best seniority-rule calibration 6/6, bare JSON, 1.6/4.7 s on rank/understand); backup `mistralai/mistral-large-3-675b-instruct-2512` (fastest 1.3/2.8 s). Previous default `nemotron-super-49b-v1` and backup `llama-3.3-70b-instruct` retired — llama-3.3-70b and deepseek-v4-pro both timeout at 150 s on free NIM tier (queue-starved, not cold-start). `NVIDIA_SYSTEM_PREFIX="detailed thinking off"` added to disable Nemotron reasoning-mode (prevents <think> from burning the token budget); wired via `LLM_SYSTEM_PREFIX` env key into `_complete_grok`. Re-runnable benchmark: `scripts/bench_nvidia.py`. [§8 Phase 10 / main.py:74-88 / execution/llm.py:240]
- 2026-07-04 — eligibility — STRETCH tier — new 4th classification `stretch`: security title + no scope gap + 20 ≤ score < 45. Keeps viable low-fit roles (pure pentest 4yr, Principal-titled IC, Senior-title with no management JD signals) as an opt-in heavy-rewrite pile instead of silently dropping them. `off_profile` is now HARD-NOs only (non-security, has_scope_gap(), score < STRETCH_FLOOR=20). `has_scope_gap()` splits title-seniority (soft, tailorable) from scope-seniority (hard: manages people, 6+ yr requirement, strategy). `--stretch` selector added to `prep`. [§8 Phase 10 / execution/eligibility.py / main.py:_print_lists/_auto_reject/cmd_prep]
- 2026-07-04 — docs — joblister + job-portal lists — `docs/joblisters.md` (aggregators + verified public JSON URLs); `docs/job_portals.md` (individual company career sites grouped by ATS platform, live-verified JSON patterns, owner's target companies mapped to slugs). Feeds the future ATSPlugin pass (§10). [§8 Phase 10]
- 2026-07-05 — job-scraper — joblister plugins — built `remotive.py`, `arbeitnow.py`, `jobicy.py`, `himalayas.py`, `themuse.py` (public-JSON, no auth) plus shared `_joblister_util.py`; all auto-discovered via the existing registry, no other code changes needed. Owner-approved scope for this pass: joblisters only; ATS jobportals deferred. [§8 Phase 10 / .claude/skills/job-scraper/plugins/]
- 2026-07-05 — job-scraper — ATS jobportal plugin design (locked, build deferred) — one plugin per ATS platform (`greenhouse.py`, `lever.py`, `ashby.py`, …) reading company slugs from a `.env` list (e.g. `GREENHOUSE_COMPANIES=crowdstrike,wiz,snyk`), NOT one file per company. **Revises §10**, which specified a tiny subclass file per company. Seed companies not yet chosen — deferred to the build pass. [§10]
- 2026-07-05 — job-scraper — ATS jobportal plugins built — `greenhouse.py`/`lever.py`/`ashby.py` implement the design above. New requirement discovered during test (not in the original design note): multi-company `*_COMPANIES` lists must be merged **round-robin across companies**, not concatenated — concatenation lets the first company alone fill a small `--limit` and silently starve every other configured company, defeating the reason a multi-company list exists. `_ats_util.round_robin()` fixes this; any future ATS plugin (SmartRecruiters, Workday, …) MUST use the same merge, not per-company sequential accumulation. [§8 Phase 10 / .claude/skills/job-scraper/plugins/_ats_util.py]
- 2026-07-05 — job-scraper — 5 more ATS jobportal plugins built — `smartrecruiters.py`, `recruitee.py`, `bamboohr.py`, `workday.py`, `workable.py` built following the greenhouse.py pattern. **Eightfold explicitly dropped from this pass** — its public API is CSRF/session-gated (403 on every company/method/header combo tried), not a bare public endpoint; needs a future Playwright custom plugin, not an ATS-JSON one. [§8 Phase 10 / §10]
- 2026-07-05 — job-scraper/workday — page size — Workday's CxS API server-enforces a **max page size of 20** (21+ → HTTP 400); `workday.py` paginates via `offset` in 20-item pages up to 5 pages/company rather than requesting one large page. Any future Workday-derived plugin work must respect this cap. [.claude/skills/job-scraper/plugins/workday.py]
- 2026-07-05 — job-scraper/workday — detail URL — the CxS detail endpoint is `.../wday/cxs/<tenant>/<site><externalPath>` with NO extra `/job` literal — `externalPath` from the list item already starts with `/job/...`; prepending an extra `/job` 404s every detail call silently (caught by the per-item try/except, so it doesn't crash — it just strips `jd_text`/`posted_at` from every result). [.claude/skills/job-scraper/plugins/workday.py]
- 2026-07-05 — job-scraper/workable — request body — the POST body must be `{"query": "..."}` ONLY; an added `"limit"` key causes `{"limit":"Not allowed"}`. The list payload has no company/description/URL field at all: `jd_text` is always `None` for this platform (no working JSON detail endpoint found — `/api/v3/.../jobs/<shortcode>` and the older `/api/v1/widget/...` both 404), and `Job.url` is constructed as `apply.workable.com/<co>/j/<shortcode>/` (verified live, 200). [.claude/skills/job-scraper/plugins/workable.py]
- 2026-07-05 — job-scraper — cybersecurity company list — live-verified (curl + role-keyword match against the candidate's résumé) across all 8 working ATS platforms; owner's 7 named targets resolved (Qualys→Workday, Mattel→SmartRecruiters configured; Qualcomm→Eightfold blocked; Simbian→Zoho deferred; Sibros→Rippling and EY→SAP SuccessFactors out of scope, not one of the 9 platforms; cyber-times.in skipped, offline). Full list + rejected/dead slugs in `docs/job_portals.md`. `.env` populated accordingly (real secrets file, not committed). Ranking of individual jobs is downstream (`profile-matcher` `match_score`), not filtered at scrape time — the company list is a quality gate only (must have ≥1 role matching the candidate's target roles to be included), never a per-job filter. [§8 Phase 10 / §10 / docs/job_portals.md]
- 2026-07-01 — orchestrator/apply — best-match-only apply set — only **eligible best
  matches** (security title + score ≥ ELIGIBLE_SCORE 70 + coverage ≥ 0.6) are prepped to
  `ready` and applied; sub-threshold jobs (e.g. Kobie 58.5) and clear year-gap roles (e.g.
  Alignity, 5-8 yrs vs ~2) are parked via `log --outcome skipped` (reversible). Going
  forward, prep targets eligible jobs only, so the apply queue stays best-match. [§5/§11]
  *(owner request 2026-07-01)*
- 2026-06-30 — jd-understander — design — reads `matched` jobs lacking a `jd_brief`
  (the work queue), writes a **strict-JSON brief** to `jd_brief` and LEAVES status at
  `matched` (resume-tailor advances it). Brief schema (`BRIEF_KEYS`): company_summary,
  role_summary (both required), key_tools, must_have, nice_to_have, keywords (ATS),
  seniority, red_flags, fit_notes. Runs in all three LLM modes: `session`=prepare
  (writes `.tmp/jd-understander/prompts.json`)→orchestrator writes briefs to
  `answers.json`→save; `api`/`grok`=`run` loops calling `llm.complete`. Per-job
  persistence = resumable; `--limit` caps cost in api/grok mode; `_extract_json`
  tolerates fenced/prose model output. [§5]
- 2026-07-06 — job-scraper — company-research routing (backfilled 2026-07-07, decided
  2026-07-06) — **Bucket A/B/C classification** for every candidate company found during
  Hyderabad/Bengaluru research: **Bucket A** = already on a known/built ATS platform → add
  directly to that platform's `.env` list, no new code. **Bucket B** = a KNOWN platform that
  is deliberately deferred (needs auth/OAuth, confirmed blocked, or just not built yet) →
  flagged in `docs/job_portals.md`, not one-off'd around. **Bucket C** = a genuinely custom
  career site with no known ATS underneath → candidate for a thin plugin built on
  `_career_util.py`. This routing rule is applied BEFORE writing any plugin code — it's what
  stopped one-off bespoke plugins from being written for companies that turned out to be on
  an already-built platform (e.g. Saviynt looked like custom HubSpot CMS, was actually plain
  Lever; InstaSafe looked like a custom Gatsby SPA, was actually a public Zoho Recruit
  endpoint). [§4/§10]
- 2026-07-06 — job-scraper — `_career_util.py` (backfilled 2026-07-07, decided 2026-07-06) —
  NEW shared-helper module, sibling to `_ats_util.py`, specifically for Bucket-C bespoke
  career sites (one plugin per site, not one plugin per many companies). Provides a 3-tier
  fetch ladder a bespoke plugin should try in order: (1) `fetch_html`/`fetch_json` — plain
  HTTP, most modern pages still server-render their initial job list for SEO; (2)
  `extract_next_data`/`extract_ld_json`/`extract_window_var` — pull a JSON blob out of
  otherwise-JS-rendered HTML (covers "looks like an SPA but the data is still in the page
  source"); (3) `render_html` — LAST RESORT, a fresh headless Chromium tab via Playwright
  (no persistent login profile — public career pages need none), gated behind
  `playwright_available()`. Also centralizes `job_id_from_url`, `mmddyyyy_to_iso`/
  `ddmmyyyy_to_iso` date parsing, and (added Wave 2, 2026-07-07) `extract_by_itemprop` for
  schema.org microdata. Every bespoke plugin should use the LOWEST tier that works rather
  than defaulting to Playwright. [§4]
- 2026-07-06 — job-scraper — Hyderabad/Bengaluru scope (backfilled 2026-07-07, decided
  2026-07-06) — owner-directed company-research scope: **Hyderabad and Bengaluru offices
  only** this wave (no other Indian cities yet); **security-first, then broadening to all-IT**
  — i.e. exhaust cybersecurity-relevant companies on a platform before adding general-IT
  companies as filler. **Pune and Delhi are an explicitly deferred future addition**, not
  in scope for Wave 1 or Wave 2. This scope directly carried into Wave 2's populate-pass
  priority order (product security → security services → general IT tail) below. [§10]
- 2026-07-07 — job-scraper — Wave 2 scope/priority (owner decision 2026-07-06, this session)
  — explicit owner-directed order for Wave 2: (1) build NEW ATS-PROVIDER plugins first —
  each provider unlocks many companies, higher leverage than one-off bespoke sites; (2) only
  once a provider is built, populate it with real companies in priority order: cybersecurity
  PRODUCT companies → cybersecurity SERVICES/consultancy companies → general IT companies
  (tail only, not the focus); (3) **stay security-first, Hyderabad/Bengaluru** — do not
  broaden to all-IT or add Pune/Delhi this wave (extends the Wave-1 scope decision above).
  Candidate provider order (research-backlog priority, owner can re-prioritize): Oracle
  Fusion → SuccessFactors → Rippling → iCIMS → Cutshort → Darwinbox → Freshteam → Avature
  (lowest priority — per-company microsites, historically assumed browser-only). [§4/§10]
- 2026-07-07 — job-scraper — Oracle Fusion Cloud Recruiting plugin BUILT — Tier-1 JSON API,
  no auth. List: `GET https://<host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions
  ?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=<site>,keyword=<query>,
  limit=<n>,offset=<n>,sortBy=POSTING_DATES_DESC` (the `expand=requisitionList` param is
  REQUIRED — omitting it returns only search-criteria echo, no job items). Detail:
  `.../recruitingCEJobRequisitionDetails?...finder=ById;Id="<jobId>",siteNumber=<site>`.
  Identifier is `host:site` (or `host:site:Display Name`) — host varies per tenant/region, site
  is a `CX_<N>` number read off the tenant's own careers URL path. Oracle's own docs call this
  REST resource "internal use only" but every tenant's public careers page calls it
  client-side with zero access control. `parse_oraclefusion_companies` added to `_ats_util.py`.
  Query is sanitized (commas/semicolons stripped) before interpolating into the `finder`
  mini-language string (both are the language's own delimiters). [§4/§10]
- 2026-07-07 — job-scraper — SAP SuccessFactors (CSB2) plugin BUILT — NO bare public JSON API
  (`/odata/v2/JobRequisition` needs per-tenant HTTP Basic Auth, confirmed live 401) — Tier-2
  instead: List via the tenant's own public `sitemap.xml` (iterated as `<url>` blocks via
  lazy `finditer`, genuinely bounded at `_MAX_SITEMAP_ENTRIES`, not a fully-materialized
  `findall`), cheap-filtered by the job URL's own slug before any detail fetch. Detail via the
  job's own URL — title/description via schema.org microdata (`itemprop="title"`/
  `"description"`, extracted by the new `_career_util.extract_by_itemprop` HTMLParser-based
  extractor — built because no existing plugin needed this level of HTML parsing). Location/
  posted-date sit in a `joblayouttoken-label` "Label: value" template — tenant-specific which
  labels are populated (W.L. Gore uses "State/Province", City of London uses "Office
  Location") — tried via an ordered candidate-label tuple, first hit wins. Identifier is the
  full tenant hostname (`<subdomain>.jobs.hr.cloud.sap`), not a bare slug. Only the CSB2
  template flavor is covered; the older "Career Portal" flavor (stateful DWR/AJAX-RPC) is
  out of scope. [§4/§10]
- 2026-07-07 — job-scraper — Rippling ATS plugin BUILT — Tier-1 JSON API, no auth. List:
  `GET https://ats.rippling.com/api/v2/board/<slug>/jobs?page=<n>&pageSize=<n>&
  searchQuery=<query>`. Detail: `.../jobs/<uuid>`. Identifier is a bare board slug (same shape
  as Greenhouse). Cleanest/simplest of the Wave 2 providers — no template variance, no auth
  quirks found across 4 live-tested tenants. [§4/§10]
- 2026-07-07 — job-scraper — iCIMS plugin BUILT (reversing the Wave-1 "HTML-only, needs
  browser" assessment) — genuinely Tier-2, no browser needed. The bare `/jobs/search`
  endpoint returns an empty client-side Angular shell UNLESS a `pr=<page>` (1-indexed) query
  param is present, in which case the SAME domain returns real server-rendered job rows —
  this `pr=N` requirement is what made the platform look JS-rendered-only in the original
  Wave-1 pass. List: `GET https://careers-<slug>.icims.com/jobs/search?pr=<page>&
  in_iframe=1`, HTML row-scraped. Detail: the job's own URL, schema.org JSON-LD `JobPosting`
  block. No working RSS/XML feed exists on this platform (4 path variants tried, all dead) —
  corrects a prior docs claim. iCIMS's own JSON-LD uses the literal string `"UNAVAILABLE"` as
  a sentinel for a blank address field — filtered out in `_location_from_ld`, or it leaks into
  stored `location` values. [§4/§10]
- 2026-07-07 — job-scraper — Cutshort plugin BUILT (unlocks Appknox) — the company page's own
  Next.js `__NEXT_DATA__` genuinely embeds the full job list server-side (a shallower Wave-1
  check saw `dehydratedState: null` — a cache/edge-case on that one check, not the platform
  default). List path: `dehydratedState.queries[?].state.data.data.pageData.companyJobs.jobs`
  (query matched on `queryKey == ["companyPageData", <alias>]`). Detail: the job's own
  `publicUrl`, schema.org JSON-LD `JobPosting` block (title/description/datePosted preferred
  over the list call's own blurb/`hiringIntentShownOn`, which is ambiguous). `validThrough`
  (an application deadline) is deliberately never used as a `posted_at` fallback — same
  reasoning as iCIMS. Scaling signal: `cutshort-data.s3.amazonaws.com/.../companies-sitemap.xml`
  lists 15k+ company URLs (not used for auto-discovery this wave, but confirms the platform's
  breadth). Identifier is the FULL opaque `/company/<alias>` string (parens/suffix and all),
  copied verbatim from the company's own page — no shorter/guessable form exists. [§4/§10]
- 2026-07-07 — job-scraper — Darwinbox plugin BUILT (unlocks Seclore) — the SPA shell is
  genuinely empty (confirms the Wave-1 finding) but the frontend's own `main.<hash>.js`
  bundle reveals a clean public, unauthenticated same-origin JSON API
  (`environment.apiURL = "/ms/candidateapi/"`). List: `GET https://<tenant>.darwinbox.{in,com}
  /ms/candidateapi/job?page=<n>`. Detail: `.../job/<id>`. Tenant TLD (`.in` vs `.com`) is
  auto-probed, not configured, since it varies per tenant with no public pattern. The detail
  call's `jd` field is DOUBLE HTML-encoded (the JSON string itself contains literal
  `&lt;div&gt;` entity sequences representing real markup that was itself entity-escaped) —
  requires `html.unescape()` once FIRST, then `strip_html()` a second time to actually strip
  the revealed tags; this exact double-unescape pattern recurred for Freshteam below and is
  now a documented, reusable fix. [§4/§10]
- 2026-07-07 — job-scraper — Freshteam plugin BUILT (reversing a Wave-1 "auth-gated,
  `/api/*` returns 401" assessment) — the confirmed-401 `/api/portal/jobs` path is a
  credential-gated RECRUITER-portal endpoint, unrelated to public job listing. The actual
  public `/jobs` career page is genuinely server-rendered HTML (job cards regex-scraped
  directly, no API call of any kind needed — the simplest plugin built this wave). Detail via
  the job's own URL, schema.org JSON-LD `JobPosting` block, same double-HTML-encoding fix as
  Darwinbox applied to the `description` field. No pagination signal was found on any tested
  tenant's `/jobs` page (every tenant's full list rendered on one page) — the plugin fetches
  page 1 only; revisit only if a tenant with a genuinely multi-page list is found. [§4/§10]
- 2026-07-07 — job-scraper — Avature plugin BUILT (reversing the Wave-1 assumption that
  Avature needs a full headless-browser approach, based on the existing bespoke
  `synopsys.py` Playwright plugin) — the common `SearchJobs`/`JobDetail` template family is
  genuinely server-rendered HTML across unrelated tenants, no JS needed; `synopsys.py`
  remains a separate bespoke plugin for that one company's specific site, unaffected. TWO
  card-layout generations were found and are both supported: generation (a — Xerox/Bloomberg/
  Koch-style) uses labeled City/State/Country `<p>` tags and a `<meta name="Description"
  content="...created DD-Mon-YYYY">` posted-date; generation (b — ManTech-style) uses plain
  `list-item-location`/`list-item-posted` spans directly on the list card instead (tried as a
  fallback when generation (a)'s labels aren't found). An OLDER, unsupported generation exists
  (KPMG US: `JobDetail?jobId=` query-param URLs, different CSS classes) — not built against
  speculatively; revisit only if a target company is actually found on it. Identifier is the
  FULL hostname (some tenants use a custom CNAME domain, e.g. ManTech's `careers.mantech.com`,
  not just `<tenant>.avature.net`). Lowest-priority provider per the Wave 2 scope decision
  above — built last, populated with only 1 clearly-relevant company (ManTech) plus the
  original non-security smoke-test seed (Xerox, kept rather than removed). [§4/§10]
- 2026-07-07 — job-scraper — Wave 2 populate pass — cybersecurity companies added, live-
  verified with real open postings before adding, in the product→services→general-IT order
  per the Wave 2 scope decision: Oracle Fusion +Fortinet (security product, 789-910 live
  jobs — NOTE: Wave 1's `docs/job_portals.md` had flagged Fortinet as "no static/rendered
  signal found, likely bot-protected" researching a DIFFERENT platform/domain; it resolves
  cleanly on Oracle Fusion under a different host, correcting that Wave-1 finding), +Kroll
  (risk-advisory w/ a real Cyber Risk practice). Rippling +RSA Security, +Swimlane (SOAR
  vendor), +Agency Cybersecurity (GRC/compliance MSSP), +Workstreet (GRC/compliance
  consultancy). Cutshort +Innefu Labs, +Securin Labs, +Metron Security, +SecurEyes. Darwinbox
  +Quick Heal Technologies, +ReBIT (pure-play security vendors proved rare on this platform —
  reported honestly as 2 found, not padded). Freshteam +Payatu, +Strobes Security. Avature
  +ManTech. SAP SuccessFactors: **0 companies added** — ~50 major security vendors/MSSPs
  checked, NONE resolve on the CSB2 flavor (all on Workday/Ashby/a legacy SF flavor instead) —
  a genuine platform-adoption gap, not a search shortfall. iCIMS: **0 companies added** — the
  only strong hit (Peraton) sits behind an inconsistent AWS WAF challenge and is a tier-2/3-
  border diversified govcon, not a pure-play vendor — excluded as unreliable rather than
  forced in. Full company list + exclusion reasoning in `docs/job_portals.md`. [§4/§8/§10]
- 2026-07-07 — job-scraper — Avature template-variance fix (found during the populate pass,
  not the initial build/review/test loop) — adding ManTech exposed that `_JOB_BLOCK_RE`/
  `_TITLE_LINK_RE` were written too strictly against Xerox's exact markup (an EXACT class-list
  match with nothing else before `">`, and a literal `<a href="` with `href` required as the
  first attribute) — ManTech's real markup has an extra CSS class + a trailing `id=` attribute
  on the article tag, and `class="link"` BEFORE `href` on the title anchor; both silently
  matched zero rows with no error. Fixed by loosening both regexes to tolerate attribute
  order/extra attributes (word-boundary + `[^>]*` instead of an exact string match). Re-review
  + re-test both PASSED after the fix, live-verified against both tenants together (no
  regression on Xerox). Self-annealing lesson: a single-tenant smoke test during the initial
  build is NOT sufficient proof a scraping regex generalizes — a second real tenant during
  populate is often the first real cross-tenant-variance test a plugin gets. [§4/§10]
- 2026-07-07 — job-scraper — Wave 3 scope [revises the Wave-2 scope entry] — owner direction:
  broaden company scope from **security-first to all-IT** ("as it's an ATS expansion, it's
  anyway easier to add companies via ATS, let's have all IT now via the ATS") across the 17
  already-built ATS platforms. City scope UNCHANGED — Hyderabad/Bengaluru only, Pune/Delhi
  still deferred (owner chose the all-IT-broadening option specifically, not the
  also-broaden-cities option, when asked). No new platform-discovery work — this is a
  company-discovery + `.env` populate pass on already-built platforms, continuing to apply the
  existing Bucket A/B/C routing rule for anything that doesn't fit. Confirmed no free public
  "directory of companies by ATS platform by city" exists (re-checked the Wave-1 finding live)
  — the only third-party option found, fantastic.jobs, is a paid job-aggregator API outside
  this project's free-API design; not used. [§4/§10, revises the 2026-07-06 Wave-2 scope entry]
- 2026-07-07 — job-scraper — Wave 3 populate pass — company discovery ran in 4 parallel research
  batches (Batch A: Greenhouse/Lever/Ashby; Batch B: Workday/SmartRecruiters/Recruitee/
  Workable/Zoho Recruit/BambooHR; Batch C: Oracle Fusion/SuccessFactors/Rippling/iCIMS/Avature;
  Batch D: Cutshort/Darwinbox/Freshteam), each live-verifying every candidate (real HTTP
  request, real Bengaluru/Hyderabad-located open posting) before recommending it — same quality
  gate as every prior wave. **Result: 119 → 371 companies, 17 → 18 platforms/plugins** (count
  includes the one custom Synopsys plugin). Per-platform new-company counts: Greenhouse +40,
  Lever +28, Ashby +28, Workday +24, SmartRecruiters +11, Recruitee +4, Workable +9, Zoho
  Recruit +12, BambooHR +1 (FIRST-EVER customer on this platform — Atlas Systems), Oracle Fusion
  +7, SAP SuccessFactors +0, Rippling +16, iCIMS +5, Cutshort +51 (largest single addition —
  mined the platform's own public `companies-sitemap.xml`, 15k+ URLs, by IT-brand keyword
  sampling rather than search-engine dorking), Darwinbox +6, Freshteam +10, Avature +0.
  Discovery reused the proven wave-1 `site:<platform-domain> "Bengaluru"/"Hyderabad"`
  search-engine-indexing technique (dropping the security-keyword filter for a city filter) for
  every platform with a shared indexed job-board domain — this wave discovered Rippling's own
  board pages are ALSO search-indexed under `ats.rippling.com` (not previously known), and that
  Oracle Fusion's `hcmUI/CandidateExperience` URL segment is indexed too, plus that Oracle's
  location master data still tags many tenants "Bangalore" not "Bengaluru" (materially changed
  yield once tried). Full company list + exclusion reasoning (aggregator/staffing-agency traps,
  wrong-city, non-IT-sector, dead/stale) lives in `docs/job_portals.md` "Wave 3" section + each
  platform's `.env` comment block. `docs/supported_companies.md` remains the single
  auto-generated source of truth for the live company list — `docs/job_portals.md`'s per-
  platform sections are narrative context, never hand-duplicated. [§4/§8/§10]
- 2026-07-07 — job-scraper — icims.py pagination bug fix (found during the Wave 3 populate
  pass, not the original build/review/test loop) — `_fetch_candidates`'s loop was
  `range(1, _MAX_LIST_PAGES + 1)` (1-indexed: `pr=1,2,3,4`), but iCIMS's real pagination is
  0-indexed — confirmed live that `pr=0` and `pr=1` return GENUINELY DIFFERENT, non-overlapping
  20-job sets on every tenant tested, including the pre-existing `here` tenant (meaning this
  plugin had been silently missing every tenant's entire first page of postings since it was
  built in Wave 2). Three Wave-3 candidates (`c1`/ConvergeOne, `shure`/Shure, `ideagenen`/
  Ideagen) return ZERO rows at `pr=1` and ONLY have content at `pr=0` — they would have silently
  scraped 0 jobs forever under the old code despite being genuinely live, verified tenants.
  Fixed by changing the loop to `range(0, _MAX_LIST_PAGES)`. Live-reverified after the fix: all
  3 previously-broken tenants now return real jobs, and `here` gained 20 previously-invisible
  postings with zero overlap against its old result set (confirmed via id-set diff) — no
  regression. Self-annealing lesson: a "no results" scrape from a live, verified endpoint should
  prompt checking the pagination indexing assumption before concluding the tenant has no jobs —
  a bare curl repro attempt is also vulnerable to iCIMS's own intermittent AWS WAF bot-challenge
  (the same one that made Peraton unreliable in Wave 2), which can look identical to a real "0
  results" response; always re-verify through the plugin's own `fetch_html` (proper headers)
  before trusting a raw curl's negative result. [§4/§10]
- 2026-07-07 — job-scraper — ashby.py URL-encoding fix (found during the Wave 3 populate pass)
  — `_fetch_company` interpolated the configured slug directly into the request URL with no
  escaping; a genuine live Ashby company ("Redesign Health") has a slug containing a literal
  space, which raised `http.client.InvalidURL` (urllib refuses a raw space in a URL path) —
  this company was fully verified-live via curl (with the space percent-encoded) but would have
  crashed the plugin's own fetch if added to `.env` as-is. Fixed by wrapping the slug in
  `urllib.parse.quote(slug, safe="")` before building the request URL — verified both the
  space-containing slug and a normal slug (`sarvam`) work correctly after the fix, no
  regression. [§4/§10]
- 2026-07-07 — job-scraper — plugins directory relocated [revises §4's location line] — owner
  request: moved `plugins/` out of `.claude/skills/job-scraper/` to the REPO ROOT (`git mv`,
  history preserved). Rationale: the plugins are plain scraping code with no Claude-specific
  dependency — usable by any orchestrator/LLM, not just Claude Code — and `.claude/` should hold
  Claude Code configuration (skills, agents, plan files), not general application code. This
  matches the existing precedent of `data/` (store module) and `execution/` (llm.py,
  candidate.py, etc.) already being top-level, non-`.claude` packages. Code changes: `_apify_keys.py`'s
  repo-root path resolution (`Path(__file__).resolve().parents[N]`) updated from `parents[4]` to
  `parents[1]` (one less directory level to climb from the new location); `scrape.py` simplified
  — `plugins` is now a top-level package importable once the repo root alone is on `sys.path`,
  so the separate `SKILL_DIR` sys.path entry it used to need is no longer necessary;
  `list_companies.py`'s `_PLUGINS_DIR` construction updated to `_REPO_ROOT / "plugins"`. Every
  individual `<platform>.py` plugin file needed ZERO changes — they all use a self-relative
  `Path(__file__).resolve().parent` pattern that works correctly regardless of where the folder
  lives, by design. `.claude/skills/job-scraper/scripts/scrape.py` and `list_companies.py`
  themselves stay put (only the `plugins/` package moved, not the skill's own scripts).
  Live-reverified after the move: `scrape.py --list` (27 plugins, no import errors),
  `list_companies.py` (371 companies, unchanged), a live Ashby scrape (5 jobs), and `scrape.py
  --keys` (exercises `_apify_keys.py`'s fixed path resolution) — all pass, no regression.
  Historical §8/§9 log entries referencing the OLD `.claude/skills/job-scraper/plugins/` path
  are left as-is (append-only convention — they accurately describe what was true on the date
  logged); only current-state spec references (§4's location line, `CLAUDE.md`, `SKILL.md`,
  `docs/job_portals.md`, `docs/joblisters.md`) were updated to the new location. [§4]
- 2026-07-10 — job-scraper — 11 new joblister plugins added (owner request) — Working Nomads,
  Best PM Jobs, SkipTheDrive, NoDesk, Remote100k, NextGen Energy Jobs, Relocate.me, Jobs in
  Education, Rejobs, We Love Product, CareerHound. All Bucket-B listers (one plugin = one
  board indexing many employers), cloning the existing public-feed pattern
  (`_joblister_util.py`/`_career_util.py`) — no registry/store changes needed (auto-discovery).
  Two sites the owner also named were resolved as follows: **startupers.com dropped** — its
  original startup-jobs board has shut down; the domain now hosts an unrelated "AI Agent
  Job Marketplace" (lists AI agents, not human postings) — no plugin built.
  **skillset.co dropped** — Cloudflare-blocked (403) and its identity could not be verified
  (the name maps to several unrelated sites: IT-cert prep, staffing agencies); skipped rather
  than guessing. **remote.co deferred** to the §10/§8 backlog — Cloudflare + FlexJobs-paywall
  gated, every fetch attempt timed out live, no RSS/API/sitemap found; low ROI vs. fragility.
  Live-verified per-site access method (2026-07-10): Working Nomads = public JSON API
  (`/api/exposed_jobs/`); Best PM Jobs = Jobboardly RSS (`/jobs.rss`); SkipTheDrive = NO
  working RSS (feed disabled site-wide — every `/feed`/`/?feed=rss2`/category-feed path
  silently returns the homepage HTML, not XML) so it scrapes the server-rendered `?s=<query>`
  search-results HTML directly instead; NoDesk = RSS (`/remote-jobs/index.xml`); Remote100k =
  sitemap (`/sitemap.xml`) → `/remote-job/<slug>` pages with `JobPosting` ld+json (no render
  needed — `robots.txt` disallows `/api/`, honored, not hit); NextGen Energy Jobs = sitemap
  INDEX (`/sitemap.xml` → `job_openings_*.xml` children) → Next.js pages needing a Tier-3
  headless render (no ld+json/`__NEXT_DATA__` — data streams via RSC payload); Relocate.me =
  sitemap (`/sitemap.xml`, filtered to trailing-numeric-id URLs) → `JobPosting` ld+json;
  Jobs in Education = SmartJobBoard RSS (`/feeds/rss.xml`); Rejobs = Atom feed
  (`/en/rss/renewable-energy-jobs`); We Love Product = public JSON API (`/api/jobs`,
  unauthenticated calls capped at page 1 / ~32 of the true total — deeper pages 401);
  CareerHound = entirely login-gated (every unauthenticated route 404s, no public
  preview/API/RSS/sitemap), built as a session-gated Playwright plugin (same
  `PLAYWRIGHT_USER_DATA_DIR` model as the custom-plugin template) with GENERIC href-pattern
  card extraction rather than unverifiable hardcoded selectors — **flagged as needing live
  verification** the first time a real logged-in session is available (self-anneal per
  SKILL.md); a wrong-guess selector would otherwise silently return 0 rows forever. [§4/§8/§10]
- 2026-07-10 — job-scraper — Indeed reworked to browser-first with Apify fallback (owner
  request) [revises the 2026-06-30 Apify-only Indeed decision] — primary fetch is now a
  headless-browser scrape of `indeed.com/jobs?q=...&l=...` (a plain `fetch_html` request gets
  HTTP 403, but a Playwright render succeeds — see the `render_html` UA fix below); the
  results are embedded as a JS object assigned to
  `providerData["mosaic-provider-jobcards"]` inside a `<script>` tag — too deeply nested for
  a non-greedy regex, so extracted via `json.JSONDecoder().raw_decode(html, idx)` instead
  (parses one complete JSON value and ignores trailing script content, unlike a plain
  `json.loads` which raises "Extra data"). Card fields verified live: `jobkey` (stable id),
  `viewJobLink` (relative detail URL), `company`, `displayTitle`, `formattedLocation`,
  `createDate` (epoch MILLISECONDS — divided by 1000 before `epoch_to_iso`), `snippet` (short
  HTML teaser, not the full JD). The original Apify actor (`borderline/indeed-scraper`) is
  kept as an automatic fallback — triggered when the browser path is unconfigured
  (`playwright_available()` False), raises, or returns 0 rows. New `.env` toggles:
  `INDEED_USE_BROWSER` (default `1`), `INDEED_APIFY_FALLBACK` (default `1`, set `0` to forbid
  any Apify spend on this portal). Live-verified: browser path alone returned 3 real jobs with
  full `jd_text`/correct fields with no Apify token spent; forcing `INDEED_USE_BROWSER=0`
  correctly fell through to the Apify path and surfaced a real "all keys exhausted/invalid"
  error rather than crashing (the configured tokens happened to be dead at test time — proves
  the fallback wiring, not actor health). [§4/§9 2026-06-30 entry]
- 2026-07-10 — job-scraper — Wellfound added as Apify-primary + session-fallback (owner
  request) — no direct-fetch path exists: `/jobs.rss` and `/sitemap.xml` both 403 live
  (DataDome + Cloudflare), no public JSON API, GraphQL needs a live session's CSRF token, and
  filtered search is login-gated. Built as a two-tier plugin (`plugins/wellfound.py`): PRIMARY
  = Apify actor `thirdwatch/wellfound-jobs-scraper` (override `APIFY_ACTOR_WELLFOUND`,
  ~$0.004-0.008/result, bypasses DataDome on Apify's infra) — input/output field names are
  from the actor's published docs, **NOT live-verified against a real run** (no Apify token
  available in the build environment); FALLBACK = logged-in Playwright session (same
  `PLAYWRIGHT_USER_DATA_DIR` model as CareerHound) with generic href-pattern extraction, used
  only when no Apify token is configured or the actor call fails/returns 0 rows. `is_available()`
  is True if EITHER tier is usable. **Both tiers need live re-verification** (actor
  field-mapping against `Job.extra`; session-path selectors against real DOM) the first time
  each actually runs — self-anneal per SKILL.md. [§4/§8/§10]
- 2026-07-10 — job-scraper — two generic bug fixes to shared plugin helpers, surfaced while
  building the above (both are library-level fixes benefiting any current/future plugin, not
  special-cased to one site): (1) `_joblister_util.parse_feed` now retries a failed
  `ET.fromstring` after repairing non-XML-standard named HTML entities (`&rsquo;`, `&ndash;`,
  `&hellip;`, ...) via a new `_repair_html_entities` helper — found on NoDesk's feed, which
  embeds raw HTML entities in `<description>` without CDATA-wrapping them (invalid strict XML;
  the feed silently parsed to 0 items with no error before this fix). Numeric character refs
  and the 5 XML-predefined entities are left untouched. (2) `_career_util.extract_ld_json` now
  parses with `json.loads(..., strict=False)` — found on relocate.me's `JobPosting` block,
  which embeds a literal (unescaped) newline inside its `description` string, invalid strict
  JSON; the block silently vanished (caught by the existing except) before this fix, leaving
  only an unrelated `BreadcrumbList` block. (3) `_career_util.render_html` now sets a
  realistic desktop-Chrome `user_agent` by default (override via the new `user_agent` param) —
  found on nextgenenergyjobs.com, whose WAF returned a bare "Forbidden" body to Playwright's
  default UA (which contains `HeadlessChrome/...`) even though a plain non-JS `fetch_html`
  request to the same URL succeeded; re-verified this doesn't regress `synopsys.py` (still
  scrapes correctly with the new default UA). [§4]
- 2026-07-10 — job-scraper — review+test loop fixes for the 12 new plugins above (code-reviewer
  FAIL→fixed, code-tester PASS with 1 MINOR note). (1) MAJOR: `careerhound.py`'s `fetch()`
  could raise an uncaught `ImportError` if `PLAYWRIGHT_USER_DATA_DIR` was set but chromium
  wasn't installed — violated the "never raise out of fetch()" contract every other plugin
  honors. Fixed by adding `playwright_available()` to `is_available()` and moving the
  `sync_playwright` import inside the existing try block; also removed the plugin's unused
  `location` kwarg (accepted but never wired to the unverified search URL — misleading API
  surface). (2) MINOR: `skipthedrive.py`'s title-link regex used `.match()` (anchored to
  position 0 of each `&lt;h2&gt;`-split chunk), which would silently break on any whitespace
  between the marker and the `&lt;a&gt;` tag; changed to `.search()`. (3) MINOR:
  `remote100k.py`'s `_find_apply_url` took the first external link on the page, which could
  match a company-website/social/GitHub link before the real "Apply" button; changed to
  prefer the site's own `?ref=remote100k` tracking param (falling back to the old deny-list
  heuristic only if absent). Fixing this surfaced a second, pre-existing bug: the raw
  `href="..."` attribute value is HTML-escaped (`&amp;` for a literal `&` in a multi-param
  query string, e.g. `?gh_jid=123&amp;ref=remote100k`) — the apply-URL regex didn't match
  the escaped separator at all (silently falling through to the fallback every time), AND
  the matched URL wasn't unescaped before storing, corrupting `Job.url`'s query string
  live-verified on a real Pinterest/Greenhouse posting. Fixed both: regex now matches
  `(?:[?&]|&amp;)ref=remote100k`, and the result is run through `html.unescape`. (4) MINOR:
  `wellfound.py`'s session-fallback path silently dropped the `location` filter (accepted by
  `fetch()`, passed to the Apify tier, but never threaded into `_fetch_via_session`'s search
  URL); fixed by threading it through and appending a best-effort `location=` query param.
  Noted but NOT changed: `wellfound.is_available()` reflects Apify token *presence*, not
  *health* (a fully dead key pool still reports available) — this exactly matches the
  existing convention in `indeed.py`/`linkedin.py`/`naukri.py`, so it's consistent
  cross-codebase behavior, not a new inconsistency; the real health check happens inside
  `run_actor`'s key rotation and degrades gracefully (verified in the Indeed forced-fallback
  test). All fixes re-verified live post-fix: `plugins/registry.py` (39 plugins, no import
  errors), `careerhound` still correctly reports unavailable/returns `[]`,
  `skipthedrive`/`remote100k` re-scraped cleanly with corrected URLs. [§4]
- 2026-07-10 — main.py/scrape.py — `search --source` default — **`all`**, not `linkedin`
  (owner request: "I never seen any stats about other aggregators getting scrapped").
  `--source linkedin`/`--source <name>` still narrows to one portal on demand. [§8, revises
  the original `main.py search` default set 2026-06-30]
- 2026-07-10 — scrape.py — scraping concurrency — **thread pool, one worker per plugin**
  (`--workers`, default 8); a single plugin's own query×location combos stay sequential
  (owner request: "lets use multi threading to get all the data faster... wait for all the
  processes to complete to further proceed"). DB upserts are serialized on the main thread
  only after every fetch thread finishes — never concurrent writers, and nothing downstream
  (the matcher) starts before the full matrix is done. [§8]
- 2026-07-10 — scrape.py — cross-plugin resource contention — plugins that launch a
  Playwright **persistent** browser context on the shared `PLAYWRIGHT_USER_DATA_DIR`
  profile (`wellfound.py`, `careerhound.py`) must never run concurrently — a new
  `uses_persistent_profile` flag on `JobSourcePlugin` + a `scrape.py`-level lock enforces
  this, engaged only when that profile dir actually exists (so it costs nothing when no
  session is configured). Any future plugin sharing that profile must set the flag too.
  [§4, found live 2026-07-10 — see §8 Phase 10]
- 2026-07-10 — job-scraper — `main.py reset` (new command) — clears the `jobs`+`runs`
  tables and `jobs.json` export; `--hard` also empties `tailored/` + `applications/`
  (owner request: "lets have options --reset to reset the data collected and make a clean
  sheet"). Interactive confirmation required unless `--yes`. Schema/DB file are NOT
  dropped — only rows and generated files, so `init_db()` still works immediately after.
  [§3, new surface]
- 2026-07-10 — job-scraper — per-source transparency — every `scrape.py` run ends with a
  SOURCE REPORT covering **every discovered plugin**, available or not (domain, fetch
  mechanism, outcome + reason) — an unavailable/errored plugin must never silently vanish
  from the output the way it did before (owner request: "I should be able to understand
  whats going on! and do all websites work or not just by the output! and which domain
  responded in which way"). Live per-plugin progress prints by default (not gated behind
  `-v`) since a full run can take several minutes. [§8]
- 2026-07-10 — frontend — stack — **Vite + React 19 + TypeScript + shadcn/ui
  (base-ui) + Tailwind v4 + Phosphor icons**, per owner request. shadcn's CLI defaults
  to base-ui primitives now (not Radix) — components use `render=` instead of
  `asChild`; don't "fix" this to Radix conventions in future edits. [§8, new surface]
  hosting — **Netlify, not GitHub Pages** (owner's original ask) — Pages can only
  serve static files and this project's interactive control panel needs a real
  backend (subprocess execution, `.env` edits, tectonic compiles); Netlify hosts the
  static-only public dashboard build, the interactive app stays local-only
  (`main.py serve`). [§8]
- 2026-07-10 — frontend — env editing scope — **session-only by default** (this
  server process's `os.environ`, inherited by its subprocess children only, gone on
  restart) with an explicit opt-in `persist: true` to rewrite `.env` on disk (owner
  request: "Make env as temporary loaded for each session"). [§8]
- 2026-07-10 — job-scraper/frontend — public data scope — the static dashboard
  publishes only `matched|tailored|ready|applied` rows and only display-safe columns
  (id/source/title/company/location/url/posted_at/scores/role_profile/status) — never
  `jd_text`/`jd_brief`/`answers_json`/`notes`/local file paths. New
  `store.export_public_json()` / `data/store.py export-public` is the only producer of
  this snapshot; nothing else should hand-roll a public data export. [§3, new surface]
- 2026-07-10 — CI — committed-secrets blocker — **resolved as a false alarm, not a
  fix.** The original plan flagged `.env`/`candidate.json`/`data/apify_keys.json`/
  `data/grok_keys.json` as needing a git-history purge before any CI/hosting work,
  based on their presence on disk during planning. Checked against actual git history
  (`git log --all --diff-filter=A` + `git ls-files`, all four paths, every branch)
  before starting Phase 4: none were ever committed. No `git filter-repo`/BFG history
  rewrite was needed or performed. Lesson for future planning: verify a "committed
  secret" claim against `git log`/`git ls-files` before treating it as a blocker, not
  just file existence + `.gitignore` presence. [§8]
- 2026-07-10 — CI — scheduled scrape — **built, cadence = daily cron +
  workflow_dispatch**, Apify-backed portals only bill if `APIFY_TOKEN` is added as a
  repo secret (unset by default — the ~39 free plugins still run and commit a fresh
  public snapshot either way). Owner explicitly requested both the scheduled-scrape
  workflow AND keeping local scraping working — this is additive to `main.py search`,
  not a replacement. Owner is not deploying live yet (no Netlify secrets configured) —
  workflows are built and validated (YAML-parsed, logic-reviewed) but not yet run for
  real in GitHub Actions. [§8]
- 2026-07-11 — web/ frontend — dev port — **5178** (was 5173), set in
  `web/vite.config.ts` + `dev.sh`'s `FRONTEND_PORT` default. [§8]
- 2026-07-11 — web/ frontend — SSE parsing — **fixed a real bug**: `web/src/lib/api.ts`
  `streamSSE()` split frames on bare `\n\n`/`\n`, but `sse-starlette` terminates events
  with CRLF (`\r\n\r\n`/`\r\n`) — every search/prep/rank run hung forever in the browser
  (server responded in ~0.2s; `onDone` never fired). Fixed with a `/\r?\n\r?\n/` /
  `/\r?\n/` regex split. [§8]
- 2026-07-11 — server/app.py — subprocess cleanup on disconnect — `run_streamed()` now
  kills the child process + cancels the pump task in a `finally` block if the SSE
  generator is torn down abnormally (client disconnect, e.g. a page refresh mid-run).
  Without this the subprocess kept running orphaned while the pipeline lock had already
  released, letting a new run race it on the same SQLite store. [§8]
- 2026-07-11 — server/app.py — output buffering — `run_streamed()` now sets
  `PYTHONUNBUFFERED=1` on the spawned subprocess's env. Python block-buffers stdout when
  it isn't a TTY, and that buffering propagates through `main.py`'s own nested
  `_run()` subprocess calls (understand.py/tailor.py/respond.py) — without this, the CMD
  panel showed nothing until the ENTIRE prep run finished, then dumped every line at
  once. Verified in isolation: identical nested-subprocess test showed all output at
  t=2.03s without the fix, streaming incrementally (t=0.02s, 0.54s, 1.04s, …) with it.
  [§8]
- 2026-07-11 — web/ frontend — `/api/lists` scope — now includes every post-match
  status (`matched, tailored, ready, applied, skipped, failed`), not just `matched`.
  Previously a job vanished from the Eligible/Needs-mod/Stretch tabs the moment prep
  advanced it past `matched`, so tailoring/apply progress was invisible in the UI. The
  CLI's `main.py lists` intentionally stays `matched`-only (it's the prep decision
  queue); the UI additionally needs prepped/applied jobs to stay visible with status.
  [§8, revises the Phase 2 UI spec]
- 2026-07-11 — web/ frontend — Applied/Skip/Failed buttons — added to each job card,
  visible only at `status='ready'`. **Pure status-recording, never opens a browser or
  submits anything** — calls a new `POST /api/log` that wraps `apply.py log`, which
  itself refuses to log any job not already at `ready` (reviewed at the human apply
  gate elsewhere). Owner's explicit instruction: "this button does nothing but it
  stores applied status." Verified end-to-end live (staged a test job to `ready`,
  clicked Applied in the browser, confirmed the real endpoint fired and the row
  updated, then reverted the test data). [§6 safety gate, §8]
- 2026-07-11 — execution/llm.py — rate-limit pacing — added `_pace()`: a per-host
  minimum-interval throttle called before every `_grok_send()` attempt (both the
  first-pass key rotation and the throttled-retry pass), so a sequential batch run
  self-paces instead of firing 429s and backing off after the fact. Researched,
  current-as-of-2026-07-11 limits: Groq (`api.groq.com`) — **30 RPM, documented,
  per-organization** (console.groq.com/docs/rate-limits) → paced at 2.0s/request.
  NVIDIA NIM (`integrate.api.nvidia.com`) — **no official rate limit published**;
  ~40 RPM is a community-reported (developer-forum) figure only, not contractual →
  paced conservatively at the same 2.0s/request budget. DeepSeek (`api.deepseek.com`)
  — **no RPM/TPM cap documented**; 429s come from a dynamic per-account concurrency
  limit instead, and DeepSeek's real current failure mode is `402 Insufficient
  Balance` (a billing issue pacing cannot fix — free-credit grant, if any, appears
  exhausted) → light 0.5s pacing only. Anthropic API was left unpaced (Start tier =
  1,000 RPM, not a bottleneck for this tool's sequential loop). No GitHub Actions
  workflow calls an LLM provider yet (`scrape.yml` only scrapes), so there was nothing
  to wire pacing into there — it will apply automatically the moment any future CI job
  calls `execution.llm.complete()`, since the throttle lives in the shared module.
  **Also flagged, not yet acted on:** DeepSeek's `deepseek-chat` model name is
  deprecated 2026-07-24 UTC (aliases to `deepseek-v4-flash` until then) — the model
  string in `main.py`'s `DEEPSEEK_MODEL` constant needs updating before that date.
  [§6, new]
- 2026-07-11 — pipeline — **`ready` status retired; `tailored` is now the apply
  gate.** Owner: "lets remove ready status! lets disable the section for now for
  showing draft answers... it doesnt makie sense as Ive decided to not add feature to
  apply directly in future!" — since no direct-apply browser automation is ever
  planned, pre-drafting screening-question answers (humanise-responder) ahead of the
  human apply gate served no purpose; every `tailored` job (résumé decided — for
  eligible jobs that's the master résumé as-is) is already apply-ready on its own.
  `main.py prep` no longer calls `humanise-responder`/`respond.py`. `apply.py`'s `log`
  guard now checks `status == "tailored"` (was `"ready"`); `_ready_jobs()` renamed
  `_tailored_jobs()`, reads `status="tailored"`. `"ready"` removed from
  `store.VALID_STATUSES` and `_PUBLIC_STATUSES`. `respond.py` (if run manually) no
  longer advances status — it only attaches `answers_json` to an already-`tailored`
  job; its `show()` now splits the same `tailored` query by whether answers are on
  file, instead of querying a `ready` bucket that no longer exists. The
  humanise-responder skill itself is NOT deleted (still runnable manually if this
  changes) but is retired from `prep`'s automated flow. Frontend: `web/`'s Applied/
  Skip/Failed buttons now gate on `status === 'tailored'`; the "ready to apply" badge
  label moved onto the `tailored` status entry (was a separate `ready` entry);
  `PrepPanel`'s label dropped "→ draft answers". A `force` override (`--force` on
  `apply.py log` / `main.py log`, `force` field on `POST /api/log`, a "Force-enable
  Applied/Skip/Failed on any job" toggle in Advanced options persisted to
  localStorage) was added the same session so a human can still record an outcome on
  a job that never went through this tool's prep flow at all — still pure
  status-recording, never opens a browser. [§1, §3, §5, §6 — revises the pipeline
  contract]
- 2026-07-11 — apply-agent — **eligible jobs are apply-gate-ready straight from
  `matched`, no prep run required.** Owner: "Eligible already has a taylored resume
  as master anyways, so enable applied for eligible by default." Rationale:
  resume-tailor's only move for an `eligible` job is copying the master résumé path
  verbatim (`MASTER_RESUME = "varakumar_resume.pdf"`) — there is nothing for prep to
  decide that isn't already decided the moment a job classifies `eligible`. New
  `apply.py._apply_gate_ready(job)`: true for `status == "tailored"`, OR
  `status == "matched" and classify(job) == "eligible"`. `log()`'s guard, `packet()`'s
  queue (renamed `_tailored_jobs` → `_apply_ready_jobs`, now unions `tailored` +
  eligible-`matched`), and `_packet()`'s résumé-path lookup (falls back to
  `MASTER_RESUME` when `tailored_resume_path` is unset on an eligible job) all use
  it. `log()` backfills `tailored_resume_path` to the master résumé on first log if
  still unset, so the record matches what was actually applied with. No `--force`
  needed for this case — it's built into the normal gate, not the override. Frontend:
  `JobsList`'s `gateReady` mirrors the same rule (`tier === 'eligible' && status ===
  'matched'`), shows the "ready to apply" badge, and calls `api.log()` without
  `force: true` for this case (the genuine `forceApply` toggle stays reserved for
  jobs outside both conditions). Verified live end-to-end via CLI (`main.py log`
  without `--force` on a `matched`+eligible job) and in the browser (buttons
  full-opacity, no toggle needed, on every eligible-tier card by default). [§5, §6 —
  revises the 2026-07-11 pipeline-contract entry above]

---

## §11 Apply-method roadmap  *(owner decision 2026-07-01)*

- **NOW (Option 1 — packet submit):** the pipeline preps best jobs to `ready` and emits
  complete apply packets (URL + résumé + cover letter + answers + `candidate_facts`); the
  **human opens the link and submits**. This is the only working path in the current
  environment (chrome-devtools MCP not configured, no logged-in browser profile).
- **TODO (Option 2 — orchestrator drives, runs ALONGSIDE Option 1):** when the
  chrome-devtools MCP is configured + a persistent LinkedIn-logged-in profile exists
  (`PLAYWRIGHT_USER_DATA_DIR`), the orchestrator fills the Easy Apply form live,
  screenshots it, and stops at the review gate for the human to Submit (apply-agent SKILL,
  §6). Keep Option 1 as the fallback/default; Option 2 is additive. Not yet buildable here.

---

## §10 Future Portals / Open Questions

- **Within-plugin company parallelism (flagged 2026-07-10, not yet built).** The
  multi-company ATS plugins (`greenhouse.py` 75 companies, `lever.py` 44, `workday.py` 39,
  and similar) iterate their configured companies sequentially inside `fetch()` — real
  per-company HTTP round trips, individually fast (~1-2s) but additive. Under the new
  parallel-plugin `scrape.py` (§8 Phase 10), these are now consistently the slowest
  plugins in a `--source all` run since every OTHER plugin finishes in parallel around
  them. `workday.py` specifically also does a detail-page fetch per matched posting
  (`_MAX_DETAIL_FETCHES=20` cap, added 2026-07-10 after finding it was previously
  uncapped) — for a large company with a loosely-matching query, this alone can be
  20-40s. A future pass could parallelize the per-company loop WITHIN each plugin (its
  own small thread pool, or a shared one passed in from the runner) — deferred for now
  since it touches ~16 already-reviewed plugin files for a performance win, not a
  correctness fix.
- **Portal-plugin design (approved 2026-07-01, build = follow-up).** Broaden sourcing via
  the existing §4 plugin system (`JobSourcePlugin` + `registry.py` auto-discovery = the
  "common manager + minimal per-portal plugin" the owner wants). Recommended, in priority:
  - **ATS-API plugins — 16 of 18 known patterns BUILT (2026-07-07).** Greenhouse, Lever,
    Ashby, SmartRecruiters, Recruitee, BambooHR, Workday, Workable, Zoho Recruit (built
    2026-07-05/06), plus Oracle Fusion Cloud Recruiting, SAP SuccessFactors, Rippling ATS,
    iCIMS, Cutshort, Darwinbox, Freshteam, Avature (Wave 2, built 2026-07-07) — 17 live
    plugins total, each reading company slugs from a `.env` list (Workday needs
    `tenant:wdN:site`, Oracle Fusion needs `host:site`, SuccessFactors/Avature need the FULL
    hostname — see each plugin's docstring for its exact identifier shape). **Eightfold is
    BLOCKED, not just deferred:** its documented public endpoint
    (`GET <co>.eightfold.ai/api/apply/v2/jobs`) returns `403 "Not authorized for PCSX"` for
    every company/method/header combination tried (Qualcomm, NVIDIA both live-tested,
    GET/POST, with/without cookies/Referer) — it's CSRF/session-gated, not a bare public API
    like the other 17, and needs a real Playwright browser session (the "custom plugin"
    category above), not an ATS-JSON plugin. **Taleo** remains genuinely un-built (HTML-only,
    no public JSON confirmed yet — would need Apify/browser); **iCIMS was reversed from this
    same "HTML-only" assessment in Wave 2** once a required `pr=<page>` query param was found
    to unlock real server-rendered rows on the SAME endpoint. Each plugin returns normalized
    `Job`s with the **job-detail link in `Job.url`** (NOT the apply-button link).
  - **Aggregators** (one plugin = many companies), via Apify or public API: **Foundit/Monster,
    Instahyre, Hirist, Internshala** (like the existing Naukri plugin). **Wellfound BUILT
    2026-07-10** (`plugins/wellfound.py`, Apify-primary + session-fallback — see §9 2026-07-10).
  - **Owner's 7 sites — RESOLVED 2026-07-05, 2 more resolved 2026-07-07 (Wave 2):**
    Qualcomm→Eightfold (still blocked, see above), Simbian→Zoho Recruit (confirmed,
    deferred/OAuth), cyber-times.in/jobs→**OFFLINE** (impersonation probe, skipped),
    Qualys→**Workday** (`qualys:5:Careers`, configured, strong role match), Mattel→
    **SmartRecruiters** (`MattelInc`, configured, weak IT-security match only), Sibros→
    **Rippling ATS** (Wave 2, 2026-07-05 note said "not one of the 9 target platforms,
    flagged" — **now built**, Sibros itself not yet re-verified/added to `.env`, but the
    platform is no longer a blocker), EY→**SAP SuccessFactors** (Wave 2, same resolution —
    platform now built, EY itself not re-verified/added since the populate pass found no
    cybersecurity vendors on this specific CSB2 flavor and EY is general-IT/consultancy, tail
    priority).
  - Live-verified company list (job counts, role-keyword matches, dead/rejected slugs) across
    all 17 working ATS platforms lives in `docs/job_portals.md`; the auto-generated, always-
    current company roster is `docs/supported_companies.md` (371 companies as of 2026-07-07,
    Wave 3 — scope broadened from cybersecurity-only to all-IT, see §9 2026-07-07 entries).
- **Easy-apply source (open).** The v1 LinkedIn actor gives no Easy-Apply flag (verified
  2026-07-01). To offer an easy-apply filter later: an actor that outputs `applyType`, or
  detect at apply-time via browser-driving (§11 Option 2).
- **Future portals** (added via §4 plugins, not v1): Instahyre, Foundit/Monster,
  Wellfound, company career pages.
- **Open questions:**
  - ~~Apify token~~ **RESOLVED 2026-06-30**: token in `.env`; user on FREE plan
    ($5/mo). All three adapters verified live (~$1.55 used during build, incl.
    Naukri's 50-job floor). Keep `--limit` small; `APIFY_MAX_CHARGE_USD` caps spend.
  - Which model/keys power jd-understander + humanise-responder (set in `.env`):
    `session` (default, free), `api` (Anthropic), or `grok` (xAI). User added Grok
    support 2026-06-30 — supply `XAI_API_KEY` + `LLM_PROVIDER=grok` to use it.
  - Portal login-session strategy for custom plugins (persistent Playwright context
    path).
  - ~~**Bulk company-discovery source for ATS platforms**~~ **RESOLVED 2026-07-06** — no
    BuiltWith-style directory was needed. Every ATS platform's public job-board pages are
    ordinary indexed webpages, so a plain `site:<platform-domain> <role keyword>` search
    engine query surfaces many companies' slugs at once straight from the result URLs — see
    `docs/job_portals.md` "Bulk company discovery via search-engine indexing" for the method
    and full results (41 → 96 companies in one pass). Each candidate was still live-verified
    (resolves + real job count) before adding — discovery ≠ verification, unchanged.
  - Master-résumé pre-send checklist items still open (see `README.md`: Metasploit
    `CONFIRM` line, OSCP `(In Progress)`, LinkedIn items 1 & 2).
