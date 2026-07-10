"""Naukri job-source plugin (Apify-backed). See PLAN.md §4.

Actor: ``muhammetakkurtt/naukri-job-scraper`` (pay-per-event). Input takes a
``keyword`` + ``maxJobs`` cap. ``fetchDetails=True`` so each job carries its full
description (``jd_text``) which jd-understander downstream needs — this charges
the actor's "Detailed Job Data" event instead of "Standard"; set
``APIFY_NAUKRI_DETAILS=0`` to fall back to standard/cheaper (no jd_text).
Override the actor via ``APIFY_ACTOR_NAUKRI``.

Output mapping is resilient and was verified live on 2026-06-30.
"""

from __future__ import annotations

import os
import re
import sys

from base import Job, JobSourcePlugin
from _apify import actor_id, as_text, derive_ext_id, first, first_text, get_token, run_actor

DEFAULT_ACTOR = "muhammetakkurtt/naukri-job-scraper"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _search_url(query: str, location: str) -> str:
    # Naukri search pattern: /<keyword>-jobs-in-<location>. The actor's `cities`
    # field needs internal numeric codes, so we encode the text location here.
    return f"https://www.naukri.com/{_slug(query)}-jobs-in-{_slug(location)}"


def _details_on() -> bool:
    return os.environ.get("APIFY_NAUKRI_DETAILS", "1") not in ("0", "false", "False")


class NaukriPlugin(JobSourcePlugin):
    name = "naukri"
    base_url = "naukri.com"
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
        actor = actor_id("APIFY_ACTOR_NAUKRI", DEFAULT_ACTOR)
        if int(limit) < 50:
            print(
                f"  ⚠ naukri: actor minimum is 50 jobs/run — it will produce and "
                f"CHARGE for ~50 even though only {limit} will be stored "
                f"(spend capped by APIFY_MAX_CHARGE_USD).",
                file=sys.stderr,
            )
        run_input = {
            # Actor requires maxJobs >= 50. run_actor reads only `limit` rows, but the
            # actor PRODUCES and CHARGES for the full maxJobs. Real spend ceiling is
            # APIFY_MAX_CHARGE_USD ($0.50 default).
            "maxJobs": max(50, int(limit)),
            "fetchDetails": _details_on(),
        }
        if location:
            run_input["searchUrl"] = _search_url(query, location)
        else:
            run_input["keyword"] = query
        # token=None → run_actor rotates over all configured keys by health
        # (see _apify_keys); the get_token() guard above is just the early
        # "nothing configured" check.
        items = run_actor(actor, run_input, limit=limit)
        return [j for j in (self._to_job(it) for it in items) if j is not None]

    @staticmethod
    def _to_job(it: dict) -> Job | None:
        # Detailed mode nests the real job under `jobDetails`; merge so we read
        # nested keys first but still work if a future/standard item is flat.
        jd = it.get("jobDetails")
        src = {**it, **jd} if isinstance(jd, dict) else it
        url = first(src, "staticUrl", "jobUrl", "jdURL", "applyRedirectUrl", "url")
        ext_id = derive_ext_id(src, url, "jobId", "id", "jobIdEncrypted")
        if ext_id is None:
            return None
        return Job(
            source="naukri",
            ext_id=ext_id,
            url=as_text(url),
            title=first_text(src, "title", "jobTitle", "jobRole", "designation"),
            company=first_text(src, "companyDetail", "companyName", "staticCompanyName", "company"),
            location=first_text(src, "locations", "location", "jobLocation"),
            posted_at=first_text(src, "createdDate", "postedDate", "postedAt"),
            jd_text=first_text(src, "description", "jobDescription", "jdText"),
            extra=it,
        )
