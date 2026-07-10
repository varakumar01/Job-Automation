"""Oracle Fusion Cloud Recruiting job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

List API: GET https://<host>/hcmRestApi/resources/latest/recruitingCEJobRequisitions
    ?onlyData=true&expand=requisitionList
    &finder=findReqs;siteNumber=<site>,keyword=<query>,limit=<n>,offset=<n>,sortBy=POSTING_DATES_DESC
— one call per configured tenant, returns
    {"items": [{..., "TotalJobsCount": N, "requisitionList": [...]}]}.
``expand=requisitionList`` is REQUIRED — without it the response only echoes
the search-criteria object (``SearchId``, ``TotalJobsCount``, ...) with no job
items at all (confirmed live: a bare call without it raises KeyError on
``requisitionList``).

Detail API: GET https://<host>/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
    ?onlyData=true&expand=all&finder=ById;Id="<jobId>",siteNumber=<site>
— a second call per matched posting, returns the full HTML JD
(``ExternalDescriptionStr``) + a fuller ISO timestamp (``ExternalPostedStartDate``).

Configure via ``ORACLEFUSION_COMPANIES`` in ``.env``. Oracle Fusion has no
single slug — each tenant's public careers site needs its own full hostname
(varies per tenant/region, e.g. ``jpmc.fa.oraclecloud.com``,
``fa-extu-saasfaprod1.fa.ocs.oraclecloud.com``) plus a per-tenant ``CX_<N>``
site number (read off the tenant's own careers URL,
``.../hcmUI/CandidateExperience/en/sites/CX_<N>/...``). Format is
``host:site`` or ``host:site:Display Name`` (see
``_ats_util.parse_oraclefusion_companies``):

    ORACLEFUSION_COMPANIES=jpmc.fa.oraclecloud.com:CX_1001:JPMorgan Chase,ebxr.fa.us2.oraclecloud.com:CX_1:DTCC

Field-shape facts (verified live 2026-07-06 against jpmc.fa.oraclecloud.com,
fa-extu-saasfaprod1.fa.ocs.oraclecloud.com (Akamai), ebxr.fa.us2.oraclecloud.com
(DTCC) — all bare 200s, no cookies/token/API key of any kind):
  - ``Id`` (string) is the job requisition id, globally unique per host —
    prefixed ``<host>:<id>`` for cross-tenant uniqueness, same convention as
    every other ATS plugin here.
  - ``PrimaryLocation`` is a pre-formatted "City, State, Country" string.
  - ``PostedDate`` on the LIST call is already ``YYYY-MM-DD`` — the DETAIL
    call's ``ExternalPostedStartDate`` (a fuller ISO timestamp) is preferred
    when the detail call succeeds, list value used as fallback.
  - ``ShortDescriptionStr`` (list) is a plain-text summary; ``ExternalDescriptionStr``
    (detail) is the full HTML JD — stripped via ``strip_html``; falls back to
    the list summary if the detail call fails.
  - The canonical public job page is
    ``https://<host>/hcmUI/CandidateExperience/en/sites/<site>/job/<Id>``.
  - Oracle's own docs label this REST resource "internal use only" even though
    every tenant's own public careers page calls it client-side with zero
    access control to render its own job list — same posture as any
    reverse-engineered ATS scraper here: works today, unsupported by Oracle,
    could change without notice.

Cost note: one extra detail call PER matched posting, same pattern as
Workday/SmartRecruiters/BambooHR.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``ORACLEFUSION_COMPANIES`` names at least one tenant.
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

from _ats_util import HEADERS, TIMEOUT, matches, parse_oraclefusion_companies, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_LIST_API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_DETAIL_API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
_JOB_URL = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
_ENV_VAR = "ORACLEFUSION_COMPANIES"
_PAGE_SIZE = 25
_MAX_PAGES = 4  # bounds list calls per tenant to <=100 postings scanned


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_company(host: str, site: str, query: str) -> list[dict]:
    """List postings for one tenant, paginating in _PAGE_SIZE chunks up to
    _MAX_PAGES, stopping early once TotalJobsCount is exhausted."""
    all_items: list[dict] = []
    total = None
    for page in range(_MAX_PAGES):
        offset = page * _PAGE_SIZE
        finder = f"findReqs;siteNumber={site},limit={_PAGE_SIZE},offset={offset},sortBy=POSTING_DATES_DESC"
        if query.strip():
            # commas/semicolons are the finder mini-language's own delimiters —
            # a query containing either would silently truncate/corrupt the
            # keyword param server-side, so scrub them before interpolating.
            safe_query = query.replace(",", " ").replace(";", " ")
            finder = (
                f"findReqs;siteNumber={site},keyword={safe_query},"
                f"limit={_PAGE_SIZE},offset={offset},sortBy=POSTING_DATES_DESC"
            )
        qs = urllib.parse.urlencode({"onlyData": "true", "expand": "requisitionList", "finder": finder})
        data = _get_json(f"{_LIST_API.format(host=host)}?{qs}")
        results = data.get("items") or []
        if not results:
            break
        block = results[0]
        if total is None:
            try:
                total = int(block.get("TotalJobsCount"))
            except (TypeError, ValueError):
                total = None
        postings = block.get("requisitionList")
        if not isinstance(postings, list) or not postings:
            break
        all_items.extend(postings)
        if isinstance(total, int) and len(all_items) >= total:
            break
        if len(postings) < _PAGE_SIZE:
            break  # last page
    return all_items


def _fetch_detail(host: str, site: str, job_id: str) -> dict:
    """Fetch one posting's full detail (JD text + a fuller posted timestamp)."""
    finder = f'ById;Id="{job_id}",siteNumber={site}'
    qs = urllib.parse.urlencode({"onlyData": "true", "expand": "all", "finder": finder})
    data = _get_json(f"{_DETAIL_API.format(host=host)}?{qs}")
    items = data.get("items") or []
    return items[0] if items else {}


