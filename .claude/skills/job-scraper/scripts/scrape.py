"""job-scraper runner — discover plugins, fetch jobs, store them. PLAN.md §5 #1.

Reads portals (Apify + custom plugins), normalizes to the `jobs` schema, and
upserts rows at status ``scraped`` (the pipeline entry point). Persists each
plugin's rows to SQLite and logs to the `runs` table THE MOMENT that plugin
finishes (not after the whole run) — so a Ctrl-C mid-run keeps every plugin
that had already completed, instead of losing the entire run's work.

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


def _fetch_one_combo_timed(plugin, query: str, limit: int, location: str | None,
                           timeout: float) -> list[dict]:
    """``_fetch_one_combo`` with a wall-clock cap. On timeout raises
    ``TimeoutError`` — the slow underlying call is abandoned (its thread runs to
    completion in the background but its result is discarded) rather than
    stalling the whole plugin, let alone the whole run.

    Deliberately NOT a ``with ThreadPoolExecutor(...) as ex:`` block: that
    would call ``ex.__exit__`` -> ``shutdown(wait=True)`` on the way out,
    which blocks until the abandoned worker thread finishes on its own —
    i.e. it would still take the full ~250s for a stuck combo, just raising
    the timeout error late instead of promptly. Verified empirically: a
    5s-sleep task with `timeout=1` raised at 5.0s under `with`, at 1.0s here.
    ``shutdown(wait=False)`` returns immediately; the orphaned thread keeps
    running until the underlying call finishes (or the process exits), same
    tradeoff already described above."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(_fetch_one_combo, plugin, query, limit, location)
    try:
        return fut.result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


