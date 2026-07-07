"""iCIMS job-portal plugin (public paginated HTML list + JSON-LD detail — no
token required). PLAN.md §4/§10.

iCIMS has NO bare public JSON search API — hitting `/jobs/search` or
`/jobs/intro` with no params (or the wrong params) returns an empty client-side
Angular shell (`ng-app="jibeapply"`, ~400KB boilerplate, 0 job rows), which is
what made this platform look JS-rendered-only in an earlier research pass. The
real, SERVER-RENDERED list lives behind a REQUIRED `pr=<page>` query param:

1. List: GET https://careers-<slug>.icims.com/jobs/search?pr=<page>&in_iframe=1
   — a plain, unauthenticated HTML page (confirmed live 2026-07-07 against 3
   real tenants) containing real `<h3>Title</h3>` job rows linking to each
   posting's detail URL (which embeds the numeric job id). Used only as a
   cheap listing + local title pre-filter before any detail fetch — no JSON
   here, just HTML row-scraping.
2. Detail: GET the job's own detail URL (plain HTML) — every iCIMS job page
   embeds one `<script type="application/ld+json">` block with full
   schema.org `JobPosting` data (title, description, jobLocation, datePosted)
   via `_career_util.extract_ld_json`, same tier-2 pattern as any other
   JSON-LD-based bespoke site.

Configure via `ICIMS_COMPANIES` in `.env`. An iCIMS tenant is addressed by the
subdomain slug in its careers URL (`careers-<slug>.icims.com`) — a single-part
identifier like Greenhouse's slug, so this reuses `_ats_util.parse_companies`
(`slug` or `slug:Display Name`):

    ICIMS_COMPANIES=here:HERE Technologies,slco:Salt Lake County

Field-shape facts (verified live 2026-07-07 against careers-here.icims.com,
careers-slco.icims.com, careers-dohertyinc.icims.com — all bare 200s, no
cookies/auth of any kind):
  - The list page's job rows are `<a href=".../jobs/<ID>/<slug>/job?in_iframe=1">
    <h3>Title</h3></a>` blocks — `<ID>` (numeric) is captured straight from the
    URL and used as the posting id (no separate id field exists), prefixed
    `<slug>:<id>` for cross-tenant uniqueness like every other ATS plugin here.
  - Pagination is via the `pr=<n>` query param, **0-indexed** (found live
    2026-07-07 — `pr=0` and `pr=1` return genuinely different, non-overlapping
    job sets on every tenant tested, including `here`; three other tenants
    tested — `c1`, `shure`, `ideagenen` — return ZERO rows at `pr=1` and only
    have real content at `pr=0`. The loop below starts at page 0, not 1 — an
    earlier version started at 1, silently skipping every tenant's first page
    of postings since this plugin was built). There is no page-size param and
    no total-count field to read, so listing simply stops when a page returns
    zero matched rows or `_MAX_LIST_PAGES` is hit.
  - The detail page's JSON-LD `JobPosting` block carries the canonical
    `title`/`description`/`jobLocation`(a list of address objects)/`datePosted`
    — all preferred over the list page's bare title when the detail fetch
    succeeds; the list row's own title text is kept ONLY as a fallback if the
    detail fetch or its JSON-LD parse fails (same "never drop a slug-matched
    posting outright" pattern as `successfactors.py`).
  - No public RSS/XML feed exists on this platform despite an earlier (now
    corrected) docs claim — `/jobs/rss`, `/jobs/feed`, `/xmlapi`, `?mode=rss`
    all fall through to the same empty Angular shell, confirmed live
    2026-07-07 across 4 path variants on 1 tenant.
  - Some tenants front their iCIMS career site with a custom domain / proxy
    (`<base href>` rewritten to a different host) or 301-redirect off iCIMS
    entirely — `urllib` follows redirects transparently, so those tenants
    either still work (if the redirect target is a normal iCIMS host) or fail
    cleanly per-company (caught by the per-company try/except below) rather
    than crashing the whole run.

Cost note: one extra detail call PER slug-matched posting, same pattern as
Oracle Fusion/SuccessFactors/Rippling/Workday/SmartRecruiters/BambooHR.

No Apify dependency — stdlib `urllib`/`html.parser`/`re`. `is_available` is
True only when `ICIMS_COMPANIES` names at least one tenant.
"""

from __future__ import annotations

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

_LIST_URL = "https://careers-{slug}.icims.com/jobs/search?pr={page}&in_iframe=1"
_ENV_VAR = "ICIMS_COMPANIES"
_MAX_LIST_PAGES = 4  # bounds list calls per tenant
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per tenant per call

_JOB_ROW_RE = re.compile(
    # Real tenant markup interposes extra tags (e.g. a
    # `<span class="sr-only field-label">Requisition Title</span>`) between
    # the anchor's opening tag and the `<h3>` title — bounded non-greedy gap
    # (not a bare `\s*`) tolerates that without risking a runaway match
    # jumping into an unrelated later row.
    r'<a[^>]+href="(?P<href>[^"]*?/jobs/(?P<id>\d+)/[^"]*)"[^>]*>.{0,300}?<h3[^>]*>\s*(?P<title>.*?)</h3>',
    re.DOTALL | re.IGNORECASE,
)


