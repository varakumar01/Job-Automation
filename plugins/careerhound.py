"""CareerHound job-source plugin (session-gated Playwright — no token). PLAN.md §4/§9.

CareerHound (careerhound.io) is a paid job-aggregator whose job-search
functionality sits ENTIRELY behind a login wall — verified live 2026-07-10:
every plausible unauthenticated route (``/jobs``, ``/search``, ``/dashboard``,
``/app``, ``/preview``) 404s, and the public marketing homepage exposes no
job data or preview cards to inspect. There is no public API/RSS/sitemap to
fall back to either. This means, unlike every other plugin in this file,
**the real in-app markup could not be verified from this environment** — it
can only be seen by an authenticated user. This plugin follows the
``_custom_template.py`` session model (drive the user's OWN persistent
logged-in Chromium profile via ``PLAYWRIGHT_USER_DATA_DIR`` — no credentials
of any kind are stored by this app, PLAN.md §6) rather than
``_career_util.py``'s public-page model, and uses GENERIC structural
extraction (any in-page link whose href contains ``/job``) instead of
hardcoded CSS classes that can't be verified — a wrong guess at exact
selectors would silently return 0 rows forever with no error, whereas a
generic href-pattern match degrades more gracefully to noisy-but-nonempty
results if the real markup differs from what's assumed here.

**Required before first real use (self-anneal per SKILL.md):** log into
CareerHound once in the browser profile at ``PLAYWRIGHT_USER_DATA_DIR``,
then run a live smoke test (``scrape.py --source careerhound --query "..."
--limit 3``) and inspect ``Job.extra``/the console warnings this plugin
prints for any card it couldn't parse cleanly. Update ``_CARD_LINK_RE``/
``_search_url`` and this docstring's "verified" date once real selectors are
confirmed — do NOT assume the current heuristic is correct.

``CAREERHOUND_SEARCH_URL`` (optional env override) lets the search URL be
corrected without a code change once the real in-app route is known post-
login; defaults to a best-guess ``/jobs?q=<query>`` pattern (common for
Next.js job boards, unverified for this specific site).
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

from _career_util import playwright_available  # noqa: E402
from _joblister_util import matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_BASE = "https://www.careerhound.io"
_DEFAULT_SEARCH_URL = _BASE + "/jobs?q={query}"
_PAGE_WAIT_MS = 15000

# Generic "this looks like a job posting link" heuristic — any anchor whose
# href contains a /job or /jobs path segment, since the real card markup is
# unverified (site is login-gated, see module docstring).
_JOB_HREF_RE = re.compile(r"/jobs?/", re.IGNORECASE)


def _search_url(query: str) -> str:
    template = os.environ.get("CAREERHOUND_SEARCH_URL", _DEFAULT_SEARCH_URL)
    return template.format(query=urllib.parse.quote(query))


def _to_job(href: str, title: str) -> Job | None:
    title = strip_html(title).strip()
    if not href or not title:
        return None
    url = href if href.startswith("http") else urllib.parse.urljoin(_BASE, href)
    # No reliable numeric/slug id is known for this markup (unverified) — the
    # full URL itself is the most stable available key.
    ext_id = url
    return Job(
        source="careerhound",
        ext_id=ext_id,
        url=url,
        title=title,
        company=None,
        location=None,
        posted_at=None,
        jd_text=None,
        extra={"href": href},
    )


class CareerHoundPlugin(JobSourcePlugin):
    """Aggregated job listings from careerhound.io via the user's logged-in session."""

    name = "careerhound"
    base_url = "careerhound.io"
    mechanism = "browser"
    # Launches a PERSISTENT context on PLAYWRIGHT_USER_DATA_DIR — same profile
    # dir as wellfound.py's session fallback. See base.py's uses_persistent_profile.
    uses_persistent_profile = True

    def is_available(self) -> bool:
        """True if a persistent logged-in session dir exists AND chromium is
        actually installed (the latter is checked here, not just implied,
        so a missing `playwright install chromium` can't reach `fetch()` and
        raise an uncaught ImportError)."""
        user_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
        return bool(user_dir) and os.path.isdir(user_dir) and playwright_available()

    def availability_detail(self) -> str:
        user_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
        if not user_dir or not os.path.isdir(user_dir):
            return "no PLAYWRIGHT_USER_DATA_DIR logged-in session"
        return "no chromium (playwright install chromium)"

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        # No `location` kwarg: this plugin's real search-URL structure is
        # unverified (login-gated site, see module docstring), so it can't
        # honestly claim location support until that's confirmed live.
        if limit <= 0 or not self.is_available():
            return []

        words = [w.lower() for w in query.split() if len(w) > 1]
        user_dir = os.environ["PLAYWRIGHT_USER_DATA_DIR"]

        jobs: list[Job] = []
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(user_dir, headless=True)
                try:
                    page = ctx.new_page()
                    page.goto(_search_url(query), timeout=_PAGE_WAIT_MS)
                    page.wait_for_timeout(2000)  # let client-side data fetch settle
                    anchors = page.locator("a").all()
                    seen: set[str] = set()
                    for a in anchors:
                        href = a.get_attribute("href") or ""
                        if not _JOB_HREF_RE.search(href) or href in seen:
                            continue
                        seen.add(href)
                        title = a.inner_text().strip()
                        blob = title
                        if words and not matches(blob, words):
                            continue
                        try:
                            job = _to_job(href, title)
                        except Exception as exc:
                            print(f"  careerhound: skipping malformed card — {exc}", file=sys.stderr)
                            continue
                        if job is not None:
                            jobs.append(job)
                        if len(jobs) >= limit:
                            break
                finally:
                    ctx.close()
        except Exception as exc:
            print(f"  careerhound: session-driven fetch failed — {exc}", file=sys.stderr)
            return []
        return jobs
