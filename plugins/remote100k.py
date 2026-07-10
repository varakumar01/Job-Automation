"""Remote100k job-source plugin (sitemap → JobPosting ld+json — no token). PLAN.md §4.

No RSS/JSON API is exposed (verified live 2026-07-10: ``/jobs.rss`` 404s;
``robots.txt`` explicitly ``Disallow: /api/`` — an internal API exists but is
NOT to be hit). Job URLs are enumerated from the site's own
``/sitemap.xml`` instead (a flat ``<urlset>``, ~650 ``/remote-job/<slug>``
entries out of ~1500 total URLs), then each job page is fetched as plain
HTML (Tier 1 — no Playwright render needed; the ``JobPosting`` ld+json block
is already present in the server-rendered response).

Field-shape facts (verified live 2026-07-10):
  - ``extract_ld_json(html, ld_type="JobPosting")`` reliably yields exactly
    one block per job page: ``title``, ``hiringOrganization.name``,
    ``datePosted``, ``applicantLocationRequirements.name``.
  - **``description`` is a short one-line teaser, NOT the full job
    description** — Remote100k is a curated aggregator that links out to the
    employer's real posting rather than hosting the JD itself. ``jd_text``
    here is that teaser only; do not expect résumé-tailoring-grade detail
    from it.
  - The page's own external "Apply for This Job" link — matched by its
    Remote100k-specific ``?ref=remote100k`` tracking param (falling back to
    "first external href that isn't a known asset/tracking host" only if no
    such link is found) — points to the ACTUAL employer posting (seen live:
    Greenhouse, Workday, Ashby, ...) — used as ``Job.url`` in place of the
    Remote100k teaser page, since it's far more useful to a human (or a
    downstream skill that re-fetches the JD) than a page with no real JD
    text. The raw ``href="..."`` attribute value is HTML-escaped (``&amp;``
    for a literal ``&`` in a multi-param query string, e.g.
    ``?gh_jid=123&amp;ref=remote100k``) — unescaped via ``html.unescape``
    before use, else the stored URL's query string would be literally wrong.
  - ``ext_id`` is the sitemap URL's trailing slug (unique, stable) via
    ``job_id_from_url``.
  - The site's own ld+json embeds already-HTML-escaped text INSIDE the JSON
    string values (e.g. a job literally titled "... FP&A ..." is JSON-encoded
    as ``"... FP\\u0026amp;A ..."``, not the plain ampersand) — a template
    quirk on their end, not a JSON-LD spec violation. ``title``/``company``/
    ``jd_text`` are run through ``html.unescape`` after JSON parsing to
    undo it; skipped for ``location`` (schema.org country names never
    contain markup-sensitive characters in practice).

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the site is unreachable, ``fetch`` returns an empty list.
"""

from __future__ import annotations

import html as html_module
import re
import sys
import time
from pathlib import Path
from urllib.error import URLError

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _career_util import (  # noqa: E402
    extract_ld_json,
    fetch_html,
    job_id_from_url,
    parse_sitemap,
)
from _joblister_util import matches  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_SITEMAP = "https://remote100k.com/sitemap.xml"
_JOB_PATH = "/remote-job/"
_PAGE_DELAY_SECS = 1.0  # polite delay between per-job-page fetches (PLAN.md §6)

# Every real apply link seen live (Greenhouse, Workday, Ashby) carries this
# Remote100k-specific tracking param — a far more reliable signal than "first
# external link", which could match a company-website/social/GitHub link
# appearing anywhere else in the page before the real apply button. The
# separator before "ref=" is a literal "&" only when this is the first query
# param; when a preceding param exists (e.g. "?gh_jid=123&ref=remote100k")
# the raw HTML attribute has it HTML-escaped as "&amp;ref=", not "&ref=" —
# both forms are matched here (found live: the Pinterest/Ashby examples use
# the escaped form, so matching only a literal "&" silently fell through to
# the weaker fallback heuristic below on every multi-param apply URL).
_APPLY_REF_RE = re.compile(r'href="(https?://[^"]*(?:[?&]|&amp;)ref=remote100k[^"]*)"')

# Fallback only (no ?ref=remote100k link found): hosts seen in a job page's
# external links that are NOT the employer's apply link (trackers/assets/
# social) — anything else external is treated as the real posting URL.
_NON_APPLY_HOST_RE = re.compile(
    r"^https?://(remote100k\.com|www\.googletagmanager\.com|d26rorylz13nfc\.cloudfront\.net|www\.linkedin\.com)"
)
_EXTERNAL_HREF_RE = re.compile(r'href="(https?://[^"]+)"')


def _candidate_job_urls(limit_pool: int) -> list[str]:
    xml_text = fetch_html(_SITEMAP)
    urls = parse_sitemap(xml_text)
    return [u for u in urls if _JOB_PATH in u][:limit_pool]


def _find_apply_url(html: str) -> str | None:
    m = _APPLY_REF_RE.search(html)
    if m:
        return html_module.unescape(m.group(1))
    for href in _EXTERNAL_HREF_RE.findall(html):
        if not _NON_APPLY_HOST_RE.match(href):
            return html_module.unescape(href)
    return None


def _to_job(url: str, html: str) -> Job | None:
    ld = extract_ld_json(html, ld_type="JobPosting")
    if not ld:
        return None
    posting = ld[0]
    org = posting.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else None
    loc = posting.get("applicantLocationRequirements")
    location = loc.get("name") if isinstance(loc, dict) else None
    apply_url = _find_apply_url(html)
    title = posting.get("title")
    description = posting.get("description")
    return Job(
        source="remote100k",
        ext_id=job_id_from_url(url),
        url=apply_url or url,
        title=html_module.unescape(title) if title else None,
        company=html_module.unescape(company) if company else None,
        location=location,
        posted_at=posting.get("datePosted"),
        jd_text=html_module.unescape(description) if description else None,
        extra=posting,
    )


class Remote100kPlugin(JobSourcePlugin):
    """$100k+ remote jobs from remote100k.com via sitemap + JobPosting ld+json."""

    name = "remote100k"
    base_url = "remote100k.com"
    mechanism = "html"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            # Over-fetch the URL pool since the slug-only keyword filter below
            # is coarser than a real title/description match.
            candidates = _candidate_job_urls(limit_pool=max(limit * 6, 30))
        except Exception as exc:
            print(f"  remote100k: sitemap fetch failed — {exc}", file=sys.stderr)
            return []

        if words:
            candidates = [u for u in candidates if matches(u.replace("-", " "), words)]

        jobs: list[Job] = []
        for i, url in enumerate(candidates):
            if len(jobs) >= limit:
                break
            if i > 0:
                time.sleep(_PAGE_DELAY_SECS)
            try:
                html = fetch_html(url)
                job = _to_job(url, html)
            except (URLError, OSError) as exc:
                print(f"  remote100k: page fetch failed for {url} — {exc}", file=sys.stderr)
                continue
            except Exception as exc:
                print(f"  remote100k: skipping malformed page {url} — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
        return jobs
