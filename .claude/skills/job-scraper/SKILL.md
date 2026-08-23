---
name: job-scraper
description: >
  Find and scrape job postings from ~37 portals (LinkedIn + Wellfound via Apify,
  ~35 more token-free via ATS/aggregator plugins; extensible via plugins) into the
  local store. Trigger on "find jobs", "scrape jobs for <query>", "pull <role> roles
  from LinkedIn/Wellfound/Greenhouse", "search <portal> for jobs". First stage of the
  pipeline — writes normalized rows at status `scraped`.
model: sonnet
---

# job-scraper

Pipeline stage **1** (PLAN.md §5). Sources jobs from portals and stores normalized
rows at `status = scraped` — the entry point every downstream skill builds on.
Reads: portals (Apify actors + custom plugins). Writes: `jobs` rows → `scraped`.

## How it works

- **Plugins** live in `plugins/` at the repo root (not under `.claude/` — they're
  plain scraping code with no Claude-specific dependency, reusable by any
  orchestrator/LLM, same as the top-level `data/`/`execution/` packages) and
  implement the `JobSourcePlugin` contract
  (`name`, `is_available()`, `fetch(query, limit, *, location=None)`). The
  `registry.py` auto-discovers every `<site>.py` — **adding a portal = dropping one
  file** (PLAN.md §4). Underscore-prefixed files (`_apify.py`, `_custom_template.py`)
  are helpers/templates and are skipped by discovery.
- Each plugin also sets `base_url` (its domain, for the SOURCE REPORT), `mechanism`
  (`"rss"|"atom"|"json"|"html"|"browser"|"apify"`), and, for any plugin whose
  `is_available()` can be False, an `availability_detail()` override naming the exact
  missing dependency (e.g. `"no APIFY_TOKEN & no chromium"`) instead of the generic
  base-class default `"check creds"`. All three are optional/cosmetic (nothing breaks
  if a new plugin skips them) but keep them — that's the whole point of the report.
- **Apify-backed** portals (`linkedin`, `wellfound` — `naukri`/`indeed` were removed
  2026-08-23, owner request to drop Apify dependency everywhere except LinkedIn, see
  PLAN.md §9) call their actor via `apify-client` using `APIFY_TOKEN` from `.env`.
  Each adapter passes the requested `limit` into the actor's own count field so cost
  is bounded (pay-per-event).
- **Custom** portals copy `_custom_template.py` → `<site>.py` and drive the user's
  logged-in Playwright session.

## Run it

```bash
# list plugins + availability (free, no actor run) — shows domain + why any are unavailable
python3 .claude/skills/job-scraper/scripts/scrape.py --list

# scrape one portal
python3 .claude/skills/job-scraper/scripts/scrape.py \
    --source linkedin --query "security engineer" --location "Bengaluru" --limit 10

# scrape every available portal (the default --source), multiple queries/locations in
# one run — the matrix is the cross product, every plugin fetches every combo
python3 .claude/skills/job-scraper/scripts/scrape.py \
    --queries "red team,detection engineer" --locations "Bengaluru,Remote" \
    --limit 5 --workers 8

# Apify key health indicator (which keys are usable / exhausted / invalid)
python3 .claude/skills/job-scraper/scripts/scrape.py --keys
python3 .claude/skills/job-scraper/scripts/scrape.py --reset-keys all   # clear flags
```

Run with the project venv: `.venv/bin/python ...` (deps from `requirements.txt`).
Rows land in `data/jobs.db`; `data/jobs.json` is refreshed for inspection.

**`main.py search` defaults to `--source all`** (changed 2026-07-10 — it used to default
to `linkedin`, so the ~35 aggregator/joblister/ATS plugins never ran unless `--source all`
was passed explicitly, and the user had no way to tell). Every run — `--source all` or a
single named source — ends with a **SOURCE REPORT** listing *every discovered plugin*,
available or not: domain, fetch mechanism (rss/atom/json/html/browser/apify), and outcome
(✓ ok / ◐ partial / ∅ empty / ✗ error / ⚠ unavailable-with-reason). An unavailable or
errored plugin never just vanishes from the output anymore.

