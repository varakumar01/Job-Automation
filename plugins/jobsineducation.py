"""Jobs in Education job-source plugin (RSS feed — no token required). PLAN.md §4.

Feed: GET https://jobsineducation.com/feeds/rss.xml — a standard RSS 2.0 feed
(SmartJobBoard SaaS platform). No server-side search param, so filtering is
entirely client-side.

Field-shape facts (verified live 2026-07-10, ~124 items per call):
  - ``dc:creator`` carries the employer/organization name — mapped to
    ``author`` by ``_joblister_util.parse_feed`` and used as ``company``.
  - There is no discrete location field. The stripped description usually
    (not always — verified live, roughly 4/5 items) STARTS with the location
    text followed immediately by the company name again, e.g. ``"Inukjuak,
    QC, Canada Kativik Ilisarniliriniq OBJECTIFS DU PROGRAMME: ..."``.
    ``_extract_location`` finds the company name near the start of the
    stripped text and treats everything before it as the location; if the
    company name doesn't appear within the first 100 chars (some postings
    open directly with the company, no location line), location is left
    ``None`` rather than guessed. ``jd_text`` keeps the full text
    unmodified — this header repetition is harmless noise, not stripped.
  - Board is bilingual (many Quebec-region listings are in French) — no
    translation is attempted, fields are passed through as-is.
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

_FEED = "https://jobsineducation.com/feeds/rss.xml"
_LOCATION_SEARCH_WINDOW = 100  # chars — company name must appear within this prefix


def _fetch_feed() -> list[dict]:
    req = urllib.request.Request(_FEED, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")
    return parse_feed(xml_text)


def _extract_location(text: str, company: str | None) -> str | None:
    """Best-effort location: the text preceding the company name, only if
    the company appears within the first _LOCATION_SEARCH_WINDOW chars."""
    if not company:
        return None
    idx = text.find(company)
    if idx <= 0 or idx > _LOCATION_SEARCH_WINDOW:
        return None
    candidate = text[:idx].strip()
    return candidate or None


def _to_job(item: dict) -> Job | None:
    ext_id = (item.get("guid") or item.get("link") or "").strip()
    if not ext_id:
        return None
    company = item.get("author")
    jd_text = strip_html(item.get("description") or "")
    return Job(
        source="jobsineducation",
        ext_id=ext_id,
        url=item.get("link"),
        title=item.get("title"),
        company=company,
        location=_extract_location(jd_text, company),
        posted_at=item.get("pubDate"),
        jd_text=jd_text or None,
        extra=item,
    )


class JobsInEducationPlugin(JobSourcePlugin):
    """Education/teaching jobs from jobsineducation.com via its RSS feed."""

    name = "jobsineducation"
    base_url = "jobsineducation.com"
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
            print(f"  jobsineducation: feed fetch failed — {exc}", file=sys.stderr)
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
                print(f"  jobsineducation: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
