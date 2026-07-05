"""Shared helpers for CUSTOM (non-ATS) company career-site plugins. PLAN.md §4/§10.

Phase 2 of the portal-plugin build (2026-07-06): while `_ats_util.py` covers
companies hosted on a known ATS PLATFORM (Greenhouse/Lever/Workday/...), this
module is for companies with a genuinely bespoke career page — their own
domain, their own markup, no known ATS underneath. One plugin per bespoke
site (unlike ATS plugins, which cover many companies via a `.env` list),
because bespoke pages can't share a request shape the way an ATS's public API
does. Every such plugin should still be THIN: URL + selectors/field-mapping
only, with everything else — fetching, JSON-blob extraction, id derivation,
and (as a last resort) browser rendering — centralized here.

Re-exports ``strip_html``/``matches``/``epoch_to_iso`` from
``_joblister_util`` rather than duplicating them (same convention as
``_ats_util.py``).

**Fetch-strategy ladder (a plugin should use the lowest tier that works):**
  1. ``fetch_html`` / ``fetch_json`` — plain HTTP, no browser. Try this first;
     most modern career pages (even React/Next.js ones) render their initial
     job list into the HTML response for SEO, even if later interactions
     need JS.
  2. ``extract_next_data`` / ``extract_ld_json`` / ``extract_window_var`` —
     pull a JSON state blob out of that HTML. Covers the vast majority of
     "looks like a JS app but the data is still in the page source" sites.
  3. ``render_html`` — LAST RESORT. Launches a fresh headless Chromium tab
     (no persistent profile, no login — public career pages need neither,
     unlike ``_custom_template.py``'s login-session assumption) and returns
     the fully-rendered HTML for the same tier-2 extractors to consume.
     Requires ``playwright install chromium`` to have been run once; gate a
     plugin's ``is_available()`` on ``playwright_available()`` if it needs
     this tier, not on ``PLAYWRIGHT_USER_DATA_DIR``.

Leading underscore keeps this out of plugin auto-discovery (``registry.py``
skips ``_``-prefixed modules), same as every other shared-helper module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _joblister_util import HEADERS, TIMEOUT, epoch_to_iso, matches, strip_html  # noqa: E402,F401

# ---------------------------------------------------------------------------
# Tier 1: plain HTTP, no browser.
# ---------------------------------------------------------------------------


def fetch_html(url: str, *, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    """GET a URL with the shared browser-like headers; return decoded text.
    Raises on HTTP/network errors — wrap in try/except like every existing
    plugin's per-company fetch does; a single bad site shouldn't take down
    a multi-source scrape run."""
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def fetch_json(url: str, *, timeout: int = TIMEOUT, headers: dict | None = None) -> dict | list:
    """GET a URL and parse the response body as JSON. For a bespoke site that
    turns out to expose its own undocumented JSON API (found during research
    but not matching any of the 8 known ATS patterns)."""
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Tier 2: JSON blob embedded in server-rendered HTML.
# ---------------------------------------------------------------------------

_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def extract_next_data(html_text: str) -> object | None:
    """Pull and parse a Next.js ``__NEXT_DATA__`` blob (the most common
    "job list is really just JSON in the page source" shape). Returns None
    (not an exception) if the tag is absent or unparsable — callers should
    treat that as "try the next tier", not a crash."""
    m = _NEXT_DATA_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def extract_ld_json(html_text: str, *, ld_type: str | None = None) -> list:
    """Pull all ``application/ld+json`` script blocks, optionally filtered to
    a given ``@type`` (e.g. ``"JobPosting"``). Returns a list (possibly
    empty) rather than None/single-item, since a page can embed several."""
    out: list = []
    for m in _LD_JSON_RE.finditer(html_text):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if ld_type is None or (isinstance(c, dict) and c.get("@type") == ld_type):
                out.append(c)
    return out


def extract_window_var(html_text: str, name: str) -> object | None:
    """Pull a ``window.<name> = {...};`` (or ``[...]``) inline assignment —
    a common non-Next.js pattern for embedding initial page state. ``name``
    is a plain identifier (e.g. ``"__INITIAL_STATE__"``), not a regex. The
    trailing semicolon is optional (JS's automatic-semicolon-insertion makes
    ``window.X = {...}\\n`` with no ``;`` equally valid) — requiring it would
    wrongly fall through to the expensive Tier-3 browser-render fallback on
    a site that would otherwise have worked at Tier 2."""
    pattern = re.compile(
        r"window\." + re.escape(name) + r"\s*=\s*(\{.*?\}|\[.*?\])\s*;?",
        re.DOTALL,
    )
    m = pattern.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Shared id derivation
# ---------------------------------------------------------------------------


def job_id_from_url(url: str | None) -> str:
    """Deterministic fallback id for a posting when the site's own payload
    has no usable id field: the last non-empty path segment if there is one,
    else a short hash of the whole URL. Guarantees a non-empty, stable
    string so ``Job.ext_id`` (required — raises in ``base.py`` if empty) is
    always satisfiable — raises ValueError for a falsy ``url`` rather than
    silently returning an empty string, so a caller can't accidentally pass
    that straight into ``Job(ext_id=...)`` and get a confusing failure two
    frames away."""
    if not url:
        raise ValueError("job_id_from_url requires a non-empty url")
    path = urlsplit(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else ""
    if segment:
        return segment
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def mmddyyyy_to_iso(raw: str | None) -> str | None:
    """Convert a US-format ``"MM/DD/YYYY"`` date string (seen on Zoho
    Recruit's ``Date_Opened`` and Synopsys's ``job-date-posted``) to ISO
    8601. Returns None on any non-matching shape or out-of-range month/day
    rather than raising — a malformed date should degrade to a missing
    ``posted_at``, not break the plugin."""
    if not raw or raw.count("/") != 2:
        return None
    mm, dd, yyyy = raw.split("/")
    if not (mm.isdigit() and dd.isdigit() and yyyy.isdigit()):
        return None
    mm_i, dd_i = int(mm), int(dd)
    if not (1 <= mm_i <= 12 and 1 <= dd_i <= 31):
        return None
    return f"{yyyy}-{mm_i:02d}-{dd_i:02d}"


# ---------------------------------------------------------------------------
# Tier 3: headless browser rendering — LAST RESORT.
# ---------------------------------------------------------------------------


def playwright_available() -> bool:
    """True only if the ``playwright`` package imports AND a chromium build
    has actually been downloaded (``playwright install chromium``). A
    plugin that needs JS rendering should gate ``is_available()`` on this —
    NOT on ``PLAYWRIGHT_USER_DATA_DIR`` (that checks for a logged-in
    session, the wrong model for public career pages, which need no login
    at all)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


def render_html(url: str, *, wait_selector: str | None = None, timeout_ms: int = 15000) -> str:
    """Render a JS-heavy page with a FRESH HEADLESS Chromium tab — no
    persistent profile, no login (public career pages need neither). Fallback
    tier only: try ``fetch_html`` + the tier-2 extractors first, and only
    reach for this when the raw HTML genuinely has no usable JSON blob (an
    empty SPA-root shell). Lazily imports ``playwright`` so every plugin that
    only needs tiers 1-2 keeps working even before ``playwright install
    chromium`` has been run. Raises on failure (missing browser, timeout,
    navigation error) — callers should catch it the same way they catch
    ``urllib`` errors from ``fetch_html``."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, timeout=timeout_ms)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout_ms)
            return page.content()
        finally:
            browser.close()
