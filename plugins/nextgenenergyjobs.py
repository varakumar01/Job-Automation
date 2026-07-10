"""NextGen Energy Jobs job-source plugin (sitemap index → rendered pages). PLAN.md §4.

No RSS/JSON API is exposed (verified live 2026-07-10: ``/jobs.rss`` returns
the homepage, not a feed). Job URLs are enumerated via a sitemap INDEX (not a
flat sitemap like ``remote100k.py``): ``/sitemap.xml`` fans out to several
child sitemaps, of which ``job_openings_1.xml``/``job_openings_2.xml`` list
the actual postings (~3000 URLs each) — the others (``companies``,
``countries``, ``cities``, ``job_titles``, ...) are filter-page sitemaps, not
postings, and are skipped.

Each job page requires a Tier-3 headless render (Next.js App Router; job data
streams in via React Server Component payload chunks, NOT a ``__NEXT_DATA__``
blob or ``application/ld+json`` script — the only ld+json present is an
unrelated ``BreadcrumbList``). Verified live 2026-07-10: the site's WAF
returns a bare "Forbidden" body to Playwright's DEFAULT headless fingerprint
(``HeadlessChrome/...`` in the UA string) even though a plain non-JS
``fetch_html`` request succeeds — this is what motivated giving
``_career_util.render_html`` a realistic default desktop-Chrome UA (see that
function's docstring); this plugin depends on that fix and does not need to
pass its own ``user_agent`` override.

Field-shape facts (verified live 2026-07-10, 2 job pages): the rendered DOM
has no ``itemprop``/microdata, so fields are pulled from a bounded window
after the page's one ``<h1>`` (title/company/location/type/posted/salary all
live within the first ~4000 chars after ``<h1>`` — well before the "Job
Description" section, avoiding accidental matches from "More jobs at
{Company}" / "Similar Jobs" listings further down the same page):
  - Title: plain ``<h1>`` text.
  - Company: the ``<a href="/company/<slug>">`` link right after the title,
    text before its trailing external-link icon ``<svg>``.
  - Location / employment type / posted date / salary: each preceded by a
    distinct ``lucide-<icon>`` SVG class (``map-pin``/``briefcase``/
    ``clock``/``banknote``) followed by ``</svg><span>...</span>`` — matched
    on the icon class name rather than a fixed DOM position, since it's the
    one stable marker across postings. ``posted`` includes a literal
    "Posted " prefix, stripped here.
  - Full JD: the ``<div class="prose ...">`` block under the "Job
    Description" heading. Extracted by scanning forward from that div's
    opening tag and cutting at the first later occurrence of a KNOWN
    following-section marker ("Ready to Apply?", "Similar Jobs", ...) —
    same best-effort trailing-boilerplate-trim approach as ``synopsys.py``,
    since the prose block can itself contain nested tags that make an exact
    closing-``</div>`` match unreliable via regex.
  - "Apply Now" links to an internal ``/api/apply/<uuid>?src=organic``
    redirect, not the employer's URL directly — NOT resolved here (would
    need to follow the redirect, another fetch, more fragility); ``Job.url``
    stays the NextGen Energy Jobs page itself.

``is_available`` gates on ``playwright_available()`` (a public page, no
login involved — same model as ``synopsys.py``, not the login-session model
in ``_custom_template.py``).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _career_util import (  # noqa: E402
    fetch_html,
    job_id_from_url,
    parse_sitemap,
    parse_sitemap_index,
    playwright_available,
    render_html,
)
from _joblister_util import matches, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_SITEMAP_INDEX = "https://nextgenenergyjobs.com/sitemap.xml"
_JOB_SITEMAP_PREFIX = "job_openings_"
_PAGE_DELAY_SECS = 1.5  # polite delay between per-job renders (PLAN.md §6) — renders are heavier than plain fetches
_HEADER_WINDOW = 4000  # chars after <h1> that reliably contain title/company/location/type/posted/salary

_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
_COMPANY_RE = re.compile(r'<a\b[^>]*?href="/company/[^"]+"[^>]*>(.*?)<svg', re.DOTALL)
_LOCATION_RE = re.compile(r'lucide-map-pin[^"]*"[^>]*>.*?</svg><span>(.*?)</span>', re.DOTALL)
_TYPE_RE = re.compile(r'lucide-briefcase[^"]*"[^>]*>.*?</svg><span>(.*?)</span>', re.DOTALL)
_POSTED_RE = re.compile(r'lucide-clock[^"]*"[^>]*>.*?</svg><span>(.*?)</span>', re.DOTALL)
_SALARY_RE = re.compile(r'lucide-banknote[^"]*"[^>]*>.*?</svg><span>(.*?)</span>', re.DOTALL)
_PROSE_MARKER = 'class="prose'
_JD_TRAILING_MARKERS = ("Ready to Apply?", "Stay Updated on", "Similar Jobs", "More at ", "More jobs at")


def _candidate_job_urls(limit_pool: int) -> list[str]:
    index_xml = fetch_html(_SITEMAP_INDEX)
    children = parse_sitemap_index(index_xml)
    urls: list[str] = []
    for child_url in children:
        if _JOB_SITEMAP_PREFIX not in child_url:
            continue
        try:
            child_xml = fetch_html(child_url)
        except Exception as exc:
            print(f"  nextgenenergyjobs: child sitemap fetch failed for {child_url} — {exc}", file=sys.stderr)
            continue
        urls.extend(parse_sitemap(child_xml))
        if len(urls) >= limit_pool:
            break
    return urls[:limit_pool]


def _extract_jd(html: str) -> str | None:
    idx = html.find(_PROSE_MARKER)
    if idx == -1:
        return None
    tag_end = html.find(">", idx)
    if tag_end == -1:
        return None
    text = strip_html(html[tag_end + 1 :])
    cut = len(text)
    for marker in _JD_TRAILING_MARKERS:
        m_idx = text.find(marker)
        if m_idx != -1:
            cut = min(cut, m_idx)
    return text[:cut].strip() or None


def _to_job(url: str, html: str) -> Job | None:
    h1_idx = html.find("<h1")
    if h1_idx == -1:
        return None
    window = html[h1_idx : h1_idx + _HEADER_WINDOW]

    title_m = _H1_RE.search(window)
    if not title_m:
        return None
    company_m = _COMPANY_RE.search(window)
    location_m = _LOCATION_RE.search(window)
    type_m = _TYPE_RE.search(window)
    posted_m = _POSTED_RE.search(window)
    salary_m = _SALARY_RE.search(window)

    posted_at = strip_html(posted_m.group(1)) if posted_m else None
    if posted_at and posted_at.startswith("Posted "):
        posted_at = posted_at[len("Posted ") :].strip()

    jd_text = _extract_jd(html)
    employment_type = strip_html(type_m.group(1)) if type_m else None
    salary = strip_html(salary_m.group(1)) if salary_m else None
    extras = ", ".join(x for x in (employment_type, salary) if x)
    if jd_text and extras:
        jd_text = f"{jd_text}\n{extras}"

    return Job(
        source="nextgenenergyjobs",
        ext_id=job_id_from_url(url),
        url=url,
        title=strip_html(title_m.group(1)),
        company=strip_html(company_m.group(1)) if company_m else None,
        location=strip_html(location_m.group(1)) if location_m else None,
        posted_at=posted_at,
        jd_text=jd_text or extras or None,
        extra={"employment_type": employment_type, "salary": salary},
    )


class NextGenEnergyJobsPlugin(JobSourcePlugin):
    """Renewable-energy jobs from nextgenenergyjobs.com via sitemap + headless render."""

    name = "nextgenenergyjobs"
    base_url = "nextgenenergyjobs.com"
    mechanism = "browser"

    def is_available(self) -> bool:
        return playwright_available()

    def availability_detail(self) -> str:
        return "no chromium (playwright install chromium)"

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]
        try:
            # Modest over-fetch multiplier — each candidate costs a full
            # headless render, unlike remote100k's plain-HTTP fetches.
            candidates = _candidate_job_urls(limit_pool=max(limit * 3, 15))
        except Exception as exc:
            print(f"  nextgenenergyjobs: sitemap fetch failed — {exc}", file=sys.stderr)
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
                html = render_html(url, wait_selector="h1", timeout_ms=20000)
                job = _to_job(url, html)
            except Exception as exc:
                print(f"  nextgenenergyjobs: page render failed for {url} — {exc}", file=sys.stderr)
                continue
            if job is not None:
                jobs.append(job)
        return jobs
