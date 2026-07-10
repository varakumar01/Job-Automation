"""Workday (CxS) job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

List API: POST https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
with body ``{"limit": n, "offset": 0, "searchText": "<query>"}`` — one call
per configured company, returns ``{"total": n, "jobPostings": [...]}``.
Detail API: GET https://<tenant>.wd<N>.myworkdayjobs.com/wday/cxs/<tenant>/<site><externalPath>
(``externalPath`` from the list item already starts with ``/job/...``, so it
is appended directly with NO extra ``/job`` literal — doing so double-prefixes
the path and 404s every detail call) — a second call per title-matched
posting, returns full JD text + real dates.

Configure via ``WORKDAY_COMPANIES`` in ``.env``. Workday has no single slug —
each tenant's career site needs THREE parts: the tenant name, the numbered
CxS shard (``wd1``-``wd5``, varies per tenant, found by trial or by reading
the careers-page network request), and a per-tenant ``site`` path segment.
Format is ``tenant:wdN:site`` or ``tenant:wdN:site:Display Name`` (see
``_ats_util.parse_workday_companies``):

    WORKDAY_COMPANIES=workday:5:Workday:Workday,cisco:1:External:Cisco

Field-shape facts (verified live 2026-07-05, tenant ``workday``, wd5, site
``Workday``):
  - ``searchText`` filters SERVER-SIDE (unlike the other ATS plugins here) —
    the local keyword ``matches()`` check still runs as a safety net for a
    query Workday's search didn't narrow, and to allow an empty query to
    accept everything.
  - No ``company`` field — the configured display name is used as-is.
  - List's ``postedOn`` is a relative string (``"Posted 2 Days Ago"``), NOT
    parseable to a real date — the detail call's ``startDate`` (an actual
    ISO date, e.g. ``"2026-07-03"``) is used instead.
  - Detail's ``externalUrl`` is the canonical public job-detail page — used
    for ``Job.url`` (falls back to a constructed URL if the detail call
    fails).
  - Detail's ``jobDescription`` is HTML — needs ``strip_html``.
  - No numeric "id" exists in the list item; ``externalPath`` itself (unique
    per tenant) is the natural identifier.

Cost note: highest-effort of the ATS plugins — one extra detail call PER
TITLE-MATCHED posting, same pattern as SmartRecruiters/BambooHR.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``WORKDAY_COMPANIES`` names at least one company.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import HEADERS, TIMEOUT, matches, parse_workday_companies, post_json, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_LIST_API = "https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
_DETAIL_API = "https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
_ENV_VAR = "WORKDAY_COMPANIES"
_PAGE_SIZE = 20  # server-enforced max (verified live 2026-07-05: 21+ -> HTTP 400 on this tenant)
_MAX_PAGES = 5  # bounds list calls per company to <=100 postings scanned
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per company per call — this
# plugin was missing the cap every other multi-fetch plugin here has (successfactors.py,
# remote100k.py's detail cap, etc.): a title-matched company with many postings did a FULL,
# uncapped detail fetch (one HTTP round trip each) for every single match, found live
# 2026-07-10 when a 39-tenant WORKDAY_COMPANIES run under the new parallel scraper took
# minutes longer than every other plugin — a broad query against a company with dozens of
# matches could do 80-100 sequential detail calls, each up to TIMEOUT seconds.


def _fetch_company(tenant: str, wd_num: int, site: str, query: str) -> list[dict]:
    """List postings for one Workday tenant (no description text included),
    paginating in _PAGE_SIZE chunks up to _MAX_PAGES."""
    url = _LIST_API.format(tenant=tenant, n=wd_num, site=site)
    all_postings: list[dict] = []
    for page in range(_MAX_PAGES):
        data = post_json(url, {"limit": _PAGE_SIZE, "offset": page * _PAGE_SIZE, "searchText": query})
        postings = data.get("jobPostings")
        if not isinstance(postings, list) or not postings:
            break
        all_postings.extend(postings)
        if len(postings) < _PAGE_SIZE:
            break  # last page
    return all_postings


def _fetch_detail(tenant: str, wd_num: int, site: str, external_path: str) -> dict:
    """Fetch one posting's full detail (JD text + real dates + canonical URL)."""
    url = _DETAIL_API.format(tenant=tenant, n=wd_num, site=site, path=external_path)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("jobPostingInfo") or {}


def _to_job(item: dict, detail: dict | None, tenant: str, wd_num: int, site: str, display_name: str) -> Job | None:
    external_path = item.get("externalPath")
    if not external_path:
        return None
    ext_id = f"{tenant}:{site}:{external_path}"  # unique within this tenant+site
    detail = detail or {}
    url = detail.get("externalUrl") or f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/{site}{external_path}"
    jd_text = strip_html(detail.get("jobDescription") or "") or None
    return Job(
        source="workday",
        ext_id=ext_id,
        url=url,
        title=item.get("title"),
        company=display_name,  # Workday tenant name isn't necessarily the pretty company name
        location=item.get("locationsText"),
        posted_at=detail.get("startDate"),  # real ISO date; list's postedOn is relative text
        jd_text=jd_text,
        extra=item,
    )


class WorkdayPlugin(JobSourcePlugin):
    """Company career-site postings across every Workday-hosted tenant
    configured in WORKDAY_COMPANIES."""

    name = "workday"
    base_url = "*.myworkdayjobs.com"
    mechanism = "json"

    def is_available(self) -> bool:
        return bool(parse_workday_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_workday_companies(_ENV_VAR)
        if not companies:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        # Collect each company's matches into its own list, then round-robin
        # merge — otherwise the first company alone can fill `limit` and
        # every other configured company is silently never represented.
        per_company: list[list[Job]] = []
        for tenant, wd_num, site, display_name in companies:
            try:
                items = _fetch_company(tenant, wd_num, site, query)
            except Exception as exc:
                print(f"  workday: {tenant}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            detail_fetches = 0
            for item in items:
                try:
                    # searchText already filtered server-side; this is a
                    # safety net for an empty/loose query.
                    if not matches(item.get("title", ""), words):
                        continue
                    detail = None
                    external_path = item.get("externalPath")
                    if external_path and detail_fetches < _MAX_DETAIL_FETCHES:
                        detail_fetches += 1
                        try:
                            detail = _fetch_detail(tenant, wd_num, site, external_path)
                        except Exception as exc:
                            print(f"  workday: {tenant}: detail fetch failed for {external_path} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, tenant, wd_num, site, display_name)
                except Exception as exc:
                    print(f"  workday: {tenant}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
