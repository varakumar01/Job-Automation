"""Avature job-portal plugin (public server-rendered HTML — no token
required). PLAN.md §4/§10.

Avature is an enterprise ATS/CRM used by large companies for career sites.
Avature career sites were previously assumed to require a full headless-
browser approach (the existing `synopsys.py` bespoke plugin does exactly
that for Synopsys's specific Avature-hosted site) — a deeper live pass
(2026-07-07) against 7 UNRELATED companies (Bloomberg, Broad Institute,
ManTech, Deloitte Belgium, Koch, Maximus, Xerox) found the SAME server-
rendered HTML template family reused across all of them, no JS execution
needed for the job-search list OR the job-detail page:

1. List: GET https://<host>/[<locale>/]careers/SearchJobs[?jobOffset=<n>] —
   a plain, unauthenticated HTML page whose job cards are already rendered
   server-side as `<article class="article article--result ...">` blocks
   (class list may have MORE classes after `article--result`, e.g. ManTech's
   `article--non-toggle` — matched on a word boundary, not an exact string),
   each with a title link (`.../JobDetail/<slug>/<id>`) inside an
   `article__header__text__title`-classed heading. TWO card-layout
   generations were found live, both server-rendered, both handled by this
   plugin: (a) Xerox/Bloomberg/Koch-style — title link is a bare
   `<a href="...">` (no other attributes), location is City/State/Province/
   Country as labeled `<p>` tags; (b) ManTech-style — title link is
   `<a class="link" href="...">` (an attribute BEFORE href — the regex uses
   `<a\b[^>]*?href=`, not a literal `<a href=`, specifically to tolerate
   this), location AND a real posted-date are both in plain
   `<span class="list-item-location">Location: ...</span>` /
   `<span class="list-item-posted">Posted Date: MM/DD/YYYY</span>` spans
   directly on the list card — tried as a fallback in `_location_str` /
   `_list_posted_at` whenever generation (a)'s labeled-paragraph pattern
   isn't found. Scraped directly via regex — no embedded JSON needed.
2. Detail: GET the job's own `JobDetail/<slug>/<id>` URL — the full JD lives
   in one or more `<div class="...field--rich-text">` blocks (About-the-
   company / Job-summary / Responsibilities sections are often separate
   blocks) — concatenated and stripped for `jd_text`. No JSON-LD exists on
   Avature detail pages (confirmed absent across all 7 tested tenants).

Configure via `AVATURE_COMPANIES` in `.env`. Some Avature tenants use a
custom CNAME domain instead of `<tenant>.avature.net` (e.g. ManTech's
`careers.mantech.com`) — so, unlike most other ATS plugins here, the FULL
HOSTNAME must be configured (not a bare tenant slug), same reasoning as
`successfactors.py`'s hostname requirement. Single-part identifier, so this
reuses `_ats_util.parse_companies` (`host` or `host:Display Name`):

    AVATURE_COMPANIES=xerox.avature.net:Xerox,careers.mantech.com:ManTech

Field-shape facts (verified live 2026-07-07 against xerox.avature.net,
koch.avature.net, bloomberg.avature.net, maximus.avature.net — all bare 200s,
no cookies/auth of any kind):
  - The job id is the trailing numeric path segment of the `JobDetail` URL
    (`.../JobDetail/<slug>/<id>`) — prefixed `<host>:<id>` for cross-tenant
    uniqueness like every other ATS plugin here, via `_career_util.job_id_from_url`.
  - Some tenants serve `SearchJobs` at the bare path, others require an
    `en_US/` locale prefix (varies per tenant, not documented anywhere
    public) — `_resolve_search` probes both, same "probe and cache the
    resolved variant" pattern as `darwinbox.py`'s `.in`/`.com` TLD probe.
  - A tenant with genuinely ZERO open postings right now is indistinguishable
    from an unresolvable host by page content alone — `_resolve_search`
    treats the FIRST prefix that returns a 200 (regardless of job count) as
    resolved, only reporting "unreachable" if BOTH prefixes raise.
  - Pagination is via `?jobOffset=<n>` (an item-count offset, not a page
    number) — the exact per-tenant page size varies (5 seen on Xerox), so
    this plugin advances the offset by however many blocks the current page
    actually returned, bounded by `_MAX_PAGES`.
  - The `field--rich-text` JD block extraction deliberately does NOT rely on
    matching a fixed number of closing `</div>` tags (the real content is
    itself full of arbitrarily nested `<div>`s, e.g. `<div><div>text</div></div>`,
    which made an early "stop after N closing divs" regex silently truncate
    real JD text mid-sentence in testing) — it instead captures everything
    up to the NEXT field-value marker or the enclosing `</article>` close,
    whichever comes first, which is reliable regardless of internal nesting
    depth since job descriptions never contain a literal `<article>` tag.
  - Posted-date sourcing differs by card-layout generation (see above): the
    detail page's `<meta name="Description" content="...created DD-Mon-YYYY">`
    tag (generation (a) tenants) is preferred when present, parsed via
    `datetime.strptime(..., "%d-%b-%Y")`; the list card's own
    `list-item-posted` span (generation (b), a `MM/DD/YYYY` string via
    `mmddyyyy_to_iso`) is used as a fallback when the detail page has no
    "created" date at all (confirmed on ManTech, whose detail pages carry a
    custom meta description with no created-date substring). Both degrade to
    `None` gracefully if absent/malformed, like every other date parse here.
  - An older Avature template generation (seen live on `uskpmgats.avature.net`,
    KPMG US — different CSS classes, `JobDetail?jobId=` query-param URLs
    instead of path segments) is NOT supported — falls through to zero
    results for that tenant rather than crashing; revisit only if a target
    company is actually found on that generation (no speculative branching
    built against an unconfirmed need).

Cost note: one extra detail call PER matched posting, same pattern as every
other ATS plugin here.

No Apify dependency — stdlib `urllib`/`re`. `is_available` is True only when
`AVATURE_COMPANIES` names at least one host.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import matches, parse_companies, round_robin, strip_html  # noqa: E402
from _career_util import fetch_html, job_id_from_url, mmddyyyy_to_iso  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_ENV_VAR = "AVATURE_COMPANIES"
_LOCALE_PREFIXES = ("", "en_US/")  # tried in order per tenant — both seen live for real tenants
_MAX_PAGES = 4  # bounds list calls per tenant
_MAX_DETAIL_FETCHES = 20  # bounds real HTTP detail-page fetches per tenant per call

_JOB_BLOCK_RE = re.compile(
    # Some tenants add extra classes after "article--result" AND/OR extra
    # attributes after the class attribute closes (e.g. ManTech's
    # `class="article article--result article--non-toggle" id="article--1"`)
    # — `[^>]*` (not `[^"]*"`) tolerates both without requiring the class
    # attribute to be the last thing before the tag closes.
    r'<article class="article article--result\b[^>]*>(.*?)</article>',
    re.DOTALL,
)
_TITLE_LINK_RE = re.compile(
    # `<a\b[^>]*?href=` (not a bare `<a href="`) tolerates `href` appearing
    # anywhere on the anchor tag, not only as the first attribute — ManTech's
    # title link is `<a class="link" href="...">`, which the original
    # `<a href="` literal would silently zero-match with no error raised.
    r'article__header__text__title[^"]*"[^>]*>\s*<a\b[^>]*?href="(?P<href>[^"]+)">\s*(?P<title>.*?)\s*</a>',
    re.DOTALL,
)
_LOCATION_LABEL_RE = re.compile(r'class="text--bold">(?:City|State/Province|Country):</span>\s*(?P<value>[^<]*)')
# A second card-layout generation (seen live on ManTech) puts location/date
# directly on the list card instead of labeled City/State/Country paragraphs —
# tried as a fallback in `_location_str`/`_fetch_candidates` when the
# City/State/Country labels aren't present on a given tenant's template.
_LIST_ITEM_LOCATION_RE = re.compile(r'class="list-item-location">Location:\s*(?P<value>[^<]*)</span>')
_LIST_ITEM_POSTED_RE = re.compile(r'class="list-item-posted">Posted Date:\s*(?P<value>\d{1,2}/\d{1,2}/\d{4})</span>')
_FIELD_VALUE_RE = re.compile(
    r'field--rich-text">\s*<div class="article__content__view__field__value">(.*?)'
    r'(?=<div class="article__content__view__field|</article>|\Z)',
    re.DOTALL,
)
_POSTED_RE = re.compile(r'name="Description"\s+content="[^"]*created\s+(\d{1,2}-[A-Za-z]{3}-\d{4})"')


def _search_url(host: str, prefix: str, offset: int) -> str:
    base = f"https://{host}/{prefix}careers/SearchJobs"
    return base if offset <= 0 else f"{base}?jobOffset={offset}"


def _resolve_search(host: str) -> tuple[str, list[str]] | None:
    """A tenant may need an `en_US/` locale prefix or none at all (varies,
    undocumented) — probe the initial page (offset 0) of each until one
    responds with a 200, returning the resolved prefix PLUS the job blocks
    already parsed from that probe (so `_fetch_candidates` doesn't re-request
    the same offset). The first prefix that returns 200 is treated as
    resolved even with zero job blocks (a tenant can genuinely have no open
    postings right now)."""
    fallback: tuple[str, list[str]] | None = None
    for prefix in _LOCALE_PREFIXES:
        try:
            page_html = fetch_html(_search_url(host, prefix, 0))
        except Exception:
            continue
        blocks = _JOB_BLOCK_RE.findall(page_html)
        if blocks:
            return prefix, blocks
        if fallback is None:
            fallback = (prefix, [])
    return fallback


def _location_str(block: str) -> str | None:
    values: list[str] = []
    for raw_value in _LOCATION_LABEL_RE.findall(block):
        value = strip_html(raw_value).strip()
        if value and (not values or values[-1] != value):
            values.append(value)
    if values:
        return ", ".join(values)
    # fallback for the "list-item-location" card layout (e.g. ManTech) —
    # tried only when the City/State/Country labels weren't found at all.
    m = _LIST_ITEM_LOCATION_RE.search(block)
    return (strip_html(m.group("value")).strip() or None) if m else None


def _list_posted_at(block: str) -> str | None:
    """Some tenants (e.g. ManTech) print a real posted date directly on the
    list card (`list-item-posted`) rather than in the detail page's meta
    description — tried as a fallback when `_posted_at`'s detail-page
    extraction comes up empty."""
    m = _LIST_ITEM_POSTED_RE.search(block)
    return mmddyyyy_to_iso(m.group("value")) if m else None


def _fetch_candidates(host: str, prefix: str, first_page_blocks: list[str], query: str) -> list[dict]:
    """List candidate jobs for one tenant, starting from the already-fetched
    page-1 blocks (from `_resolve_search`'s probe) and paginating further via
    `jobOffset` up to `_MAX_PAGES`, pre-filtered by a cheap title match
    before any detail fetch. Stops early once a page returns zero blocks."""
    words = [w.lower() for w in query.split() if len(w) > 1]
    candidates: list[dict] = []
    blocks = first_page_blocks
    offset = 0
    for page in range(_MAX_PAGES):
        if not blocks:
            break
        for block in blocks:
            m = _TITLE_LINK_RE.search(block)
            if not m:
                continue
            title = strip_html(m.group("title"))
            if not matches(title, words):
                continue
            href = urljoin(_search_url(host, prefix, 0), m.group("href"))
            try:
                job_id = job_id_from_url(href)
            except ValueError:
                continue
            candidates.append(
                {
                    "job_id": job_id,
                    "url": href,
                    "title": title,
                    "location": _location_str(block),
                    "list_posted_at": _list_posted_at(block),
                }
            )
            if len(candidates) >= _MAX_DETAIL_FETCHES:
                return candidates
        if page >= _MAX_PAGES - 1:
            break  # this was the last page we're allowed to process — don't fetch one more that would never be consumed
        offset += len(blocks)
        try:
            page_html = fetch_html(_search_url(host, prefix, offset))
        except Exception as exc:
            print(f"  avature: {host}: page at offset {offset} fetch failed — {exc}", file=sys.stderr)
            break  # keep everything collected so far rather than discarding it via a propagated exception
        blocks = _JOB_BLOCK_RE.findall(page_html)
    return candidates


def _jd_text(detail_html: str) -> str | None:
    segments = _FIELD_VALUE_RE.findall(detail_html)
    if not segments:
        return None
    return strip_html(" ".join(segments)) or None


def _posted_at(detail_html: str) -> str | None:
    m = _POSTED_RE.search(detail_html)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def _fetch_detail(url: str) -> dict:
    detail_html = fetch_html(url)
    return {"jd_text": _jd_text(detail_html), "posted_at": _posted_at(detail_html)}


def _to_job(candidate: dict, detail: dict, host: str, display_name: str) -> Job:
    ext_id = f"{host}:{candidate['job_id']}"
    return Job(
        source="avature",
        ext_id=ext_id,
        url=candidate["url"],
        title=candidate.get("title"),
        company=display_name,  # Avature postings carry no company field on the list card
        location=candidate.get("location"),
        posted_at=detail.get("posted_at") or candidate.get("list_posted_at"),
        jd_text=detail.get("jd_text"),
        extra=candidate,
    )


class AvaturePlugin(JobSourcePlugin):
    """Company career-site postings across every Avature tenant configured
    in AVATURE_COMPANIES."""

    name = "avature"
    base_url = "*.avature.net"
    mechanism = "html"

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
            resolved = _resolve_search(host)
            if not resolved:
                print(f"  avature: {host}: no reachable SearchJobs locale variant found", file=sys.stderr)
                continue
            prefix, first_page_blocks = resolved
            try:
                candidates = _fetch_candidates(host, prefix, first_page_blocks, query)
            except Exception as exc:
                print(f"  avature: {host}: list fetch failed — {exc}", file=sys.stderr)
                continue
            company_jobs: list[Job] = []
            for candidate in candidates:
                detail: dict = {}
                try:
                    detail = _fetch_detail(candidate["url"])
                except Exception as exc:
                    # A transient detail-fetch failure shouldn't drop the
                    # posting entirely — the list identity (url + title)
                    # alone is enough to store a minimally-useful row.
                    print(f"  avature: {host}: detail fetch failed for {candidate['url']} — {exc}", file=sys.stderr)
                try:
                    job = _to_job(candidate, detail, host, display_name)
                except Exception as exc:
                    print(f"  avature: {host}: skipping {candidate.get('url')} — {exc}", file=sys.stderr)
                    continue
                company_jobs.append(job)
            per_company.append(company_jobs)
        return round_robin(per_company)[:limit]
