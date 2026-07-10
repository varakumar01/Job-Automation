# Job Listers (Aggregators)

Job **listers** are aggregator sites that index jobs from many employers — as opposed to
individual company career sites (see [job_portals.md](job_portals.md)).

Plugins for job listers live in `plugins/` at the repo root (not under `.claude/`).

`main.py search` / `scrape.py` default to `--source all`, running every plugin below
(plus every ATS plugin in `job_portals.md`) in parallel and printing a SOURCE REPORT
naming each one's domain, fetch mechanism, and outcome — see `job-scraper/SKILL.md`.

## Currently built (plugins live)

| Lister | Mechanism | Plugin file | Notes |
|--------|-----------|-------------|-------|
| **LinkedIn** | Apify actor `curious_coder/linkedin-jobs-scraper` | `linkedin.py` | `APIFY_TOKEN` required; `LINKEDIN_POSTED_DAYS` for recency filter |
| **Naukri** | Apify actor `muhammetakkurtt/naukri-job-scraper` | `naukri.py` | `APIFY_TOKEN` required; min 50 jobs/run |
| **Indeed** | Browser-first (headless render) + Apify fallback | `indeed.py` | `INDEED_USE_BROWSER=1` (default) parses `providerData["mosaic-provider-jobcards"]`; falls back to Apify actor `borderline/indeed-scraper` if unconfigured/blocked/empty. See §9 2026-07-10 |
| **RemoteOK** | Public JSON API (no token) | `remoteok.py` | `GET https://remoteok.com/api[?tag=<query>]` |
| **Remotive** | Public JSON API (no token) | `remotive.py` | `GET https://remotive.com/api/remote-jobs?search=<q>&limit=N` (server-side over-fetches 2x to compensate for client-side filtering) |
| **Arbeitnow** | Public JSON API (no token) | `arbeitnow.py` | `GET https://www.arbeitnow.com/api/job-board-api` (no server search — client-filtered); `created_at` is Unix epoch, converted to ISO |
| **Jobicy** | Public JSON API (no token) | `jobicy.py` | `GET https://jobicy.com/api/v2/remote-jobs?count=N&tag=<q>` |
| **Himalayas** | Public JSON API (no token) | `himalayas.py` | `GET https://himalayas.app/jobs/api?limit=N&q=<q>` — `q=` unreliable (over-fetches 4x + client-filters); no `id` field, uses `guid`; `pubDate` is Unix epoch, converted to ISO |
| **The Muse** | Public JSON API (no token) | `themuse.py` | `GET https://www.themuse.com/api/public/jobs?page=N` — 0-indexed pagination, no search param (client-filtered, capped at 5 pages) |
| **Working Nomads** | Public JSON API (no token) | `workingnomads.py` | `GET https://www.workingnomads.com/api/exposed_jobs/` — no search param, no `id` field (derived from trailing URL segment) |
| **Best PM Jobs** | RSS (Jobboardly SaaS) | `bestpmjobs.py` | `GET https://www.bestpmjobs.com/jobs.rss` — title parsed as `"{Role} - {Company} - {Location}"` |
| **SkipTheDrive** | Server-rendered HTML search (no feed works) | `skipthedrive.py` | `GET https://www.skipthedrive.com/?s=<query>` — every RSS/feed path is disabled site-wide (verified live); `location` hardcoded `"Remote"` |
| **NoDesk** | RSS (no token) | `nodesk.py` | `GET https://nodesk.co/remote-jobs/index.xml` — embeds raw HTML entities, invalid strict XML (fixed generically in `parse_feed`) |
| **Remote100k** | Sitemap → `JobPosting` ld+json | `remote100k.py` | `/sitemap.xml` → `/remote-job/<slug>` — `jd_text` is a teaser only; `Job.url` is the real employer posting found via the page's own "Apply" link |
| **NextGen Energy Jobs** | Sitemap index → headless render | `nextgenenergyjobs.py` | `/sitemap.xml` → `job_openings_*.xml` children → Next.js pages (Tier-3 render; RSC payload, no ld+json) |
| **Relocate.me** | Sitemap → `JobPosting` ld+json | `relocateme.py` | `/sitemap.xml` filtered to trailing-numeric-id URLs — title/description double-HTML-escaped, unescaped twice |
| **Jobs in Education** | RSS (SmartJobBoard SaaS) | `jobsineducation.py` | `GET https://jobsineducation.com/feeds/rss.xml` — company from `dc:creator` |
| **Rejobs** | Atom feed (no token) | `rejobs.py` | `GET https://rejobs.org/en/rss/renewable-energy-jobs` — title parsed as `"{Role} - {Company}"` |
| **We Love Product** | Public JSON API (no token) | `weloveproduct.py` | `GET https://weloveproduct.co/api/jobs` — unauthenticated calls capped at page 1 (~32 of the true total) |
| **Wellfound** | Apify actor + logged-in-session fallback | `wellfound.py` | PRIMARY actor `thirdwatch/wellfound-jobs-scraper` (`APIFY_ACTOR_WELLFOUND`); FALLBACK a logged-in Playwright session. No public API/RSS/sitemap — DataDome + Cloudflare gate everything. Field mapping/selectors unverified against a real run — self-anneal on first live use |
| **CareerHound** | Session-gated Playwright (no token) | `careerhound.py` | Entirely login-gated (no public preview/API/RSS/sitemap); generic href-pattern extraction. Needs live-session verification on first real use |

