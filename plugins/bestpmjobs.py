"""Best PM Jobs job-source plugin (RSS feed — no token required). PLAN.md §4.

Feed: GET https://www.bestpmjobs.com/jobs.rss — a standard RSS 2.0 feed
(Jobboardly SaaS platform; ``/jobs.rss`` is the standard path for any
Jobboardly-powered board — reuse this pattern for other Jobboardly sites).
No server-side search param, so filtering is entirely client-side.

Field-shape facts (verified live 2026-07-10, ~1000 items per call):
  - Jobboardly titles consistently follow ``"{Role} - {Company} - {Location}"``
    (ASCII " - " separator; role text itself may contain en/em-dashes like
    "–"/"—" without colliding with the split). Parsed by taking the LAST two
    " - "-separated segments as location/company and rejoining the rest as
    the role — best-effort: a role that itself contains a literal " - " will
    mis-split, degrading to a wrong company/location split rather than a
    crash (title itself is always preserved in ``extra``).
  - ``guid`` (not ``link``) is the stable per-posting id.
  - ``description`` is full HTML (job body incl. salary) — stripped via
    ``strip_html``.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _joblister_util import HEADERS, TIMEOUT, matches, parse_feed, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_FEED = "https://www.bestpmjobs.com/jobs.rss"


def _fetch_feed() -> list[dict]:
    req = urllib.request.Request(_FEED, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")
    return parse_feed(xml_text)


def _split_title(raw_title: str) -> tuple[str, str | None, str | None]:
    """Best-effort ``"{Role} - {Company} - {Location}"`` split. Returns
    ``(role, company, location)``; company/location are None if the title
    doesn't have at least 2 " - "-separated segments."""
    parts = raw_title.split(" - ")
    if len(parts) >= 3:
        return " - ".join(parts[:-2]), parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return raw_title, None, None


def _to_job(item: dict) -> Job | None:
    ext_id = (item.get("guid") or item.get("link") or "").strip()
    if not ext_id:
        return None
    raw_title = item.get("title") or ""
    role, company, location = _split_title(raw_title)
    return Job(
        source="bestpmjobs",
        ext_id=ext_id,
        url=item.get("link"),
        title=role or raw_title or None,
        company=company,
        location=location,
        posted_at=item.get("pubDate"),
        jd_text=strip_html(item.get("description") or "") or None,
        extra=item,
    )


class BestPMJobsPlugin(JobSourcePlugin):
    """Product-management jobs from bestpmjobs.com via its Jobboardly RSS feed."""

    name = "bestpmjobs"
    base_url = "bestpmjobs.com"
    mechanism = "rss"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_feed()
        except Exception as exc:
            print(f"  bestpmjobs: feed fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            try:
                desc_snip = strip_html((item.get("description") or "")[:800])
                blob = f"{item.get('title', '')} {desc_snip}"
                if not matches(blob, words):
                    continue
                job = _to_job(item)
            except Exception as exc:
                print(f"  bestpmjobs: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
