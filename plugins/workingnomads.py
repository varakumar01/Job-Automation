"""Working Nomads job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://www.workingnomads.com/api/exposed_jobs/ — returns a bare JSON
array of job objects (no wrapper, no pagination — one call returns the full
current set, ~30 recent postings). No server-side search param, so filtering
is entirely client-side (title + category_name + tags + description snippet).

Field-shape facts (verified live 2026-07-10):
  - No numeric ``id`` field. ``url`` is
    ``https://www.workingnomads.com/job/go/<numeric-id>/`` — the trailing path
    segment is the stable id, derived via ``_career_util.job_id_from_url``.
  - ``tags`` is a single comma-separated string, not a list.
  - ``pub_date`` is already an ISO 8601 string (with a numeric UTC offset,
    e.g. ``"2026-07-07T12:04:59-04:00"``) — used as-is for ``posted_at``.

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

from _career_util import job_id_from_url  # noqa: E402
from _joblister_util import HEADERS, TIMEOUT, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://www.workingnomads.com/api/exposed_jobs/"


def _fetch_api() -> list[dict]:
    """Call the Working Nomads API and return the job list. No query param
    exists on this endpoint — the full current set is always returned."""
    req = urllib.request.Request(_API, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, list) else []


def _to_job(item: dict) -> Job | None:
    url = item.get("url")
    if not url:
        return None
    ext_id = job_id_from_url(url)
    jd_text = strip_html(item.get("description") or "")
    tags = (item.get("tags") or "").strip()
    if tags:
        jd_text = f"{jd_text}\nTags: {tags}"
    return Job(
        source="workingnomads",
        ext_id=ext_id,
        url=url,
        title=item.get("title"),
        company=item.get("company_name"),
        location=item.get("location"),
        posted_at=item.get("pub_date"),
        jd_text=jd_text or None,
        extra=item,
    )


class WorkingNomadsPlugin(JobSourcePlugin):
    """Remote jobs from workingnomads.com via their public JSON API."""

    name = "workingnomads"
    base_url = "workingnomads.com"
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
            print(f"  workingnomads: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or "")[:800])
                blob = f"{item.get('title', '')} {item.get('category_name', '')} {item.get('tags', '')} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  workingnomads: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