**Threaded fetch:** with `--source all` (or `--queries`/`--locations` producing more than
one combo), plugins are fetched in parallel (`--workers`, default 8 — one worker thread
per plugin). A single plugin's own query×location combos still run **sequentially inside
its own task**, so one domain is never hit concurrently (politeness / PLAN §6 rate
limits). DB upserts are serialized on the main thread after every fetch task has
finished — the executor `with` block guarantees the whole matrix is done before the
report prints or `main.py`'s matcher runs next.

## Chosen actors (verified 2026-06-30; naukri/indeed rows removed 2026-08-23 — see PLAN.md §9)

| Portal | Actor | Cost | Notes |
|---|---|---|---|
| linkedin | `curious_coder/linkedin-jobs-scraper` | ~$0.001/result | builds a search URL from query+location; `scrapeCompany=False` |
| wellfound | `thirdwatch/wellfound-jobs-scraper` (primary; session-based fallback) | ~$0.004-0.008/result | PRIMARY tier only — see `wellfound.py` docstring |

Override any actor id via `APIFY_ACTOR_{LINKEDIN,WELLFOUND}` in `.env`.

## Safety / cost (PLAN.md §6)

- Actors bill **per produced result**, and LinkedIn's actor **overshoots** the
  requested limit — it requires `count ≥ 10` and scrapes whole pages (can produce
  ~50+). The runner only *stores* `--limit` rows, but the actor charges for what it
  produces. The real spend ceiling is **`APIFY_MAX_CHARGE_USD`** (default $0.50/run);
  lower it for tighter control.
- The user is on the Apify **FREE** plan (~$5/mo **per key**). Keep `--limit` small;
  confirm before large or all-source runs.
- Never hardcode `APIFY_TOKEN` — it comes from `.env` only.

## Multi-key rotation + health (PLAN.md §9)

Configure **several** Apify keys to extend free credit: comma/space-separate them in
`APIFY_TOKEN`, and/or add `APIFY_TOKEN_1`, `APIFY_TOKEN_2`, … `run_actor` tries them
**best-health-first** (`healthy → unknown → exhausted → invalid`); on a usage/credit
limit it marks the key `exhausted` and rotates to the next, on an auth error it marks
it `invalid`. A **non-key** error (bad actor input, timeout) is NOT swallowed — it
propagates so a real bug isn't hidden behind rotation. Health persists in
`data/apify_keys.json` (gitignored; stores only a masked `…last5` hint + a hash, never
the secret). `exhausted` auto-resets to `unknown` when the month rolls over (free
credit renews monthly). Logic lives in `plugins/_apify_keys.py`; `--keys` shows the
table, `--reset-keys all|exhausted|invalid|<hint>` clears flags.

## Adding a portal (extensibility)

A new job source = **one file**, no edits anywhere else (registry auto-discovers it).

1. **ATS platform** (Greenhouse/Lever/Workday/etc. — covers ANY company on that
   platform): copy an existing `plugins/<platform>.py` (e.g. `greenhouse.py`), point it
   at the platform's public JSON endpoint, and configure companies via a `.env`
   `<PLATFORM>_COMPANIES` list (`slug` or `slug:Display Name`). Reuse `_ats_util.py`'s
   `parse_companies`/`round_robin`/`post_json` — don't reinvent them. To add a COMPANY
   to an already-built platform, just add its slug to the existing `.env` list — no new
   file needed. See `docs/job_portals.md` for verified endpoint patterns per platform.
