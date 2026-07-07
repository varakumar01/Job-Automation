"""Shared helpers for ATS-backed job-portal plugins (Greenhouse/Lever/Ashby).

Design (PLAN.md §9 2026-07-05, revises §10): **one plugin per ATS platform**,
not one file per company. Each plugin reads its companies from a single
`.env` list, e.g.::

    GREENHOUSE_COMPANIES=crowdstrike,wiz,snyk:Snyk Security

A bare slug (``crowdstrike``) is used as both the API path segment and (title-
cased, dashes/underscores turned to spaces) the display company name. An
optional ``slug:Display Name`` form overrides the display name for companies
whose API doesn't expose one (Lever, Ashby — see each plugin's docstring).

Re-exports ``strip_html``/``matches``/``epoch_to_iso`` from
``_joblister_util`` rather than duplicating them — both families of plugins
live in the same folder and share the same no-auth public-JSON shape.
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.request
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))

from _joblister_util import HEADERS, TIMEOUT, epoch_to_iso, matches, strip_html  # noqa: E402,F401
from _career_util import mmddyyyy_to_iso  # noqa: E402,F401

_JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}


def parse_companies(env_var: str) -> list[tuple[str, str]]:
    """Parse a comma-separated ``GREENHOUSE_COMPANIES``-style env var into
    ``[(slug, display_name), ...]``. Each entry is ``slug`` or ``slug:Display
    Name``; a bare slug's display name is derived by title-casing with
    dashes/underscores replaced by spaces (e.g. ``arctic-wolf`` -> ``Arctic
    Wolf``). Blank/whitespace-only entries are skipped. Returns ``[]`` if the
    env var is unset or empty."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            slug, _, name = entry.partition(":")
            slug, name = slug.strip(), name.strip()
        else:
            slug = entry
            name = slug.replace("-", " ").replace("_", " ").title()
        if slug:
            out.append((slug, name or slug))
    return out


def post_json(url: str, body: dict, timeout: int = TIMEOUT) -> dict:
    """POST a JSON body and return the parsed JSON response. Used by
    platforms whose search endpoint is POST-only (Workday's CxS API,
    Workable's v3 accounts API) rather than a GET with query params."""
    req = urllib.request.Request(
        url, data=_json.dumps(body).encode("utf-8"), headers=_JSON_HEADERS, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def parse_workday_companies(env_var: str) -> list[tuple[str, int, str, str]]:
    """Parse a ``WORKDAY_COMPANIES``-style env var into
    ``[(tenant, wd_num, site, display_name), ...]``.

    Workday has no single "slug" — each tenant's career site is addressed by
    three parts (``tenant``, the numbered CxS shard ``wd1``-``wd5``, and a
    per-tenant ``site`` path segment that varies company to company and must
    be found by hand, e.g. by opening the careers page and reading the
    network request). Each entry is ``tenant:wdN:site`` or
    ``tenant:wdN:site:Display Name``:

        WORKDAY_COMPANIES=workday:5:Workday:Workday,cisco:1:External:Cisco

    Malformed entries (wrong field count, non-numeric wdN) are skipped with
    no exception — a typo'd entry should not take down every other
    configured company. Returns ``[]`` if the env var is unset or empty.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    out: list[tuple[str, int, str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) not in (3, 4):
            continue
        tenant, wd_raw, site = parts[0], parts[1], parts[2]
        if not tenant or not site or not wd_raw.isdigit():
            continue
        name = parts[3] if len(parts) == 4 and parts[3] else tenant.replace("-", " ").replace("_", " ").title()
        out.append((tenant, int(wd_raw), site, name))
    return out


def parse_oraclefusion_companies(env_var: str) -> list[tuple[str, str, str]]:
    """Parse an ``ORACLEFUSION_COMPANIES``-style env var into
    ``[(host, site_number, display_name), ...]``.

    Oracle Fusion Cloud Recruiting has no single "slug" either — each tenant's
    career site is addressed by its own full hostname (e.g.
    ``jpmc.fa.oraclecloud.com``) plus a per-tenant ``CX_<N>`` site number
    (found on the tenant's public careers URL,
    ``.../hcmUI/CandidateExperience/en/sites/CX_<N>/...``). Each entry is
    ``host:site`` or ``host:site:Display Name``:

        ORACLEFUSION_COMPANIES=jpmc.fa.oraclecloud.com:CX_1001:JPMorgan Chase

    Malformed entries (wrong field count, empty host/site) are skipped with no
    exception — a typo'd entry should not take down every other configured
    company. Returns ``[]`` if the env var is unset or empty.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return []
    out: list[tuple[str, str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) not in (2, 3):
            continue
        host, site = parts[0], parts[1]
        if not host or not site:
            continue
        name = parts[2] if len(parts) == 3 and parts[2] else host.split(".", 1)[0].replace("-", " ").title()
        out.append((host, site, name))
    return out


def epoch_ms_to_iso(epoch_ms: object) -> str | None:
    """Convert a Unix epoch in MILLISECONDS (Lever's ``createdAt``) to ISO
    8601. Returns None on any non-numeric/out-of-range input."""
    try:
        return epoch_to_iso(int(epoch_ms) / 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def round_robin(per_company: list[list]) -> list:
    """Interleave several companies' match lists one-at-a-time (round-robin)
    instead of concatenating them in order.

    Without this, a multi-company ``.env`` list (the entire point of the
    per-platform design) is broken under any small ``--limit``: if the first
    configured company alone has >= ``limit`` matches, later companies are
    never represented in the result at all. Round-robin guarantees every
    configured company gets a fair share before any one company can fill the
    whole limit (verified live 2026-07-05 — code-tester found the
    concatenation bug reproducibly on all three ATS plugins)."""
    out: list = []
    i = 0
    while True:
        added = False
        for lst in per_company:
            if i < len(lst):
                out.append(lst[i])
                added = True
        if not added:
            return out
        i += 1