def _to_job(item: dict, detail: dict | None, host: str, site: str, display_name: str) -> Job | None:
    job_id = item.get("Id")
    if not job_id:
        return None
    ext_id = f"{host}:{job_id}"  # ids are only unique per-host
    detail = detail or {}
    jd_text = strip_html(detail.get("ExternalDescriptionStr") or item.get("ShortDescriptionStr") or "") or None
    posted_at = detail.get("ExternalPostedStartDate") or item.get("PostedDate")
    return Job(
        source="oraclefusion",
        ext_id=ext_id,
        url=_JOB_URL.format(host=host, site=site, job_id=job_id),
        title=item.get("Title"),
        company=display_name,  # tenant hostname isn't necessarily the pretty company name
        location=item.get("PrimaryLocation"),
        posted_at=posted_at,
        jd_text=jd_text,
        extra=item,
    )


class OracleFusionPlugin(JobSourcePlugin):
    """Company career-site postings across every Oracle Fusion Cloud
    Recruiting tenant configured in ORACLEFUSION_COMPANIES."""

    name = "oraclefusion"
    base_url = "*.oraclecloud.com"
    mechanism = "json"

    def is_available(self) -> bool:
        return bool(parse_oraclefusion_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_oraclefusion_companies(_ENV_VAR)
        if not companies:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        # Collect each tenant's matches into its own list, then round-robin
        # merge — otherwise the first tenant alone can fill `limit` and every
        # other configured tenant is silently never represented.
        per_company: list[list[Job]] = []
        for host, site, display_name in companies:
            try:
                items = _fetch_company(host, site, query)
            except Exception as exc:
                print(f"  oraclefusion: {host}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    # `keyword` already filtered server-side; this is a safety
                    # net for an empty/loose query.
                    if not matches(item.get("Title", ""), words):
                        continue
                    detail = None
                    job_id = item.get("Id")
                    if job_id:
                        try:
                            detail = _fetch_detail(host, site, job_id)
                        except Exception as exc:
                            print(f"  oraclefusion: {host}: detail fetch failed for {job_id} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, host, site, display_name)
                except Exception as exc:
                    print(f"  oraclefusion: {host}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
