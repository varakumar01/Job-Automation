"""Workable job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: POST https://apply.workable.com/api/v3/accounts/<co>/jobs with body
``{"query": "<query>"}`` — one call per configured company, returns
``{"total": n, "results": [...]}``.

Configure via ``WORKABLE_COMPANIES`` in ``.env`` (comma-separated account
slugs; see ``_ats_util.parse_companies`` for the ``slug:Display Name``
override syntax):

    WORKABLE_COMPANIES=dispel:Dispel

Field-shape facts (verified live 2026-07-05 against a real populated
account, ``dispel``):
  - The request body must be ``{"query": "..."}`` ONLY — an added
    ``"limit"`` key makes the API respond ``{"limit":"Not allowed"}``.
  - The list item has NO ``company``/``company_name`` field — the
    configured display name is used as-is (like Lever/Ashby/BambooHR).
  - The list item has NO description/JD text field at all, and no working
    JSON detail endpoint was found (``/api/v3/accounts/<co>/jobs/<shortcode>``
    and the older ``/api/v1/widget/...`` path both 404). ``jd_text`` is
    therefore always ``None`` for this platform — same precedent as
    BambooHR's always-``None`` ``posted_at``.
  - The list item has NO ``url`` field either; the canonical public job page
    is constructed as ``https://apply.workable.com/<co>/j/<shortcode>/``
    (verified live: returns 200).
  - The unique id is ``shortcode`` (e.g. ``"0008AC2441"``); a numeric ``id``
    is also present but ``shortcode`` is what the public URL uses.
  - ``published`` is the posting date, ISO 8601 with a ``Z`` suffix (e.g.
    ``"2026-05-28T00:00:00.000Z"``) — used as-is, no conversion needed.
  - ``department`` is a LIST of strings (e.g. ``["Engineering"]``), not a
    plain string — joined before use in the keyword-match blob.
  - ``location`` is a dict (``country``/``countryCode``/``city``/``region``);
    ``city`` is frequently an empty string for remote roles — composed,
    skipping falsy parts.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``WORKABLE_COMPANIES`` names at least one company.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import matches, parse_companies, post_json, round_robin  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://apply.workable.com/api/v3/accounts/{co}/jobs"
_JOB_URL = "https://apply.workable.com/{co}/j/{shortcode}/"
_ENV_VAR = "WORKABLE_COMPANIES"


def _fetch_company(slug: str, query: str) -> list[dict]:
    """Call the Workable jobs API for one account and return its job list.
    Body must be exactly {"query": ...} — an added "limit" key errors."""
    data = post_json(_API.format(co=slug), {"query": query})
    results = data.get("results")
    return results if isinstance(results, list) else []


def _department_str(item: dict) -> str:
    dept = item.get("department")
    if isinstance(dept, list):
        return " ".join(str(d) for d in dept)
    return str(dept) if dept else ""


def _location_str(item: dict) -> str | None:
    loc = item.get("location")
    if not isinstance(loc, dict):
        return None
    composed = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
    return composed or None


def _to_job(item: dict, slug: str, fallback_name: str) -> Job | None:
    shortcode = item.get("shortcode")
    if not shortcode:
        return None
    ext_id = f"{slug}:{shortcode}"  # prefix: ids are only unique per-account
    return Job(
        source="workable",
        ext_id=ext_id,
        url=_JOB_URL.format(co=slug, shortcode=shortcode),  # no url field in payload; constructed
        title=item.get("title"),
        company=fallback_name,  # Workable postings carry no company field
        location=_location_str(item),
        posted_at=item.get("published"),  # already ISO 8601
        jd_text=None,  # no description field in list payload, no working detail endpoint found
        extra=item,
    )


class WorkablePlugin(JobSourcePlugin):
    """Company career-site postings across every Workable-hosted account
    configured in WORKABLE_COMPANIES."""

    name = "workable"
    base_url = "apply.workable.com"
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
                items = _fetch_company(slug, query)
            except Exception as exc:
                print(f"  workable: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    blob = f"{item.get('title', '')} {_department_str(item)}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, slug, fallback_name)
                except Exception as exc:
                    print(f"  workable: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
