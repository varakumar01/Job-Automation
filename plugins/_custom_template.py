"""TEMPLATE — custom (non-Apify) job-source plugin using a Playwright session.

This file is a scaffold, not a live plugin: the leading underscore makes the
registry skip it. To add a portal Apify doesn't cover:

  1. Copy this file to ``<site>.py`` (no leading underscore).
  2. Rename the class and set a unique ``name`` (also stored as ``jobs.source``).
  3. Implement ``is_available`` (is the logged-in session present?) and
     ``fetch`` (drive the page, normalize rows to :class:`Job`).
  4. ``python3 plugins/registry.py`` should now list it; then live-smoke it.

It drives the user's OWN logged-in browser session from
``PLAYWRIGHT_USER_DATA_DIR`` (PLAN.md §6 — no credentials are stored by this
app). Run ``playwright install chromium`` once before first use.
"""

from __future__ import annotations

import os

from base import Job, JobSourcePlugin


class CustomSitePlugin(JobSourcePlugin):
    # Rename this to your portal's id when you copy the file to <site>.py. A
    # non-empty name is required (base enforces it at class-definition time), so
    # the template ships with a placeholder to stay importable for reference.
    name = "examplesite"

    def is_available(self) -> bool:
        """True if a persistent logged-in session dir exists for this portal."""
        user_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
        return bool(user_dir) and os.path.isdir(user_dir)

    def fetch(self, query: str, limit: int = 25, *, location: str | None = None) -> list[Job]:
        """Search the portal with a logged-in Playwright context; normalize rows.

        Skeleton (uncomment + adapt; keep within PLAN.md §6 rate limits):

            from playwright.sync_api import sync_playwright

            jobs: list[Job] = []
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    os.environ["PLAYWRIGHT_USER_DATA_DIR"], headless=True
                )
                page = ctx.new_page()
                page.goto("https://example.com/jobs?q=" + query)
                for card in page.locator(".job-card").all()[:limit]:
                    jobs.append(Job(
                        source=self.name,
                        ext_id=card.get_attribute("data-job-id"),
                        url=card.locator("a").get_attribute("href"),
                        title=card.locator(".title").inner_text(),
                        company=card.locator(".company").inner_text(),
                        location=card.locator(".loc").inner_text(),
                        jd_text=None,  # open the posting for the full JD if needed
                    ))
                    page.wait_for_timeout(1500)  # polite delay between actions
                ctx.close()
            return jobs
        """
        raise NotImplementedError("copy this template to <site>.py and implement fetch()")
