"""Recruitee job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: GET https://<co>.recruitee.com/api/offers — one call per configured
company (subdomain-based, not a path segment), returns ``{"offers": [...]}``
with the FULL description already included (single-call, like
Greenhouse/Lever/Ashby — no separate detail fetch needed).

Configure via ``RECRUITEE_COMPANIES`` in ``.env`` (comma-separated
subdomains; see ``_ats_util.parse_companies`` for the ``slug:Display Name``
override syntax):

    RECRUITEE_COMPANIES=personio,vanta:Vanta

Field-shape facts (verified live 2026-07-05, company ``personio``):
  - ``company_name`` IS present in the payload, so the configured display
    name is only a fallback.
  - ``careers_url`` is the job-detail page — use that for ``Job.url``, NOT
    ``careers_apply_url`` (the direct apply-form link).
  - ``published_at``/``created_at`` are ``"YYYY-MM-DD HH:MM:SS UTC"``
    strings (space-separated, not ISO 8601) — converted via
    ``_to_iso_z`` below.
  - ``location`` is already a human-readable composed string (e.g. ``"Berlin,
    Berlin, Deutschland"``) — used as-is.
  - ``description`` is HTML — needs ``strip_html``.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``RECRUITEE_COMPANIES`` names at least one company.
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

_API = "https://{co}.recruitee.com/api/offers"
_ENV_VAR = "RECRUITEE_COMPANIES"


def _fetch_company(slug: str) -> list[dict]:
    """Call the Recruitee offers API for one company and return its job list."""
    req = urllib.request.Request(_API.format(co=slug), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    offers = data.get("offers")
    return offers if isinstance(offers, list) else []


def _to_iso_z(ts: str | None) -> str | None:
    """Convert Recruitee's ``"YYYY-MM-DD HH:MM:SS UTC"`` to ISO 8601. Returns
    None (rather than raising) if the shape doesn't match — a Recruitee field
    format change should degrade to a missing date, not break the plugin."""
    if not ts or not ts.endswith(" UTC"):
        return None
    body = ts[: -len(" UTC")].strip()
    if " " not in body:
        return None
    date_part, time_part = body.split(" ", 1)
    return f"{date_part}T{time_part}+00:00"


def _to_job(item: dict, slug: str, fallback_name: str) -> Job | None:
    raw_id = item.get("id")
    if raw_id is None:
        return None
    ext_id = f"{slug}:{raw_id}"  # prefix: ids are only unique per-company
    return Job(
        source="recruitee",
        ext_id=ext_id,
        url=item.get("careers_url"),  # detail page, NOT careers_apply_url
        title=item.get("title"),
        company=item.get("company_name") or fallback_name,
        location=item.get("location"),
        posted_at=_to_iso_z(item.get("published_at") or item.get("created_at")),
        jd_text=strip_html(item.get("description") or "") or None,
        extra=item,
    )


class RecruiteePlugin(JobSourcePlugin):
    """Company career-site postings across every Recruitee-hosted company
    configured in RECRUITEE_COMPANIES."""

    name = "recruitee"
    base_url = "recruitee.com"
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
                print(f"  recruitee: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    desc_snip = strip_html((item.get("description") or "")[:1200])
                    blob = f"{item.get('title', '')} {item.get('department', '')} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, slug, fallback_name)
                except Exception as exc:
                    print(f"  recruitee: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
