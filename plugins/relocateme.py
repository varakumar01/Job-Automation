"""Relocate.me job-source plugin (sitemap → JobPosting ld+json — no token). PLAN.md §4.

No RSS/JSON API is exposed (verified live 2026-07-10: ``/jobs.rss`` and
``/feed`` both 404). Job URLs are enumerated from ``/sitemap.xml`` (a flat
``<urlset>`` mixing job postings, tax-calculator pages, and other content) —
filtered to the ``/{country}/{city}/{company}/{slug}-{numeric-id}`` shape via
a trailing-digits check on the URL path, since that's the only reliable
structural marker separating job postings from the sitemap's other URLs.
Each job page is then fetched as plain HTML (Tier 1 — no Playwright render
needed; fully server-rendered).

Field-shape facts (verified live 2026-07-10, 3 job pages):
  - The page embeds a REAL ``JobPosting`` ld+json block (title, hiring
    organization, location, description, datePosted) alongside an unrelated
    ``BreadcrumbList`` block.
  - That ``JobPosting`` block failed to ``json.loads`` under strict mode —
    its ``description`` string contains a literal (unescaped) newline/tab
    instead of ``\\n``/``\\t``, invalid strict JSON but a real markup quirk.
    Fixed generically in ``_career_util.extract_ld_json`` (now parses with
    ``strict=False``) rather than special-cased here — this plugin was the
    one that surfaced the bug, but the fix benefits any future ld+json
    consumer.
  - ``title`` and ``description`` are DOUBLE-HTML-escaped: literal ``<b>``/
    ``<br/>`` tags sit next to already-escaped ``&lt;p&gt;``/``&amp;nbsp;``
    remnants in the same string. A single ``strip_html`` call (which strips
    real tags THEN unescapes once) would leave the double-escaped portions
    as visible literal ``<p>`` text. Fixed by unescaping TWICE before
    stripping (``html.unescape`` is idempotent on already-plain text, so
    this is harmless for a normally-escaped block too).
  - ``title`` also carries a trailing ``" | Relocation Offered"`` suffix —
    left as-is (accurate, not noise) rather than stripped.
  - Location: ``jobLocation.address.addressLocality`` + ``.addressCountry``
    (schema.org ``PostalAddress``), joined ``"City Country"``.
  - ``ext_id`` is the URL's trailing ``<slug>-<numeric-id>`` segment (unique,
    stable) via ``job_id_from_url``.

No Apify dependency — stdlib ``urllib``. Always available (``is_available``
returns True); if the site is unreachable, ``fetch`` returns an empty list.
"""

from __future__ import annotations

import html as html_module
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _career_util import extract_ld_json, fetch_html, job_id_from_url, parse_sitemap  # noqa: E402
from _joblister_util import matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_SITEMAP = "https://relocate.me/sitemap.xml"
_JOB_URL_RE = re.compile(r"-\d+$")  # trailing numeric id — the only marker distinguishing job pages
_PAGE_DELAY_SECS = 1.0  # polite delay between per-job-page fetches (PLAN.md §6)


def _clean(raw: str | None) -> str | None:
    """Undo relocate.me's double HTML-escaping, then strip any real tags."""
    if not raw:
        return None
    return strip_html(html_module.unescape(html_module.unescape(raw))) or None


def _candidate_job_urls(limit_pool: int) -> list[str]:
    xml_text = fetch_html(_SITEMAP)
    urls = parse_sitemap(xml_text)
    return [u for u in urls if _JOB_URL_RE.search(urlsplit(u).path)][:limit_pool]


def _to_job(url: str, html: str) -> Job | None:
    ld = extract_ld_json(html, ld_type="JobPosting")
    if not ld:
        return None
    posting = ld[0]
    org = posting.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else None
    job_location = posting.get("jobLocation")
    address = job_location.get("address") if isinstance(job_location, dict) else None
    location = None
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressCountry")]
        location = " ".join(p for p in parts if p) or None
    return Job(
        source="relocateme",
        ext_id=job_id_from_url(url),
        url=url,
        title=_clean(posting.get("title")),
        company=company,
        location=location,
        posted_at=posting.get("datePosted"),
        jd_text=_clean(posting.get("description")),
        extra=posting,
    )


class RelocateMePlugin(JobSourcePlugin):
    """Relocation-sponsoring tech jobs from relocate.me via sitemap + JobPosting ld+json."""

    name = "relocateme"
    base_url = "relocate.me"
    mechanism = "html"

    def is_available(self) -> bool:
        return True  # no token required

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            candidates = _candidate_job_urls(limit_pool=max(limit * 6, 30))
        except Exception as exc:
            print(f"  relocateme: sitemap fetch failed — {exc}", file=sys.stderr)
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
            except Exception as exc:
                print(f"  relocateme: page fetch failed for {url} — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
        return jobs