Shared helpers (HTML stripping, keyword matching, RSS/Atom feed parsing, epoch→ISO
conversion) for the public-JSON/RSS plugins above live in `_joblister_util.py`; sitemap
parsing + ld+json/render-based Tier-2/3 helpers for the sitemap-driven plugins live in
`_career_util.py` (both leading-underscore, kept out of plugin auto-discovery).

## Public JSON APIs — verified endpoints

All endpoints verified HTTP 200 live on 2026-07-05 (re-verified against actual field shapes
during the plugin build; some fields differ from the earlier 2026-07-04 research pass — see
notes).

| Lister | Public JSON endpoint | Fields available | Notes |
|--------|----------------------|-----------------|-------|
| **RemoteOK** | `https://remoteok.com/api` | id, position, company, location, tags, description, url, epoch | Element [0] is metadata — skip it. Pre-filter via `?tag=<query>`. Plugin live. |
| **Remotive** | `https://remotive.com/api/remote-jobs` | id, url, title, company_name, candidate_required_location, tags, description, publication_date | `?search=<q>&limit=N`. Plugin live. |
| **Arbeitnow** | `https://www.arbeitnow.com/api/job-board-api` | slug, url, title, company_name, location, tags, description, created_at (epoch int, not ISO) | Returns `data[]` array; no query param — client-filter only. Plugin live. |
| **Jobicy** | `https://jobicy.com/api/v2/remote-jobs` | id, url, jobTitle, companyName, jobIndustry, jobGeo, jobExcerpt, jobDescription, pubDate | `?count=N&tag=<q>` (tag reliably pre-filters). Plugin live. |
| **Himalayas** | `https://himalayas.app/jobs/api` | guid (no `id`!), title, companyName, locationRestrictions, description, applicationLink (== guid), pubDate (epoch int, not ISO), seniority, categories | `?limit=N&q=<query>` — `q=` verified NOT to reliably filter; plugin over-fetches + client-filters. Plugin live. |
| **The Muse** | `https://www.themuse.com/api/public/jobs?page=N` | id, name, company.name, locations[].name, categories[].name, levels, contents, refs.landing_page, publication_date | Paginated, **0-indexed** (`page=0` is first page — verified live, differs from `page=1`); no search param. Plugin live. |

## Key-authenticated (free tier available)

These require a free API key but are not rate-limited like Apify:

| Lister | Endpoint | Key source | Notes |
|--------|----------|-----------|-------|
| **Adzuna** | `https://api.adzuna.com/v1/api/jobs/{country}/search/{page}` | [developer.adzuna.com](https://developer.adzuna.com) | `app_id` + `app_key` params; good India coverage |
| **USAJobs** | `https://data.usajobs.gov/api/search` | [usajobs.gov/developer](https://www.usajobs.gov/developer/accountrequest) | `Authorization-Key` header; US gov positions |
| **Findwork** | `https://findwork.dev/api/jobs/` | [findwork.dev](https://findwork.dev/api/) | `Authorization: Token <key>` |
| **HN Who's Hiring** | `https://hacker-news.firebaseio.com/v0/item/{thread_id}.json` + `{comment_id}.json` | none | Free; parse monthly "Ask HN: Who is hiring?" threads via Algolia HN Search |

## Apify-gated (queued, §8)

Require Apify token; queued for future plugin builds:

| Lister | Notes |
|--------|-------|
| **Dice** | US tech-focused | PLAN §8 |
| **Glassdoor** | Reviews + jobs | PLAN §8 |
| **Google Jobs** | Aggregates from ATS sources | PLAN §8 |
| **We Work Remotely** | Remote-only, no public JSON | PLAN §8 |

## Deferred / dropped (owner request 2026-07-10)

| Lister | Status | Notes |
|--------|--------|-------|
| **remote.co** | Deferred | Cloudflare + FlexJobs paywall — every fetch attempt timed out live, no RSS/API/sitemap found. Low ROI vs. fragility. |
| **startupers.com** | Dropped | Original startup-jobs board has shut down; domain now hosts an unrelated "AI Agent Job Marketplace" (no human postings). |
| **skillset.co** | Dropped | Cloudflare-blocked (403) and identity unverifiable (name maps to several unrelated sites). |

---
*Updated: 2026-07-10. Re-verify endpoints when adding a new plugin — public APIs change.*
