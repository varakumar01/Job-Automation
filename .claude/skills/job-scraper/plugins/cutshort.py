"""Cutshort job-portal plugin (public Next.js embedded JSON + JSON-LD detail
— no token required). PLAN.md §4/§10.

Cutshort (cutshort.io) is an Indian tech-hiring platform hosting many
companies' job listings under `/company/<alias>`. A shallower research pass
once found `dehydratedState: null` on a company page and assumed the job list
was client-fetched only — a deeper live pass (2026-07-07) against 3 real
company pages found the FULL job list genuinely server-rendered into
`__NEXT_DATA__` (that earlier null was likely a cache-miss/edge-case on a
different company, not a platform-wide fact):

1. List: GET https://cutshort.io/company/<alias> — a plain, unauthenticated
   Next.js page (confirmed live against Appknox and others) whose
   `__NEXT_DATA__.props.pageProps.dehydratedState.queries` includes one entry
   keyed `["companyPageData", <alias>]` with
   `state.data.data.pageData.companyJobs.{jobs, page, totalPages}` — each job
   object carries `_id`, `headline`, `allSkills`, `locationsText`,
   `sanitizedComment` (HTML job blurb), and its own canonical `publicUrl`.
   Used via `_career_util.extract_next_data`, same tier-2 pattern as any
   other Next.js-based bespoke site.
2. Detail: GET the job's own `publicUrl` — embeds one
   `<script type="application/ld+json">` `JobPosting` block with the
   canonical `title`/`description`/`datePosted` (richer + more current than
   the company-page listing's own blurb) via `_career_util.extract_ld_json`.

Configure via `CUTSHORT_COMPANIES` in `.env`. A Cutshort company is addressed
by the FULL alias segment of its `/company/<alias>` URL — an opaque,
company-specific string (often containing parentheses and a random suffix,
e.g. `appknox-(xysec-labs-pte-ltd)-j2I4OU56`) that must be copied verbatim
from visiting the company's own Cutshort page; there is no shorter/guessable
form. Single-part identifier like Greenhouse's slug, so this reuses
`_ats_util.parse_companies` (`alias` or `alias:Display Name`):

    CUTSHORT_COMPANIES=appknox-(xysec-labs-pte-ltd)-j2I4OU56:Appknox

Field-shape facts (verified live 2026-07-07 against Appknox's Cutshort page —
bare 200, no cookies/auth of any kind):
  - `_id` is the job's Cutshort-internal id, globally unique per company —
    prefixed `<alias>:<id>` for cross-company uniqueness like every other ATS
    plugin here.
  - `publicUrl` is already the canonical absolute public job page — no URL
    construction needed.
  - `locationsText` on the LIST call is already a clean pre-formatted human
    string (e.g. "Bengaluru (Bangalore)") — used as-is rather than parsing
    the detail page's JSON-LD `jobLocation` address object, since it's
    already exactly what's wanted and avoids duplicating iCIMS's more
    involved address-object parsing for no benefit.
  - The DETAIL call's `datePosted` is the only reliable posted-date signal —
    the list call's own `hiringIntentShownOn` field name is ambiguous (could
    mean "created" or "last renewed for visibility") so it is NOT used as a
    posted_at proxy; `validThrough` (an application deadline, not a posting
    date) is likewise never used as a posted_at fallback, same reasoning as
    `icims.py`.
  - The DETAIL call's `description` (richer, current) is preferred for
    `jd_text`; the list call's own `sanitizedComment` (HTML blurb) is kept
    ONLY as a fallback if the detail fetch fails.
  - The company alias can contain parentheses/other punctuation — URL-quoted
    with `safe="()"` when building request URLs (parens work unencoded in
    practice too, but quoting is defensive against other punctuation in a
    future alias).
  - Pagination (`companyJobs.page`/`totalPages`) exists but the exact
    triggering query param was never empirically confirmed during research —
    `_fetch_candidates` tries `?page=<n>` defensively and stops as soon as a
    "next" page returns only already-seen job ids (server silently ignoring
    an unsupported param), so an unconfirmed pagination scheme degrades to
    "just use page 1" rather than looping or duplicating.

Cost note: one extra detail call PER slug-matched posting, same pattern as
Oracle Fusion/SuccessFactors/Rippling/iCIMS/Workday/SmartRecruiters/BambooHR.

No Apify dependency — stdlib `urllib`. `is_available` is True only when
`CUTSHORT_COMPANIES` names at least one company.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import matches, parse_companies, round_robin, strip_html  # noqa: E402
from _career_util import extract_ld_json, extract_next_data, fetch_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_COMPANY_URL = "https://cutshort.io/company/{alias}"
_ENV_VAR = "CUTSHORT_COMPANIES"
_MAX_PAGES = 3  # bounds list calls per company
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per company per call


def _company_url(alias: str, page: int) -> str:
    base = _COMPANY_URL.format(alias=quote(alias, safe="()"))
    return base if page == 1 else f"{base}?page={page}"


def _find_query(queries: list, key_name: str, alias: str) -> dict | None:
    for q in queries:
        key = q.get("queryKey") if isinstance(q, dict) else None
        if isinstance(key, list) and key and key[0] == key_name:
            # a page's dehydrated state can carry more than one query (e.g. a
            # "similar companies" sidebar) — match the alias too, when present,
            # so a differently-keyed companyPageData entry is never picked up
            if len(key) < 2 or key[1] == alias:
                return q
    return None


def _dig(obj: object, *keys: str) -> dict | None:
    """Nested `.get()` traversal that stops at the first `None`/non-dict
    value instead of crashing — plain `.get(key, {})` chaining only falls
    back to `{}` when a key is ABSENT, not when it's present and `None` (the
    Python form of JSON `null`); a documented live response with
    `dehydratedState: null` would otherwise raise `AttributeError` on the
    next `.get()` in the chain."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj if isinstance(obj, dict) else None


