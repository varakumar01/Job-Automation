"""SAP SuccessFactors (CSB2) job-portal plugin (public sitemap + HTML — no
token required). PLAN.md §4/§10.

Unlike Greenhouse/Zoho/Oracle Fusion, SuccessFactors has NO bare public JSON
API — its documented OData v2 endpoint (`/odata/v2/JobRequisition`) requires
per-tenant HTTP Basic Auth (`user@companyID:password`, live-tested 2026-07-06
-> `401 Unauthorized`), not usable anonymously. This plugin is Tier-2
HTML/XML instead, in two calls per posting:

1. List: GET https://<host>/sitemap.xml — a plain, unauthenticated XML
   sitemap every "Career Site Builder 2" (CSB2) tenant publishes, one call
   per configured tenant. Entries are `<url><loc>JOB_URL</loc><lastmod>DATE
   </lastmod>...</url>` — the job title is baked into the URL's slug, which
   is used for a CHEAP local pre-filter before any detail-page fetch (some
   tenants' sitemaps run into the millions of bytes — colas.jobs.hr.cloud.sap
   was 7.8MB / tens of thousands of entries in live testing).
2. Detail: GET the job's own `<loc>` URL (plain HTML, no JS needed — curl -L
   matches what a browser renders) for the full JD + real fields, one call
   per slug-matched posting.

Configure via `SUCCESSFACTORS_COMPANIES` in `.env`. A CSB2 tenant is
addressed by its full hostname (varies per company, always ending
`.jobs.hr.cloud.sap` for tenants seen live) — this is a single-part
identifier like Greenhouse's slug, so it reuses `_ats_util.parse_companies`
(`host` or `host:Display Name`):

    SUCCESSFACTORS_COMPANIES=wlgore.jobs.hr.cloud.sap:W.L. Gore,cityoflondon.jobs.hr.cloud.sap:City of London Corporation

Field-shape facts (verified live 2026-07-06 against wlgore.jobs.hr.cloud.sap,
cityoflondon.jobs.hr.cloud.sap, tsbcareers.jobs.hr.cloud.sap,
colas.jobs.hr.cloud.sap — all bare 200s, no cookies/auth of any kind):
  - No numeric "id" field anywhere — the trailing path segment of the job URL
    (via `_career_util.job_id_from_url`) is the natural identifier, prefixed
    `<host>:<id>` for cross-tenant uniqueness like every other ATS plugin here.
  - Title/description are schema.org microdata (`itemprop="title"` /
    `itemprop="description"`) via `_career_util.extract_by_itemprop` — a
    posting's description is commonly split across 2-3 SEPARATE
    `itemprop="description"` spans (About/Role/How-to-apply sections), joined
    here before stripping.
  - Location and the real posting-start date are NOT microdata-tagged — they
    sit in a `joblayouttoken-label"` "Label: / value span" template
    (`_label_value` below), specifically "Office Location" and "Posting
    Start Date" (a `DD/MM/YYYY` string, converted via `ddmmyyyy_to_iso`).
    Falls back to the sitemap's own `<lastmod>` if the label isn't found on
    a given tenant's template variant.
  - If the detail page's own title/label markup ever fails to parse (tenant
    template variance not yet seen), the job is still built using the
    sitemap URL's own slug as a best-effort title/location fallback rather
    than dropped outright — the sitemap identity (URL + id) alone is enough
    to store a minimally-useful row.
  - Only the CSB2 flavor (`*.jobs.hr.cloud.sap`) is covered. The older
    "Career Portal" flavor (`career<N>.successfactors.com/career?company=...`,
    seen live on SmithGroup/Shangri-La) renders search results via a
    stateful DWR/AJAX-RPC call requiring a live session — genuinely harder,
    out of scope for this plugin (see docs/job_portals.md).

Cost note: sitemap fetch can be large (single tenants seen up to ~7.8MB) — the
XML itself is still fetched whole, but block scanning stops after the first
`_MAX_SITEMAP_ENTRIES` `<url>` blocks (via lazy `finditer`, not a fully
materialized `findall`, so a giant sitemap's tail is never regex-scanned at
all); slug-matched candidates per tenant are further capped at
`_MAX_DETAIL_FETCHES` real HTTP fetches.

No Apify dependency — stdlib `urllib`/`html.parser`. `is_available` is True
only when `SUCCESSFACTORS_COMPANIES` names at least one tenant.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import matches, parse_companies, round_robin, strip_html  # noqa: E402
from _career_util import ddmmyyyy_to_iso, extract_by_itemprop, fetch_html, job_id_from_url  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_SITEMAP_URL = "https://{host}/sitemap.xml"
_ENV_VAR = "SUCCESSFACTORS_COMPANIES"
_MAX_SITEMAP_ENTRIES = 500  # bounds regex scanning on multi-MB sitemaps
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per tenant per call

_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.DOTALL)
_LOC_RE = re.compile(r"<loc>(.*?)</loc>")
_LASTMOD_RE = re.compile(r"<lastmod>(.*?)</lastmod>")


def _slug_title(url: str) -> str:
    """Derive a rough human-readable title from the job URL's path slug —
    used both as a cheap local pre-filter (before any detail-page fetch) and
    as a last-resort title fallback if detail parsing fails. The URL shape is
    `/job/<TITLE-SLUG>/<NUMERIC-ID>/` — the title is the SECOND-TO-LAST path
    segment, not the last (that's the id; see `job_id_from_url`)."""
    parts = urlsplit(url).path.rstrip("/").split("/")
    slug = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "")
    return slug.replace("-", " ")