def _fetch_candidates(slug: str, query: str) -> list[tuple[str, str, str]]:
    """List (job_id, href, list_title) candidates for one tenant across up to
    `_MAX_LIST_PAGES` of the paginated HTML list, pre-filtered by a cheap
    title match before any detail fetch. Stops early once a page returns zero
    rows (either end-of-results or an unsupported tenant template) or once
    `_MAX_DETAIL_FETCHES` matched candidates have been collected."""
    words = [w.lower() for w in query.split() if len(w) > 1]
    candidates: list[tuple[str, str, str]] = []
    for page in range(0, _MAX_LIST_PAGES):
        list_url = _LIST_URL.format(slug=slug, page=page)
        list_html = fetch_html(list_url)
        rows = _JOB_ROW_RE.findall(list_html)
        if not rows:
            break
        for href, job_id, raw_title in rows:
            # every tenant seen live uses absolute hrefs, but resolve against
            # the list page's own URL in case a tenant ever emits a relative
            # one — a bare relative path would otherwise 404 on both the
            # later fetch_html(href) detail call and the stored Job.url.
            href = urljoin(list_url, href)
            title = strip_html(raw_title)
            if not matches(title, words):
                continue
            candidates.append((job_id, href, title))
            if len(candidates) >= _MAX_DETAIL_FETCHES:
                return candidates
    return candidates


def _location_from_ld(data: dict) -> str | None:
    """Join each jobLocation entry's city/region/country into a human string.
    iCIMS's own JSON-LD uses the literal string "UNAVAILABLE" as a sentinel
    for a field the tenant left blank (confirmed live 2026-07-07, e.g. a
    Beijing posting's addressRegion) — filtered out here alongside the usual
    falsy check, or it would leak into stored locations as real data."""
    loc = data.get("jobLocation")
    if isinstance(loc, dict):
        locs = [loc]
    elif isinstance(loc, list):
        locs = loc
    else:
        locs = []
    formatted: list[str] = []
    for entry in locs:
        if not isinstance(entry, dict):
            continue
        addr = entry.get("address")
        if not isinstance(addr, dict):
            continue
        pieces = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
        # schema.org allows addressCountry/addressRegion to be a nested
        # {"@type": "Country", "name": "US"} object rather than a plain
        # string — a dict is truthy but would crash the join below.
        pieces = [p for p in pieces if isinstance(p, str) and p and p != "UNAVAILABLE"]
        if pieces:
            formatted.append(", ".join(pieces))
    return "; ".join(formatted) or None


def _parse_detail(detail_html: str) -> dict:
    postings = extract_ld_json(detail_html, ld_type="JobPosting")
    data = postings[0] if postings and isinstance(postings[0], dict) else {}
    return {
        "title": strip_html(data.get("title") or "") or None,
        "jd_text": strip_html(data.get("description") or "") or None,
        "location": _location_from_ld(data),
        # NOT a fallback to validThrough — that's the application deadline
        # in the schema.org JobPosting vocabulary, not the posting date; a
        # missing posted_at is safer downstream than a misleading one.
        "posted_at": data.get("datePosted"),
    }


def _to_job(job_id: str, href: str, list_title: str, info: dict, slug: str, display_name: str) -> Job:
    ext_id = f"{slug}:{job_id}"
    return Job(
        source="icims",
        ext_id=ext_id,
        url=href.split("?")[0],
        title=info.get("title") or list_title or None,
        company=display_name,  # iCIMS postings' hiringOrganization can lag the configured display name
        location=info.get("location"),
        posted_at=info.get("posted_at"),
        jd_text=info.get("jd_text"),
        extra=info,
    )


class ICIMSPlugin(JobSourcePlugin):
    """Company career-site postings across every iCIMS tenant configured in
    ICIMS_COMPANIES."""

    name = "icims"

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
        for slug, display_name in companies:
            try:
                candidates = _fetch_candidates(slug, query)
            except Exception as exc:
                print(f"  icims: {slug}: list fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for job_id, href, list_title in candidates:
                info: dict = {}
                try:
                    detail_html = fetch_html(href)
                    info = _parse_detail(detail_html)
                except Exception as exc:
                    # A transient detail-fetch failure shouldn't drop the
                    # posting entirely — the list identity (href + title)
                    # alone is enough to store a minimally-useful row.
                    print(f"  icims: {slug}: detail fetch failed for {href} — {exc}", file=sys.stderr)
                try:
                    job = _to_job(job_id, href, list_title, info, slug, display_name)
                except Exception as exc:
                    print(f"  icims: {slug}: skipping {href} — {exc}", file=sys.stderr)
                    continue
                company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