def _fetch_candidates(alias: str, query: str) -> list[dict]:
    """List candidate jobs for one company across up to `_MAX_PAGES` of the
    embedded `companyJobs.jobs` array, pre-filtered by a cheap headline+skills
    match before any detail fetch. Stops early once a "page" returns only
    already-seen job ids (the pagination param may be a no-op — this is the
    graceful-degradation signal for that), once `totalPages` is exhausted, or
    once `_MAX_DETAIL_FETCHES` matched candidates have been collected."""
    words = [w.lower() for w in query.split() if len(w) > 1]
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        page_html = fetch_html(_company_url(alias, page))
        next_data = extract_next_data(page_html)
        if not isinstance(next_data, dict):
            break
        dehydrated_state = _dig(next_data, "props", "pageProps", "dehydratedState")
        queries_list = dehydrated_state.get("queries") if dehydrated_state else None
        if not isinstance(queries_list, list):
            break
        q = _find_query(queries_list, "companyPageData", alias)
        if not q:
            break
        page_data = _dig(q, "state", "data", "data", "pageData")
        company_jobs = page_data.get("companyJobs") if page_data else None
        jobs = company_jobs.get("jobs") if isinstance(company_jobs, dict) else None
        if not isinstance(jobs, list) or not jobs:
            break

        new_count = 0
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = job.get("_id")
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            new_count += 1
            headline = job.get("headline") or ""
            skills = job.get("allSkills") if isinstance(job.get("allSkills"), list) else []
            searchable = " ".join([headline] + [s for s in skills if isinstance(s, str)])
            if not matches(searchable, words):
                continue
            candidates.append(
                {
                    "job_id": job_id,
                    "url": job.get("publicUrl"),
                    "headline": headline,
                    "location": job.get("locationsText"),
                    "jd_fallback": job.get("sanitizedComment"),
                }
            )
            if len(candidates) >= _MAX_DETAIL_FETCHES:
                return candidates

        if new_count == 0:
            break  # pagination param had no effect — same jobs as a prior page
        total_pages = company_jobs.get("totalPages")
        if isinstance(total_pages, int) and page >= total_pages:
            break
    return candidates


def _fetch_detail(url: str) -> dict:
    """Fetch one posting's detail page for the canonical title/description/
    posted-date via its embedded JSON-LD JobPosting block."""
    detail_html = fetch_html(url)
    postings = extract_ld_json(detail_html, ld_type="JobPosting")
    data = postings[0] if postings and isinstance(postings[0], dict) else {}
    return {
        "title": strip_html(data.get("title") or "") or None,
        "jd_text": strip_html(data.get("description") or "") or None,
        "posted_at": data.get("datePosted"),
    }


def _to_job(candidate: dict, detail: dict, alias: str, display_name: str) -> Job | None:
    job_id = candidate.get("job_id")
    url = candidate.get("url")
    if not job_id or not url:
        return None
    ext_id = f"{alias}:{job_id}"
    jd_text = detail.get("jd_text") or (strip_html(candidate.get("jd_fallback") or "") or None)
    return Job(
        source="cutshort",
        ext_id=ext_id,
        url=url,
        title=detail.get("title") or candidate.get("headline") or None,
        company=display_name,  # Cutshort postings' own company name can lag the configured display name
        location=candidate.get("location"),
        posted_at=detail.get("posted_at"),
        jd_text=jd_text,
        extra=candidate,
    )


class CutshortPlugin(JobSourcePlugin):
    """Company career-page postings across every Cutshort company configured
    in CUTSHORT_COMPANIES."""

    name = "cutshort"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []

        # Collect each company's matches into its own list, then round-robin
        # merge — otherwise the first company alone can fill `limit` and
        # every other configured company is silently never represented.
        per_company: list[list[Job]] = []
        for alias, display_name in companies:
            try:
                candidates = _fetch_candidates(alias, query)
            except Exception as exc:
                print(f"  cutshort: {alias}: list fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for candidate in candidates:
                detail: dict = {}
                url = candidate.get("url")
                try:
                    if url:
                        detail = _fetch_detail(url)
                except Exception as exc:
                    # A transient detail-fetch failure shouldn't drop the
                    # posting entirely — the list identity (url + headline)
                    # alone is enough to store a minimally-useful row.
                    print(f"  cutshort: {alias}: detail fetch failed for {url} — {exc}", file=sys.stderr)
                try:
                    job = _to_job(candidate, detail, alias, display_name)
                except Exception as exc:
                    print(f"  cutshort: {alias}: skipping {url} — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
