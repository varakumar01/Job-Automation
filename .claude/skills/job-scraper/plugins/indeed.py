"""Indeed job-source plugin (Apify-backed). See PLAN.md §4.

Actor: ``borderline/indeed-scraper`` (pay-per-event, ~$0.005/job). Clean
query/location input with a ``maxRows`` cap. Override via ``APIFY_ACTOR_INDEED``.
``country`` defaults to ``in`` (India) to match the user's market; override with
``APIFY_INDEED_COUNTRY``.

Output mapping is resilient and was verified live on 2026-06-30.
"""

from __future__ import annotations

import os

from base import Job, JobSourcePlugin
from _apify import actor_id, as_text, derive_ext_id, first, first_text, get_token, run_actor

DEFAULT_ACTOR = "borderline/indeed-scraper"


class IndeedPlugin(JobSourcePlugin):
    name = "indeed"

    def is_available(self) -> bool:
        return get_token() is not None

    def fetch(self, query: str, limit: int = 25, *, location: str | None = None) -> list[Job]:
        # Defensive early-out: is_available() already gates the runner, but a
        # direct fetch() with no key configured returns empty rather than raising.
        if get_token() is None:
            return []
        actor = actor_id("APIFY_ACTOR_INDEED", DEFAULT_ACTOR)
        run_input = {
            "query": query,
            "maxRows": max(1, int(limit)),
            "country": os.environ.get("APIFY_INDEED_COUNTRY", "in"),
        }
        if location:
            run_input["location"] = location
        # token=None → run_actor rotates over all configured keys by health
        # (see _apify_keys); the get_token() guard above is just the early
        # "nothing configured" check.
        items = run_actor(actor, run_input, limit=limit)
        return [j for j in (self._to_job(it) for it in items) if j is not None]

    @staticmethod
    def _to_job(it: dict) -> Job | None:
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
