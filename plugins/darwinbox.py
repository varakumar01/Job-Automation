"""Darwinbox job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

Darwinbox is a common Indian HRMS/ATS platform. Its career-site SPA shell
(`https://<tenant>.darwinbox.{in,com}/ms/candidate(v2)?/careers/...`) is a
near-empty Angular shell with no embedded job data — the frontend itself
populates the job list with a SAME-ORIGIN client-side JSON API call, found
live 2026-07-07 by pulling the shell's own `main.<hash>.js` bundle and reading
its Angular environment config (`apiURL: "/ms/candidateapi/"`):

List API: GET https://<tenant>.darwinbox.{in,com}/ms/candidateapi/job?page=<n>
    — returns `{"status": "success", "message": {"jobscount": N, "jobs": [...]}}`.
Detail API: GET https://<tenant>.darwinbox.{in,com}/ms/candidateapi/job/<id>
    — returns `{"status": "success", "message": {"job": [{...full fields incl. "jd"...}]}}`.

Both confirmed live, no cookies/auth/API key of any kind required (the
response's `access-control-allow-origin: *` header confirms it's meant to be
called cross-origin/publicly) — verified against 6 independent tenants
(Seclore, Hetero, SpotDraft, Darwinbox's own dbox/dbx tenants, Blibli/Indonesia)
across both the `candidate` (v1) and `candidatev2` shell variants and both
`.in`/`.com` TLDs — same endpoint path works identically regardless.

Configure via `DARWINBOX_COMPANIES` in `.env`. A Darwinbox tenant is addressed
by the subdomain segment of its career-site URL (`<tenant>.darwinbox.in` or
`.com`) — this plugin tries `.in` first, falling back to `.com` (see
`_fetch_company`), so only the bare tenant slug is needed. Single-part
identifier like Greenhouse's slug, so this reuses `_ats_util.parse_companies`
(`tenant` or `tenant:Display Name`):

    DARWINBOX_COMPANIES=seclore:Seclore,hetero:Hetero

Field-shape facts (verified live 2026-07-07 against seclore.darwinbox.in):
  - `id` (string) is the job id, globally unique per tenant — prefixed
    `<tenant>:<id>` for cross-tenant uniqueness like every other ATS plugin
    here.
  - No direct public job-detail URL was found on the tenant's own career site
    (the SPA renders detail client-side, no visible `<a href>` slug pattern
    confirmed) — the tenant's own careers homepage
    (`https://<tenant>.darwinbox.in/ms/candidate/careers`, no per-job path
    segment) is used as `url` instead of a per-job URL, same "no better URL
    exists" fallback already accepted for other list-only fields elsewhere in
    this codebase.
  - `jd` (the detail call's HTML job description) is DOUBLE HTML-encoded —
    the string itself contains literal `&lt;div ...&gt;` sequences, i.e. real
    HTML markup that was itself entity-escaped before being embedded as a
    JSON string value. `strip_html` alone does NOT work here (it strips
    literal `<tag>` markup first and unescapes entities only as its last
    step, so a raw `&lt;div&gt;`-encoded string strips nothing and leaves the
    entities merely decoded, not the tags stripped) — this plugin calls
    `html.unescape()` once FIRST to reveal the real tags, then `strip_html`
    a second time to actually strip them (its own internal unescape pass is
    a harmless no-op on the now-clean text). This two-pass approach also
    degrades safely on single-encoded or plain-text JDs — an `html.unescape`
    on text with no entities is simply a no-op, so the same code path
    handles all three cases correctly.
  - `officelocation_show_arr` (list-call) / `officelocations_without_area`
    (detail-call, a list) both carry a human-readable location — the
    detail-call's list is preferred (joined with "; ") when present, since it
    resolves "Multiple locations" into the actual city names; falls back to
    the list call's own pre-formatted string.
  - `created_on` is a real ISO-8601 timestamp on BOTH the list and detail
    calls — the detail call's value is used when available (marginally more
    likely to be fresh), list value as fallback.
  - No `company` field on a posting — the configured display name is used
    as-is (like Lever/Ashby/BambooHR/Zoho Recruit/Rippling/Cutshort).

Cost note: one extra detail call PER matched posting, same pattern as every
other ATS plugin here (Oracle Fusion/SuccessFactors/Rippling/iCIMS/Cutshort/
Workday/SmartRecruiters/BambooHR).

No Apify dependency — stdlib `urllib`. `is_available` is True only when
`DARWINBOX_COMPANIES` names at least one tenant.
"""

from __future__ import annotations

