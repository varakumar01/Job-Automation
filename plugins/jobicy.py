"""Jobicy job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://jobicy.com/api/v2/remote-jobs?count=N&tag=<tag> — returns
``{"jobs": [...], "jobCount": N, ...}``. The server-side ``tag`` param does
keyword pre-filtering (verified: ``tag=security`` returned security-titled
roles); the first query word is used as the tag, then all words are checked
client-side against title/description for precision (mirrors ``remoteok.py``).

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

_API = "https://jobicy.com/api/v2/remote-jobs"


def _fetch_api(tag: str | None, count: int) -> list[dict]:
    """Call the Jobicy API and return the job list."""
    params: dict[str, str] = {"count": str(max(count, 1))}
    if tag:
        params["tag"] = tag
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
    jd_html = item.get("jobDescription") or item.get("jobExcerpt") or ""
    jd_text = strip_html(jd_html)
    industry = item.get("jobIndustry") or []
    if industry:
        jd_text = f"{jd_text}\nIndustry: {', '.join(str(x) for x in industry)}"
    return Job(
        source="jobicy",
        ext_id=ext_id,
        url=item.get("url"),
        title=item.get("jobTitle"),
        company=item.get("companyName"),
        location=item.get("jobGeo"),
        posted_at=item.get("pubDate"),  # ISO 8601 string
        jd_text=jd_text or None,
        extra=item,
    )


class JobicyPlugin(JobSourcePlugin):
    """Remote-first jobs from jobicy.com via their public JSON API."""

    name = "jobicy"
    base_url = "jobicy.com"
    mechanism = "json"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        tag = words[0] if words else None
        try:
            items = _fetch_api(tag, limit)
        except Exception as exc:
            print(f"  jobicy: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("jobDescription") or item.get("jobExcerpt") or "")[:800])
                blob = f"{item.get('jobTitle', '')} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  jobicy: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
