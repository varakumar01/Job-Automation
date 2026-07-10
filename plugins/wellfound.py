"""Wellfound job-source plugin (Apify actor primary + session fallback). PLAN.md §4/§9.

Wellfound (formerly AngelList Talent) has NO usable direct-fetch path —
verified live 2026-07-10: ``/jobs.rss`` and ``/sitemap.xml`` both return
HTTP 403 (DataDome + Cloudflare gate essentially everything except the
marketing landing page), there is no public JSON API, and its internal
GraphQL endpoint requires a live browser session's CSRF token + cookies.
Filtered job search is also login-gated. Per the user's 2026-07-10 decision
(PLAN.md §9), this plugin therefore uses a TWO-TIER strategy instead of the
single-mechanism approach every other plugin in this file uses:

  1. PRIMARY — Apify actor ``thirdwatch/wellfound-jobs-scraper`` (override
     via ``APIFY_ACTOR_WELLFOUND``), which bypasses DataDome/Cloudflare on
     Apify's own infrastructure (~$0.004-0.008/result). Input field names
     (``roles``, ``locations``, ``maxResults``) are taken from the actor's
     published documentation — **NOT live-verified against a real run**
     (no Apify token was available in the environment this plugin was
     built in). Output-field mapping uses the same resilient
     multi-candidate-key picker (`first`/`first_text`/`derive_ext_id`) every
     Apify plugin here uses specifically so a wrong guess degrades to a
     missing field rather than a crash — self-anneal the candidate key list
     against ``Job.extra`` the first time this actually runs.
  2. FALLBACK — a logged-in Playwright session (same
     ``PLAYWRIGHT_USER_DATA_DIR`` model as ``careerhound.py``; log in once,
     no credentials stored by this app, PLAN.md §6), used only when no Apify
     token is configured or the actor call fails/returns 0 rows. Wellfound's
     real DOM markup is behind DataDome + login and could not be inspected
     from this environment either, so this tier uses the same GENERIC
     href-pattern extraction as ``careerhound.py`` rather than unverifiable
     hardcoded selectors.

``is_available()`` is True if EITHER path is usable (an Apify token is
configured, OR a logged-in session dir exists) — the runner should not skip
this portal just because one tier is unconfigured.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _apify import actor_id, as_text, derive_ext_id, first, first_text, get_token, run_actor  # noqa: E402
from _joblister_util import matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_DEFAULT_ACTOR = "thirdwatch/wellfound-jobs-scraper"
_BASE = "https://wellfound.com"
_DEFAULT_SEARCH_URL = _BASE + "/jobs?q={query}"
_PAGE_WAIT_MS = 20000
_JOB_HREF_RE = re.compile(r"/(jobs|company/[^/]+/jobs)/", re.IGNORECASE)


def _to_job_apify(it: dict) -> Job | None:
    url = first(it, "jobUrl", "url", "applyUrl", "link")
    ext_id = derive_ext_id(it, url, "id", "jobId", "job_id", "slug")
    if ext_id is None:
        return None
    return Job(
        source="wellfound",
        ext_id=ext_id,
        url=as_text(url),
        title=first_text(it, "title", "jobTitle", "role"),
        company=first_text(it, "companyName", "company", "startupName"),
        location=first_text(it, "location", "locations", "jobLocation"),
        posted_at=first_text(it, "postedAt", "datePosted", "publishedAt"),
        jd_text=first_text(it, "description", "jobDescription", "descriptionText"),
        extra=it,
    )


def _fetch_via_apify(query: str, limit: int, location: str | None) -> list[Job]:
    actor = actor_id("APIFY_ACTOR_WELLFOUND", _DEFAULT_ACTOR)
    run_input = {
        "roles": [query] if query else [],
        "locations": [location] if location else ["remote"],
        "maxResults": max(1, int(limit)),
    }
    items = run_actor(actor, run_input, limit=limit)
    return [j for j in (_to_job_apify(it) for it in items) if j is not None]


def _search_url(query: str, location: str | None = None) -> str:
    template = os.environ.get("WELLFOUND_SEARCH_URL", _DEFAULT_SEARCH_URL)
    url = template.format(query=urllib.parse.quote(query))
    if location:
        # Best-effort — the real query-param name is unverified (login-gated
        # site, see module docstring); appending rather than silently
        # dropping the filter is still strictly better than ignoring it.
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}location={urllib.parse.quote(location)}"
    return url


def _to_job_session(href: str, title: str) -> Job | None:
    title = strip_html(title).strip()
    if not href or not title:
        return None
    url = href if href.startswith("http") else urllib.parse.urljoin(_BASE, href)
    return Job(
        source="wellfound",
        ext_id=url,  # no reliable id known for this unverified markup — full URL is the stable key
        url=url,
        title=title,
        company=None,
        location=None,
        posted_at=None,
        jd_text=None,
        extra={"href": href},
    )


def _fetch_via_session(query: str, limit: int, location: str | None = None) -> list[Job]:
    user_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
    if not user_dir or not os.path.isdir(user_dir):
        return []
    from playwright.sync_api import sync_playwright

    words = [w.lower() for w in query.split() if len(w) > 1]
    jobs: list[Job] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(user_dir, headless=True)
        try:
            page = ctx.new_page()
            page.goto(_search_url(query, location), timeout=_PAGE_WAIT_MS)
            page.wait_for_timeout(2000)  # let client-side data fetch settle
            seen: set[str] = set()
            for a in page.locator("a").all():
                href = a.get_attribute("href") or ""
                if not _JOB_HREF_RE.search(href) or href in seen:
                    continue
                seen.add(href)
                title = a.inner_text().strip()
                if words and not matches(title, words):
                    continue
                try:
                    job = _to_job_session(href, title)
                except Exception as exc:
                    print(f"  wellfound: skipping malformed card — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    jobs.append(job)
                if len(jobs) >= limit:
                    break
        finally:
            ctx.close()
    return jobs


class WellfoundPlugin(JobSourcePlugin):
    """Startup jobs from wellfound.com via an Apify actor, with a logged-in-session fallback."""

    name = "wellfound"
    base_url = "wellfound.com"
    mechanism = "apify"
    # Session fallback launches a PERSISTENT context on PLAYWRIGHT_USER_DATA_DIR —
    # same profile dir as careerhound.py. See base.py's uses_persistent_profile.
    uses_persistent_profile = True

    def is_available(self) -> bool:
        user_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
        has_session = bool(user_dir) and os.path.isdir(user_dir)
        return get_token() is not None or has_session

    def availability_detail(self) -> str:
        return "no APIFY_TOKEN & no PLAYWRIGHT_USER_DATA_DIR session"

    def fetch(self, query: str, limit: int = 25, *, location: str | None = None) -> list[Job]:
        if limit <= 0:
            return []

        if get_token() is not None:
            try:
                jobs = _fetch_via_apify(query, limit, location)
                if jobs:
                    return jobs
                print("  wellfound: Apify actor returned 0 rows — trying session fallback", file=sys.stderr)
            except Exception as exc:
                print(f"  wellfound: Apify actor failed ({exc}) — trying session fallback", file=sys.stderr)

        try:
            return _fetch_via_session(query, limit, location)
        except Exception as exc:
            print(f"  wellfound: session-driven fallback also failed — {exc}", file=sys.stderr)
            return []
