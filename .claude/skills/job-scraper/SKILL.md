---
name: job-scraper
description: >
  Find and scrape job postings from portals (LinkedIn, Naukri, Indeed; extensible
  via plugins) into the local store. Trigger on "find jobs", "scrape jobs for
  <query>", "pull <role> roles from LinkedIn/Naukri/Indeed", "search <portal> for
  jobs". First stage of the pipeline — writes normalized rows at status `scraped`.
model: sonnet
---

# job-scraper

Pipeline stage **1** (PLAN.md §5). Sources jobs from portals and stores normalized
rows at `status = scraped` — the entry point every downstream skill builds on.
Reads: portals (Apify actors + custom plugins). Writes: `jobs` rows → `scraped`.

## How it works

- **Plugins** live in `plugins/` and implement the `JobSourcePlugin` contract
  (`name`, `is_available()`, `fetch(query, limit, *, location=None)`). The
  `registry.py` auto-discovers every `<site>.py` — **adding a portal = dropping one
  file** (PLAN.md §4). Underscore-prefixed files (`_apify.py`, `_custom_template.py`)
  are helpers/templates and are skipped by discovery.
- **Apify-backed** portals (`linkedin`, `naukri`, `indeed`) call their actor via
  `apify-client` using `APIFY_TOKEN` from `.env`. Each adapter passes the requested
  `limit` into the actor's own count field so cost is bounded (pay-per-event).
- **Custom** portals copy `_custom_template.py` → `<site>.py` and drive the user's
  logged-in Playwright session.

## Run it

```bash
# list plugins + availability (free, no actor run)
python3 .claude/skills/job-scraper/scripts/scrape.py --list

# scrape one portal
python3 .claude/skills/job-scraper/scripts/scrape.py \
    --source linkedin --query "security engineer" --location "Bengaluru" --limit 10

# scrape every available portal
python3 .claude/skills/job-scraper/scripts/scrape.py --source all --query "red team" --limit 5

# Apify key health indicator (which keys are usable / exhausted / invalid)
python3 .claude/skills/job-scraper/scripts/scrape.py --keys
python3 .claude/skills/job-scraper/scripts/scrape.py --reset-keys all   # clear flags
```

Run with the project venv: `.venv/bin/python ...` (deps from `requirements.txt`).
Rows land in `data/jobs.db`; `data/jobs.json` is refreshed for inspection.

## Chosen actors (verified 2026-06-30)

| Portal | Actor | Cost | Notes |
|---|---|---|---|
| linkedin | `curious_coder/linkedin-jobs-scraper` | ~$0.001/result | builds a search URL from query+location; `scrapeCompany=False` |
| indeed | `borderline/indeed-scraper` | ~$0.005/job | `country` defaults to `in` (`APIFY_INDEED_COUNTRY`) |
| naukri | `muhammetakkurtt/naukri-job-scraper` | per-event | `fetchDetails=True` for jd_text (`APIFY_NAUKRI_DETAILS=0` to skip) |

Override any actor id via `APIFY_ACTOR_{LINKEDIN,NAUKRI,INDEED}` in `.env`.

## Safety / cost (PLAN.md §6)

- Actors bill **per produced result**, and several **overshoot or floor** the
  requested limit: LinkedIn requires `count ≥ 10` and scrapes whole pages (can
  produce ~50+), **Naukri has a hard 50-job minimum** (so `--limit 3` still
  produces/charges ~50). The runner only *stores* `--limit` rows, but the actor
  charges for what it produces. The real spend ceiling is **`APIFY_MAX_CHARGE_USD`**
  (default $0.50/run); lower it for tighter control.
- The user is on the Apify **FREE** plan (~$5/mo **per key**). Keep `--limit` small
  and prefer LinkedIn/Indeed for tiny scrapes; confirm before large or all-source runs.
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

1. **Apify-backed portal:** copy an existing `plugins/<portal>.py` (e.g. `indeed.py`),
   set a unique `name` (this is also `jobs.source`), point `actor_id(...)` at the new
   actor, and map its output fields with `first_text(...)`. Multi-key rotation, cost
   caps, and normalization come for free via `_apify`.
2. **Custom (non-Apify) portal:** copy `plugins/_custom_template.py` to `<site>.py`
   (drop the leading underscore — underscore files are skipped), rename the class, set
   `name`, and implement `is_available()` (logged-in Playwright session present?) +
   `fetch()` (drive the page, normalize rows to `Job`). Uses your own logged-in browser
   session from `PLAYWRIGHT_USER_DATA_DIR` (PLAN §6 — no stored credentials).
3. **Verify discovery:** `scrape.py --list` should show the new `name`; then live-smoke
   with a tiny `--limit`. Document the addition in PLAN.md §4/§10.

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
