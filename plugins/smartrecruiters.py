"""SmartRecruiters job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

List API: GET https://api.smartrecruiters.com/v1/companies/<co>/postings —
one call per configured company, returns ``{"content": [...]}``. The list
payload has NO description text, so a second call per matched posting fetches
the full detail: GET https://api.smartrecruiters.com/v1/companies/<co>/postings/<id>.

Configure via ``SMARTRECRUITERS_COMPANIES`` in ``.env`` (comma-separated
company identifiers — SmartRecruiters slugs are usually the CamelCase company
name, e.g. ``Visa`` not ``visa``; see ``_ats_util.parse_companies`` for the
``slug:Display Name`` override syntax):

    SMARTRECRUITERS_COMPANIES=Visa,CheckPointSoftware:Check Point

Field-shape facts (verified live 2026-07-05, company ``Visa``):
  - ``company.name`` IS present on both list and detail items — the
    configured display name is only a fallback.
  - The list item's ``id`` is what the detail endpoint takes as ``<id>``.
  - The detail item's ``postingUrl`` is the public job-detail page — use
    that for ``Job.url``, NOT ``applyUrl`` (adds ``?oga=true`` tracking).
  - ``releasedDate`` is already ISO 8601 — no conversion needed.
  - Full JD text lives in ``jobAd.sections`` on the DETAIL item only (a dict
    of ``{sectionKey: {"title": ..., "text": ...}}``, e.g.
    ``companyDescription``/``jobDescription``/``qualifications``/
    ``additionalInformation``); each ``text`` is HTML needing ``strip_html``.

Cost note: unlike Greenhouse/Lever/Ashby (single call), this plugin makes one
extra detail call PER TITLE-MATCHED posting (not per posting in the company),
to avoid an unbounded number of detail calls on a large careers page.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``SMARTRECRUITERS_COMPANIES`` names at least one company.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import HEADERS, TIMEOUT, matches, parse_companies, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_LIST_API = "https://api.smartrecruiters.com/v1/companies/{co}/postings?limit=100"
_DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{co}/postings/{id}"
_ENV_VAR = "SMARTRECRUITERS_COMPANIES"
_MAX_DETAIL_CALLS = 100  # bounds per-company detail fetches under a broad query on a large board


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_company(slug: str) -> list[dict]:
    """List postings for one company (no description text included)."""
    data = _get_json(_LIST_API.format(co=slug))
    content = data.get("content")
    return content if isinstance(content, list) else []


def _fetch_detail(slug: str, posting_id: str) -> dict:
    """Fetch one posting's full detail (JD sections + canonical URL)."""
    return _get_json(_DETAIL_API.format(co=slug, id=posting_id))


def _jd_text(detail: dict) -> str:
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    parts = [strip_html(sec.get("text") or "") for sec in sections.values() if isinstance(sec, dict)]
    return " ".join(p for p in parts if p)


def _to_job(item: dict, detail: dict | None, slug: str, fallback_name: str) -> Job | None:
    raw_id = item.get("id")
    if raw_id is None:
        return None
    ext_id = f"{slug}:{raw_id}"  # prefix: ids are only unique per-company
    company = (item.get("company") or {}).get("name") or fallback_name
    location = (item.get("location") or {}).get("fullLocation")
    url = (detail or {}).get("postingUrl")  # detail-page link, NOT applyUrl (PLAN §10)
    jd_text = _jd_text(detail or {}) or None
    return Job(
        source="smartrecruiters",
        ext_id=ext_id,
        url=url,
        title=item.get("name"),
        company=company,
        location=location,
        posted_at=item.get("releasedDate"),  # already ISO 8601
        jd_text=jd_text,
        extra=item,
    )


class SmartRecruitersPlugin(JobSourcePlugin):
    """Company career-site postings across every SmartRecruiters-hosted
    company configured in SMARTRECRUITERS_COMPANIES."""

    name = "smartrecruiters"
    base_url = "api.smartrecruiters.com"
    mechanism = "json"

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
        for slug, fallback_name in companies:
            try:
                items = _fetch_company(slug)
            except Exception as exc:
                print(f"  smartrecruiters: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            detail_calls = 0
            for item in items:
                try:
                    # Title-only prefilter (list has no description text) —
                    # only pay for a detail call on postings that pass it.
                    if not matches(item.get("name", ""), words):
                        continue
                    detail = None
                    raw_id = item.get("id")
                    if raw_id is not None and detail_calls < _MAX_DETAIL_CALLS:
                        try:
                            detail = _fetch_detail(slug, str(raw_id))
                            detail_calls += 1
                        except Exception as exc:
                            print(f"  smartrecruiters: {slug}: detail fetch failed for {raw_id} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, slug, fallback_name)
                except Exception as exc:
                    print(f"  smartrecruiters: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
