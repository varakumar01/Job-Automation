"""NoDesk job-source plugin (RSS feed — no token required). PLAN.md §4.

Feed: GET https://nodesk.co/remote-jobs/index.xml — a standard RSS 2.0 feed.
No server-side search param, so filtering is entirely client-side.

Field-shape facts (verified live 2026-07-10, ~10 items per call):
  - The feed embeds raw (non-CDATA-wrapped) HTML named entities like
    ``&rsquo;``/``&mdash;`` in ``<description>`` — technically invalid XML
    (only 5 named entities are XML-predefined). ``_joblister_util.parse_feed``
    now repairs this generically (retried via ``_repair_html_entities`` on
    the first ``ParseError``), so this plugin needs no special-casing — just
    documenting why a naive ``ET.fromstring`` on this feed would silently
    return 0 items instead of raising.
  - ``title`` consistently follows ``"{Role} at {Company}"`` — split at the
    LAST ``" at "`` (space-bounded, case-sensitive) so a role that happens to
    contain the word "at" elsewhere degrades to a wrong split rather than a
    crash (full title kept in ``extra``).
  - ``guid`` == ``link`` (both the job's NoDesk page URL) — used as ``ext_id``.
  - No discrete location field — left ``None`` (NoDesk is remote-only by
    definition, same caveat as ``skipthedrive.py``, but unlike that plugin
    this one is left ``None`` rather than hardcoded "Remote" since some
    listings specify a region restriction inside the description text).

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the site is unreachable, ``fetch`` returns an empty list.
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

_FEED = "https://nodesk.co/remote-jobs/index.xml"


def _fetch_feed() -> list[dict]:
    req = urllib.request.Request(_FEED, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        xml_text = resp.read().decode("utf-8", errors="replace")
    return parse_feed(xml_text)


def _split_title(raw_title: str) -> tuple[str, str | None]:
    """Best-effort ``"{Role} at {Company}"`` split at the LAST " at "."""
    if " at " in raw_title:
        role, _, company = raw_title.rpartition(" at ")
        return role, company or None
    return raw_title, None


def _to_job(item: dict) -> Job | None:
    ext_id = (item.get("guid") or item.get("link") or "").strip()
    if not ext_id:
        return None
    raw_title = item.get("title") or ""
    role, company = _split_title(raw_title)
    return Job(
        source="nodesk",
        ext_id=ext_id,
        url=item.get("link"),
        title=role or raw_title or None,
        company=company,
        location=None,
        posted_at=item.get("pubDate"),
        jd_text=strip_html(item.get("description") or "") or None,
        extra=item,
    )


class NoDeskPlugin(JobSourcePlugin):
    """Remote jobs from nodesk.co via its RSS feed."""

    name = "nodesk"
    base_url = "nodesk.co"
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
            print(f"  nodesk: feed fetch failed — {exc}", file=sys.stderr)
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
                print(f"  nodesk: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
            if len(jobs) >= limit:
                break
        return jobs
