# Job Portals (Individual Company Career Sites)

Job **portals** are individual company career sites — as opposed to aggregator listers
(see [joblisters.md](joblisters.md)).

They are typically powered by an **ATS platform** (Greenhouse, Lever, Workday, etc.) which
exposes a public JSON API.

## Currently built (plugins live)

**Design (locked 2026-07-05, revises the original one-file-per-company plan below):** one
plugin per ATS platform, configured via a comma-separated `.env` list of company slugs —
NOT one file per company. Multiple companies on the same platform are scraped **fairly**
(round-robin merge), so a small `--limit` doesn't let the first-listed company starve the
rest.

| ATS Platform | Plugin file | `.env` var | Notes |
|---|---|---|---|
| **Greenhouse** | `greenhouse.py` | `GREENHOUSE_COMPANIES` | `company_name` in payload; `content` field is HTML-entity-escaped TWICE — needs an extra `html.unescape` before stripping |
| **Lever** | `lever.py` | `LEVER_COMPANIES` | No company-name field (use `slug:Display Name`); `createdAt` is epoch **milliseconds**, not seconds |
| **Ashby** | `ashby.py` | `ASHBY_COMPANIES` | No company-name field (use `slug:Display Name`); use `jobUrl` not `applyUrl` |
| **SmartRecruiters** | `smartrecruiters.py` | `SMARTRECRUITERS_COMPANIES` | List call has no JD text; a 2nd detail call per title-matched posting gets full text + real `postingUrl` (not `applyUrl`) |
| **Recruitee** | `recruitee.py` | `RECRUITEE_COMPANIES` | Single call, full description included; dates are `"YYYY-MM-DD HH:MM:SS UTC"` strings, not epoch/ISO |
| **BambooHR** | `bamboohr.py` | `BAMBOOHR_COMPANIES` | List + detail 2-call pattern; no company-name field AND no date field anywhere — `posted_at` is always `None` for this platform |
| **Workday** | `workday.py` | `WORKDAY_COMPANIES` | No single slug — needs `tenant:wdN:site` (3 parts; found by trial/network-tab). Server-enforced page-size max is **20** (21+ → HTTP 400). Detail URL is `.../site<externalPath>` with NO extra `/job` literal — `externalPath` already starts with `/job/...` |
| **Workable** | `workable.py` | `WORKABLE_COMPANIES` | POST body must be `{"query": "..."}` ONLY — an added `"limit"` key errors. No description field or URL in the payload at all: `jd_text` is always `None`; `Job.url` is constructed as `apply.workable.com/<co>/j/<shortcode>/` |
| **Zoho Recruit** | `zoho_recruit.py` | `ZOHORECRUIT_COMPANIES` | Discovered 2026-07-06 (corrects an earlier "needs OAuth" note below — that's only true of Zoho's authenticated CRUD API): every org with a published public career page exposes an UNAUTHENTICATED `.../recruit/v2/public/Job_Openings` endpoint. `subdomain.tld` addressing (TLD varies, `.com`/`.in` both seen), not a bare slug. No pagination params accepted — one call returns everything. No company-name field; `Date_Opened` is US `MM/DD/YYYY`. |

**Custom (non-ATS) company career-site plugins — a different pattern.** Bespoke sites can't
share a `.env` company list the way ATS platforms do (no shared request shape), so each gets
its own thin plugin file using the shared `_career_util.py` manager (HTTP fetch → JSON-blob
extraction → Playwright headless-render fallback, in that cost order — see the module
docstring). No `.env` config needed unless the plugin defines one itself.

