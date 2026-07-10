"""We Love Product job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://weloveproduct.co/api/jobs — returns
``{"jobs": [...], "jobCount": N, "totalPages": N, "hasMore": bool}``.
No server-side search param, so filtering is entirely client-side.

Field-shape facts (verified live 2026-07-10):
  - Unauthenticated calls only ever return page 1 (~32 of the newest jobs,
    ``jobCount`` reports the true total, e.g. 64 across 2 pages) —
    ``?page=2`` returns HTTP 401. This is a freemium gate on Wellfound-style
    deep history, not a bug; there is no pagination path here. Document the
    "~32 latest per poll" ceiling — a broad/rare query may under-return vs.
    the site's true total.
  - ``company`` is a nested dict (``company.title``); ``locations`` is a
    list of dicts (``city``/``country``/``state``) — flattened via
    ``_flatten_locations`` (dedup, comma-joined, skips ``None`` parts).
  - ``published_at`` is already an ISO 8601 string.

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the API is unreachable, ``fetch`` returns an empty list.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _joblister_util import HEADERS, TIMEOUT, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://weloveproduct.co/api/jobs"


def _fetch_api() -> list[dict]:
    req = urllib.request.Request(_API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return jobs if isinstance(jobs, list) else []


def _flatten_locations(locations: object) -> str | None:
    """``locations`` is a list of {city, country, state, ...} dicts — join
    each entry's non-empty parts, dedup, comma-separate the whole list."""
    if not isinstance(locations, list):
        return None
    seen: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        parts = [str(loc[k]) for k in ("city", "state", "country") if loc.get(k)]
        text = ", ".join(parts)
        if text and text not in seen:
            seen.append(text)
    return ", ".join(seen) or None


def _to_job(item: dict) -> Job | None:
    raw_id = item.get("id")
    ext_id = str(raw_id).strip() if raw_id is not None else ""
    if not ext_id:
        return None
    company = item.get("company")
    company_name = company.get("title") if isinstance(company, dict) else None
    category = item.get("job_category")
    category_name = category.get("title") if isinstance(category, dict) else None
    contract = item.get("job_contract_type")
    contract_name = contract.get("title") if isinstance(contract, dict) else None
    jd_text = strip_html(item.get("description") or "")
    extras = ", ".join(x for x in (category_name, contract_name, item.get("seniority")) if x)
    if extras:
        jd_text = f"{jd_text}\n{extras}"
    return Job(
        source="weloveproduct",
        ext_id=ext_id,
        url=item.get("url_apply"),
        title=item.get("title"),
        company=company_name,
        location=_flatten_locations(item.get("locations")),
        posted_at=item.get("published_at"),
        jd_text=jd_text or None,
        extra=item,
    )


class WeLoveProductPlugin(JobSourcePlugin):
    """Product-management jobs from weloveproduct.co via its public JSON API."""

    name = "weloveproduct"
    base_url = "weloveproduct.co"
    mechanism = "json"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_api()
        except Exception as exc:
            print(f"  weloveproduct: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or "")[:800])
                category = item.get("job_category") or {}
                category_name = category.get("title") if isinstance(category, dict) else ""
                blob = f"{item.get('title', '')} {category_name} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  weloveproduct: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
