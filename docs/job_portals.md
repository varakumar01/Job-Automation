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
| **Oracle Fusion Cloud Recruiting** | `oraclefusion.py` | `ORACLEFUSION_COMPANIES` | Built 2026-07-07. Public, unauthenticated REST (`recruitingCEJobRequisitions` list + `recruitingCEJobRequisitionDetails` detail) — `expand=requisitionList` is REQUIRED on the list call or it returns only search-criteria echo. Identifier is `host:site` or `host:site:Display Name` (host varies per tenant/region; site is a `CX_<N>` number read off the tenant's own careers URL). Oracle's own docs call this REST resource "internal use only" — works anyway, no access control. |
| **SAP SuccessFactors (CSB2)** | `successfactors.py` | `SUCCESSFACTORS_COMPANIES` | Built 2026-07-07. NO bare public JSON API (OData v2 needs per-tenant Basic Auth, confirmed 401) — scrapes the public `sitemap.xml` + server-rendered job-detail HTML instead (schema.org microdata via a new `_career_util.extract_by_itemprop` helper). Identifier is the FULL tenant hostname (`<subdomain>.jobs.hr.cloud.sap`), not a bare slug. Location/posted-date labels vary per tenant template (tried via an ordered candidate list). Only the CSB2 flavor is covered, not the older "Career Portal" DWR/AJAX-RPC flavor. |
| **Rippling ATS** | `rippling.py` | `RIPPLING_COMPANIES` | Built 2026-07-07. Clean public, unauthenticated JSON API (`ats.rippling.com/api/v2/board/<slug>/jobs`), bare slug identifier. Simplest/cleanest of the 2026-07-07 batch — no template variance or auth quirks found across 4 tenants. |
| **iCIMS** | `icims.py` | `ICIMS_COMPANIES` | Built 2026-07-07 — reverses an earlier "HTML-only, needs browser" assessment (see below). The bare `/jobs/search` endpoint needs a required `pr=<page>` query param or it silently falls through to an empty Angular shell; WITH it, the same domain returns real server-rendered job rows. Detail via schema.org JSON-LD. No working RSS/XML feed exists (corrects an earlier claim). The literal string `"UNAVAILABLE"` is iCIMS's own sentinel for a blank address field — filtered out, not stored as real location data. |
| **Cutshort** | `cutshort.py` | `CUTSHORT_COMPANIES` | Built 2026-07-07 (unlocks Appknox) — an Indian tech-hiring aggregator; the company page's own Next.js `__NEXT_DATA__` genuinely embeds the full job list server-side (a shallower earlier check saw a null `dehydratedState`, a cache/edge-case, not the platform default). Identifier is the FULL opaque `/company/<alias>` string (parens/suffix and all), copied verbatim — no shorter form exists. |
| **Darwinbox** | `darwinbox.py` | `DARWINBOX_COMPANIES` | Built 2026-07-07 (unlocks Seclore) — a common Indian HRMS/ATS. The SPA shell is genuinely empty, but the frontend's own JS bundle reveals a clean public same-origin JSON API (`/ms/candidateapi/job`). Tenant TLD (`.in`/`.com`) is auto-probed, not configured. The detail call's `jd` field is DOUBLE HTML-encoded — needs `html.unescape()` then `strip_html()` (the same fix recurred for Freshteam). |
| **Freshteam** | `freshteam.py` | `FRESHTEAM_COMPANIES` | Built 2026-07-07 — reverses an earlier "auth-gated, `/api/*` returns 401" assessment (see below); that 401'd path is a credential-gated RECRUITER-portal endpoint, unrelated to public job listing. The actual public `/jobs` page is genuinely server-rendered HTML — simplest plugin in this batch, no API call at all. Same double-HTML-encoding fix as Darwinbox for the `description` field. No pagination signal found on any tested tenant (fetches page 1 only). |
| **Avature** | `avature.py` | `AVATURE_COMPANIES` | Built 2026-07-07 — reverses an assumption that Avature needs a full headless-browser approach (based on the existing bespoke `synopsys.py` Playwright plugin, which remains separate/unaffected). The common `SearchJobs`/`JobDetail` template is genuinely server-rendered across unrelated tenants. TWO card-layout generations supported (labeled City/State/Country paragraphs vs. plain `list-item-location`/`list-item-posted` spans); a THIRD, older generation (KPMG-style `JobDetail?jobId=` query params) is confirmed to exist but not supported. Identifier is the FULL hostname (some tenants use a custom CNAME, not `<tenant>.avature.net`). Lowest-priority provider in the batch — built last. |

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

### Oracle Fusion Cloud Recruiting (added 2026-07-07, Wave 2)
Akamai (WAF/DDoS/Zero Trust vendor, smoke-test seed), Fortinet (`edel.fa.us2.oraclecloud.com:
CX_2001` — 789-910 live roles), Kroll (`hcxs.fa.us2.oraclecloud.com:CX_1` — risk-advisory firm
with a real Cyber Risk practice).

### SAP SuccessFactors (added 2026-07-06, populated 2026-07-07, Wave 2)
W.L. Gore (smoke-test seed) only. ~50 major security vendors/MSSPs checked during the populate
pass (McAfee, CyberArk, Check Point, Darktrace, Mandiant, Fortinet, Sophos, Bitdefender, ESET,
Kaspersky, Secureworks, Trustwave, NCC Group, Optiv, etc.) — none resolve on this specific CSB2
flavor; all are on Workday/Ashby/a legacy SF "Career Portal" flavor instead. Genuine platform-
adoption gap for this vertical, not a search shortfall.

### Rippling ATS (added 2026-07-07, Wave 2)
Chess.com (smoke-test seed, not security), RSA Security (SIEM/identity vendor), Swimlane (SOAR
product vendor), Agency Cybersecurity (GRC/compliance MSSP), Workstreet (GRC/compliance
consultancy).

### iCIMS (added 2026-07-07, Wave 2)
HERE Technologies (smoke-test seed, not security) only. The one strong candidate found,
Peraton (national-security contractor, real cyber-intel roles), was excluded — its tenant
sits behind an inconsistent AWS WAF "Human Verification" challenge and is tier-2/3-border
(diversified govcon, not pure-play security).

### Cutshort (added 2026-07-07, Wave 2)
Appknox (mobile app security vendor, smoke-test seed AND genuine target), Innefu Labs
(AI-driven national/cyber security product), Securin Labs (exposure/vuln management
platform), Metron Security (Splunk/QRadar/CrowdStrike security services), SecurEyes (security
consulting/MSSP).

### Darwinbox (added 2026-07-07, Wave 2)
Seclore (data-security vendor, smoke-test seed AND genuine target), Quick Heal Technologies
(antivirus/endpoint security vendor, owns Seqrite), ReBIT (RBI-owned fintech infra org with
real AppSec/SSDLC postings). Pure-play security vendors proved rare on this platform during a
broad ~40-tenant sweep.

### Freshteam (added 2026-07-07, Wave 2)
Cyware (threat-intel vendor, smoke-test seed AND genuine target), Payatu (pentest/red-team/IoT
security services), Strobes Security (risk-based vuln mgmt / PTaaS / CTEM product — the
rebrand of WeSecureApp, flagged unresolved in Wave 1).

### Avature (added 2026-07-07, Wave 2, lowest priority)
Xerox (smoke-test seed, not security), ManTech (defense/intel IT services with a real cyber
practice — required a template-variance regex fix during the populate pass, see PLAN.md §9).

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

**RESOLVED in Wave 2 (2026-07-07)** — these Wave-1 "flagged/deferred/needs more work" items
turned out to have real public endpoints once a proper platform-level discovery pass ran,
rather than needing per-company custom work:
- **Seclore** (was: "genuine client-side SPA shell, 335-byte raw HTML, no embedded JSON") —
  the SPA shell finding was correct, but its frontend's own JS bundle reveals a clean public
  same-origin JSON API (`/ms/candidateapi/job`) — see the new **Darwinbox** platform plugin
  (`darwinbox.py`) in the "Currently built" table above. Now configured as `seclore`.
- **Appknox** (was: "Cutshort's `dehydratedState: null`, real API not yet identified") — a
  deeper pass found the company page's `__NEXT_DATA__` genuinely embeds the full job list
  server-side; the earlier `null` was a cache/edge-case, not the platform default — see the
  new **Cutshort** platform plugin (`cutshort.py`). Now configured as
  `appknox-(xysec-labs-pte-ltd)-j2I4OU56`.
- **Fortinet** (was: "no static/rendered signal found, likely bot-protected") — that finding
  was for Fortinet's own branded domain; Fortinet's REAL careers page runs on **Oracle Fusion
  Cloud Recruiting** (`edel.fa.us2.oraclecloud.com:CX_2001`), confirmed live with 789-910 open
  postings — a different-platform resolution, not a bot-protection fix.
- **WeSecureApp** (was: "rebranded to Strobes Security, new domain not yet found") — found:
  `strobes.freshteam.com`, now configured on the new **Freshteam** platform plugin.

**Still flagged — no static or rendered signal found, needs a dedicated follow-up pass:**
Radware, Sophos (fetch consistently failed, likely bot-protected), Wells Fargo, Deutsche Bank,
SentinelOne's own careers page (note: SentinelOne is already covered via Greenhouse under a
different board name, `sentinellabs`), Deepfence (bot-challenge blocked), Imperva (old Jobvite
board dead post-Thales-acquisition, new career site not yet found).