2. **Apify-backed portal:** copy an existing `plugins/<portal>.py` (e.g. `wellfound.py`),
   set a unique `name` (this is also `jobs.source`), point `actor_id(...)` at the new
   actor, and map its output fields with `first_text(...)`. Multi-key rotation, cost
   caps, and normalization come for free via `_apify`. New Apify-backed portals need
   explicit owner sign-off first — the owner's 2026-08-23 decision was to keep Apify
   usage to the minimum (LinkedIn + Wellfound only), not grow it by default.
3. **Custom (non-ATS) portal** — a single bespoke company site, no known ATS
   underneath: copy an existing custom plugin (e.g. `synopsys.py`) as the template
   rather than `_custom_template.py` (that scaffold's `is_available()` wrongly assumes
   a *logged-in* session via `PLAYWRIGHT_USER_DATA_DIR` — wrong model for a public
   career page, which needs no login at all). Use `_career_util.py`'s fetch-strategy
   ladder: `fetch_html`/`fetch_json` first, then `extract_next_data`/`extract_ld_json`/
   `extract_window_var` (JSON blob in the HTML — covers most sites without a browser),
   and only fall back to `render_html()` (real headless Chromium, no persistent
   profile) if the page genuinely needs JS to render its job list. Gate
   `is_available()` on `playwright_available()` if using the render fallback, not on
   `PLAYWRIGHT_USER_DATA_DIR`.
4. **Verify discovery:** `scrape.py --list` should show the new `name`; then live-smoke
   with a tiny `--limit`. Document the addition in PLAN.md §4/§10.
5. **Refresh the supported-companies list:** any time a company is added to a
   `.env` `*_COMPANIES` var (or a new custom single-company plugin ships), run
   `python3 .claude/skills/job-scraper/scripts/list_companies.py` to regenerate
   `docs/supported_companies.md` — it reads live from `.env`, so it can't drift if
   you remember to re-run it. **But a BRAND NEW ATS platform must first be added to
   `list_companies.py`'s own `_SLUG_PLATFORMS` list** (or, for a non-`slug:Display Name`
   identifier shape like Workday/Oracle Fusion, a dedicated `_<platform>_rows()`
   function) — the script only knows the platforms hardcoded there; a new plugin
   whose platform isn't registered in the script gets silently omitted from the
   generated doc with no error (found live 2026-07-07: 8 new Wave-2 platforms were
   fully populated in `.env` and scraping correctly, but invisible in
   `docs/supported_companies.md` until the script itself was updated).

Rules: the registry skips `base`, `registry`, `__init__`, and any `_`-prefixed module,
and only registers `JobSourcePlugin` subclasses defined in their own module; duplicate
`name`s raise. (Auto-discovery smoke-tested 2026-06-30: a stub plugin dropped in is
listed + fetchable, and de-registers cleanly on removal.)

## Self-annealing

Actor output schemas drift. Field mapping uses a resilient `first(...)` picker with
several candidate keys per field (`plugins/<portal>.py`). If a field stops
populating after a live scrape, inspect the raw item (stored in `Job.extra` /
`data/jobs.json`), add the new key to the candidate list, and update the
"verified" date in that plugin's docstring + here.

**HTML-scraping regexes must never assume exact attribute order or an exact class
list** (found repeatedly across `icims.py`, `freshteam.py`, and `avature.py` — a
single-tenant/single-company smoke test during the initial build is NOT sufficient
proof a scraping regex generalizes; a SECOND real tenant, often only encountered
during the later "populate with real companies" pass, is frequently the first real
cross-tenant-markup-variance test a plugin gets). Concretely: write `<a\b[^>]*?href="`
instead of a literal `<a href="` (tolerates an attribute like `class="link"` appearing
before `href`); write `class="foo\b[^>]*>` instead of `class="foo">` (tolerates extra
classes/attributes after the one you're matching on). A regex written against ONE
company's markup will look correct and pass its own review/test loop, then silently
return zero rows (no exception, no error) the moment a second company on the same
platform uses a slightly different but semantically-equivalent markup shape.
