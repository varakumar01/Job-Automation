"""Rippling ATS job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

List API: GET https://ats.rippling.com/api/v2/board/<slug>/jobs
    ?page=<n>&pageSize=<n>&searchQuery=<query>
— one call per configured company, returns
    {"items": [...], "page": n, "pageSize": n, "totalItems": N, "totalPages": N}.
Detail API: GET https://ats.rippling.com/api/v2/board/<slug>/jobs/<uuid>
— a second call per title-matched posting, returns the full HTML description
(split into `description.company` / `description.role`) + a real ISO
`createdOn` timestamp.

Configure via `RIPPLING_COMPANIES` in `.env`. A Rippling ATS company is
addressed by a single board slug (visible directly in its careers URL,
`ats.rippling.com/<slug>/jobs`) — same shape as Greenhouse, so this reuses
`_ats_util.parse_companies` (`slug` or `slug:Display Name`):

    RIPPLING_COMPANIES=chess:Chess.com,boom-supersonic:Boom Supersonic

Field-shape facts (verified live 2026-07-06 against chess, boom-supersonic,
anaconda, d-wave-quantum — all bare 200s, no cookies/auth/API key of any kind;
an unknown/mistyped slug cleanly 404s with `{"error_code":
"RESOURCE_NOT_FOUND", ...}` rather than a confusing partial response):
  - `id` (a UUID string) is the job id, globally unique per board — prefixed
    `<slug>:<id>` for cross-company uniqueness like every other ATS plugin here.
  - `name` is the job title; the list item's own `url` field is already the
    canonical absolute public job page (no URL construction needed).
  - `locations` is a LIST of location objects (a posting can be multi-site) —
    each has `name` (pre-formatted human string) plus separate
    `country`/`state`/`city`/`workplaceType` fields; every location's `name`
    is joined with `"; "` since a single posting can legitimately span
    several offices.
  - No `company` field on a posting — the configured display name is used
    as-is (like Lever/Ashby/BambooHR/Zoho Recruit).
  - `searchQuery` filters SERVER-SIDE — the local keyword `matches()` check
    still runs as a safety net for a query Rippling's search didn't narrow,
    and to accept everything on an empty query.
  - The DETAIL call's `description.company` (company blurb) and
    `description.role` (the actual job description) are separate HTML
    strings — both stripped and joined for `jd_text` (skips whichever half
    is empty rather than assuming both are always present).
  - The DETAIL call's `createdOn` is a real ISO-8601 timestamp — preferred
    over any list-level date (the list response carries no date field at all).

Cost note: one extra detail call PER title-matched posting, same pattern as
Workday/Oracle Fusion/SmartRecruiters/BambooHR.

No Apify dependency — stdlib `urllib`. `is_available` is True only when
`RIPPLING_COMPANIES` names at least one company.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import HEADERS, TIMEOUT, matches, parse_companies, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_LIST_API = "https://ats.rippling.com/api/v2/board/{slug}/jobs"
_DETAIL_API = "https://ats.rippling.com/api/v2/board/{slug}/jobs/{job_id}"
_ENV_VAR = "RIPPLING_COMPANIES"
_PAGE_SIZE = 25
_MAX_PAGES = 4  # bounds list calls per company to <=100 postings scanned


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_company(slug: str, query: str) -> list[dict]:
    """List postings for one company, paginating in _PAGE_SIZE chunks up to
    _MAX_PAGES, stopping early once totalPages/totalItems is exhausted."""
    all_items: list[dict] = []
    total_pages = None
    for page in range(_MAX_PAGES):
        params = {"page": page, "pageSize": _PAGE_SIZE}
        if query.strip():
            params["searchQuery"] = query.strip()
        qs = urllib.parse.urlencode(params)
        data = _get_json(f"{_LIST_API.format(slug=slug)}?{qs}")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            break
        all_items.extend(items)
        if total_pages is None:
            try:
                total_pages = int(data.get("totalPages"))
            except (TypeError, ValueError):
                total_pages = None
        if isinstance(total_pages, int) and page + 1 >= total_pages:
            break
        if len(items) < _PAGE_SIZE:
            break  # last page
    return all_items


def _fetch_detail(slug: str, job_id: str) -> dict:
    """Fetch one posting's full detail (JD text + a real posted timestamp)."""
    return _get_json(_DETAIL_API.format(slug=slug, job_id=job_id))


def _location_str(item: dict) -> str | None:
    locations = item.get("locations")
    if not isinstance(locations, list):
        return None
    names = [loc.get("name") for loc in locations if isinstance(loc, dict) and loc.get("name")]
    return "; ".join(names) or None


def _jd_text(detail: dict) -> str | None:
    description = detail.get("description")
    if not isinstance(description, dict):
        description = {}
    parts = [description.get("company") or "", description.get("role") or ""]
    joined = strip_html(" ".join(p for p in parts if p))
    return joined or None


def _to_job(item: dict, detail: dict | None, slug: str, display_name: str) -> Job | None:
    job_id = item.get("id")
    if not job_id:
        return None
    ext_id = f"{slug}:{job_id}"  # ids are only unique per-company board
    detail = detail or {}
    return Job(
        source="rippling",
        ext_id=ext_id,
        url=item.get("url"),
        title=item.get("name"),
        company=display_name,  # Rippling postings carry no company field
        location=_location_str(item),
        posted_at=detail.get("createdOn"),
        jd_text=_jd_text(detail),
        extra=item,
    )


class RipplingPlugin(JobSourcePlugin):
    """Company career-site postings across every Rippling ATS board
    configured in RIPPLING_COMPANIES."""

    name = "rippling"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        # Collect each company's matches into its own list, then round-robin
        # merge — otherwise the first company alone can fill `limit` and
        # every other configured company is silently never represented.
        per_company: list[list[Job]] = []
        for slug, display_name in companies:
            try:
                items = _fetch_company(slug, query)
            except Exception as exc:
                print(f"  rippling: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    # `searchQuery` already filtered server-side; this is a
                    # safety net for an empty/loose query.
                    if not matches(item.get("name", ""), words):
                        continue
                    detail = None
                    job_id = item.get("id")
                    if job_id:
                        try:
                            detail = _fetch_detail(slug, job_id)
                        except Exception as exc:
                            print(f"  rippling: {slug}: detail fetch failed for {job_id} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, slug, display_name)
                except Exception as exc:
                    print(f"  rippling: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
