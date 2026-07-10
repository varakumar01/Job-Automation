"""RemoteOK job-source plugin (public JSON API — no token required). PLAN.md §4.

API: GET https://remoteok.com/api[?tag=<tag>] — returns JSON array.
Element [0] is metadata; elements [1..] are job objects. No pagination;
each call returns up to 100 recent jobs.  Server-side ``?tag=`` filter
reduces the set to jobs bearing that tag; client-side keyword filter is
applied on top for precision.

No Apify dependency — uses stdlib ``urllib``. Always available (``is_available``
returns True); if the API is unreachable, ``fetch`` returns an empty list and
the runner logs the error.

Live-verified: 2026-07-01. Field schema confirmed from live API response.
If RemoteOK changes its API, extend the field-name list in ``_to_job`` and
note the date here.
"""

from __future__ import annotations

import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Make base importable when run standalone from within the plugins dir.
_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from base import Job, JobSourcePlugin  # noqa: E402

_API_BASE = "https://remoteok.com/api"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}
_TIMEOUT = 20  # seconds


def _strip_html(raw: str) -> str:
    """Remove HTML tags (incl. script/style content), decode entities, collapse whitespace."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _fetch_api(tag: str | None) -> list[dict]:
    """Call the RemoteOK API and return the job list (skips the metadata element)."""
    url = _API_BASE if not tag else f"{_API_BASE}?tag={urllib.parse.quote(tag)}"
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list) or len(data) < 2:
        return []
    # Element [0] is the metadata dict ({last_updated, legal}); skip it.
    return [item for item in data[1:] if isinstance(item, dict)]


def _matches(item: dict, words: list[str]) -> bool:
    """True if any query word appears in the job's title, tags, or description.
    Returns True unconditionally when words is empty (accept all)."""
    if not words:
        return True
    title = (item.get("position") or "").lower()
    tags = " ".join(item.get("tags") or []).lower()
    desc = _strip_html((item.get("description") or "")[:800]).lower()
    blob = f"{title} {tags} {desc}"
    return any(w in blob for w in words)


def _to_job(item: dict) -> Job | None:
    raw_id = item.get("id")
    ext_id = str(raw_id).strip() if raw_id is not None else ""
    if not ext_id:
        return None
    tags = item.get("tags") or []
    desc_html = item.get("description") or ""
    jd_text = _strip_html(desc_html)
    if tags:
        jd_text = f"{jd_text}\nTags: {', '.join(tags)}"
    return Job(
        source="remoteok",
        ext_id=ext_id,
        url=item.get("url") or item.get("apply_url"),
        title=item.get("position"),
        company=item.get("company"),
        location=item.get("location"),
        posted_at=item.get("date"),  # ISO 8601 string
        jd_text=jd_text or None,
        extra=item,
    )


class RemoteOKPlugin(JobSourcePlugin):
    """Remote-first jobs from remoteok.com via their public JSON API."""

    name = "remoteok"
    base_url = "remoteok.com"
    mechanism = "json"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        words = [w.lower() for w in query.split() if len(w) > 1]
        # Use the first query word as a server-side tag pre-filter, then
        # client-side filter by all words for precision.
        tag = words[0] if words else None
        try:
            items = _fetch_api(tag)
        except Exception as exc:
            print(f"  remoteok: API fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            if not _matches(item, words):
                continue
            job = _to_job(item)
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs

