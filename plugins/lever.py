"""Lever job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: GET https://api.lever.co/v0/postings/<co>?mode=json — one call per
configured company, returns a bare JSON array of postings (no wrapper key).

Configure via ``LEVER_COMPANIES`` in ``.env`` (comma-separated slugs; see
``_ats_util.parse_companies`` for the ``slug:Display Name`` override syntax —
useful here since Lever postings carry NO company name field):

    LEVER_COMPANIES=leverdemo:Lever,huntress:Huntress

Field-shape quirks (verified live 2026-07-05, company ``leverdemo``):
  - No ``company`` field at all — the configured display name is used as-is.
  - ``descriptionPlain`` is ALREADY plain text (Lever pre-strips it) — only
    ``strip_html`` the HTML ``description`` field if ``descriptionPlain`` is
    missing, to avoid double-processing.
  - ``createdAt`` is a Unix epoch in MILLISECONDS, not seconds (13-digit
    values like ``1553186035299``) — converted via ``epoch_ms_to_iso``.
  - Location/team/department live under ``categories.{location,team,department}``.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``LEVER_COMPANIES`` names at least one company.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import (  # noqa: E402
    HEADERS,
    TIMEOUT,
    epoch_ms_to_iso,
    matches,
    parse_companies,
    round_robin,
    strip_html,
)
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://api.lever.co/v0/postings/{co}?mode=json"
_ENV_VAR = "LEVER_COMPANIES"


def _fetch_company(slug: str) -> list[dict]:
    """Call the Lever postings API for one company and return its job list."""
    req = urllib.request.Request(_API.format(co=slug), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, list) else []


def _description(item: dict) -> str:
    plain = item.get("descriptionPlain")
    if plain:
        return plain
    return strip_html(item.get("description") or "")


def _to_job(item: dict, slug: str, display_name: str) -> Job | None:
    raw_id = item.get("id")
    ext_id = f"{slug}:{raw_id}" if raw_id else ""  # prefix: ids collide across companies
    if not ext_id:
        return None
    categories = item.get("categories") or {}
    return Job(
        source="lever",
        ext_id=ext_id,
        url=item.get("hostedUrl"),
        title=item.get("text"),
        company=display_name,  # Lever postings carry no company field
        location=categories.get("location"),
        posted_at=epoch_ms_to_iso(item.get("createdAt")),
        jd_text=_description(item) or None,
        extra=item,
    )


class LeverPlugin(JobSourcePlugin):
    """Company career-site postings across every Lever-hosted company
    configured in LEVER_COMPANIES."""

    name = "lever"
    base_url = "api.lever.co"
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
        for slug, display_name in companies:
            try:
                items = _fetch_company(slug)
            except Exception as exc:
                print(f"  lever: {slug}: API fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for item in items:
                try:
                    desc_snip = _description(item)[:1200]
                    categories = item.get("categories") or {}
                    blob = f"{item.get('text', '')} {categories.get('team', '')} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, slug, display_name)
                except Exception as exc:
                    print(f"  lever: {slug}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