def _run_plugin(plugin, queries: list[str], locations: list[str | None], limit: int,
                 timeout: float) -> dict:
    """Run every combo for ONE plugin, sequentially (politeness: a single domain
    is never hit concurrently even though different plugins run in parallel).
    Returns the raw rows + any per-combo errors; does NOT touch the DB — the
    caller upserts as soon as this plugin's future completes (see ``main()``),
    so writes are never concurrent and a Ctrl-C only loses the plugin(s) still
    in flight, not the ones already done.

    Plugins whose ``fetch()`` doesn't accept a ``location`` kwarg (most of
    them — see ``_fetch_one_combo``) can't act on location at all, so calling
    them once per location would just re-fetch identical rows
    len(locations) times. Those run each query exactly ONCE, ignoring
    locations entirely; only location-aware plugins iterate the full
    query×location cross product.
    """
    accepts_location = "location" in inspect.signature(plugin.fetch).parameters
    combos = ([(loc, q) for loc in locations for q in queries] if accepts_location
              else [(None, q) for q in queries])

    def _run_combos() -> tuple[list[dict], list[str]]:
        rows: list[dict] = []
        errors: list[str] = []
        for loc, q in combos:
            try:
                rows.extend(_fetch_one_combo_timed(plugin, q, limit, loc, timeout))
            except concurrent.futures.TimeoutError:
                errors.append(f"{q!r}@{loc!r}: timed out after {timeout:.0f}s")
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
    print(f"  → {plugin.name} starting ({len(combos)} combo(s))...")
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
    ap.add_argument("--workers", type=int, default=0,
                    help="max plugins fetched in parallel; 0 (default) = auto, one worker "
                    "per available plugin (~38 today) so a run finishes in roughly the time "
                    "of its single slowest source. Each plugin's own query×location combos "
                    "still run sequentially — a single domain is never hit concurrently")
    ap.add_argument("--plugin-timeout", type=float, default=180.0,
                    help="max seconds per query×location combo before it's abandoned and "
                    "recorded as a timeout error (default 180s) — keeps one slow ATS "
                    "portal (observed: cutshort ~250s, oraclefusion ~230s per combo) from "
                    "stalling the whole run")
    ap.add_argument("--recheck", action="store_true",
                    help="re-seen jobs that are currently 'rejected' are moved back to "
                    "'scraped' so the matcher/classifier re-evaluate them (e.g. after an "
                    "eligibility rule change). Off by default: a normal re-scrape leaves "
                    "existing pipeline status untouched.")
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
    if args.workers < 0:
        ap.error("--workers must be >= 0 (0 = auto: one worker per available plugin)")

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

    # --workers 0 (default) = auto: one worker per available plugin, so the whole
    # run finishes in roughly the time of its single slowest source instead of
    # queueing behind a fixed pool. An explicit value is still capped to the
    # number of plugins actually being fetched — more workers than plugins is a
    # no-op, not a speedup.
    worker_count = max(1, min(args.workers or len(fetch_plugins) or 1, len(fetch_plugins) or 1))

    print(f"Fetching {len(fetch_plugins)}/{len(targets_all)} available source(s) — "
          f"{len(queries)} quer(ies) × {len(locations)} location(s), "
          f"up to {worker_count} in parallel"
          f"{' (auto)' if not args.workers else ''}.")

    # Run plugin fetches in parallel, but persist EACH plugin's rows the moment
    # its future lands — do not wait for the whole run. A Ctrl-C only loses the
    # plugin(s) still in flight; everything already `done_n` is already in
    # SQLite and in `runs`, because the upsert happens right here in the same
    # loop that drains futures, not in a second pass afterward.
    total_new = 0
    total_found = 0
    all_rejected_ids: list[int] = []
    failures: list[str] = []
    done_n = 0
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    try:
        futures = {ex.submit(_run_plugin, p, queries, locations, args.limit,
                              args.plugin_timeout): p for p in fetch_plugins}
        for fut in concurrent.futures.as_completed(futures):
            p = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive; _run_plugin already catches
                res = {"rows": [], "errors": [str(exc)]}
            rows, errors = res["rows"], res["errors"]

            # Upsert THIS plugin's rows now — the only writer, so no concurrent
            # DB access — before moving on to the next completed future.
            counts = (store.upsert_jobs(rows) if rows
                      else {"found": 0, "new": 0, "updated": 0, "rejected_ids": []})
            store.log_run("scrape", source=p.name, query=", ".join(queries),
                          counts={k: v for k, v in counts.items() if k != "rejected_ids"} | {"errors": errors})
            total_new += counts["new"]
            total_found += counts["found"]
            all_rejected_ids.extend(counts["rejected_ids"])

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

            done_n += 1
            # Live progress (not gated behind -v): a full --source all run can
            # legitimately take minutes — some ATS plugins iterate dozens of
            # configured companies sequentially (e.g. greenhouse ~75, workday
            # ~39), each a real HTTP round trip. Printing as each plugin lands
            # (and is already saved) is the difference between "still running"
            # and "looks hung".
            tag = "✗" if (errors and not rows) else "✓"
            print(f"  [{done_n}/{len(fetch_plugins)}] {tag} {p.name} done — "
                  f"saved {counts['new']} new / {counts['updated']} updated "
                  f"(of {counts['found']} found), {len(errors)} error(s)")
    except KeyboardInterrupt:
        # Everything upserted above this point is already committed. Don't
        # wait for in-flight combos to finish (they may be minutes from
        # timing out) — abandon them and exit now; os._exit skips the
        # non-daemon-thread join that `ex.shutdown(wait=True)`/interpreter
        # exit would otherwise block on.
        ex.shutdown(wait=False, cancel_futures=True)
        print(f"\n⚠ interrupted — {done_n}/{len(fetch_plugins)} source(s) had already "
              f"finished and are saved ({total_new} new job(s)). "
              f"Re-run to pick up the rest.", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    else:
        ex.shutdown(wait=True)

    # Report in discovery order so re-runs are easy to diff.
    order = {p.name: i for i, p in enumerate(all_plugins)}
    records.sort(key=lambda r: order.get(r["name"], len(order)))

    # Dedupe — the same job can be seen by more than one query×location combo
    # in this same run (e.g. two queries both matching one posting).
    all_rejected_ids = sorted(set(all_rejected_ids))
    recheck_n = 0
    if args.recheck and all_rejected_ids:
        for job_id in all_rejected_ids:
            store.update_job(job_id, status="scraped")
        recheck_n = len(all_rejected_ids)

    out = store.export_json()
    already_known = total_found - total_new
    reject_note = ""
    if all_rejected_ids:
        reject_note = (f" re-queued {recheck_n} for re-evaluation)" if args.recheck
                        else f" {len(all_rejected_ids)} previously rejected — "
                             f"re-run with `--recheck` to re-evaluate them)")
        reject_note = f" ({already_known} already known,{reject_note}"
    elif already_known:
        reject_note = f" ({already_known} already known — unchanged status)"
    print(f"\n✓ done. {total_new} new job(s){reject_note}. exported → {out}")
    print(f"  stats: {store.stats()}")
    _print_source_report(records)
    if failures:
        print(f"\n  ⚠ failed sources (0 rows, see SOURCE REPORT above): {', '.join(failures)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # NOT `raise SystemExit(_rc)` — that runs Python's normal interpreter
    # shutdown, which calls the atexit hook `concurrent.futures.thread.
    # _python_exit` that JOINS every ThreadPoolExecutor worker thread ever
    # created in this process. `_fetch_one_combo_timed`'s per-combo timeout
    # deliberately abandons a thread on expiry via `ex.shutdown(wait=False)`
    # so the combo loop can move on — but wait=False does NOT exempt that
    # thread from the atexit join; SystemExit still blocks on it until its
    # underlying (possibly timeout-less) HTTP/browser call finishes on its
    # own. Verified empirically: a run with several timed-out combos
    # (greenhouse/icims/indeed/lever/oraclefusion/smartrecruiters) hung 5+
    # minutes AFTER printing its final summary and committing every row to
    # SQLite — the exact "hangs, have to kill it" symptom this whole fix set
    # out to remove, just relocated to the very end of a clean run instead of
    # losing data. os._exit() skips atexit entirely; everything that needed
    # persisting is already committed by this point, so nothing is lost.
    os._exit(_rc)