**New platforms discovered in Wave 1 — ALL BUILT in Wave 2 (2026-07-07):** Avature, Freshteam/
Freshworks, Darwinbox, Cutshort, Oracle Fusion Cloud Recruiting, plus SAP SuccessFactors and
Rippling ATS (identified independently during Wave 2's own provider-discovery pass) and iCIMS
(reversing a Wave-1 "HTML-only, needs browser" assessment). See the "Currently built" table at
the top of this doc for each platform's endpoint pattern and build date; see PLAN.md §9 for
the full technical decision log of what each discovery pass found and how each plugin works.

---

## Bulk company discovery via search-engine indexing (2026-07-06)

**The technique:** every ATS platform's public job-board pages are ordinary indexed webpages
— `job-boards.greenhouse.io/<slug>/jobs/<id>`, `jobs.lever.co/<slug>/<id>`, etc. A plain
`site:<platform-domain> <role keyword>` web search surfaces many companies' slugs directly
from the result URLs, in one query — no directory/API needed, since search engines already
crawled these pages. This answers the "can't we just check every company at once?" question
from earlier: not via the ATS platforms themselves (they expose no customer-list API), but
via a search engine that already indexed their public pages.

**Method:** `site:<domain> "detection engineer" OR vulnerability OR "product security" OR
"cloud security" OR "penetration test"` (varied per query) against each platform's shared
domain (`job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`,
`apply.workable.com`, `*.recruitee.com`, `*.zohorecruit.com`/`.in`, `myworkdayjobs.com`).
Extract the company slug from each result URL, dedupe against what's already configured,
then **live-verify every candidate** (curl the real API, confirm it resolves with a real job
count) before adding — discovery ≠ verification, same rule as every company added so far.

**Result: 41 → 96 companies in one pass** (full current list: `docs/supported_companies.md`,
regenerate via `list_companies.py`). Notable finds: Anthropic, Palantir, HackerOne,
1Password, Adobe, Red Hat (all major names, previously missed by manual one-by-one
research), plus strong candidate-fit companies: Nozomi Networks (ICS/OT — Greenhouse),
Horizon3.ai (offensive security — Ashby), Evolve Security/BreachLock/Rhino Security Labs-style
firms (security consultancies — Workable), ON2IT (pure cybersecurity — Recruitee), Unit21/CDIT
(fraud/security fintech — Zoho Recruit).

**Rejected during verification (real findings, not guesses):**
- `jobgether` (Lever) — resolved with 4,803 postings, but inspecting actual items showed
  unrelated roles (.NET dev, TPM, blockchain frontend) — it's a recruiting/job-marketplace
  platform posting for many unrelated employers under one Lever account, not a single
  company. Adding it would mislabel every posting's `company` field. Excluded.
- `certifyos` (Lever), `dbtlabs`/`dbtlabsinc` (Greenhouse), `rhino-security-labs` (Workable),
  `firstdue.com` (Zoho Recruit) — all resolved with 0 jobs (wrong/stale slug or genuinely
  empty board). Excluded, not worth an empty `.env` entry.
- `1x.recruitee.com`, `helpag.recruitee.com` — valid, resolving orgs (1X Technologies, Help AG)
  but 0 open roles at verify time. Not added this pass; worth rechecking later since the org
  itself is legitimate and thematically relevant (1X = robotics/product security, Help AG =
  security consultancy).

**Known pre-existing data quirk found during this pass, not introduced by it:** Corelight's
Greenhouse `company_name` field returns the literal string `"Job Board"` instead of
`"Corelight"` — a data-quality issue on Corelight's own Greenhouse configuration. `source`
stays correctly `"corelight"` in the store, so identification isn't affected, only the
cosmetic `company` display value. Not fixed in code: `greenhouse.py`'s `_to_job` prefers the
API's `company_name` over the configured display name whenever the API provides ANY value
(right behavior for the other 34 companies, whose real `company_name` is more accurate than
an auto-derived one) — special-casing one company's bad data isn't worth the added
complexity. If this becomes a real problem, the fix is a per-slug "force override" flag in
`parse_companies`, not a global priority flip.

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
