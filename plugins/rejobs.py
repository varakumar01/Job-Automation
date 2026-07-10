"""Rejobs job-source plugin (Atom feed — no token required). PLAN.md §4.

Feed: GET https://rejobs.org/en/rss/renewable-energy-jobs — a standard
Atom 1.0 feed (renewable-energy / climate jobs niche board). No server-side
search param, so filtering is entirely client-side.

Field-shape facts (verified live 2026-07-10, ~200 entries per call):
  - ``title`` consistently follows ``"{Role} - {Company}"`` (ASCII " - "
    separator) — split at the LAST occurrence so a role containing its own
    " - " degrades to a wrong split rather than a crash (full title kept in
    ``extra``).
  - Atom ``<id>`` (e.g. ``https://rejobs.org/141124``) is the short stable
    per-posting id, distinct from ``<link>`` (the full slugged URL) — used
    as ``ext_id``.
  - ``summary`` is HTML — stripped via ``strip_html`` for ``jd_text``.
  - No discrete location field; location lives inside the summary text if
    present at all — left ``None`` here rather than guessed.
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

_FEED = "https://rejobs.org/en/rss/renewable-energy-jobs"


def _fetch_feed() -> list[dict]:
    req = urllib.request.Request(_FEED, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")
    return parse_feed(xml_text)


def _split_title(raw_title: str) -> tuple[str, str | None]:
    """Best-effort ``"{Role} - {Company}"`` split at the LAST " - "."""
    if " - " in raw_title:
        role, _, company = raw_title.rpartition(" - ")
        return role, company or None
    return raw_title, None


def _to_job(item: dict) -> Job | None:
    ext_id = (item.get("guid") or item.get("link") or "").strip()
    if not ext_id:
        return None
    raw_title = item.get("title") or ""
    role, company = _split_title(raw_title)
    return Job(
        source="rejobs",
        ext_id=ext_id,
        url=item.get("link"),
        title=role or raw_title or None,
        company=company,
        location=None,
        posted_at=item.get("pubDate"),
        jd_text=strip_html(item.get("description") or "") or None,
        extra=item,
    )


class RejobsPlugin(JobSourcePlugin):
    """Renewable-energy / climate jobs from rejobs.org via its Atom feed."""

    name = "rejobs"
    base_url = "rejobs.org"
    mechanism = "atom"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            items = _fetch_feed()
        except Exception as exc:
            print(f"  rejobs: feed fetch failed — {exc}", file=sys.stderr)
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
                print(f"  rejobs: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
