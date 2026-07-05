#!/usr/bin/env python3
"""Generate docs/supported_companies.md from the live .env config.

Reads the SAME parsers each plugin uses (`parse_companies` /
`parse_workday_companies` from `_ats_util.py`) so this list can never drift
out of sync with what actually gets scraped — no hand-maintained company
list to forget updating. Run this any time a company is added/removed from
`.env`:

    python3 .claude/skills/job-scraper/scripts/list_companies.py

ATS platforms are configured via `.env` (`slug:Display Name` lists);
Workday needs the richer `tenant:wdN:site:Display Name` form. Custom (non-ATS)
plugins are single-company and need no `.env` entry — that small list is
maintained by hand in `_CUSTOM_PLUGINS` below since there's no shared parser
to introspect (add a line here whenever a new custom plugin like `synopsys.py`
ships).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGINS_DIR = _REPO_ROOT / ".claude" / "skills" / "job-scraper" / "plugins"
for p in (str(_REPO_ROOT), str(_PLUGINS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

from _ats_util import parse_companies, parse_workday_companies  # noqa: E402

# (display name, .env var, plugin file) for every ATS platform plugin using
# the plain slug[:Display Name] format.
_SLUG_PLATFORMS = [
    ("Greenhouse", "GREENHOUSE_COMPANIES", "greenhouse.py"),
    ("Lever", "LEVER_COMPANIES", "lever.py"),
    ("Ashby", "ASHBY_COMPANIES", "ashby.py"),
    ("SmartRecruiters", "SMARTRECRUITERS_COMPANIES", "smartrecruiters.py"),
    ("Recruitee", "RECRUITEE_COMPANIES", "recruitee.py"),
    ("BambooHR", "BAMBOOHR_COMPANIES", "bamboohr.py"),
    ("Workable", "WORKABLE_COMPANIES", "workable.py"),
    ("Zoho Recruit", "ZOHORECRUIT_COMPANIES", "zoho_recruit.py"),
]

# Custom (non-ATS) single-company plugins — no .env entry, so maintained by
# hand. Add a line here whenever a new one ships (mirrors the plugin's own
# hardcoded `company=` value in its `_to_job`).
_CUSTOM_PLUGINS = [
    ("Synopsys", "synopsys.py", "Tier 3 (Playwright render)"),
]


def _slug_platform_rows() -> list[tuple[str, str, str]]:
    """Return (platform, company_display_name, plugin_file) for every
    slug-style ATS platform's configured companies."""
    rows = []
    for platform, env_var, plugin_file in _SLUG_PLATFORMS:
        for _slug, name in parse_companies(env_var):
            rows.append((platform, name, plugin_file))
    return rows


def _workday_rows() -> list[tuple[str, str, str]]:
    rows = []
    for _tenant, _wd, _site, name in parse_workday_companies("WORKDAY_COMPANIES"):
        rows.append(("Workday", name, "workday.py"))
    return rows


def generate_markdown() -> str:
    all_rows = _slug_platform_rows() + _workday_rows()
    all_rows.sort(key=lambda r: (r[0], r[1]))

    lines = [
        "# Supported Companies",
        "",
        "**Auto-generated — do not hand-edit.** Regenerate after any `.env` change:",
        "",
        "```",
        "python3 .claude/skills/job-scraper/scripts/list_companies.py",
        "```",
        "",
        f"Total: **{len(all_rows) + len(_CUSTOM_PLUGINS)}** companies across "
        f"**{len({r[0] for r in all_rows}) + len(_CUSTOM_PLUGINS)}** platforms/plugins.",
        "",
    ]

    by_platform: dict[str, list[tuple[str, str]]] = {}
    for platform, name, plugin_file in all_rows:
        by_platform.setdefault(platform, []).append((name, plugin_file))

    for platform in sorted(by_platform):
        companies = by_platform[platform]
        lines.append(f"## {platform} ({len(companies)})")
        lines.append("")
        for name, _plugin_file in companies:
            lines.append(f"- {name}")
        lines.append("")

    lines.append(f"## Custom (non-ATS) plugins ({len(_CUSTOM_PLUGINS)})")
    lines.append("")
    for name, plugin_file, tier in _CUSTOM_PLUGINS:
        lines.append(f"- {name} (`{plugin_file}`, {tier})")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    out_path = _REPO_ROOT / "docs" / "supported_companies.md"
    out_path.write_text(generate_markdown())
    print(f"wrote {out_path}")
