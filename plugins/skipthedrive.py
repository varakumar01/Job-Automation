"""SkipTheDrive job-source plugin (server-rendered HTML search — no token). PLAN.md §4.

No RSS/feed of any kind is reachable on this site (verified live 2026-07-10:
``/feed/``, ``/?feed=rss2``, ``/?s=<q>&feed=rss2``, and every
``/job-category/<slug>/feed/`` path all silently return the plain homepage
HTML, not XML — the WordPress feed has been disabled site-wide). BUT the
plain search-results page ``https://www.skipthedrive.com/?s=<query>`` DOES
still server-render real job cards directly into the HTML (no JS needed) —
this plugin scrapes that page instead of relying on a feed.

Verified live 2026-07-10, query "engineer" (20 results/page, up to 25 pages):
  - Each result is a ``<div class="post-content">`` block. Regex parsing
    splits on the reliably-one-per-job ``<h2 class="post-title
    entry-title">`` marker rather than trying to balance nested ``<div>``s.
  - Title + URL: ``<a href="...">Title</a></h2>``. The trailing numeric
    suffix in the URL slug (e.g. ``...-1361944/``) is the stable id, taken
    via ``_career_util.job_id_from_url`` (the whole slug, not just the
    digits — still unique and stable, simpler than extracting the number).
  - Company: ``custom_fields_company_name_display_search_results`` span,
    always preceded by an EMPTY icon span (``<span class='fa
    fa-building-o' ...></span>``) then ``&nbsp;CompanyName`` — matched
    explicitly around that empty icon span rather than a naive non-greedy
    ``.*?</span>`` (which stops at the icon's own closing tag).
  - Posted date: ``custom_fields_job_date_display_search_results`` span,
    same icon-then-text shape, but the text is RELATIVE ("14 days ago"), not
    absolute — stored as-is in ``posted_at`` (no reliable way to convert to
    an absolute ISO date from this text alone).
  - No location field anywhere on the page — the entire board is remote-only
    by definition (its whole premise), so ``location`` is hardcoded to
    ``"Remote"`` rather than left ``None``.
  - Pagination: ``https://www.skipthedrive.com/page/<n>/?s=<query>`` for
    page 2+ (page 1 is the bare ``?s=<query>`` URL). Capped at ``_MAX_PAGES``
    regardless of how many pages the query claims to have.
  - A query with zero matches returns 0 ``post-title`` blocks, not an error.

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the site is unreachable, ``fetch`` returns an empty list.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _career_util import job_id_from_url  # noqa: E402
from _joblister_util import HEADERS, TIMEOUT, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_BASE = "https://www.skipthedrive.com"
_H2_MARKER = '<h2 class="post-title entry-title">'
_MAX_PAGES = 3  # bounds requests per fetch regardless of how many result pages exist

_LINK_RE = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_COMPANY_RE = re.compile(
    r"custom_fields_company_name_display_search_results[^>]*>\s*(?:<span[^>]*></span>)?&nbsp;([^<]+)</span>",
    re.DOTALL,
)
_DATE_RE = re.compile(
    r"custom_fields_job_date_display_search_results[^>]*>\s*(?:<span[^>]*></span>)?&nbsp;([^<]+)</span>",
    re.DOTALL,
)
_EXCERPT_RE = re.compile(r'<span class="excerpt_part">(.*?)</span>', re.DOTALL)


def _search_url(query: str, page: int) -> str:
    qs = urllib.parse.urlencode({"s": query})
    if page <= 1:
        return f"{_BASE}/?{qs}"
    return f"{_BASE}/page/{page}/?{qs}"


def _fetch_page_html(query: str, page: int) -> str:
    req = urllib.request.Request(_search_url(query, page), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_search_html(html: str) -> list[dict]:
    """Split on the per-job ``<h2>`` marker and extract fields from each chunk."""
    chunks = html.split(_H2_MARKER)[1:]  # chunk[0] is page chrome before any job
    items: list[dict] = []
    for chunk in chunks:
        # .search, not .match: tolerates whitespace/newlines between the <h2>
        # marker and the nested <a> tag, which a naive position-0 anchor
        # would silently break on (0 rows, no error) if the site's markup
        # ever reformats.
        link_m = _LINK_RE.search(chunk)
        if not link_m:
            continue
        url, title_html = link_m.groups()
        company_m = _COMPANY_RE.search(chunk)
        date_m = _DATE_RE.search(chunk)
        excerpt_m = _EXCERPT_RE.search(chunk)
        items.append(
            {
                "url": url,
                "title": strip_html(title_html),
                "company": strip_html(company_m.group(1)) if company_m else None,
                "posted_at": strip_html(date_m.group(1)) if date_m else None,
                "excerpt": strip_html(excerpt_m.group(1)) if excerpt_m else None,
            }
        )
    return items


def _to_job(item: dict) -> Job | None:
    url = item.get("url")
    if not url:
        return None
    return Job(
        source="skipthedrive",
        ext_id=job_id_from_url(url),
        url=url,
        title=item.get("title"),
        company=item.get("company"),
        location="Remote",  # the entire board is remote-only; no per-job location field exists
        posted_at=item.get("posted_at"),
        jd_text=item.get("excerpt"),
        extra=item,
    )


class SkipTheDrivePlugin(JobSourcePlugin):
    """Remote jobs from skipthedrive.com via its server-rendered search page."""

    name = "skipthedrive"
    base_url = "skipthedrive.com"
    mechanism = "html"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []

        jobs: list[Job] = []
        page = 1
        while page <= _MAX_PAGES and len(jobs) < limit:
            try:
                html = _fetch_page_html(query, page)
            except Exception as exc:
                print(f"  skipthedrive: page {page} fetch failed — {exc}", file=sys.stderr)
                break
            items = _parse_search_html(html)
            if not items:
                break  # no more results (or query matched nothing)
            for item in items:
                try:
                    job = _to_job(item)
                except Exception as exc:
                    print(f"  skipthedrive: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    jobs.append(job)
                if len(jobs) >= limit:
                    break
            page += 1
        return jobs
