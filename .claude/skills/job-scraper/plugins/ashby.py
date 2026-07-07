"""Ashby job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: GET https://api.ashbyhq.com/posting-api/job-board/<co> — one call per
configured company, returns ``{"jobs": [...], "apiVersion": "..."}``.

Configure via ``ASHBY_COMPANIES`` in ``.env`` (comma-separated slugs; see
``_ats_util.parse_companies`` for the ``slug:Display Name`` override syntax —
useful here since Ashby postings carry NO company name field):

    ASHBY_COMPANIES=linear:Linear,ramp:Ramp

Field-shape quirks (verified live 2026-07-05, companies ``linear``/``ramp``/
``notion``/``openai``):
  - No ``company`` field at all — the configured display name is used as-is.
  - ``descriptionPlain`` is ALREADY plain text (Ashby pre-strips it) — only
    ``strip_html`` the ``descriptionHtml`` field if ``descriptionPlain`` is
    missing.
  - Use ``jobUrl`` (the job-board listing page) as ``Job.url``, per PLAN §10's
    rule to store the job-detail link, NOT ``applyUrl`` (the apply-button link).
  - ``publishedAt`` is already ISO 8601 — no conversion needed (unlike Lever's
    epoch-ms ``createdAt``).

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``ASHBY_COMPANIES`` names at least one company.
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

_API = "https://api.ashbyhq.com/posting-api/job-board/{co}"
_ENV_VAR = "ASHBY_COMPANIES"


def _fetch_company(slug: str) -> list[dict]:
    """Call the Ashby job-board API for one company and return its job list.
    Some real Ashby slugs contain a literal space (e.g. "Redesign Health",
    found live 2026-07-07) — `urllib.parse.quote` percent-encodes it (and any
    other URL-unsafe character) before it's interpolated into the request
    path; a no-op for the common alphanumeric-slug case."""
    req = urllib.request.Request(_API.format(co=urllib.parse.quote(slug, safe="")), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _description(item: dict) -> str:
    plain = item.get("descriptionPlain")
    if plain:
        return plain
    return strip_html(item.get("descriptionHtml") or "")


def _to_job(item: dict, slug: str, display_name: str) -> Job | None:
    raw_id = item.get("id")
    ext_id = f"{slug}:{raw_id}" if raw_id else ""  # prefix: ids collide across companies
    if not ext_id:
        return None
    return Job(
        source="ashby",
        ext_id=ext_id,
        url=item.get("jobUrl"),  # job-board detail link, NOT applyUrl (PLAN §10)
        title=item.get("title"),
        company=display_name,  # Ashby postings carry no company field
        location=item.get("location"),
        posted_at=item.get("publishedAt"),  # already ISO 8601
        jd_text=_description(item) or None,
        extra=item,
    )


class AshbyPlugin(JobSourcePlugin):
    """Company career-site postings across every Ashby-hosted company
    configured in ASHBY_COMPANIES."""

    name = "ashby"

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
                print(f"  ashby: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    desc_snip = _description(item)[:1200]
                    blob = f"{item.get('title', '')} {item.get('department', '')} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, slug, display_name)
                except Exception as exc:
                    print(f"  ashby: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