| Company | Plugin file | Rendering tier | Notes |
|---|---|---|---|
| **Synopsys** | `synopsys.py` | Tier 3 (Playwright render, no login) | First plugin using the browser-render fallback. Search results are directly URL-navigable (`/search-jobs/<kw>/44408/<page>`) once JS runs — no form interaction needed. `is_available()` gates on `playwright_available()` (chromium actually installed via `playwright install chromium`), NOT `PLAYWRIGHT_USER_DATA_DIR` (that's a login-session check; public career pages need none). |

Shared helpers (slug/display-name parsing, ms-epoch conversion, round-robin merge, POST-JSON,
Workday's 3-part config parser) live in `_ats_util.py`, which itself re-exports
HTML-stripping/matching from the joblister plugins' `_joblister_util.py` (leading underscores
keep both out of plugin auto-discovery).

**Deferred — not a simple public-JSON plugin:** Eightfold's documented public endpoint
(`GET /api/apply/v2/jobs`) returns `403 "Not authorized for PCSX"` for every company tested
(Qualcomm, NVIDIA), with or without cookies/Referer/X-Requested-With headers, on both GET and
POST. This is a CSRF/session-gated endpoint, not a bare public API like the platforms above —
it needs a real Playwright browser session (the "custom plugin" category in PLAN §4), not an
ATS-JSON plugin. Qualcomm (an owner target) is unreachable via this platform until that's built.

Each entry in a `*_COMPANIES` var is `slug` or `slug:Display Name`, e.g.:
```
GREENHOUSE_COMPANIES=huntress,wizinc:Wiz,gitlab
LEVER_COMPANIES=certik:CertiK
ASHBY_COMPANIES=vanta:Vanta,chainalysis-careers:Chainalysis
```

## ATS Platforms — verified public JSON patterns

Endpoints verified live on 2026-07-04/2026-07-05.

| ATS Platform | Public JSON pattern | Method | Auth | Status | Notes |
|---|---|---|---|---|---|
| **Greenhouse** | `https://boards-api.greenhouse.io/v1/boards/{co}/jobs?content=true` | GET | none | ✅ Plugin live | `content` is double-HTML-escaped; ids unique only per-company (`ext_id` prefixed `slug:id`) |
| **Lever** | `https://api.lever.co/v0/postings/{co}?mode=json` | GET | none | ✅ Plugin live | Bare JSON array; `createdAt` epoch is in **milliseconds** |
| **Ashby** | `https://api.ashbyhq.com/posting-api/job-board/{co}` | GET | none | ✅ Plugin live | `jobUrl` = detail page, `applyUrl` = apply-button link (use `jobUrl`) |
| **SmartRecruiters** | `https://api.smartrecruiters.com/v1/companies/{co}/postings` (+ `/postings/{id}` detail) | GET | none | ✅ Plugin live | `{co}` = company display name (CamelCase); list has no JD text, needs detail call |
| **Recruitee** | `https://{co}.recruitee.com/api/offers` | GET | none | ✅ Plugin live | Subdomain-based; single call, full description included |
| **Workday (CxS)** | `https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (+ `.../{site}{externalPath}` detail) | POST JSON | none | ✅ Plugin live | `N`=1-5; POST `{"limit":20,"offset":0,"searchText":"..."}` — **20 is the server-enforced max**, 21+ returns 400 |
| **Workable** | `https://apply.workable.com/api/v3/accounts/{co}/jobs` | POST JSON | none | ✅ Plugin live | POST `{"query":"..."}` ONLY — a `"limit"` key errors. No description/URL field in the payload |
| **BambooHR** | `https://{co}.bamboohr.com/careers/list` (+ `.../careers/{id}/detail`) | GET JSON | none | ✅ Plugin live | Returns `{ "result": [...] }`; no date field anywhere, `posted_at` always `None` |
| **Eightfold** | `https://{co}.eightfold.ai/api/apply/v2/jobs` | GET | none | ❌ **Blocked** (verified 2026-07-05) | Returns `403 "Not authorized for PCSX"` for every company/method/header combo tried (Qualcomm, NVIDIA). CSRF/session-gated — needs a Playwright custom plugin, not ATS-JSON |
| **Zoho Recruit** | `https://{subdomain}.zohorecruit.{tld}/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite` | GET | **none** (public endpoint) | ✅ Plugin live | Corrects the row below dated 2026-07-05: the authenticated `recruit.zoho.com` CRUD API needs OAuth, but every org's public career page exposes this separate unauthenticated endpoint (discovered 2026-07-06 via network-tab capture). No pagination params accepted — one call returns everything |
| **iCIMS** | `https://careers-{co}.icims.com/jobs/search?pr_iis=Indeed&searchKeyword=&in_iframe=1` | HTML scrape / RSS | none | Pattern, not built | No clean JSON; RSS available at `/jobs/feed` |
| **Taleo** | `https://{tenant}.taleo.net/careersection/{section}/jobsearch.ftl` | HTML only | — | Pattern, not built | No public JSON; Apify/browser required |

## Owner's Target Companies (PLAN §10) — resolved 2026-07-05

| Company | ATS Platform | Slug / Tenant | Status |
|---------|---|---|---|
| Qualys | Workday | `qualys:5:Careers` | ✅ Configured (159 open roles, strong role match) |
| Mattel | SmartRecruiters | `MattelInc` | ✅ Configured (332 roles, weak IT-security match only) |
| **Qualcomm** | Eightfold | `qualcomm.eightfold.ai` (subdomain confirmed) | ❌ Blocked — public API returns 403; needs a future Playwright plugin |
| **Simbian** | Zoho Recruit | `simbian.zohorecruit.in` | ✅ Configured (unblocked 2026-07-06 — the public endpoint needs no OAuth after all) |
| Sibros | **Rippling ATS** | `ats.rippling.com/sibros-technologies` | Not one of the 9 target platforms — good IoT/automotive-security fit if Rippling gets built later |
| EY | **SAP SuccessFactors** | `careers.ey.com` | Out of scope — not one of the 9 target platforms |
| cyber-times.in | **SKIP** | — | Site offline / impersonation probe |

## Companies by ATS — live-verified cybersecurity vendors (2026-07-05)

Full research notes (job counts, per-title matches, rejected/dead slugs) live in the
2026-07-05 PLAN.md §9 decision log entries. Configured in `.env` today:

### Greenhouse (best yield for this vertical)
Huntress, SentinelOne (`sentinellabs`), Wiz (`wizinc`), Dragos, Tenable (`tenableinc`),
Databricks, Cloudflare, Corelight, Okta, Abnormal Security, Recorded Future, Datadog, GitLab,
Netskope, CloudSEK (`cloudsek`), Zscaler (2026-07-06 addition — see below).
Snyk's board (`snyk`) 404s — appears to have migrated off Greenhouse, not re-identified.

### Lever (thin — most seed guesses were actually Greenhouse/Ashby)
CertiK, Saviynt (2026-07-06 addition).

### Ashby
Chainalysis (`chainalysis-careers`), Vanta, Material Security, Drata (weak match), Nudge
Security (0 current openings, kept configured for future postings).

### Workday (needs `tenant:wdN:site`, not a bare slug)
Cisco (`cisco:5:Cisco_Careers` — Splunk postings are folded into this same tenant), Palo Alto
Networks (`paloaltonetworks:5:panwexternalcareers`), Qualys (`qualys:5:Careers`), Trend Micro
(`trendmicro:3:External`), Arctic Wolf (`arcticwolf:1:External`), Rapid7 (`mymoose:1:careers`
— non-obvious tenant name, confirmed via real job URLs; very few current openings), F5
(`ffive:5:f5jobs`), Trellix (`trellix:1:EnterpriseCareers`), CrowdStrike
(`crowdstrike:5:crowdstrikecareers`) — last three added 2026-07-06.

### Zoho Recruit
Simbian (`simbian.in` — Phase 1 owner target, unblocked), InstaSafe (`instasafe.com`), Astra
Security (`getastraus.in` — note the subdomain differs from the public brand name).

### SmartRecruiters
Bosch (`BoschGroup`), Visa, Mattel (`MattelInc`). Ignore stale slug `PaloAltoNetworks1` — a
single dead 2017 posting; PAN's real career site is the Workday tenant above.

### Recruitee
Aikido Security (`aikidosecurity`) — strong AI-pentest match.

### Workable
Dispel — Zero Trust remote access for ICS/SCADA, strong thematic fit.

### BambooHR
No live cybersecurity customer identified yet — plugin built and tested, `.env` list left
empty until one is found.

---

## Hyderabad/Bengaluru company research — Phase 2, wave 1 (2026-07-06)

Ongoing, open-ended effort (no batch cap) to find companies with real Hyderabad/Bengaluru
offices, security-related first, then broadening to all IT companies in later waves. Pune
and Delhi are a stated future city addition. Classification method: **Bucket A** = already
on a known ATS platform → just add to that platform's `.env` list (no new plugin). **Bucket
B** = known-but-deferred platform (Eightfold, Zoho Recruit's auth API, iCIMS, Taleo, SAP
SuccessFactors, Rippling) → flagged, not built one-off. **Bucket C** = genuinely custom site
→ candidate for a new thin plugin using `_career_util.py`.

**Bucket A — added to `.env` this wave:** Palo Alto Networks (already configured — confirmed
still correct), F5, Netskope, Trellix, Tenable (already configured), CloudSEK, Qualys (already
configured), CrowdStrike, Zscaler (research initially mis-flagged this as "abandoned
Greenhouse" — live-verified the standard `boards-api.greenhouse.io/v1/boards/zscaler/jobs`
slug still resolves directly, 320 jobs, no proxy needed), Saviynt (research called this
"HubSpot CMS custom" — a live network-capture during Playwright rendering found the real job
data is fetched client-side from `api.lever.co/v0/postings/saviynt`, i.e. it's plain Lever).

**Not onboarded — flagged:** Contrast Security (Ashby, slug `contrast-security` confirmed live)
— India office presence unconfirmed by research; don't add on office-presence grounds alone
until verified.

**Bucket B (deferred, not built):** Astra Security → Zoho Recruit (**correction**: initially
Bucket B per the old "needs OAuth" assumption — re-classified to Bucket A once the public
endpoint was discovered this wave; now configured as `getastraus.in`). American Express →
Eightfold (still blocked, same 403 pattern as Phase 1's Qualcomm/NVIDIA finding).

**Bucket C — built this wave:** Synopsys (`synopsys.py`, Tier 3 Playwright render — see the
"Currently built" table above). InstaSafe was initially flagged Bucket C ("Gatsby SPA, no
visible data") but a live network capture found its real data source is a **public Zoho
Recruit endpoint** — re-classified to the Zoho Recruit platform above, not a custom plugin.

**Bucket C — investigated, not built this wave (genuinely needs more work):**
- **Check Point** (`careers.checkpoint.com`) — returns `403` to a real headless-browser
  request too (not just curl); likely bot/WAF-protected regardless of rendering approach.
- **Seclore** (`seclore.darwinbox.in`) — genuine client-side SPA shell (335-byte raw HTML,
  no embedded JSON blob); would need either full Playwright DOM-scraping (not yet attempted)
  or a Darwinbox-specific API discovery pass like the one that worked for Zoho Recruit.
- **Appknox** (outbound to `cutshort.io/job/<slug>`) — Cutshort's `__NEXT_DATA__` blob has
  `dehydratedState: null`; the real job data is fetched via a client-side call not yet
  identified. Cutshort is itself a multi-company Indian job aggregator — worth a dedicated
  investigation pass (like Zoho Recruit) rather than a single-company plugin, since finding
  its API would unlock many companies at once, not just Appknox.

**Flagged — no static or rendered signal found, needs a dedicated follow-up pass:** Radware,
Fortinet, Sophos (fetch consistently failed, likely bot-protected), Wells Fargo, Deutsche
Bank, SentinelOne's own careers page (note: SentinelOne is already covered via Greenhouse
under a different board name, `sentinellabs`), Deepfence (bot-challenge blocked), Imperva
(old Jobvite board dead post-Thales-acquisition, new career site not yet found), WeSecureApp
(rebranded to "Strobes Security", new domain not yet found).

**New platforms discovered, worth a future dedicated build (like Zoho Recruit was this
wave) once more companies on them are confirmed:** Avature (Synopsys uses it for their EVP
site alongside the custom search-jobs micro-site actually built), Freshteam/Freshworks
(Cyware — public job list not found, `/api/*` paths all `401`), Darwinbox (Seclore — common
Indian HRMS/ATS, likely to recur in later all-IT waves), Cutshort (Appknox — Indian job-board
aggregator), Oracle Fusion Cloud Recruiting / `CX_#` URL pattern (JPMorgan Chase confirmed,
Akamai suspected — distinct from classic Taleo, medium confidence only).

---

## How to add a company to an existing ATS plugin

No new file needed for any of the 8 platforms above — just add the slug to the `.env` list:

```
# 1. Find the company's slug (visit the pattern URL above with a guessed slug)
curl https://boards-api.greenhouse.io/v1/boards/crowdstrike/jobs | jq '.jobs[0]'

# 2. Add it to .env (comma-separated; slug:Display Name if the ATS has no company field)
GREENHOUSE_COMPANIES=gitlab,crowdstrike

# 3. Scrape (registry auto-discovers the plugin; no --list step needed for config changes):
python3 .claude/skills/job-scraper/scripts/scrape.py --source greenhouse --query "security engineer"
```

## How to add a NEW ATS platform (Eightfold via Playwright, SmartRecruiters-style JSON, …)

```
# 1. Verify the platform's public JSON pattern (see table above) — curl it BEFORE writing
#    code; several "Pattern" endpoints in earlier passes turned out wrong on contact
#    (Workday's real page-size cap, Workable's real body shape, Eightfold's 403 gate).
# 2. Create .claude/skills/job-scraper/plugins/<platform>.py following the
#    greenhouse.py/lever.py/ashby.py/smartrecruiters.py pattern: parse_companies(ENV_VAR)
#    (or a bespoke parser like parse_workday_companies for multi-part configs) from
#    _ats_util, round_robin() merge across companies, is_available() gated
#    on the env var being non-empty.
# 3. Verify auto-discovery (note: registry.py alone does NOT load .env — use scrape.py --list):
python3 .claude/skills/job-scraper/scripts/scrape.py --list
```

See PLAN §4 and §10 for the plugin interface spec and full roadmap.

---
*Updated: 2026-07-05. Verify ATS slugs when adding companies — they change ATSes occasionally.*
