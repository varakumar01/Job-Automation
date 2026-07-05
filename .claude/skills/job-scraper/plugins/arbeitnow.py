"""Arbeitnow job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://www.arbeitnow.com/api/job-board-api — returns
``{"data": [...]}``, no server-side search param, so filtering is entirely
client-side (title + tags + description snippet). Each call returns the
current ~100 most recent postings; no pagination cursor is exposed.

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the API is unreachable, ``fetch`` returns an empty list.

Live-verified: 2026-07-05. Field schema confirmed from live API response.
``created_at`` is a Unix epoch int — converted to ISO 8601 for ``posted_at``.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _joblister_util import HEADERS, TIMEOUT, epoch_to_iso, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://www.arbeitnow.com/api/job-board-api"


def _fetch_api() -> list[dict]:
    """Call the Arbeitnow API and return the job list."""
    req = urllib.request.Request(_API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    jobs = data.get("data")
    return jobs if isinstance(jobs, list) else []


def _to_job(item: dict) -> Job | None:
    ext_id = str(item.get("slug") or "").strip()
    if not ext_id:
        return None
    tags = item.get("tags") or []
    jd_text = strip_html(item.get("description") or "")
    if tags:
        jd_text = f"{jd_text}\nTags: {', '.join(tags)}"
    return Job(
        source="arbeitnow",
        ext_id=ext_id,
        url=item.get("url"),
        title=item.get("title"),
        company=item.get("company_name"),
        location=item.get("location"),
        posted_at=epoch_to_iso(item.get("created_at")),
        jd_text=jd_text or None,
        extra=item,
    )


class ArbeitnowPlugin(JobSourcePlugin):
    """Jobs (EU-heavy, remote + on-site) from arbeitnow.com via their public JSON API."""

    name = "arbeitnow"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_api()
        except Exception as exc:
            print(f"  arbeitnow: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or "")[:800])
                blob = (f"{item.get('title', '')} {' '.join(item.get('tags') or [])} "
                        f"{desc_snip}")
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  arbeitnow: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