def _label_value(html_text: str, label: str) -> str | None:
    """Pull the value paired with a `joblayouttoken-label` field (e.g.
    "Office Location", "Posting Start Date") — anchored on that specific
    class (matched as one class among possibly several via a word-boundary
    class-list pattern, not a literal `class="joblayouttoken-label"` exact
    match, in case a tenant's template adds more classes to the element) so
    it can't accidentally match the label text appearing elsewhere (e.g.
    inside the free-text job description)."""
    pattern = re.compile(
        r'class="[^"]*\bjoblayouttoken-label\b[^"]*"[^>]*>\s*'
        + re.escape(label)
        + r'\s*:.*?class="[^"]*\brtltextaligneligible\b[^"]*"[^>]*>(.*?)</span>',
        re.DOTALL,
    )
    m = pattern.search(html_text)
    if not m:
        return None
    val = strip_html(m.group(1))
    return val or None


def _fetch_candidates(host: str, query: str) -> list[tuple[str, str]]:
    """List (url, lastmod) candidates for one tenant via its public sitemap,
    pre-filtered by a cheap slug-based title match before any detail fetch.
    Each `<url>...</url>` block is matched independently for `<loc>`/
    `<lastmod>` (tolerant of whitespace, sibling elements like `<changefreq>`
    in between, and an entirely absent `<lastmod>`) rather than requiring the
    two tags to sit immediately adjacent. Iterates lazily via `finditer` and
    stops at `_MAX_SITEMAP_ENTRIES` blocks scanned — unlike `findall`, this
    actually bounds the regex engine's work on a multi-MB sitemap instead of
    just slicing an already-fully-materialized match list."""
    xml_text = fetch_html(_SITEMAP_URL.format(host=host))
    words = [w.lower() for w in query.split() if len(w) > 1]
    candidates: list[tuple[str, str]] = []
    for i, block_m in enumerate(_URL_BLOCK_RE.finditer(xml_text)):
        if i >= _MAX_SITEMAP_ENTRIES:
            break
        block = block_m.group(1)
        loc_m = _LOC_RE.search(block)
        if not loc_m:
            continue
        url = html.unescape(loc_m.group(1))
        lastmod_m = _LASTMOD_RE.search(block)
        lastmod = lastmod_m.group(1) if lastmod_m else ""
        if not matches(_slug_title(url), words):
            continue
        candidates.append((url, lastmod))
        if len(candidates) >= _MAX_DETAIL_FETCHES:
            break
    return candidates


# Per-tenant CSB2 templates are independently configurable — which labels a
# posting shows varies (City of London uses "Office Location", W.L. Gore uses
# "State/Province" and has no "Office Location" field at all) — tried in
# order, first hit wins, same "resilient candidate-key" pattern as the Apify
# plugins' `first(...)` field picker.
_LOCATION_LABELS = ("Office Location", "Location", "State/Province", "City", "Country")


def _first_label_value(detail_html: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        val = _label_value(detail_html, label)
        if val:
            return val
    return None


def _parse_detail(detail_html: str) -> dict:
    titles = extract_by_itemprop(detail_html, "title")
    title = strip_html(titles[0]) if titles else None
    desc_segments = extract_by_itemprop(detail_html, "description")
    jd_text = strip_html(" ".join(desc_segments)) if desc_segments else None
    location = _first_label_value(detail_html, _LOCATION_LABELS)
    posted_at = ddmmyyyy_to_iso(_label_value(detail_html, "Posting Start Date"))
    return {"title": title or None, "jd_text": jd_text or None, "location": location, "posted_at": posted_at}


def _to_job(url: str, lastmod: str, info: dict, host: str, display_name: str) -> Job:
    ext_id = f"{host}:{job_id_from_url(url)}"
    return Job(
        source="successfactors",
        ext_id=ext_id,
        url=url,
        title=info.get("title") or _slug_title(url),
        company=display_name,  # SuccessFactors postings carry no company field
        location=info.get("location"),
        posted_at=info.get("posted_at") or lastmod,  # fall back to the sitemap's own <lastmod>
        jd_text=info.get("jd_text"),
        extra=info,
    )


class SuccessFactorsPlugin(JobSourcePlugin):
    """Company career-site postings across every SAP SuccessFactors (CSB2)
    tenant configured in SUCCESSFACTORS_COMPANIES."""

    name = "successfactors"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []

        # Collect each tenant's matches into its own list, then round-robin
        # merge — otherwise the first tenant alone can fill `limit` and every
        # other configured tenant is silently never represented.
        per_company: list[list[Job]] = []
        for host, display_name in companies:
            try:
                candidates = _fetch_candidates(host, query)
            except Exception as exc:
                print(f"  successfactors: {host}: sitemap fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for url, lastmod in candidates:
                info: dict = {}
                try:
                    detail_html = fetch_html(url)
                    info = _parse_detail(detail_html)
                except Exception as exc:
                    # A transient detail-fetch failure (timeout/HTTP error)
                    # shouldn't drop the posting entirely — the sitemap
                    # identity (URL + lastmod) alone is enough to store a
                    # minimally-useful row; `_to_job`'s own fallbacks
                    # (slug-derived title, lastmod as posted_at) cover it.
                    print(f"  successfactors: {host}: detail fetch failed for {url} — {exc}", file=sys.stderr)
                try:
                    job = _to_job(url, lastmod, info, host, display_name)
                except Exception as exc:
                    print(f"  successfactors: {host}: skipping {url} — {exc}", file=sys.stderr)
                    continue
                company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
