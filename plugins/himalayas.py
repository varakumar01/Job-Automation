"""Himalayas job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://himalayas.app/jobs/api?limit=N&q=<query> — returns
``{"jobs": [...], "totalCount": N, ...}``. The ``q`` param was tested live and
did NOT reliably filter (a ``q=security`` call returned an unrelated "Manager,
Commercial Quality" posting), so filtering here is entirely client-side
(title + categories + description snippet).

Field-shape quirks vs the other joblisters (verified 2026-07-05):
  - No numeric ``id``; the unique key is ``guid`` (used as ``ext_id``).
  - No flat ``url``; the best available link is ``applicationLink`` (verified
    live: identical to ``guid`` — both are the Himalayas job-listing page, not
    a distinct short id or apply-button link).
  - ``locationRestrictions`` and ``seniority`` are lists of strings, not a
    single field — joined with ", ".
  - ``pubDate`` is a Unix epoch int, NOT an ISO string (unlike every other
    joblister here) — converted via ``epoch_to_iso``.

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the API is unreachable, ``fetch`` returns an empty list.
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

from _joblister_util import HEADERS, TIMEOUT, epoch_to_iso, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://himalayas.app/jobs/api"


def _fetch_api(query: str, limit: int) -> list[dict]:
    """Call the Himalayas API and return the job list."""
    params: dict[str, str] = {"limit": str(max(limit * 4, 20))}  # over-fetch; q= is unreliable
    if query:
        params["q"] = query
    url = f"{_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _to_job(item: dict) -> Job | None:
    ext_id = str(item.get("guid") or "").strip()
    if not ext_id:
        return None
    jd_text = strip_html(item.get("description") or item.get("excerpt") or "")
    categories = item.get("categories") or []
    if categories:
        jd_text = f"{jd_text}\nCategories: {', '.join(str(x) for x in categories)}"
    locations = item.get("locationRestrictions") or []
    return Job(
        source="himalayas",
        ext_id=ext_id,
        url=item.get("applicationLink"),
        title=item.get("title"),
        company=item.get("companyName"),
        location=", ".join(locations) if locations else None,
        posted_at=epoch_to_iso(item.get("pubDate")),
        jd_text=jd_text or None,
        extra=item,
    )


class HimalayasPlugin(JobSourcePlugin):
    """Remote-first jobs from himalayas.app via their public JSON API."""

    name = "himalayas"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_api(query, limit)
        except Exception as exc:
            print(f"  himalayas: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or item.get("excerpt") or "")[:800])
                categories = " ".join(str(c) for c in (item.get("categories") or []))
                blob = f"{item.get('title', '')} {categories} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  himalayas: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
