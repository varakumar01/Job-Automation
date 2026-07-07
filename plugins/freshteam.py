"""Freshteam job-portal plugin (public server-rendered HTML + JSON-LD detail
— no token required). PLAN.md §4/§10.

Freshteam (a Freshworks HRMS/ATS product) career sites are genuinely
server-rendered — NOT a JS-only SPA. A prior lighter research pass found a
Freshteam company's `/api/*` path 401'ing and assumed the platform was
auth-gated; a deeper live pass (2026-07-07) against 4 independent tenants
found the actual public career pages need no API call at all:

1. List: GET https://<tenant>.freshteam.com/jobs — a plain, unauthenticated
   HTML page whose job cards are already rendered server-side, each a
   `<a href="/jobs/<id>/<slug>" data-portal-location="...">` wrapping a
   `<div class="job-title">Title</div>` — scraped directly via regex, no
   embedded JSON blob needed at all (simpler than any ATS plugin built so far
   in this codebase). The confirmed-401 `/api/portal/jobs` path is a
   credential-gated recruiter-portal endpoint, unrelated to and unnecessary
   for public job listing.
2. Detail: GET the job's own detail URL — embeds one
   `<script type="application/ld+json">` `JobPosting` block with the
   canonical `title`/`description`/`datePosted` via
   `_career_util.extract_ld_json`, same tier-2 pattern as iCIMS/Cutshort.

Configure via `FRESHTEAM_COMPANIES` in `.env`. A Freshteam tenant is
addressed by the subdomain slug in its careers URL
(`<tenant>.freshteam.com`) — single-part identifier like Greenhouse's slug,
so this reuses `_ats_util.parse_companies` (`tenant` or `tenant:Display Name`):

    FRESHTEAM_COMPANIES=cyware:Cyware,nxtwave:NxtWave

Field-shape facts (verified live 2026-07-07 against cyware.freshteam.com,
nxtwave.freshteam.com — both bare 200s, no cookies/auth of any kind):
  - The list page's job id is the FIRST path segment after `/jobs/` in each
    card's href (e.g. `/jobs/oJgcZDzVLAOG/sr-digital-marketing-strategist` →
    id `oJgcZDzVLAOG`) — prefixed `<tenant>:<id>` for cross-tenant uniqueness
    like every other ATS plugin here.
  - No pagination signal was found on the `/jobs` list page during research
    (every tested tenant's full job list rendered on one page) — this plugin
    fetches page 1 only; if a tenant with more postings than fit on one page
    is found during the populate pass, that's a signal to revisit, not an
    assumption to build speculative pagination against now.
  - The list page's own `data-portal-location` attribute is already a clean
    pre-formatted human string (e.g. "Bengaluru, India") — used as-is rather
    than the detail page's JSON-LD `jobLocation` address object (which,
    confirmed live, has `addressLocality`/`addressRegion` populated in a
    swapped/inconsistent order across tenants), same "prefer the clean
    list-level string" decision already made for `cutshort.py`.
  - The DETAIL call's `description` (job JD, richer HTML) is, like
    Darwinbox's `jd` field, DOUBLE HTML-encoded — the JSON-LD string value
    itself contains literal `&lt;p ...&gt;` entity sequences representing
    real HTML markup that was itself entity-escaped before being embedded.
    Same fix as `darwinbox.py`: `html.unescape()` once FIRST to reveal the
    real tags, then `strip_html()` a second time to actually strip them
    (safe no-op on single-encoded/plain text too).
  - The DETAIL call's `datePosted` (e.g. `"2026-07-06 03:14:49 UTC"`) is NOT
    strict ISO-8601 (space instead of "T", literal "UTC" instead of an
    offset) but is a fixed-width, lexicographically-sortable format — stored
    as-is rather than reformatted, since no other plugin in this codebase
    needs to compare posted_at values across different plugins' differing
    native formats.

Cost note: one extra detail call PER matched posting, same pattern as every
other ATS plugin here (Oracle Fusion/SuccessFactors/Rippling/iCIMS/Cutshort/
Darwinbox/Workday/SmartRecruiters/BambooHR).

No Apify dependency — stdlib `urllib`/`re`. `is_available` is True only when
`FRESHTEAM_COMPANIES` names at least one tenant.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import matches, parse_companies, round_robin, strip_html  # noqa: E402
from _career_util import extract_ld_json, fetch_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_JOBS_URL = "https://{tenant}.freshteam.com/jobs"
_ENV_VAR = "FRESHTEAM_COMPANIES"
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per tenant per call

_JOB_ROW_RE = re.compile(
    # `\b[^>]*?href=` (not a bare `<a href="`) tolerates `href` appearing
    # anywhere on the anchor tag, not only as the first attribute — a
    # platform markup change reordering attributes would otherwise silently
    # zero out every match with no error raised.
    r'<a\b[^>]*?href="(?P<href>/jobs/[^"]+)"[^>]*data-portal-location="(?P<location>[^"]*)"[^>]*>'
    r'.{0,300}?<div class="job-title">(?P<title>.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)


def _job_id_from_href(href: str) -> str | None:
    """The job id is the path segment right after `/jobs/`
    (`/jobs/<id>/<slug>`) — the slug alone (last segment) is not guaranteed
    stable/unique the way the id is, so this is NOT the same fallback as
    `_career_util.job_id_from_url`."""
    parts = href.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "jobs" and parts[1]:
        return parts[1]
    return None


def _fetch_candidates(tenant: str, query: str) -> list[dict]:
    """List candidate jobs for one tenant from the single (unpaginated)
    `/jobs` page, pre-filtered by a cheap title match before any detail
    fetch."""
    words = [w.lower() for w in query.split() if len(w) > 1]
    base_url = _JOBS_URL.format(tenant=tenant)
    list_html = fetch_html(base_url)
    candidates: list[dict] = []
    for href, location, raw_title in _JOB_ROW_RE.findall(list_html):
        title = strip_html(raw_title)
        if not matches(title, words):
            continue
        job_id = _job_id_from_href(href)
        if not job_id:
            continue
        candidates.append(
            {
                "job_id": job_id,
                "url": urljoin(base_url, href),
                "title": title,
                "location": html.unescape(location).strip() or None,
            }
        )
        if len(candidates) >= _MAX_DETAIL_FETCHES:
            break
    return candidates


def _jd_text(data: dict) -> str | None:
    raw = data.get("description")
    if not raw:
        return None
    # `description` is HTML markup that was itself HTML-entity-escaped
    # before being embedded as a JSON-LD string value (same quirk as
    # darwinbox.py's `jd` field) — unescape once to reveal real tags, THEN
    # strip_html (which strips tags first, unescapes second) to strip them;
    # strip_html alone on the still-encoded string strips nothing.
    return strip_html(html.unescape(raw)) or None


def _fetch_detail(url: str) -> dict:
    detail_html = fetch_html(url)
    postings = extract_ld_json(detail_html, ld_type="JobPosting")
    data = postings[0] if postings and isinstance(postings[0], dict) else {}
    return {
        "title": strip_html(data.get("title") or "") or None,
        "jd_text": _jd_text(data),
        "posted_at": data.get("datePosted"),
    }


def _to_job(candidate: dict, detail: dict, tenant: str, display_name: str) -> Job:
    ext_id = f"{tenant}:{candidate['job_id']}"
    return Job(
        source="freshteam",
        ext_id=ext_id,
        url=candidate["url"],
        title=detail.get("title") or candidate.get("title"),
        company=display_name,  # Freshteam postings' hiringOrganization can lag the configured display name
        location=candidate.get("location"),
        posted_at=detail.get("posted_at"),
        jd_text=detail.get("jd_text"),
        extra=candidate,
    )


class FreshteamPlugin(JobSourcePlugin):
    """Company career-site postings across every Freshteam tenant configured
    in FRESHTEAM_COMPANIES."""

    name = "freshteam"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []

        # Collect each tenant's matches into its own list, then round-robin
        # merge — otherwise the first tenant alone can fill `limit` and every
        # other configured tenant is silently never represented.
        per_company: list[list[Job]] = []
        for tenant, display_name in companies:
            try:
                candidates = _fetch_candidates(tenant, query)
            except Exception as exc:
                print(f"  freshteam: {tenant}: list fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for candidate in candidates:
                detail: dict = {}
                try:
                    detail = _fetch_detail(candidate["url"])
                except Exception as exc:
                    # A transient detail-fetch failure shouldn't drop the
                    # posting entirely — the list identity (url + title)
                    # alone is enough to store a minimally-useful row.
                    print(f"  freshteam: {tenant}: detail fetch failed for {candidate['url']} — {exc}", file=sys.stderr)
                try:
                    job = _to_job(candidate, detail, tenant, display_name)
                except Exception as exc:
                    print(f"  freshteam: {tenant}: skipping {candidate.get('url')} — {exc}", file=sys.stderr)
                    continue
                company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
