# Job Listers (Aggregators)

Job **listers** are aggregator sites that index jobs from many employers — as opposed to
individual company career sites (see [job_portals.md](job_portals.md)).

Plugins for job listers live in `plugins/` at the repo root (not under `.claude/`).

## Currently built (plugins live)

| Lister | Mechanism | Plugin file | Notes |
|--------|-----------|-------------|-------|
| **LinkedIn** | Apify actor `curious_coder/linkedin-jobs-scraper` | `linkedin.py` | `APIFY_TOKEN` required; `LINKEDIN_POSTED_DAYS` for recency filter |
| **Naukri** | Apify actor `muhammetakkurtt/naukri-job-scraper` | `naukri.py` | `APIFY_TOKEN` required; min 50 jobs/run |
| **Indeed** | Apify actor `borderline/indeed-scraper` | `indeed.py` | `APIFY_TOKEN` required; `APIFY_INDEED_COUNTRY=in` |
| **RemoteOK** | Public JSON API (no token) | `remoteok.py` | `GET https://remoteok.com/api[?tag=<query>]` |
| **Remotive** | Public JSON API (no token) | `remotive.py` | `GET https://remotive.com/api/remote-jobs?search=<q>&limit=N` (server-side over-fetches 2x to compensate for client-side filtering) |
| **Arbeitnow** | Public JSON API (no token) | `arbeitnow.py` | `GET https://www.arbeitnow.com/api/job-board-api` (no server search — client-filtered); `created_at` is Unix epoch, converted to ISO |
| **Jobicy** | Public JSON API (no token) | `jobicy.py` | `GET https://jobicy.com/api/v2/remote-jobs?count=N&tag=<q>` |
| **Himalayas** | Public JSON API (no token) | `himalayas.py` | `GET https://himalayas.app/jobs/api?limit=N&q=<q>` — `q=` unreliable (over-fetches 4x + client-filters); no `id` field, uses `guid`; `pubDate` is Unix epoch, converted to ISO |
| **The Muse** | Public JSON API (no token) | `themuse.py` | `GET https://www.themuse.com/api/public/jobs?page=N` — 0-indexed pagination, no search param (client-filtered, capped at 5 pages) |

Shared helpers (HTML stripping, keyword matching, epoch→ISO conversion) for the five
public-JSON plugins above live in `_joblister_util.py` (leading underscore keeps it out of
plugin auto-discovery).

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
| **Wellfound** (formerly AngelList) | Startup jobs; PLAN §8 |
| **Dice** | US tech-focused | PLAN §8 |
| **Glassdoor** | Reviews + jobs | PLAN §8 |
| **Google Jobs** | Aggregates from ATS sources | PLAN §8 |
| **We Work Remotely** | Remote-only, no public JSON | PLAN §8 |

---
*Updated: 2026-07-04. Re-verify endpoints when adding a new plugin — public APIs change.*
