"""LinkedIn job-source plugin (Apify-backed). See PLAN.md §4.

Actor: ``curious_coder/linkedin-jobs-scraper`` (pay-per-event, ~$0.001/result).
Its input takes LinkedIn *search URLs* + a ``count`` cap, so this adapter builds
a jobs-search URL from the query/location and maps the actor output onto the
normalized :class:`Job` schema. Override the actor via ``APIFY_ACTOR_LINKEDIN``.

Output field mapping is resilient (tries several key names) and was verified
against a live run on 2026-06-30; if LinkedIn/the actor changes its output,
extend the candidate-key lists below and note the date.
"""

from __future__ import annotations

import os
import urllib.parse

from base import Job, JobSourcePlugin
from _apify import actor_id, as_text, derive_ext_id, first, first_text, get_token, run_actor

DEFAULT_ACTOR = "curious_coder/linkedin-jobs-scraper"


def _posted_days() -> int | None:
    """Recency window in days from LINKEDIN_POSTED_DAYS env (e.g. 7 = last week)."""
    raw = (os.environ.get("LINKEDIN_POSTED_DAYS") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _search_url(query: str, location: str | None, posted_days: int | None) -> str:
    params = {"keywords": query}
    if location:
        params["location"] = location
    if posted_days:
        # LinkedIn date-posted filter: f_TPR=r<seconds>. Also sort newest-first so
        # the scrape (and downstream apply order) runs recent → older.
        params["f_TPR"] = f"r{posted_days * 86400}"
        params["sortBy"] = "DD"  # date descending
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


class LinkedInPlugin(JobSourcePlugin):
    name = "linkedin"
    base_url = "linkedin.com"
    mechanism = "apify"

    def is_available(self) -> bool:
        return get_token() is not None

    def availability_detail(self) -> str:
        return "no APIFY_TOKEN in .env"

    def fetch(self, query: str, limit: int = 25, *, location: str | None = None) -> list[Job]:
        # Defensive early-out: is_available() already gates the runner, but a
        # direct fetch() with no key configured returns empty rather than raising.
        if get_token() is None:
            return []
        actor = actor_id("APIFY_ACTOR_LINKEDIN", DEFAULT_ACTOR)
        run_input = {
            "urls": [_search_url(query, location, _posted_days())],
            # Actor requires count >= 10 and may overshoot it (scrapes whole pages);
            # run_actor reads only `limit` rows but the actor can PRODUCE and CHARGE
            # for more. Real spend ceiling is APIFY_MAX_CHARGE_USD ($0.50 default).
            "count": max(10, int(limit)),
            "scrapeCompany": False,  # job fields only — cheaper/faster
        }
        # token=None → run_actor rotates over all configured keys by health
        # (see _apify_keys); the get_token() guard above is just the early
        # "nothing configured" check.
        items = run_actor(actor, run_input, limit=limit)
        return [j for j in (self._to_job(it) for it in items) if j is not None]

    @staticmethod
    def _to_job(it: dict) -> Job | None:
        url = first(it, "link", "jobUrl", "url", "jobPostingUrl")
        ext_id = derive_ext_id(it, url, "id", "jobId", "jobPostingId", "entityUrn")
        if ext_id is None:
            return None
        return Job(
            source="linkedin",
            ext_id=ext_id,
            url=as_text(url),
            title=first_text(it, "title", "jobTitle"),
            company=first_text(it, "companyName", "company", "companyName.name"),
            location=first_text(it, "location", "formattedLocation", "jobLocation"),
            posted_at=first_text(it, "postedAt", "listedAt", "postedTime", "publishedAt"),
            jd_text=first_text(it, "descriptionText", "description", "jobDescription", "descriptionHtml"),
            extra=it,
        )