import html
import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import HEADERS, TIMEOUT, matches, parse_companies, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API_BASE = "https://{host}/ms/candidateapi"
_CAREERS_URL = "https://{host}/ms/candidate/careers"
_ENV_VAR = "DARWINBOX_COMPANIES"
_TLDS = ("in", "com")  # tried in order per tenant — both seen live for real tenants
_MAX_PAGES = 4  # bounds list calls per tenant to <=100 postings scanned (25/page observed)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_host(tenant: str) -> tuple[str, list] | None:
    """A Darwinbox tenant lives on either the .in or .com TLD (both seen live
    for real tenants) — probe page 1 of the job list on each until one
    responds, so the configured identifier can stay a bare tenant slug rather
    than requiring the caller to know which TLD their company uses. Returns
    the resolved host PLUS the page-1 jobs already fetched during the probe,
    so `_fetch_company` doesn't re-request the exact same page-1 URL."""
    for tld in _TLDS:
        host = f"{tenant}.darwinbox.{tld}"
        try:
            data = _get_json(f"{_API_BASE.format(host=host)}/job?page=1")
        except Exception:
            continue
        message = data.get("message") if isinstance(data, dict) else None
        jobs = message.get("jobs") if isinstance(message, dict) else None
        if isinstance(data, dict) and data.get("status") == "success" and isinstance(jobs, list):
            return host, jobs
    return None


def _fetch_company(host: str, first_page: list) -> list[dict]:
    """List postings for one tenant, starting from the already-fetched page-1
    `first_page` (from `_resolve_host`'s probe) and paginating up to
    `_MAX_PAGES` further, stopping early once a page comes back empty. A
    failure on any page AFTER the first (network blip, timeout) stops
    pagination but still returns everything collected so far, rather than
    discarding already-good postings just because a later page hiccuped."""
    if not first_page:
        return []
    all_items: list[dict] = list(first_page)
    for page in range(2, _MAX_PAGES + 1):
        try:
            data = _get_json(f"{_API_BASE.format(host=host)}/job?page={page}")
        except Exception as exc:
            print(f"  darwinbox: {host}: page {page} fetch failed — {exc}", file=sys.stderr)
            break
        message = data.get("message") if isinstance(data, dict) else None
        jobs = message.get("jobs") if isinstance(message, dict) else None
        if not isinstance(jobs, list) or not jobs:
            break
        all_items.extend(jobs)
    return all_items


def _fetch_detail(host: str, job_id: str) -> dict:
    """Fetch one posting's full detail (JD text + a fuller location list)."""
    data = _get_json(f"{_API_BASE.format(host=host)}/job/{job_id}")
    message = data.get("message") if isinstance(data, dict) else None
    jobs = message.get("job") if isinstance(message, dict) else None
    if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict):
        return jobs[0]
    return {}


def _jd_text(detail: dict) -> str | None:
    raw = detail.get("jd")
    if not raw:
        return None
    # `jd` is HTML markup that was itself HTML-entity-escaped before being
    # embedded as a JSON string — unescape once to reveal real tags, THEN
    # strip_html (which strips tags first, unescapes second) actually strips
    # them; calling strip_html alone on the still-encoded string strips
    # nothing since its tag-regex never matches `&lt;div&gt;` text.
    return strip_html(html.unescape(raw)) or None


def _location_str(detail: dict, item: dict) -> str | None:
    locs = detail.get("officelocations_without_area")
    if isinstance(locs, list) and locs:
        names = [str(loc).strip() for loc in locs if loc and str(loc).strip()]
        if names:
            return "; ".join(names)
    return item.get("officelocation_show_arr") or None


def _to_job(item: dict, detail: dict | None, host: str, tenant: str, display_name: str) -> Job | None:
    job_id = item.get("id")
    if not job_id:
        return None
    ext_id = f"{tenant}:{job_id}"
    detail = detail or {}
    return Job(
        source="darwinbox",
        ext_id=ext_id,
        url=_CAREERS_URL.format(host=host),  # no per-job public URL found; tenant careers homepage is the best available
        title=item.get("title"),
        company=display_name,  # Darwinbox postings carry no company field
        location=_location_str(detail, item),
        posted_at=detail.get("created_on") or item.get("created_on"),
        jd_text=_jd_text(detail),
        extra=item,
    )


class DarwinboxPlugin(JobSourcePlugin):
    """Company career-site postings across every Darwinbox tenant configured
    in DARWINBOX_COMPANIES."""

    name = "darwinbox"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        # Collect each tenant's matches into its own list, then round-robin
        # merge — otherwise the first tenant alone can fill `limit` and every
        # other configured tenant is silently never represented.
        per_company: list[list[Job]] = []
        for tenant, display_name in companies:
            resolved = _resolve_host(tenant)
            if not resolved:
                print(f"  darwinbox: {tenant}: no reachable .in/.com tenant host found", file=sys.stderr)
                continue
            host, first_page = resolved
            try:
                items = _fetch_company(host, first_page)
            except Exception as exc:
                print(f"  darwinbox: {tenant}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    if not matches(item.get("title", ""), words):
                        continue
                    detail = None
                    job_id = item.get("id")
                    if job_id:
                        try:
                            detail = _fetch_detail(host, job_id)
                        except Exception as exc:
                            print(f"  darwinbox: {tenant}: detail fetch failed for {job_id} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, host, tenant, display_name)
                except Exception as exc:
                    print(f"  darwinbox: {tenant}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
