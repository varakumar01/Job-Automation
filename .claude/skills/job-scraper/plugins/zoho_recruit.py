"""Zoho Recruit job-portal plugin (public JSON API — no token required). PLAN.md §4/§10.

API: GET https://<subdomain>.zohorecruit.<tld>/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite
— one call per configured org, returns ``{"code": "success", "data": [...], "info": {...}}``.
No pagination params are accepted (`page`/`per_page` -> `400 EXTRA_PARAM_FOUND`); the
endpoint returns the whole current job list in one call.

**Correction to a Phase 1 finding (docs/job_portals.md, PLAN.md §9 2026-07-05):**
Zoho Recruit was previously marked "needs OAuth, deferred" — that's true of Zoho's
*authenticated* CRUD API, but every Zoho Recruit customer with a published public
career-site page also exposes this UNAUTHENTICATED read-only endpoint (discovered
2026-07-06 via live network-tab capture on InstaSafe's career page, then confirmed
against Astra Security and Simbian — the last a Phase 1 owner-priority target
previously unreachable). Do not re-defer this platform.

Configure via ``ZOHORECRUIT_COMPANIES`` in ``.env``. A Zoho Recruit org is addressed by
``<subdomain>.zohorecruit.<tld>`` — the TLD varies per org (``.com`` and ``.in`` both seen
live), so each entry is ``subdomain.tld`` (NOT a bare slug), optionally followed by a
display name:

    ZOHORECRUIT_COMPANIES=simbian.in:Simbian,instasafe.com:InstaSafe,getastraus.in:Astra Security

Field-shape facts (verified live 2026-07-06 against instasafe.com, getastraus.in,
simbian.in):
  - No company-name field — the configured display name is used as-is (like
    Lever/Ashby/BambooHR).
  - ``id`` is a large numeric string, globally unique per org — prefixed
    ``<subdomain>:<id>`` for cross-org uniqueness, same convention as every other
    ATS plugin here.
  - ``$url`` (a literal dollar-sign key) is the canonical public job-detail page.
  - ``Date_Opened`` is ``"MM/DD/YYYY"`` (US format, e.g. ``"10/30/2025"``), not ISO —
    converted via ``_ats_util``'s re-exported ``mmddyyyy_to_iso``. Not every org populates it.
  - ``Job_Description`` is present on most but not all orgs' responses (Astra's
    postings, e.g., can omit it) — treated as optional, run through ``strip_html``
    defensively in case an org embeds HTML in it.
  - ``City``/``State``/``Country`` are separate fields, frequently empty strings
    (not missing keys) for remote postings — composed, skipping empty parts.
  - The career-page name is assumed to be ``"Careers"`` (confirmed live across all
    three orgs tested) and is not currently made configurable — if a future org uses
    a different page name, its fetch will fail cleanly (caught per-company, isolated
    from every other configured org) rather than crash; self-anneal by adding a
    per-entry pagename override if that's ever hit.

No Apify dependency — stdlib ``urllib``. ``is_available`` is True only when
``ZOHORECRUIT_COMPANIES`` names at least one org.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _ats_util import HEADERS, TIMEOUT, matches, mmddyyyy_to_iso, parse_companies, round_robin, strip_html  # noqa: E402
from base import Job, JobSourcePlugin  # noqa: E402

_API = "https://{host}/recruit/v2/public/Job_Openings?pagename=Careers&source=CareerSite"
_ENV_VAR = "ZOHORECRUIT_COMPANIES"


def _fetch_org(subdomain_tld: str) -> list[dict]:
    """Call the Zoho Recruit public jobs API for one org and return its job
    list. ``subdomain_tld`` is e.g. ``"simbian.in"`` -> host
    ``simbian.zohorecruit.in``. Splits on the FIRST dot (not the last) —
    Zoho subdomains never contain a dot, but Zoho's regional TLDs can be
    multi-part (``.co.in``, ``.com.au``), which ``rsplit`` would misparse."""
    subdomain, tld = subdomain_tld.split(".", 1)
    host = f"{subdomain}.zohorecruit.{tld}"
    req = urllib.request.Request(_API.format(host=host), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    items = data.get("data")
    return items if isinstance(items, list) else []


def _location_str(item: dict) -> str | None:
    parts = [item.get("City"), item.get("State"), item.get("Country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _to_job(item: dict, subdomain_tld: str, fallback_name: str) -> Job | None:
    raw_id = item.get("id")
    if not raw_id:
        return None
    ext_id = f"{subdomain_tld}:{raw_id}"  # prefix: ids are only unique per-org
    title = item.get("Posting_Title") or item.get("Job_Opening_Name")
    return Job(
        source="zoho_recruit",
        ext_id=ext_id,
        url=item.get("$url"),
        title=title,
        company=fallback_name,  # Zoho Recruit postings carry no company field
        location=_location_str(item),
        posted_at=mmddyyyy_to_iso(item.get("Date_Opened")),
        jd_text=strip_html(item.get("Job_Description") or "") or None,
        extra=item,
    )


class ZohoRecruitPlugin(JobSourcePlugin):
    """Company career-site postings across every Zoho Recruit org configured
    in ZOHORECRUIT_COMPANIES."""

    name = "zoho_recruit"

    def is_available(self) -> bool:
        return bool(parse_companies(_ENV_VAR))

    def fetch(self, query: str, limit: int = 25) -> list[Job]:
        if limit <= 0:
            return []
        companies = parse_companies(_ENV_VAR)
        if not companies:
            return []
        words = [w.lower() for w in query.split() if len(w) > 1]

        # Collect each org's matches into its own list, then round-robin
        # merge — otherwise the first org alone can fill `limit` and every
        # other configured org is silently never represented.
        per_company: list[list[Job]] = []
        for subdomain_tld, fallback_name in companies:
            try:
                items = _fetch_org(subdomain_tld)
            except Exception as exc:
                print(f"  zoho_recruit: {subdomain_tld}: API fetch failed — {exc}", file=sys.stderr)
                continue
            org_jobs: list[Job] = []
            for item in items:
                try:
                    desc_snip = strip_html((item.get("Job_Description") or "")[:1200])
                    title = item.get("Posting_Title") or item.get("Job_Opening_Name") or ""
                    blob = f"{title} {desc_snip}"
                    if not matches(blob, words):
                        continue
                    job = _to_job(item, subdomain_tld, fallback_name)
                except Exception as exc:
                    print(f"  zoho_recruit: {subdomain_tld}: skipping malformed item — {exc}", file=sys.stderr)
                    continue
                if job is not None:
                    org_jobs.append(job)
            per_company.append(org_jobs)
        return round_robin(per_company)[:limit]
