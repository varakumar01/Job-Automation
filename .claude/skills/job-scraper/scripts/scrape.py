"""job-scraper runner — discover plugins, fetch jobs, store them. PLAN.md §5 #1.

Reads portals (Apify + custom plugins), normalizes to the `jobs` schema, and
upserts rows at status ``scraped`` (the pipeline entry point). Persists per
source and logs each scrape to the `runs` table, so an interrupted run resumes.

Usage::

    python3 .claude/skills/job-scraper/scripts/scrape.py --list
    python3 .claude/skills/job-scraper/scripts/scrape.py \
        --source linkedin --query "security engineer" --location "Bengaluru" --limit 10
    python3 .claude/skills/job-scraper/scripts/scrape.py --source all --query "red team" --limit 5

``--source all`` runs every *available* plugin. Small limits by default
(PLAN.md §6 conservative rate limits; actors are billed per result).
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path

# Repo root = .../job-search ; skill dir holds the plugins package.
SKILL_DIR = Path(__file__).resolve().parent.parent          # .../job-scraper
ROOT = SKILL_DIR.parent.parent.parent                       # repo root
for p in (str(ROOT), str(SKILL_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")  # secrets before plugins read os.environ

from data import store  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity, vprint  # noqa: E402
from plugins.registry import discover_plugins, get_plugin  # noqa: E402
import _apify_keys  # noqa: E402 — registry put the plugins dir on sys.path


# Health glyphs for the key indicator (PLAN.md §9 multi-key Apify).
_KEY_GLYPH = {"healthy": "✓", "unknown": "?", "exhausted": "∅", "invalid": "✗"}


def _show_keys() -> int:
    rows = _apify_keys.status_table()
    if not rows:
        print("No Apify keys configured. Set APIFY_TOKEN (or APIFY_TOKEN_1, _2, …) in .env.")
        return 1
    print(f"{len(rows)} Apify key(s):\n")
    for r in rows:
        g = _KEY_GLYPH.get(r["status"], "?")
        line = f"  {g} [{r['n']}] {r['hint']:<8} {r['status']:<9} checked={r['checked_at']}"
        if r["last_error"]:
            line += f"\n        last error: {r['last_error'][:100]}"
        print(line)
    print("\nlegend: ✓ healthy · ? unused · ∅ exhausted (credit used, auto-resets monthly) · ✗ invalid")
    return 0


def _run_one(plugin, query: str, limit: int, location: str | None) -> dict:
    print(f"\n▶ {plugin.name}: query={query!r} limit={limit} location={location!r}")
    vprint(1, f"  plugin class: {type(plugin).__name__}  available={plugin.is_available()}")
    # Pass location only if the plugin's fetch() actually accepts it.
    accepts_location = "location" in inspect.signature(plugin.fetch).parameters
    if accepts_location:
        jobs = plugin.fetch(query, limit, location=location)
    else:
        jobs = plugin.fetch(query, limit)
    rows = [j.to_row() for j in jobs]
    counts = store.upsert_jobs(rows) if rows else {"found": 0, "new": 0, "updated": 0}
    store.log_run("scrape", source=plugin.name, query=query, counts=counts)
    print(f"  fetched={len(rows)}  new={counts['new']}  updated={counts['updated']}")
    vprint(2, f"  sample urls: {[r.get('url','') for r in rows[:3]]}")
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scrape jobs into the store (status=scraped)")
    ap.add_argument("--source", default="all",
                    help="portal name (linkedin/naukri/indeed/...) or 'all'")
    ap.add_argument("--query", help="search query, e.g. 'security engineer'")
    ap.add_argument("--location", default=None, help="optional location filter")
    ap.add_argument("--limit", type=int, default=10, help="max jobs per source")
    ap.add_argument("--list", action="store_true", help="list plugins + availability and exit")
    ap.add_argument("--keys", action="store_true",
                    help="show Apify key health (which keys are usable/exhausted/invalid) and exit")
    ap.add_argument("--reset-keys", metavar="WHICH", default=None,
                    help="reset key health to 'unknown': 'all' | 'exhausted' | 'invalid' | a key hint tail")
    add_verbose_arg(ap)
    args = ap.parse_args(argv)
    apply_verbosity(args)

    if args.keys:
        return _show_keys()

    if args.reset_keys is not None:
        n = _apify_keys.reset(args.reset_keys)
        print(f"reset {n} key(s) → unknown.")
        return 0

    if args.list:
        for p in discover_plugins():
            state = "available" if p.is_available() else "unavailable (check creds)"
            print(f"- {p.name}: {state}")
        return 0

    if not args.query:
        ap.error("--query is required (unless --list)")

    store.init_db()

    if args.source == "all":
        targets = [p for p in discover_plugins() if p.is_available()]
        if not targets:
            print("No available plugins (is APIFY_TOKEN set in .env?)", file=sys.stderr)
            return 1
    else:
        try:
            plugin = get_plugin(args.source)
        except KeyError:
            print(f"Unknown source {args.source!r}. Try --list.", file=sys.stderr)
            return 1
        if not plugin.is_available():
            print(f"Source {args.source!r} unavailable (check creds in .env).", file=sys.stderr)
            return 1
        targets = [plugin]

    total_new = 0
    failures: list[str] = []
    for plugin in targets:
        # Isolate per-plugin failures so one portal's error (timeout, auth,
        # network) does not abort the others on a `--source all` run.
        try:
            counts = _run_one(plugin, args.query, args.limit, args.location)
            total_new += counts["new"]
        except Exception as exc:  # noqa: BLE001 — report and keep going
            failures.append(plugin.name)
            print(f"  ✗ {plugin.name} failed: {exc}", file=sys.stderr)
            store.log_run("scrape", source=plugin.name, query=args.query,
                          counts={"error": str(exc)})

    out = store.export_json()
    print(f"\n✓ done. {total_new} new job(s). exported → {out}")
    print(f"  stats: {store.stats()}")
    if failures:
        print(f"  ⚠ failed sources: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
