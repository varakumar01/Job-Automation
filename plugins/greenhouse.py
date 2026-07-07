"""Greenhouse job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: GET https://boards-api.greenhouse.io/v1/boards/<co>/jobs?content=true —
one call per configured company, returns ``{"jobs": [...]}``.

Configure via ``GREENHOUSE_COMPANIES`` in ``.env`` (comma-separated slugs, see
``_ats_util.parse_companies`` for the ``slug:Display Name`` override syntax):

    GREENHOUSE_COMPANIES=crowdstrike,wiz,gitlab

Field-shape quirk (verified live 2026-07-05): ``content`` is HTML that has
been HTML-ENTITY-ESCAPED ONCE MORE than usual (e.g. the literal text
``&lt;div&gt;`` rather than ``<div>``) — a single ``html.unescape`` pass turns
it into real HTML, which ``strip_html`` (itself doing a further unescape +
tag-strip) then reduces to plain text. Skipping the extra unescape leaves
literal ``<div>`` tags in the stored ``jd_text``.

``company_name`` IS present in the payload (unlike Lever/Ashby), so the
configured display name is only a fallback for a slug with no live jobs.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``GREENHOUSE_COMPANIES`` names at least one company.
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

_API = "https://boards-api.greenhouse.io/v1/boards/{co}/jobs?content=true"
_ENV_VAR = "GREENHOUSE_COMPANIES"


def _fetch_company(slug: str) -> list[dict]:
    """Call the Greenhouse board API for one company and return its job list."""
    req = urllib.request.Request(_API.format(co=slug), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _to_job(item: dict, slug: str, fallback_name: str) -> Job | None:
    raw_id = item.get("id")
    if raw_id is None:
        return None
    ext_id = f"{slug}:{raw_id}"  # prefix: ids are only unique per-company
    jd_text = strip_html(html.unescape(item.get("content") or ""))  # double-escaped, see module docstring
    location = (item.get("location") or {}).get("name")
    return Job(
        source="greenhouse",
        ext_id=ext_id,
        url=item.get("absolute_url"),
        title=item.get("title"),
        company=item.get("company_name") or fallback_name,
        location=location,
        posted_at=item.get("first_published"),  # ISO 8601 string
        jd_text=jd_text or None,
        extra=item,
    )


class GreenhousePlugin(JobSourcePlugin):
    """Company career-site postings across every Greenhouse-hosted company
    configured in GREENHOUSE_COMPANIES."""

    name = "greenhouse"

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
                print(f"  greenhouse: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    desc_snip = strip_html(html.unescape((item.get("content") or "")[:1200]))
                    blob = f"{item.get('title', '')} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, slug, fallback_name)
                except Exception as exc:
                    print(f"  greenhouse: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
