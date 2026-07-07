"""BambooHR job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

List API: GET https://<co>.bamboohr.com/careers/list — one call per
configured company, returns ``{"result": [...]}`` with NO description text.
Detail API: GET https://<co>.bamboohr.com/careers/<id>/detail — a second call
per title-matched posting, returns the full description + share URL.

Configure via ``BAMBOOHR_COMPANIES`` in ``.env`` (comma-separated
subdomains; see ``_ats_util.parse_companies`` for the ``slug:Display Name``
override syntax — useful here since BambooHR postings carry NO company-name
field):

    BAMBOOHR_COMPANIES=gitkraken:GitKraken

Field-shape facts (verified live 2026-07-05, company ``gitkraken``):
  - Must send ``Accept: application/json`` (part of the shared ``HEADERS``)
    or BambooHR redirects to the marketing site instead of returning JSON;
    some subdomains still 403 behind Cloudflare bot-protection even with the
    right header — treat that per-company as a fetch failure, not fatal to
    the plugin.
  - Neither list nor detail exposes a company-name field — the configured
    display name is used as-is.
  - No posting-date field exists anywhere in the payload — ``posted_at`` is
    always ``None`` for this platform.
  - Detail's ``jobOpeningShareUrl`` is the public job-detail page — use that
    for ``Job.url``.
  - Detail's ``description`` is HTML — needs ``strip_html``.
  - ``location`` is a dict (``city``/``state``[/``postalCode``/
    ``addressCountry`` on detail]); composed to a single string.

Cost note: like SmartRecruiters, this makes one extra detail call PER
TITLE-MATCHED posting (list has no description to prefilter on more richly).

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``BAMBOOHR_COMPANIES`` names at least one company.
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

_LIST_API = "https://{co}.bamboohr.com/careers/list"
_DETAIL_API = "https://{co}.bamboohr.com/careers/{id}/detail"
_ENV_VAR = "BAMBOOHR_COMPANIES"
_MAX_DETAIL_CALLS = 100  # bounds per-company detail fetches under a broad query on a large board


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_company(slug: str) -> list[dict]:
    """List openings for one company (no description text included)."""
    data = _get_json(_LIST_API.format(co=slug))
    result = data.get("result")
    return result if isinstance(result, list) else []


def _fetch_detail(slug: str, opening_id: str) -> dict:
    """Fetch one opening's full detail (description + share URL)."""
    data = _get_json(_DETAIL_API.format(co=slug, id=opening_id))
    return (data.get("result") or {}).get("jobOpening") or {}


def _location_str(loc: dict | None) -> str | None:
    if not loc:
        return None
    parts = [loc.get("city"), loc.get("state"), loc.get("addressCountry")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _to_job(item: dict, detail: dict | None, slug: str, display_name: str) -> Job | None:
    raw_id = item.get("id")
    if raw_id is None:
        return None
    ext_id = f"{slug}:{raw_id}"  # prefix: ids are only unique per-company
    detail = detail or {}
    location = _location_str(detail.get("location")) or _location_str(item.get("location"))
    jd_text = strip_html(detail.get("description") or "") or None
    return Job(
        source="bamboohr",
        ext_id=ext_id,
        url=detail.get("jobOpeningShareUrl"),
        title=item.get("jobOpeningName"),
        company=display_name,  # BambooHR postings carry no company field
        location=location,
        posted_at=None,  # no date field anywhere in this platform's payload
        jd_text=jd_text,
        extra=item,
    )


class BambooHRPlugin(JobSourcePlugin):
    """Company career-site postings across every BambooHR-hosted company
    configured in BAMBOOHR_COMPANIES."""

    name = "bamboohr"

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
                items = _fetch_company(slug)
            except Exception as exc:
                print(f"  bamboohr: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            detail_calls = 0
            for item in items:
                try:
                    # Title-only prefilter (list has no description text) —
                    # only pay for a detail call on postings that pass it.
                    if not matches(item.get("jobOpeningName", ""), words):
                        continue
                    detail = None
                    raw_id = item.get("id")
                    if raw_id is not None and detail_calls < _MAX_DETAIL_CALLS:
                        try:
                            detail = _fetch_detail(slug, str(raw_id))
                            detail_calls += 1
                        except Exception as exc:
                            print(f"  bamboohr: {slug}: detail fetch failed for {raw_id} — {exc}", file=sys.stderr)
                    job = _to_job(item, detail, slug, display_name)
                except Exception as exc:
                    print(f"  bamboohr: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
