"""job-scraper runner — discover plugins, fetch jobs, store them. PLAN.md §5 #1.

Reads portals (Apify + custom plugins), normalizes to the `jobs` schema, and
upserts rows at status ``scraped`` (the pipeline entry point). Persists per
source and logs each scrape to the `runs` table, so an interrupted run resumes.

Usage::

    python3 .claude/skills/job-scraper/scripts/scrape.py --list
    python3 .claude/skills/job-scraper/scripts/scrape.py \
        --source linkedin --query "security engineer" --location "Bengaluru" --limit 10
    python3 .claude/skills/job-scraper/scripts/scrape.py --source all --query "red team" --limit 5

    # Multiple queries/locations in one run (the matrix is the cross product;
    # every plugin fetches every query×location combo):
    python3 .claude/skills/job-scraper/scripts/scrape.py --source all \
        --queries "security engineer,detection engineer" \
        --locations "Hyderabad,Bengaluru" --limit 10 --workers 8

``--source all`` (the default) runs every *available* plugin, in parallel
(one worker thread per plugin — a single plugin's own query×location combos
still run sequentially so no single domain is ever hit concurrently; PLAN.md
§6 conservative rate limits). Every run ends with a SOURCE REPORT listing
*every discovered plugin* — available or not, worked or not — so it's always
clear which sites ran, which returned nothing, which errored, and why an
unavailable one was skipped. Small limits by default (actors are billed per
result).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import inspect
import os
import sys
import threading
from pathlib import Path

# Repo root — the `plugins` package lives at the top level (sibling to
# `data`/`execution`), not under `.claude` — it's plain scraping code with no
# Claude-specific dependency, reusable by any orchestrator.
ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent  # .../job-search
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")  # secrets before plugins read os.environ

from data import store  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity  # noqa: E402
from plugins.registry import discover_plugins, get_plugin  # noqa: E402
import _apify_keys  # noqa: E402 — registry put the plugins dir on sys.path


# Health glyphs for the key indicator (PLAN.md §9 multi-key Apify).
_KEY_GLYPH = {"healthy": "✓", "unknown": "?", "exhausted": "∅", "invalid": "✗"}

# Outcome glyphs for the end-of-run SOURCE REPORT.
_OUTCOME_GLYPH = {"ok": "✓", "partial": "◐", "empty": "∅", "error": "✗", "unavailable": "⚠"}

# Plugins with `uses_persistent_profile = True` (e.g. careerhound, wellfound) all
# launch a Playwright PERSISTENT context on the same PLAYWRIGHT_USER_DATA_DIR
# profile. Chromium locks that profile directory, so launching two persistent
# contexts on it at once hangs (observed live: the whole run stalled with no
# CPU progress). This lock serializes just those plugins' fetches — everything
# else still runs fully in parallel.
_PROFILE_LOCK = threading.Lock()


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


def _fetch_one_combo(plugin, query: str, limit: int, location: str | None) -> list[dict]:
    """Fetch one query×location combo for a plugin; returns normalized rows.
    Raises on failure — the caller (``_run_plugin``) decides how to record it."""
    # Pass location only if the plugin's fetch() actually accepts it.
    accepts_location = "location" in inspect.signature(plugin.fetch).parameters
    if accepts_location:
        jobs = plugin.fetch(query, limit, location=location)
    else:
        jobs = plugin.fetch(query, limit)
    return [j.to_row() for j in jobs]


def _run_plugin(plugin, queries: list[str], locations: list[str | None], limit: int) -> dict:
    """Run every query×location combo for ONE plugin, sequentially (politeness:
    a single domain is never hit concurrently even though different plugins run
    in parallel). Returns the raw rows + any per-combo errors; does NOT touch
    the DB — upserts happen on the main thread after every plugin task has
    finished, so writes are never concurrent and the report reflects a fully
    settled run ("wait for all to complete" before advancing the pipeline).
    """
    def _run_combos() -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        errors: list[str] = []
        for loc in locations:
            for q in queries:
                try:
                    rows.extend(_fetch_one_combo(plugin, q, limit, loc))
                except Exception as exc:  # noqa: BLE001 — record and keep going
                    errors.append(f"{q!r}@{loc!r}: {exc}")
        return rows, errors

    # Only worth locking if a persistent profile dir actually exists — the
    # same check every uses_persistent_profile plugin's own is_available()/
    # fetch() already gates on, so if it's unset neither plugin can reach a
    # persistent-context launch and there's nothing to serialize. Narrows
    # the lock to the one scenario it protects instead of coarsely
    # serializing wellfound's whole fetch (including its Apify-only path,
    # which never touches the profile) whenever the flag is set.
    profile_dir = os.environ.get("PLAYWRIGHT_USER_DATA_DIR")
    needs_lock = (getattr(plugin, "uses_persistent_profile", False)
                  and profile_dir and os.path.isdir(profile_dir))
    if needs_lock:
        # Never let two persistent-profile plugins launch a browser on the
        # shared PLAYWRIGHT_USER_DATA_DIR profile concurrently (profile lock).
        with _PROFILE_LOCK:
            rows, errors = _run_combos()
    else:
        rows, errors = _run_combos()
    return {"rows": rows, "errors": errors}


def _print_source_report(records: list[dict]) -> None:
    avail_n = sum(1 for r in records if r["available"])
    print(f"\nSOURCE REPORT — {len(records)} plugin(s) discovered ({avail_n} available)")
    name_w = max((len(r["name"]) for r in records), default=8)
    domain_w = max((len(r["domain"]) for r in records), default=8)
    mech_w = max((len(r["mechanism"]) for r in records), default=7)
    for r in records:
        glyph = _OUTCOME_GLYPH[r["outcome"]]
        head = (f"  {glyph} {r['name']:<{name_w}}  {r['domain']:<{domain_w}}  "
                f"{r['mechanism']:<{mech_w}}  ")
        if r["outcome"] == "unavailable":
            print(head + f"unavailable: {r['reason']}")
        elif r["outcome"] == "error":
            print(head + f"error: {r['detail']}")
        elif r["outcome"] == "empty":
            print(head + "empty (0 results)")
        elif r["outcome"] == "partial":
            print(head + f"fetched={r['fetched']} new={r['new']} updated={r['updated']}"
                         f"  (some combos failed: {r['detail']})")
        else:
            print(head + f"fetched={r['fetched']} new={r['new']} updated={r['updated']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Scrape jobs into the store (status=scraped)",
        epilog="Every run prints a SOURCE REPORT covering ALL discovered plugins "
               "(available or not) so it's clear which sites ran and how each responded. "
               "Use --list to check availability without fetching.",
    )
    ap.add_argument("--source", default="all",
                    help="portal name (linkedin/naukri/indeed/...) or 'all' (default: all)")
    ap.add_argument("--query", help="single search query, e.g. 'security engineer' "
                    "(use --queries for more than one)")
    ap.add_argument("--queries", help="comma-separated search queries, e.g. "
                    "'security engineer,detection engineer' — every plugin runs the "
                    "full query×location cross product")
    ap.add_argument("--location", default=None,
                    help="single optional location filter (use --locations for more than one)")
    ap.add_argument("--locations", default=None, help="comma-separated location filters")
    ap.add_argument("--limit", type=int, default=10,
                    help="max jobs per source per query×location combo")
    ap.add_argument("--workers", type=int, default=8,
                    help="max plugins fetched in parallel (default 8). Each plugin's own "
                    "query×location combos still run sequentially — a single domain is "
                    "never hit concurrently")
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
            state = "available" if p.is_available() else f"unavailable ({p.availability_detail()})"
            domain = f" [{p.base_url}]" if p.base_url else ""
            print(f"- {p.name}{domain}: {state}")
        return 0

    queries = ([s.strip() for s in args.queries.split(",") if s.strip()] if args.queries
               else ([args.query] if args.query else []))
    if not queries:
        ap.error("--query or --queries is required (unless --list)")
    locations: list[str | None] = (
        [s.strip() for s in args.locations.split(",") if s.strip()] if args.locations
        else [args.location]
    )
    if not locations:
        # An all-whitespace/empty-token --locations (e.g. "--locations ' , '")
        # would otherwise silently produce zero query×location combos — every
        # plugin "succeeds" with 0 rows and nothing in the output says why.
        locations = [None]
    if args.workers < 1:
        ap.error("--workers must be >= 1")

    store.init_db()

    all_plugins = discover_plugins()
    if args.source == "all":
        targets_all = all_plugins
    else:
        try:
            plugin = get_plugin(args.source)
        except KeyError:
            print(f"Unknown source {args.source!r}. Try --list.", file=sys.stderr)
            return 1
        if not plugin.is_available():
            print(f"Source {args.source!r} unavailable ({plugin.availability_detail()}).",
                  file=sys.stderr)
            return 1
        targets_all = [plugin]

    # Split into "will fetch" (available) vs "report only" (unavailable) —
    # every discovered plugin gets a SOURCE REPORT row either way, so an
    # unavailable portal never silently vanishes from the output.
    records: list[dict] = []
    fetch_plugins = []
    for p in targets_all:
        try:
            avail = p.is_available()
        except Exception as exc:  # noqa: BLE001 — a broken is_available() shouldn't crash the run
            avail, reason = False, f"is_available() raised: {exc}"
        else:
            reason = None if avail else p.availability_detail()
        if avail:
            fetch_plugins.append(p)
        else:
            records.append({"name": p.name, "domain": p.base_url or "?",
                            "mechanism": p.mechanism or "?", "available": False,
                            "outcome": "unavailable", "reason": reason})

    print(f"Fetching {len(fetch_plugins)}/{len(targets_all)} available source(s) — "
          f"{len(queries)} quer(ies) × {len(locations)} location(s), "
          f"up to {args.workers} in parallel.")

    # Run all plugin fetches in parallel; the executor `with` block (plus the
    # as_completed loop draining every future) guarantees we wait for every
    # plugin to finish before touching the DB or printing anything further —
    # nothing downstream (upserts, the matcher) can start on a partial result.
    plugin_results: dict[str, dict] = {}
    if fetch_plugins:
        done_n = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, len(fetch_plugins))) as ex:
            futures = {ex.submit(_run_plugin, p, queries, locations, args.limit): p
                       for p in fetch_plugins}
            for fut in concurrent.futures.as_completed(futures):
                p = futures[fut]
                try:
                    plugin_results[p.name] = fut.result()
                except Exception as exc:  # noqa: BLE001 — defensive; _run_plugin already catches
                    plugin_results[p.name] = {"rows": [], "errors": [str(exc)]}
                done_n += 1
                res = plugin_results[p.name]
                # Live progress (not gated behind -v): a full --source all run can
                # legitimately take minutes — some ATS plugins iterate dozens of
                # configured companies sequentially (e.g. greenhouse ~75, workday
                # ~39), each a real HTTP round trip. Printing as each plugin lands
                # is the difference between "still running" and "looks hung".
                tag = "✗" if (res["errors"] and not res["rows"]) else "✓"
                print(f"  [{done_n}/{len(fetch_plugins)}] {tag} {p.name} done — "
                      f"{len(res['rows'])} row(s), {len(res['errors'])} error(s)")

    # Serialize DB upserts on the main thread only (no concurrent writers).
    total_new = 0
    failures: list[str] = []
    for p in fetch_plugins:
        res = plugin_results[p.name]
        rows, errors = res["rows"], res["errors"]
        counts = store.upsert_jobs(rows) if rows else {"found": 0, "new": 0, "updated": 0}
        store.log_run("scrape", source=p.name, query=", ".join(queries),
                      counts={**counts, "errors": errors})
        total_new += counts["new"]

        if errors and not rows:
            outcome, detail = "error", "; ".join(errors[:2])
            failures.append(p.name)
        elif counts["found"] == 0:
            outcome, detail = "empty", None
        elif errors:
            outcome, detail = "partial", "; ".join(errors[:2])
        else:
            outcome, detail = "ok", None
        records.append({
            "name": p.name, "domain": p.base_url or "?", "mechanism": p.mechanism or "?",
            "available": True, "outcome": outcome, "detail": detail,
            "fetched": counts["found"], "new": counts["new"], "updated": counts["updated"],
        })

    # Report in discovery order so re-runs are easy to diff.
    order = {p.name: i for i, p in enumerate(all_plugins)}
    records.sort(key=lambda r: order.get(r["name"], len(order)))

    out = store.export_json()
    print(f"\n✓ done. {total_new} new job(s). exported → {out}")
    print(f"  stats: {store.stats()}")
    _print_source_report(records)
    if failures:
        print(f"\n  ⚠ failed sources (0 rows, see SOURCE REPORT above): {', '.join(failures)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
