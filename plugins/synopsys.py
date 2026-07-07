"""Synopsys custom career-site plugin (Avature-powered, JS-rendered). PLAN.md §4/§10.

Careers site: https://careers.synopsys.com — NOT on any of the 8 known ATS
platforms (Avature backend, confirmed live 2026-07-06). The initial
`/search-jobs` page is just a search FORM — no job data server-rendered —
but the results page IS directly navigable by URL (no form interaction
needed) and DOES render real job data into the DOM once JS runs:

    List:   https://careers.synopsys.com/search-jobs/<keyword>/44408/<page>
            (keyword may be empty — `/search-jobs//44408/1` returns all open
            roles; `44408` is Synopsys's Avature org id, embedded in the URL)
    Detail: https://careers.synopsys.com/job/<location-slug>/<title-slug>/44408/<job-id>
            (the href on each list item — already a full path, use as-is)

This is a Tier-3 (`_career_util.render_html`) plugin — the first proof this
project's browser-rendering fallback works end-to-end. `is_available()`
gates on `playwright_available()` (chromium actually downloaded), not the
wrong `PLAYWRIGHT_USER_DATA_DIR` login-session check `_custom_template.py`
uses — this is a public page, no login of any kind is involved.

Field-shape facts (verified live 2026-07-06 via real Playwright render,
query "security", org 44408):
  - Pagination is real: the `<section id="search-results" data-total-pages="N">`
    wrapper on page 1 gives the true page count; capped at `_MAX_PAGES` here
    regardless, so a broad query on this ~580-job board can't runaway-fetch.
  - Each result is a `<li class="search-results-list__list-item">` containing
    an `<a class="sr-job-link" href="/job/...">` (title in a trailing `<h2>`,
    with a decorative `<img>` needing stripping), a `.job-location` span, a
    `.category` span, and a `.job-date-posted` span (`"Posted: MM/DD/YYYY"`,
    US format — converted via `_career_util.mmddyyyy_to_iso`).
  - The numeric `data-job-id` attribute is the stable per-posting id (used
    for `ext_id`; no cross-company prefix needed since this plugin is a
    single company, same convention as every other single-site plugin here).
  - The detail page has no single clean "job description" container — the
    Overview/Job-Description/Benefits/How-We-Hire tabs are all anchor-scroll
    sections already present in one `<main>` element, not JS show/hide
    panels — but `<main>` also includes the search form (before) and, after
    the real content, a location-map teaser ("Get an idea of what your daily
    routine..."), a "Hiring Journey" process blurb, and a "Similar Jobs /
    Recently Viewed / Saved Jobs" carousel. `jd_text` is `<main>`'s stripped
    text, cut at the FIRST of these markers found (best-effort — code-tester
    verified 2026-07-06 that the initial marker set missed the first two,
    leaking ~11% trailing boilerplate into `jd_text`; both are now included.
    A further site-markup change degrades to a longer `jd_text` with a bit of
    trailing chrome, not a crash — self-anneal by adding the new marker text
    if this recurs).

No Apify dependency — Playwright only, no persistent profile/login.
`is_available` is True only when a chromium build is actually installed.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _career_util import matches, mmddyyyy_to_iso, playwright_available, render_html, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_ORG_ID = "44408"
_BASE = "https://careers.synopsys.com"
_LIST_URL = _BASE + "/search-jobs/{kw}/" + _ORG_ID + "/{page}"
_MAX_PAGES = 5  # bounds render_html() calls per fetch regardless of the board's real size

_LIST_ITEM_RE = re.compile(
    r'<li class="search-results-list__list-item">(.*?)</li>', re.DOTALL
)
_LINK_RE = re.compile(r'<a class="sr-job-link" href="([^"]+)" data-job-id="(\d+)">(.*?)</a>', re.DOTALL)
_H2_RE = re.compile(r"<h2>(.*?)</h2>", re.DOTALL)
_LOCATION_RE = re.compile(r'<span class="job-location">(.*?)</span>', re.DOTALL)
_DATE_RE = re.compile(r'<span class="job-date-posted">.*?Posted:\s*</strong>\s*([\d/]+)', re.DOTALL)
_TOTAL_PAGES_RE = re.compile(r'data-total-pages="(\d+)"')

_TRAILING_MARKERS = (
    "Get an idea of what your daily routine",  # location-map widget after the Benefits tab
    "Hiring Journey",  # process-steps blurb (Apply/Phone Screen/.../Onboarding) after the JD tabs
    "Similar Jobs",
    "Recently Viewed Jobs",
    "Saved Jobs",
    "View All Jobs",
)


def _parse_list_page(html: str) -> tuple[list[dict], int | None]:
    """Return (items, total_pages) for one rendered search-results page."""
    total_m = _TOTAL_PAGES_RE.search(html)
    total_pages = int(total_m.group(1)) if total_m else None

    items: list[dict] = []
    for block in _LIST_ITEM_RE.findall(html):
        link_m = _LINK_RE.search(block)
        if not link_m:
            continue
        href, job_id, _link_inner = link_m.groups()
        h2_m = _H2_RE.search(block)
        title = strip_html(h2_m.group(1)) if h2_m else None
        loc_m = _LOCATION_RE.search(block)
        location = strip_html(loc_m.group(1)) if loc_m else None
        date_m = _DATE_RE.search(block)
        posted_raw = date_m.group(1) if date_m else None
        items.append(
            {
                "job_id": job_id,
                "url": urllib.parse.urljoin(_BASE, href),
                "title": title,
                "location": location,
                "posted_at": mmddyyyy_to_iso(posted_raw),
            }
        )
    return items, total_pages


def _fetch_list(query: str) -> list[dict]:
    kw = urllib.parse.quote(query.strip())
    all_items: list[dict] = []
    page = 1
    total_pages = None
    while page <= _MAX_PAGES and (total_pages is None or page <= total_pages):
        html = render_html(_LIST_URL.format(kw=kw, page=page), timeout_ms=20000)
        items, tp = _parse_list_page(html)
        if page == 1:
            total_pages = tp
        if not items:
            break
        all_items.extend(items)
        page += 1
    return all_items


def _fetch_jd_text(url: str) -> str | None:
    html = render_html(url, wait_selector="main", timeout_ms=20000)
    m = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL)
    if not m:
        return None
    text = strip_html(m.group(1))
    cut = len(text)
    for marker in _TRAILING_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip() or None


def _to_job(item: dict, jd_text: str | None) -> Job | None:
    job_id = item.get("job_id")
    if not job_id:
        return None
    return Job(
        source="synopsys",
        ext_id=job_id,
        url=item.get("url"),
        title=item.get("title"),
        company="Synopsys",
        location=item.get("location"),
        posted_at=item.get("posted_at"),
        jd_text=jd_text,
        extra=item,
    )


class SynopsysPlugin(JobSourcePlugin):
    """Synopsys career-site postings (Avature, JS-rendered — Tier 3)."""

    name = "synopsys"

    def is_available(self) -> bool:
        return playwright_available()

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        try:
            items = _fetch_list(query)
        except Exception as exc:
            print(f"  synopsys: list fetch failed — {exc}", file=sys.stderr)
            return []

        jobs: list[Job] = []
        for item in items:
            if len(jobs) >= limit:
                break
            title = item.get("title") or ""
            if not matches(title, words):
                continue
            jd_text = None
            url = item.get("url")
            if url:
                try:
                    jd_text = _fetch_jd_text(url)
                except Exception as exc:
                    print(f"  synopsys: detail fetch failed for {url} — {exc}", file=sys.stderr)
            try:
                job = _to_job(item, jd_text)
            except Exception as exc:
                print(f"  synopsys: skipping malformed item — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
        return jobs
