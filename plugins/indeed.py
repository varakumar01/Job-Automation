"""Indeed job-source plugin — browser-first, Apify fallback. See PLAN.md §4/§9.

Reworked 2026-07-10 (user decision, PLAN.md §9): PRIMARY fetch is now a
headless-browser scrape (no Apify cost); the original Apify actor
(``borderline/indeed-scraper``) is kept as a FALLBACK for when the browser
path is unconfigured, errors, or is blocked and returns 0 rows.

**Browser path (primary):** Indeed blocks a plain (non-JS) request outright
(verified live 2026-07-10: ``fetch_html`` on a search URL gets HTTP 403), but
a Playwright render succeeds once ``_career_util.render_html``'s default UA
masks the headless fingerprint (see that function's docstring) — Indeed's
anti-bot did not block the rendered request in this test. The rendered page
embeds its search results as a JS object literal assigned to
``providerData["mosaic-provider-jobcards"]`` inside a ``<script>`` tag — NOT
one of the existing tier-2 shapes (``__NEXT_DATA__``/``ld+json``/a
``window.<name> =`` assignment), and NOT reliably regex-extractable either,
since the object is large and deeply nested (a naive non-greedy
``\\{.*?\\}`` stops at the first inner closing brace). Extracted here instead
via ``json.JSONDecoder().raw_decode(html, idx)`` starting right after the
``providerData["mosaic-provider-jobcards"]=`` marker, which correctly parses
one complete JSON value and ignores any trailing script content after it.

Field-shape facts (verified live 2026-07-10, query "engineer", 24 results):
  the real card list is at ``metaData.mosaicProviderJobCardsModel.results``.
  Per card: ``jobkey`` (stable id), ``viewJobLink`` (relative detail-page
  URL), ``company``, ``displayTitle``, ``formattedLocation``, ``createDate``
  (epoch MILLISECONDS, not seconds — divided by 1000 before
  ``epoch_to_iso``), ``snippet`` (short HTML teaser, not the full JD — same
  "aggregator teaser" caveat as ``remote100k.py``).

**Apify path (fallback):** unchanged from the original implementation —
actor ``borderline/indeed-scraper`` (pay-per-event, ~$0.005/job), overridable
via ``APIFY_ACTOR_INDEED``; ``country`` defaults to ``in`` (India), override
with ``APIFY_INDEED_COUNTRY``. Output mapping verified live 2026-06-30.

Toggles (``.env``): ``INDEED_USE_BROWSER`` (default ``1`` — set ``0`` to skip
straight to Apify), ``INDEED_APIFY_FALLBACK`` (default ``1`` — set ``0`` to
disable the fallback entirely, e.g. to avoid any Apify spend on this portal).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _apify import actor_id, as_text, derive_ext_id, first, first_text, get_token, run_actor  # noqa: E402
from _career_util import playwright_available, render_html  # noqa: E402
from _joblister_util import epoch_to_iso, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_DEFAULT_ACTOR = "borderline/indeed-scraper"
_BASE = "https://www.indeed.com"
_PROVIDER_MARKER = 'providerData["mosaic-provider-jobcards"]='


def _search_url(query: str, location: str | None) -> str:
    params = {"q": query}
    if location:
        params["l"] = location
    return f"{_BASE}/jobs?{urllib.parse.urlencode(params)}"


def _extract_jobcards(html: str) -> list[dict]:
    """Pull the search-results list out of the ``providerData[...]=`` blob.

    Uses ``JSONDecoder.raw_decode`` (not a regex) because the object is deep
    enough that a non-greedy brace-matching regex would truncate early."""
    idx = html.find(_PROVIDER_MARKER)
    if idx == -1:
        return []
    start = idx + len(_PROVIDER_MARKER)
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start)
    except (json.JSONDecodeError, ValueError):
        return []
    model = (data.get("metaData") or {}).get("mosaicProviderJobCardsModel") or {}
    results = model.get("results")
    return results if isinstance(results, list) else []


def _to_job_browser(item: dict) -> Job | None:
    jobkey = item.get("jobkey")
    if not jobkey:
        return None
    view_link = item.get("viewJobLink")
    url = urllib.parse.urljoin(_BASE, view_link) if view_link else None
    create_date = item.get("createDate")
    posted_at = epoch_to_iso(create_date / 1000) if isinstance(create_date, (int, float)) else None
    return Job(
        source="indeed",
        ext_id=jobkey,
        url=url,
        title=item.get("displayTitle"),
        company=item.get("company"),
        location=item.get("formattedLocation"),
        posted_at=posted_at,
        jd_text=strip_html(item.get("snippet") or "") or None,
        extra=item,
    )


def _fetch_via_browser(query: str, limit: int, location: str | None) -> list[Job]:
    html = render_html(_search_url(query, location), wait_selector="body", timeout_ms=20000)
    items = _extract_jobcards(html)
    jobs: list[Job] = []
    for item in items:
        try:
            job = _to_job_browser(item)
        except Exception as exc:
            print(f"  indeed: skipping malformed browser card — {exc}", file=sys.stderr)
            continue
        if job is not None:
            jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def _to_job_apify(it: dict) -> Job | None:
    url = first(it, "jobUrl", "url", "applyUrl", "link")
    ext_id = derive_ext_id(it, url, "jobKey", "jobkey", "id", "jobId")
    if ext_id is None:
        return None
    return Job(
        source="indeed",
        ext_id=ext_id,
        url=as_text(url),
        title=first_text(it, "title", "positionName", "jobTitle"),
        company=first_text(it, "companyName", "company"),
        # `location` is a dict (formattedAddress/city/country) — first_text flattens it.
        location=first_text(it, "location", "formattedLocation", "jobLocation"),
        posted_at=first_text(it, "datePublished", "postedAt", "date", "age"),
        jd_text=first_text(it, "descriptionText", "description", "jobDescription", "snippet"),
        extra=it,
    )


def _fetch_via_apify(query: str, limit: int, location: str | None) -> list[Job]:
    if get_token() is None:
        return []
    actor = actor_id("APIFY_ACTOR_INDEED", _DEFAULT_ACTOR)
    run_input = {
        "query": query,
        "maxRows": max(1, int(limit)),
        "country": os.environ.get("APIFY_INDEED_COUNTRY", "in"),
    }
    if location:
        run_input["location"] = location
    items = run_actor(actor, run_input, limit=limit)
    return [j for j in (_to_job_apify(it) for it in items) if j is not None]


def _flag_enabled(env_var: str, default: str = "1") -> bool:
    return os.environ.get(env_var, default) != "0"


class IndeedPlugin(JobSourcePlugin):
    name = "indeed"
    base_url = "indeed.com"
    mechanism = "browser"

    def is_available(self) -> bool:
        return playwright_available() or get_token() is not None

    def availability_detail(self) -> str:
        return "no chromium (playwright install chromium) & no APIFY_TOKEN"

    def fetch(self, query: str, limit: int = 25, *, location: str | None = None) -> list[Job]:
        if limit <= 0:
            return []

        if _flag_enabled("INDEED_USE_BROWSER") and playwright_available():
            try:
                jobs = _fetch_via_browser(query, limit, location)
                if jobs:
                    return jobs
                print("  indeed: browser path returned 0 rows — trying Apify fallback", file=sys.stderr)
            except Exception as exc:
                print(f"  indeed: browser path failed ({exc}) — trying Apify fallback", file=sys.stderr)

        if _flag_enabled("INDEED_APIFY_FALLBACK"):
            try:
                return _fetch_via_apify(query, limit, location)
            except Exception as exc:
                print(f"  indeed: Apify fallback failed — {exc}", file=sys.stderr)
        return []
