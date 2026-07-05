"""Remotive job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://remotive.com/api/remote-jobs?search=<query>&limit=N — returns
``{"jobs": [...]}``. The server-side ``search`` param already keyword-matches
title/description, but a light client-side check on title+tags+description is
still applied for precision (mirrors ``remoteok.py``).

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the API is unreachable, ``fetch`` returns an empty list.

Live-verified: 2026-07-05. Field schema confirmed from live API response.
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

from _joblister_util import HEADERS, TIMEOUT, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://remotive.com/api/remote-jobs"


def _fetch_api(query: str, limit: int) -> list[dict]:
    """Call the Remotive API and return the job list.

    Over-fetches (``limit * 2``) since the server-side ``search`` match is
    looser than our client-side precision filter — asking for exactly
    ``limit`` would under-return once filtered.
    """
    params: dict[str, str] = {"limit": str(max(limit * 2, 1))}
    if query:
        params["search"] = query
    url = f"{_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _to_job(item: dict) -> Job | None:
    raw_id = item.get("id")
    ext_id = str(raw_id).strip() if raw_id is not None else ""
    if not ext_id:
        return None
    tags = item.get("tags") or []
    jd_text = strip_html(item.get("description") or "")
    if tags:
        jd_text = f"{jd_text}\nTags: {', '.join(tags)}"
    return Job(
        source="remotive",
        ext_id=ext_id,
        url=item.get("url"),
        title=item.get("title"),
        company=item.get("company_name"),
        location=item.get("candidate_required_location"),
        posted_at=item.get("publication_date"),  # ISO 8601 string
        jd_text=jd_text or None,
        extra=item,
    )


class RemotivePlugin(JobSourcePlugin):
    """Remote-first jobs from remotive.com via their public JSON API."""

    name = "remotive"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_api(query, limit)
        except Exception as exc:
            print(f"  remotive: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or "")[:800])
                blob = f"{item.get('title', '')} {' '.join(item.get('tags') or [])} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  remotive: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
