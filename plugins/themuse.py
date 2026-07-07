"""The Muse job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://www.themuse.com/api/public/jobs?page=N — returns
``{"results": [...], "page_count": N, ...}``, 20 postings per page. There is
no free-text search param, so this plugin paginates (capped at
``_MAX_PAGES``) and filters client-side on title + company + categories +
content snippet, stopping once ``limit`` matches are found.

Live-verified: 2026-07-05. Field schema confirmed from live API response.
``company`` and ``refs``/``locations`` are nested objects/lists, not flat
strings — see ``_to_job``.

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

from _joblister_util import HEADERS, TIMEOUT, matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://www.themuse.com/api/public/jobs"
_MAX_PAGES = 5  # no free-text search; cap pagination to bound request volume


def _fetch_page(page: int) -> list[dict]:
    """Call The Muse API for one page and return its results list."""
    url = f"{_API}?{urllib.parse.urlencode({'page': page})}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data.get("results")
    return results if isinstance(results, list) else []


def _to_job(item: dict) -> Job | None:
    raw_id = item.get("id")
    ext_id = str(raw_id).strip() if raw_id is not None else ""
    if not ext_id:
        return None
    jd_text = strip_html(item.get("contents") or "")
    categories = [c.get("name", "") for c in (item.get("categories") or []) if c.get("name")]
    if categories:
        jd_text = f"{jd_text}\nCategories: {', '.join(categories)}"
    locations = [loc.get("name", "") for loc in (item.get("locations") or []) if loc.get("name")]
    return Job(
        source="themuse",
        ext_id=ext_id,
        url=(item.get("refs") or {}).get("landing_page"),
        title=item.get("name"),
        company=(item.get("company") or {}).get("name"),
        location=", ".join(locations) if locations else None,
        posted_at=item.get("publication_date"),  # ISO 8601 string
        jd_text=jd_text or None,
        extra=item,
    )


class TheMusePlugin(JobSourcePlugin):
    """Jobs from themuse.com via their public JSON API (paginated, no search param)."""

    name = "themuse"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        jobs: list[Job] = []
        for page in range(0, _MAX_PAGES):  # The Muse pages are 0-indexed (verified live)
            try:
                items = _fetch_page(page)
            except Exception as exc:
                print(f"  themuse: API fetch failed (page {page}) — {exc}", file=sys.stderr)
                break
            if not items:
                break
            for item in items:
                try:
                    categories = [c.get("name", "") for c in (item.get("categories") or [])]
                    desc_snip = strip_html((item.get("contents") or "")[:800])
                    blob = f"{item.get('name', '')} {' '.join(categories)} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item)
                except Exception as exc:
                    print(f"  themuse: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    jobs.append(job)
                if len(jobs) >= limit:
                    return jobs
        return jobs
