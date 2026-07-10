#!/usr/bin/env python3
"""main.py — one terminal entrypoint for the whole job-search pipeline.

Wires the per-stage skill scripts into repeatable commands so the pipeline runs
from the terminal, optionally automated with Grok (or the Anthropic API), and
stores every artifact itself. See PLAN.md §11.

Commands can be written bare (`apply`) or as a flag (`--apply`); options always
take `--`. The full reference (every command + its flags + examples) lives in the
argparse help — run `python3 main.py -h` (it is the single source of truth, so it
is not duplicated here). Pipeline order:

    search → lists → prep → apply → log → report

Job selection (so you can target just the best matches): `prep --eligible` /
`prep --jobs "1,2"`, and `apply --query <text>` / `apply --jobs "1,2"` /
`apply --source <portal>`.

LLM choice (`prep --llm`): claude = this Claude Code session (free, default),
grok = xAI/Groq automation (.env keys), api = Anthropic API.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # load .env so key health / provider checks in this process see the keys
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv optional; env may already be exported
    pass

from data import store  # noqa: E402
from execution import eligibility  # noqa: E402
from execution.eligibility import classify, MIN_PROFILE_SCORE  # noqa: E402
from execution.log import vprint  # noqa: E402

PY = sys.executable
SKILLS = ROOT / ".claude" / "skills"
SCRAPE = SKILLS / "job-scraper" / "scripts" / "scrape.py"
MATCH = SKILLS / "profile-matcher" / "scripts" / "match.py"
UNDERSTAND = SKILLS / "jd-understander" / "scripts" / "understand.py"
TAILOR = SKILLS / "resume-tailor" / "scripts" / "tailor.py"
RESPOND = SKILLS / "humanise-responder" / "scripts" / "respond.py"
APPLY = SKILLS / "apply-agent" / "scripts" / "apply.py"
LLM_RANK = SKILLS / "profile-matcher" / "scripts" / "llm_rank.py"
APPLICATIONS_DIR = ROOT / "applications"

# Eligibility classification lives in execution/eligibility.py (shared with llm_rank.py
# so the thresholds/keywords can't drift). classify + MIN_PROFILE_SCORE imported above.

# Groq (the user's key is a gsk_ Groq key — OpenAI-compatible). xAI proper would be
# https://api.x.ai/v1 + a grok-* model; override with .env if you have a real xAI key.
GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"

# DeepSeek — OpenAI-compatible, no free-tier TPM wall (concurrency-based), very cheap.
# Reuses the `grok` provider path with a different base/key/model. Needs DEEPSEEK_API_KEY.
# `deepseek-chat` is the general model (maps to deepseek-v4-flash); override via LLM_MODEL.
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# NVIDIA NIM — OpenAI-compatible, 100+ open-weight models, free-tier ~40 req/min.
# Reuses the `grok` provider path with NVIDIA's base/key/model. Needs NVIDIA_API_KEY (nvapi-…).
# Model selection (benchmarked 2026-07-04 on real ranking + JD-understand prompts):
#   Primary   moonshotai/kimi-k2.6 — best seniority-rule calibration (6/6 rubric), bare JSON,
#             1.6 s rank / 4.7 s understand. Correct absolute scores matter because
#             eligibility.py gates on absolute 60/70 thresholds.
#   Backup    mistralai/mistral-large-3-675b-instruct-2512 — fastest (1.3/2.8 s), reliable,
#             fires only on model-specific errors; strips fenced JSON automatically.
#   Prev. default (nvidia/llama-3.3-nemotron-super-49b-v1) — latency spikes 93 s on longer
#             outputs; kept as LLM_SYSTEM_PREFIX guard below prevents its reasoning bleed.
#   Rejected  meta/llama-3.3-70b-instruct, deepseek-v4-pro — 150 s timeouts on free tier.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "moonshotai/kimi-k2.6"
NVIDIA_BACKUP_MODEL = "mistralai/mistral-large-3-675b-instruct-2512"
# Sent as the FIRST line of the system prompt on the NVIDIA path to disable Nemotron
# reasoning-mode (which defaults ON and burns the whole token budget on <think> instead of
# producing JSON). Harmless for non-reasoning models like Kimi/Mistral; protects any
# Nemotron call. Set LLM_SYSTEM_PREFIX="" in .env to suppress if you switch models.
NVIDIA_SYSTEM_PREFIX = "detailed thinking off"


# ── helpers ────────────────────────────────────────────────────────────────

def _run(script: Path, *args: str, env: dict | None = None) -> int:
    """Run a skill script as a subprocess, streaming its output. Returns exit code."""
    cmd = [PY, str(script), *args]
    print(f"\n$ {script.name} {' '.join(args)}")
    merged = {**os.environ, **(env or {})}
    vprint(2, f"  [vv] env overrides: {list((env or {}).keys())}")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=merged)
    return proc.returncode


def _ordered(jobs: list[dict]) -> list[dict]:
    """Sort by best score first (llm_score if present, else match_score), newest id tiebreak.
    Intended to be called on an already-classified subset."""
    def _score(j):
        s = j.get("llm_score")
        return s if s is not None else (j.get("match_score") or 0)
    return sorted(jobs, key=lambda j: (-_score(j), -(j.get("id") or 0)))


# ── commands ───────────────────────────────────────────────────────────────

def cmd_search(args) -> int:
    locations = [s.strip() for s in args.locations.split(",") if s.strip()]
    queries = [s.strip() for s in args.queries.split(",") if s.strip()]
    env = {"LINKEDIN_POSTED_DAYS": str(args.days)} if args.days else {}
    print(f"Searching {args.source} — {len(queries)} quer(ies) × {len(locations)} location(s), "
          f"last {args.days or '∞'} days, newest-first.")
    # One scrape.py invocation covers the whole plugin × query × location matrix —
    # scrape.py parallelizes across plugins itself (--workers) and prints ONE
    # consolidated SOURCE REPORT covering every discovered plugin (available or
    # not), instead of the old per-query×location subprocess loop that only ever
    # showed whichever single --source was requested (default was `linkedin`,
    # which is why other aggregators never appeared in the output before).
    rc = _run(SCRAPE, "--source", args.source, "--queries", args.queries,
              "--locations", args.locations, "--limit", str(args.limit),
              "--workers", str(args.workers), env=env)
    # Rank everything just scraped (only NEW rows advance scraped→matched — resumable/
    # incremental: already-applied/ready/rejected jobs are untouched on a re-search).
    _run(MATCH)
    # Park off-profile jobs so the LLM ranker/prep only ever touches relevant ones.
    rejected = _auto_reject()
    if rejected:
        print(f"\n🚫 auto-rejected {rejected} off-profile job(s) → rejected list "
              f"(see `main.py rejected`).")
    print("\n" + "=" * 60)
    _print_lists()
    return rc


def _auto_reject() -> int:
    """Move off-profile `matched` jobs to `rejected` so the LLM never ranks/preps them
    (efficiency: keeps token spend on relevant jobs only). Returns how many were rejected.

    Only HARD-NOs are rejected: non-security titles, genuine scope/seniority gaps (manages
    people / 6+ yr requirements), or scores below the stretch floor. Security roles that are
    merely a low-fit stretch are KEPT (classify == 'stretch') so the user can opt in to a
    heavier résumé rewrite rather than having viable roles silently dropped.
    """
    off = [j for j in store.get_jobs(status="matched") if classify(j) == "off_profile"]
    for j in off:
        store.update_job(j["id"], status="rejected")
    if off:
        store.export_json()
    return len(off)


def _reject_by_llm(threshold: float = eligibility.LLM_BEST_SCORE) -> int:
    """Reject matched jobs the Grok reranker scored BELOW the cutoff (llm_dud) — the
    tuned ranking, not the keyword score, filters the duds. Returns how many."""
    duds = [j for j in store.get_jobs(status="matched") if eligibility.llm_dud(j, threshold)]
    for j in duds:
        # Only flip status — the job already carries the reason in llm_score/llm_reason, so
        # we DON'T overwrite `notes` (it holds the profile-matcher breakdown JSON that
        # coverage()/classify() need if the job is ever moved back to 'matched').
        store.update_job(j["id"], status="rejected")
    if duds:
        store.export_json()
    return len(duds)


def cmd_reject(args) -> int:
    """Reject off-profile matched jobs → the 'rejected' list. With --by-llm, instead
    reject the jobs the Grok reranker scored below the best-cutoff (needs `rank --save`)."""
    if args.by_llm:
        n = _reject_by_llm()
        counts = store.stats()
        print(f"Grok-filtered: rejected {n} low-scored job(s) → 'rejected'. "
              f"({counts.get('matched', 0)} remain at 'matched', "
              f"{counts.get('rejected', 0)} rejected total.)")
        if n == 0:
            print("  (nothing rejected — jobs may lack an llm_score; run "
                  "`main.py rank --llm grok --eligible --save` first.)")
        return 0
    n = _auto_reject()
    counts = store.stats()
    print(f"rejected {n} off-profile job(s) → 'rejected'. "
          f"({counts.get('matched', 0)} relevant remain at 'matched', "
          f"{counts.get('rejected', 0)} rejected total.)")
    return 0


def cmd_rejected(args) -> int:
    """Show the rejected (non-relevant) list — excluded from ranking/prep."""
    rows = store.get_jobs(status="rejected")
    if not rows:
        print("No rejected jobs yet. Run `main.py reject` (or `search`) to park off-profile jobs.")
        return 0
    print(f"🚫 REJECTED — non-relevant jobs ({len(rows)}); excluded from ranking/prep:\n")
    for j in _ordered(rows):
        print(f"  {(j.get('match_score') or 0):>5.1f}  {(j.get('title') or '')[:42]:<42} @ "
              f"{(j.get('company') or '')[:20]:<20} | {(j.get('location') or '')[:22]}")
    return 0


def _print_lists(raw: bool = False) -> None:
    jobs = store.get_jobs(status="matched")
    # classify() runs has_scope_gap() (regex) + coverage() (JSON parse) per job —
    # compute once and bucket to avoid 4× redundant work across the list comprehensions.
    tiers: dict[str, list] = {"eligible": [], "needs_mod": [], "stretch": [], "off_profile": []}
    for j in jobs:
        tiers.setdefault(classify(j), []).append(j)
    eligible = _ordered(tiers["eligible"])
    needs = _ordered(tiers["needs_mod"])
    stretch = _ordered(tiers["stretch"])
    off = tiers["off_profile"]

    def _row(j):
        # Show the job id + posting link so each row is directly actionable
        # (`apply --jobs <id>`, or open the link). Easy-Apply isn't shown: the LinkedIn
        # actor doesn't expose it (verified) — see PLAN §10 (easy-apply source, follow-up).
        link = (j.get("url") or "").split("?")[0]
        return (f"  {(j.get('match_score') or 0):>5.1f}  [{j['id']:>3}]  "
                f"{(j.get('title') or '')[:40]:<40} @ {(j.get('company') or '')[:18]:<18} "
                f"| {(j.get('location') or '')[:18]}\n"
                f"           ↳ {link}")

    # LIST 1 — the raw scraped pool (everything sourced & still unactioned). Summarized
    # by portal so 300+ rows don't flood the terminal; `--raw` dumps the full list.
    by_src: dict[str, int] = {}
    for j in jobs:
        by_src[j.get("source") or "?"] = by_src.get(j.get("source") or "?", 0) + 1
    src_summary = ", ".join(f"{k} {v}" for k, v in sorted(by_src.items()))
    print(f"\n📥 SCRAPED — all sourced jobs ({len(jobs)}) [{src_summary or 'none'}]")
    if raw:
        for j in _ordered(jobs):
            print(_row(j) + f"  <{classify(j)}>")
    else:
        print("   (full list: `main.py lists --raw`  or  `main.py rank`)")

    # LIST 2 — best matches (apply with the master résumé as-is).
    print(f"\n✅ BEST MATCH — eligible as-is, apply with the master résumé ({len(eligible)}):")
    for j in eligible:
        print(_row(j))

    # LIST 3 — needs résumé modification before applying.
    print(f"\n✏️  NEEDS RÉSUMÉ MODIFICATION — tailor first ({len(needs)}):")
    for j in needs:
        print(_row(j))

    # LIST 4 — stretch roles: security-on-profile but low fit / pentest / title-senior-only.
    # These need a heavier résumé rewrite; the human opts in (`prep --stretch`).
    print(f"\n🧗 STRETCH — security-adjacent, low fit, applyable with a heavy rewrite ({len(stretch)}):")
    if stretch:
        for j in stretch:
            print(_row(j))
        print(f"   Prep these with: `main.py prep --stretch --llm <provider>`")
    else:
        print("   (none — run `main.py reject` to park hard-nos if needed)")

    print(f"\n({len(off)} off-profile hard-nos still at 'matched' — non-security / scope gap / "
          f"score < {eligibility.STRETCH_FLOOR:.0f}; run `main.py reject` to park them.)")


def cmd_lists(args) -> int:
    _print_lists(raw=args.raw)
    return 0


def _llm_env(provider: str) -> dict:
    if provider == "grok":
        return {"LLM_PROVIDER": "grok",
                "XAI_BASE_URL": os.environ.get("XAI_BASE_URL") or GROQ_BASE,
                "LLM_MODEL": os.environ.get("LLM_MODEL") or GROQ_MODEL}
    if provider == "deepseek":
        # OpenAI-compatible → reuse the grok backend with DeepSeek's base/model/key.
        # Feed DeepSeek's key into the slot the grok backend reads. ALWAYS set it (to ""
        # if missing) so a stray XAI_API_KEY (Groq) in the environment can't leak to
        # DeepSeek's server via the env merge — an empty key yields a clear missing-key error.
        env = {"LLM_PROVIDER": "grok",
               "XAI_BASE_URL": os.environ.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE,
               "LLM_MODEL": os.environ.get("LLM_MODEL") or DEEPSEEK_MODEL,
               "XAI_API_KEY": os.environ.get("DEEPSEEK_API_KEY") or "",
               "GROK_API_KEY": ""}  # clear alias so _grok_pool can't discover a stale Groq key
        return env
    if provider == "nvidia":
        # OpenAI-compatible NIM endpoint → reuse the grok backend with NVIDIA's base/key/model.
        # ALWAYS set both XAI_API_KEY and GROK_API_KEY explicitly (to "" if missing) so neither
        # Groq key alias can leak to NVIDIA's server via the subprocess env merge.
        # LLM_BACKUP_MODEL is read by execution/llm.py complete() for a one-shot fallback.
        # LLM_SYSTEM_PREFIX is prepended to every system prompt so Nemotron reasoning-mode
        # stays OFF (prevents <think> from eating the whole token budget on reasoning models).
        return {"LLM_PROVIDER": "grok",
                "XAI_BASE_URL": os.environ.get("NVIDIA_BASE_URL") or NVIDIA_BASE,
                "LLM_MODEL": os.environ.get("LLM_MODEL") or NVIDIA_MODEL,
                "LLM_BACKUP_MODEL": os.environ.get("LLM_BACKUP_MODEL") or NVIDIA_BACKUP_MODEL,
                "XAI_API_KEY": os.environ.get("NVIDIA_API_KEY") or "",
                "GROK_API_KEY": "",  # clear alias so _grok_pool can't discover a stale Groq key
                "LLM_SYSTEM_PREFIX": os.environ.get("LLM_SYSTEM_PREFIX", NVIDIA_SYSTEM_PREFIX)}
    if provider == "api":
        return {"LLM_PROVIDER": "api"}
    return {"LLM_PROVIDER": "session"}


def cmd_prep(args) -> int:
    if args.llm == "claude":
        print("--llm=claude → session mode: the writing is done by the Claude Code "
              "session.\nJust ask Claude: \"run jd-understander, resume-tailor and "
              "humanise-responder for the matched jobs\" — it will prepare/save each "
              "stage. (No automation here by design.)")
        return 0
    env = _llm_env(args.llm)
    lim = ["--limit", str(args.limit)] if args.limit is not None else []
    store.init_db()

    # Resolve which jobs to prep: explicit --jobs wins; else --eligible (best-match) and/
    # or --needs-mod (non-best, need tailoring) select by classification; else everything
    # pending. `ids` (or None for all) threads through all stages.
    ids = store.parse_ids(args.jobs)
    if ids is None and args.llm_best:
        # Grok/LLM reranker decides the best (not the keyword score) — needs `rank
        # --llm grok --eligible --save` first so jobs carry an llm_score.
        ids = [j["id"] for j in store.get_jobs(status="matched") if eligibility.llm_best(j)]
        if not ids:
            print("no Grok-scored best jobs at 'matched'. Run "
                  "`main.py rank --llm grok --eligible --save` first, then retry --llm-best.")
            return 0
    wanted = {c for c, on in (("eligible", args.eligible),
                              ("needs_mod", args.needs_mod),
                              ("stretch", args.stretch)) if on}
    if ids is None and wanted:
        ids = [j["id"] for j in store.get_jobs(status="matched") if classify(j) in wanted]
        if not ids:
            print(f"no {' or '.join(sorted(wanted))} jobs at 'matched' to prep "
                  "(they may already be ready). Nothing to do.")
            return 0
    jobs_arg = ["--jobs", ",".join(map(str, ids))] if ids else []
    id_set = set(ids) if ids else None
    scope = f"{len(ids)} selected job(s)" if ids else "all pending jobs"
    print(f"Prep via --llm={args.llm} (model {env.get('LLM_MODEL', 'default')}) — {scope}.")

    # NOTE the user about non-best jobs whose résumé will be LLM-MODIFIED. needs_mod jobs
    # are tailored by default; with --modify-resume, eligible jobs get modified too; with
    # --stretch, stretch jobs get a heavier tailoring pass. Master résumé never touched.
    in_scope = [j for j in store.get_jobs(status="matched")
                if id_set is None or j["id"] in id_set]
    nonbest = [j for j in in_scope if classify(j) == "needs_mod"]
    stretch_jobs = [j for j in in_scope if classify(j) == "stretch"]
    if nonbest:
        print(f"  ⚠ NOTE: {len(nonbest)} NON-best-match job(s) need tailoring — they'll be "
              f"applied with an LLM-MODIFIED résumé (master untouched). Review each tailored "
              f"PDF before submitting.")
    if stretch_jobs:
        print(f"  🧗 NOTE: {len(stretch_jobs)} STRETCH job(s) — low-fit but security-adjacent; "
              f"require a heavier résumé rewrite. Review PDFs carefully before submitting.")
    if args.modify_resume:
        elig_n = sum(1 for j in in_scope if classify(j) == "eligible")
        if elig_n:
            print(f"  ⚠ NOTE: --modify-resume also tailors {elig_n} eligible job(s) that the "
                  f"master résumé already fits (modification is optional for those).")

    # 1) JD briefs for every targeted matched job lacking one.
    if _run(UNDERSTAND, "run", *lim, *jobs_arg, env=env) != 0:
        print("⚠ jd-understander reported errors; continuing.", file=sys.stderr)

    # 2) Résumé: tailor 'needs_mod' jobs; master-as-is for 'eligible' jobs. With
    #    --modify-resume, tailor everything; otherwise only the needs_mod list.
    if args.modify_resume:
        _run(TAILOR, "run", *lim, *jobs_arg, env=env)
    else:
        # eligible → passthrough (master as-is); needs_mod → tailor.
        elig = [j["id"] for j in store.get_jobs(status="matched")
                if classify(j) == "eligible" and not (j.get("tailored_resume_path") or "").strip()
                and (id_set is None or j["id"] in id_set)]
        if elig:
            print(f"  {len(elig)} eligible job(s) → master résumé as-is")
            _run(TAILOR, "--no-modify", "--jobs", ",".join(map(str, elig)))
        _run(TAILOR, "run", *lim, *jobs_arg, env=env)  # the rest (needs_mod + stretch) get tailored

    # 3) Answers for every tailored job in scope.
    if _run(RESPOND, "run", *lim, *jobs_arg, env=env) != 0:
        print("⚠ humanise-responder reported errors; continuing.", file=sys.stderr)

    print("\n" + "=" * 60)
    print(f"Pipeline state: {store.stats()}")
    return 0


def cmd_apply(args) -> int:
    a = ["packet"]
    if args.limit is not None:
        a += ["--limit", str(args.limit)]
    if args.source:
        a += ["--source", args.source]
    if args.query:
        a += ["--query", args.query]
    if args.jobs:
        a += ["--jobs", args.jobs]
    return _run(APPLY, *a)


def cmd_applied(args) -> int:
    """The applied-jobs log: what's already been submitted/skipped/failed, so we never
    re-apply. (Re-scraping an applied job keeps its status — see store.upsert_jobs.)"""
    buckets = {s: store.get_jobs(status=s) for s in ("applied", "skipped", "failed")}
    total = sum(len(v) for v in buckets.values())
    if not total:
        print("No applications logged yet. Apply to a ready job, then "
              "`main.py log --job N --outcome applied`.")
        return 0
    icons = {"applied": "✓", "skipped": "↷", "failed": "✗"}
    print(f"📒 APPLIED-JOBS LOG — {total} entr(ies) (these are excluded from new apply lists):\n")
    for status in ("applied", "skipped", "failed"):
        rows = _ordered(buckets[status])
        if not rows:
            continue
        print(f"{icons[status]} {status.upper()} ({len(rows)}):")
        for j in rows:
            note = f"  — {j['notes']}" if (j.get("notes") or "").strip() else ""
            print(f"  [{j['id']:>3}] {(j.get('title') or '')[:40]:<40} @ "
                  f"{(j.get('company') or '')[:20]:<20} ({j.get('source') or '?'}) "
                  f"{j.get('applied_at') or ''}{note}")
        print()
    return 0


def cmd_log(args) -> int:
    extra = ["--note", args.note] if args.note else []
    rc = _run(APPLY, "log", "--job", str(args.job), "--outcome", args.outcome, *extra)
    if rc == 0 and args.screenshot:
        store.init_db()
        store.update_job(args.job, screenshot_path=args.screenshot)
        print(f"  saved screenshot path → {args.screenshot}")
    return rc


def cmd_report(args) -> int:
    """Build the applications dashboard + store every artifact under applications/."""
    APPLICATIONS_DIR.mkdir(exist_ok=True)
    # Jobs that have produced artifacts (tailored onward).
    relevant = [j for j in store.get_jobs()
                if j["status"] in ("tailored", "ready", "applied", "skipped", "failed")]
    relevant = _ordered(relevant)

    index = []
    for j in relevant:
        jdir = APPLICATIONS_DIR / str(j["id"])
        jdir.mkdir(exist_ok=True)
        record = {
            "job_id": j["id"], "title": j.get("title"), "company": j.get("company"),
            "location": j.get("location"), "url": j.get("url"), "source": j.get("source"),
            "status": j["status"], "match_score": j.get("match_score"),
            "role_profile": j.get("role_profile"),
            "eligibility": classify(j) if j["status"] == "matched" else None,
            "applied_at": j.get("applied_at"), "outcome": j.get("outcome"),
            "notes": j.get("notes"),
        }
        # Copy résumé PDF for one-click opening.
        rp = j.get("tailored_resume_path")
        if rp:
            src = ROOT / rp if not Path(rp).is_absolute() else Path(rp)
            if src.exists():
                dest = jdir / f"resume{src.suffix}"
                shutil.copy2(src, dest)
                record["resume_file"] = str(dest.relative_to(ROOT))
        # Answers.
        if (j.get("answers_json") or "").strip():
            try:
                (jdir / "answers.json").write_text(
                    json.dumps(json.loads(j["answers_json"]), ensure_ascii=False, indent=2),
                    encoding="utf-8")
                record["answers_file"] = str((jdir / "answers.json").relative_to(ROOT))
            except json.JSONDecodeError:
                pass
        # Screenshot.
        sp = j.get("screenshot_path")
        if sp:
            src = ROOT / sp if not Path(sp).is_absolute() else Path(sp)
            if src.exists():
                dest = jdir / f"screenshot{src.suffix}"
                shutil.copy2(src, dest)
                record["screenshot_file"] = str(dest.relative_to(ROOT))
        (jdir / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        index.append(record)

    (APPLICATIONS_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # Dashboard.
    applied = [r for r in index if r["status"] in ("applied", "skipped", "failed")]
    ready = [r for r in index if r["status"] == "ready"]
    print(f"\n📋 APPLICATIONS — artifacts under {APPLICATIONS_DIR.relative_to(ROOT)}/  "
          f"(index.json + per-job folders)\n")
    print(f"DONE ({len(applied)}):")
    for r in applied:
        icon = {"applied": "✓", "skipped": "↷", "failed": "✗"}.get(r["outcome"] or r["status"], "?")
        print(f"  {icon} [{r['job_id']:>3}] {(r['title'] or '')[:38]:<38} @ {(r['company'] or '')[:18]:<18}"
              f" {r['applied_at'] or ''}")
        print(f"        résumé: {r.get('resume_file','—')} | answers: {r.get('answers_file','—')} "
              f"| shot: {r.get('screenshot_file','—')}")
    print(f"\nREADY to submit ({len(ready)}):")
    for r in ready:
        print(f"  • [{r['job_id']:>3}] {(r['title'] or '')[:38]:<38} @ {(r['company'] or '')[:18]:<18}"
              f" | résumé: {r.get('resume_file','—')}")
    print(f"\nFunnel: {store.stats()}")
    return 0


def cmd_keys(args) -> int:
    """Key health. Default = Apify tokens; `--llm` = Grok/Groq keys. `--reset` clears flags."""
    if args.llm:
        from execution import llm as _llm
        pool = _llm._grok_pool()
        if args.reset:
            print(f"reset {pool.reset(args.reset)} Grok key(s) → unknown.")
            return 0
        rows = pool.status_table()
        if not rows:
            print("No Grok keys configured. Set XAI_API_KEY (one or many) and/or "
                  "XAI_API_KEY_1 / _2 / … in .env.")
            return 0
        icons = {"healthy": "✓", "unknown": "?", "throttled": "⏳", "invalid": "✗"}
        print(f"{len(rows)} Grok key(s):\n")
        for r in rows:
            err = f"  — {r['last_error']}" if r["last_error"] else ""
            print(f"  {icons.get(r['status'], '?')} [{r['n']}] {r['hint']}   "
                  f"{r['status']:<9} checked={r['checked_at']}{err}")
        print("\nlegend: ✓ healthy · ? unused · ⏳ throttled (TPM, auto-recovers ~90s) · "
              "✗ invalid.  Multiple keys rotate automatically on a 429.")
        return 0
    if args.reset:
        return _run(SCRAPE, "--reset-keys", args.reset)
    return _run(SCRAPE, "--keys")


def cmd_sources(args) -> int:
    """Portals/plugins available right now (incl. custom plugins)."""
    return _run(SCRAPE, "--list")


def cmd_serve(args) -> int:
    """Launch the local control-panel web app (FastAPI backend at --port,
    serving the built web/dist frontend if present; otherwise API-only —
    run the Vite dev server separately with `npm run dev` inside web/)."""
    import uvicorn
    print(f"Serving control-panel API on http://{args.host}:{args.port} "
          f"(Ctrl+C to stop). API docs: http://{args.host}:{args.port}/docs")
    uvicorn.run("server.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_reset(args) -> int:
    """Wipe the pipeline back to a clean sheet. Always clears the DB (jobs + runs)
    and the jobs.json export; --hard also clears tailored/ and applications/."""
    counts = store.stats()
    total = sum(counts.values())
    scope = "DB (jobs + runs) + jobs.json export"
    if args.hard:
        scope += " + tailored/ + applications/ (ALL generated résumés and apply artifacts)"
    if not args.yes:
        print(f"This will permanently clear: {scope}.")
        print(f"Current store: {total} job(s) across {len(counts)} status(es) — {counts}")
        reply = input("Type 'yes' to proceed: ").strip().lower()
        if reply != "yes":
            print("Aborted — nothing was cleared.")
            return 1
    summary = store.reset(hard=args.hard)
    print(f"✓ cleared {summary['jobs']} job(s), {summary['runs']} run(s)"
          f"{' , jobs.json' if summary['jobs_json'] else ''}.")
    if args.hard:
        print(f"  hard reset: cleared {summary['tailored_dirs']} tailored/ dir(s), "
              f"{summary['application_dirs']} applications/ dir(s).")
    print("Clean sheet — ready for a fresh `main.py search`.")
    return 0


def cmd_stats(args) -> int:
    """Pipeline funnel: job counts per status."""
    counts = store.stats()
    order = ["scraped", "matched", "tailored", "ready", "applied", "skipped", "failed",
             "rejected"]
    print("Pipeline funnel:")
    for s in order:
        if s in counts:
            print(f"  {s:<9} {counts[s]}")
    extra = {k: v for k, v in counts.items() if k not in order}
    for k, v in extra.items():
        print(f"  {k:<9} {v}")
    print(f"  {'TOTAL':<9} {sum(counts.values())}")
    return 0


def cmd_rank(args) -> int:
    """Ranked match list. Default = deterministic Python matcher; `--llm grok|api|deepseek`
    reranks a shortlist by résumé fit (judging the JD duties, not the title)."""
    if args.llm == "python":
        if args.eligible or args.jobs or args.save:
            print("note: --eligible/--jobs/--save apply only to the LLM reranker "
                  "(--llm grok|deepseek|api); ignored for the Python matcher.",
                  file=sys.stderr)
        return _run(MATCH, "--show")
    env = _llm_env(args.llm)
    a = ["--limit", str(args.limit)]
    if args.eligible:
        a.append("--eligible")
    if args.jobs:
        a += ["--jobs", args.jobs]
    if args.save:
        a.append("--save")
    return _run(LLM_RANK, *a, env=env)


HELP_DESC = """\
main.py — one terminal entrypoint for the AI job-search pipeline.

Commands work bare or as flags:  main.py search  ==  main.py --search
Pipeline runs left to right:

    search → lists → [rank] → prep → apply → log → report

━━━ SCRAPE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  search        Scrape portals, score every result, auto-reject hard-nos.
                Ends with a SOURCE REPORT: every discovered plugin, whether it
                ran, what it found, and why an unavailable one was skipped.
    --queries   Comma-separated role keywords  (e.g. "detection eng,appsec")
    --locations Comma-separated cities         (e.g. "Bangalore,Remote")
    --source    Portal plugin name, or 'all' to run every available one
                  (default: all)
    --days N    Recency window (LinkedIn's date filter only; other portals
                  ignore it)                    (default: 7; 0 = no filter)
    --limit N   Max results per plugin per query×location  (default 30)
    --workers N Plugins fetched in parallel (a plugin's own query×location
                  combos still run one at a time)  (default 8)

━━━ TRIAGE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  lists         Show the four classified tiers, best-score-first in each.
    --raw       Also dump every job row with its classification tag

  stats         Pipeline funnel — count of jobs at each status.

  rank          LLM rerank matched jobs by résumé fit (supplements keyword score).
    --llm       Provider: nvidia | grok | deepseek | api  (default: python scorer)
    --limit N   Shortlist size to rerank  (default 20)
    --eligible  Rank only eligible best-match jobs
    --jobs "…"  Rank only these comma-separated job ids
    --save      Persist llm_score + llm_reason to the store

  reject        Park hard-nos in 'rejected' so they never re-appear in lists/prep.
                (search does this automatically; re-run after score tweaks.)
    --by-llm    Reject jobs the LLM reranker scored below the cutoff instead
                (needs `rank --save` first)

  rejected      Show the rejected list — excluded from all ranking and prep.

━━━ PREP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  prep          Write JD briefs → tailor résumé → draft answers. Advances to 'ready'.
    --llm       Who writes: nvidia | grok | deepseek | api | claude
                  nvidia   — NVIDIA NIM (Kimi-k2.6 primary, Mistral-large-3 backup)
                  grok     — Groq free tier (rate-limited)
                  deepseek — DeepSeek API (paid, cheap, no TPM wall)
                  api      — Anthropic API
                  claude   — this Claude Code session (free, manual)

    JOB SELECTION (pick one, or omit for all pending):
    --eligible      ✅ Best-match jobs only (apply with master résumé as-is)
    --needs-mod     ✏️  Jobs that need résumé tailoring (modified copy, master untouched)
    --stretch       🧗 Stretch jobs — low-fit security roles needing a heavy rewrite;
                       you opt in and review each PDF before applying
    --llm-best      LLM reranker's top picks (needs `rank --save` first)
    --jobs "1,2,3"  Specific job ids

    OTHER:
    --modify-resume Also tailor eligible jobs (they use master as-is by default)
    --limit N       Cap how many jobs are processed

━━━ APPLY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  apply         Fill application forms, best-matched first. STOPS for human review —
                YOU click submit. Never auto-submits.
    --limit N   Max packets per run  (default 3)
    --source    Only jobs from this portal
    --query     Filter by title/company text
    --jobs "…"  Only these job ids

  log           Record your outcome after submitting.
    --job N     Job id                          (required)
    --outcome   applied | skipped | failed      (required)
    --note "…"  Free-text note
    --screenshot  Path to a review screenshot

  applied       Show the log of submitted / skipped / failed jobs.

━━━ UTILITIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  sources       List all auto-discovered portal plugins and their availability.

  keys          Apify key health and token usage.
    --llm       Show LLM provider key health instead (auto-rotates on 429)
    --reset     all | exhausted | invalid | <key-hint>

  reset         Wipe the pipeline back to a clean sheet. Asks to confirm.
    --hard      Also clear tailored/ résumés and applications/ artifacts
    --yes       Skip the confirmation prompt

  report        Build the full application dashboard.

  serve         Launch the local control-panel web app (FastAPI + frontend).
    --host      Bind address (default: 127.0.0.1 — localhost only)
    --port      Port (default: 8000)
    --reload    Auto-reload on code changes (dev only)

Tip: `main.py <command> -h` shows that command's flags.
"""

EXAMPLES = """\
examples:
  # Search & triage
  main.py search --queries "detection eng,appsec,cloud security" --locations "Bangalore,Remote" --days 14
  main.py search --queries "security engineer" --source remoteok    # one portal only
  main.py search --queries "security engineer" --days 2             # all portals (default)
  main.py lists
  main.py lists --raw

  # LLM rank, then prep by tier
  main.py rank --llm nvidia --eligible --save
  main.py prep --llm-best --llm nvidia          # LLM's top picks
  main.py prep --eligible  --llm nvidia          # keyword-eligible, no tailoring
  main.py prep --needs-mod --llm nvidia          # tailor résumés
  main.py prep --stretch   --llm nvidia          # stretch / pentest / senior-IC roles
  main.py prep --jobs "42,57" --llm nvidia       # specific ids

  # Apply & log
  main.py apply --limit 3
  main.py apply --query crowdstrike
  main.py log --job 212 --outcome applied --note "sent cover letter"
  main.py applied

  # Housekeeping
  main.py reject                                 # park hard-nos
  main.py reject --by-llm                        # use LLM scores to filter
  main.py stats
  main.py reset                                  # clean-sheet the DB (asks to confirm)
  main.py reset --hard --yes                     # + wipe tailored/ + applications/, no prompt
"""


COMMANDS = ("search", "lists", "prep", "apply", "log", "applied", "report",
            "reject", "rejected", "keys", "sources", "stats", "rank", "reset", "serve")


def _normalize_argv(argv: list[str]) -> list[str]:
    """Accept commands as --tags too: `main.py --apply ...` == `main.py apply ...`.
    Finds the first `--<command>` token at any position so that `-v --lists` works.
    Bare-word commands and the new position-independent form both keep working."""
    argv = list(argv)
    for i, arg in enumerate(argv):
        if arg.startswith("--") and arg[2:] in COMMANDS:
            argv[i] = arg[2:]
            break
    return argv


def main(argv=None) -> int:
    argv = _normalize_argv(sys.argv[1:] if argv is None else list(argv))
    ap = argparse.ArgumentParser(
        prog="main.py",
        usage="main.py [-v|-vv] [-h] <--command> [options]",
        description=HELP_DESC,
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v verbose (scoring details, actor IDs); -vv debug (full prompts)")
    # Every command + its flags is documented ONCE in HELP_DESC above. Suppressing the
    # whole subparsers action keeps `-h` from re-listing the commands (no duplication).
    sub = ap.add_subparsers(dest="cmd", metavar="<--command>", required=True,
                            help=argparse.SUPPRESS)

    p = sub.add_parser("keys")
    p.add_argument("--llm", action="store_true", help="show Grok/Groq keys instead of Apify")
    p.add_argument("--reset", metavar="WHICH", default=None)
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser(
        "reset", help="Wipe the pipeline back to a clean sheet.",
        description="Clear the job store (and, with --hard, all generated artifacts) "
                    "back to a clean sheet. Destructive — asks for confirmation unless --yes.")
    p.add_argument("--hard", action="store_true",
                   help="also clear tailored/ (résumé variants) and applications/ (apply artifacts)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "serve", help="Launch the local control-panel web app (FastAPI + frontend).",
        description="Launch the local control-panel web app. Binds localhost only — "
                    "the API can run subprocesses, edit .env, and compile the résumé, "
                    "so never expose it beyond your machine.")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev only)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("rank")
    p.add_argument("--llm", choices=["python", "grok", "deepseek", "nvidia", "api"], default="python",
                   help="python = deterministic matcher; grok/deepseek/nvidia/api = LLM rerank")
    p.add_argument("--limit", type=int, default=20, help="shortlist size to rerank")
    p.add_argument("--eligible", action="store_true", help="only eligible best-match jobs")
    p.add_argument("--jobs", default=None, help="comma-separated job ids")
    p.add_argument("--save", action="store_true", help="persist llm_score/llm_reason")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser(
        "search", help="Scrape portals, score every result, auto-reject hard-nos.",
        description="Scrape portals, score every result, auto-reject hard-nos. "
                    "Prints a SOURCE REPORT covering every discovered plugin.")
    p.add_argument("--locations", default="Hyderabad,Bengaluru,India",
                   help="comma-separated cities, e.g. 'Bangalore,Remote' (default: %(default)s)")
    p.add_argument("--queries", default="security engineer,detection engineer,vulnerability",
                   help="comma-separated role keywords (default: %(default)s)")
    p.add_argument("--days", type=int, default=7,
                   help="recency window in days, applied to the LinkedIn plugin's date filter "
                        "(default: %(default)s; other portals ignore this)")
    p.add_argument("--limit", type=int, default=30,
                   help="max results per plugin per query×location combo (default: %(default)s)")
    p.add_argument("--source", default="all",
                   help="portal plugin name (linkedin/naukri/indeed/remoteok/...) or 'all' "
                        "to run every available plugin (default: %(default)s)")
    p.add_argument("--workers", type=int, default=8,
                   help="max plugins fetched in parallel; a single plugin's own combos still "
                        "run sequentially (default: %(default)s)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("lists")
    p.add_argument("--raw", action="store_true")
    p.set_defaults(func=cmd_lists)

    p = sub.add_parser("applied")
    p.set_defaults(func=cmd_applied)

    p = sub.add_parser("reject")
    p.add_argument("--by-llm", action="store_true",
                   help="reject jobs the Grok reranker scored below the best-cutoff")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("rejected")
    p.set_defaults(func=cmd_rejected)

    p = sub.add_parser("prep")
    p.add_argument("--llm", choices=["claude", "grok", "deepseek", "nvidia", "api"], default="claude")
    p.add_argument("--eligible", action="store_true",
                   help="only eligible best-match jobs (keyword score)")
    p.add_argument("--llm-best", action="store_true",
                   help="only jobs the Grok reranker scored best (needs rank --save first)")
    p.add_argument("--needs-mod", action="store_true",
                   help="only non-best jobs that need résumé tailoring")
    p.add_argument("--stretch", action="store_true",
                   help="only stretch jobs (security-adjacent, low-fit; needs heavy rewrite)")
    p.add_argument("--jobs", default=None, help="comma-separated job ids")
    p.add_argument("--modify-resume", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_prep)

    p = sub.add_parser("apply")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--source", default=None)
    p.add_argument("--query", default=None, help="filter ready jobs by title/company text")
    p.add_argument("--jobs", default=None, help="comma-separated job ids")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("log")
    p.add_argument("--job", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=["applied", "skipped", "failed"])
    p.add_argument("--note", default=None)
    p.add_argument("--screenshot", default=None)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    args = ap.parse_args(argv)
    # Thread verbosity level into subprocess env via JOBSEARCH_VERBOSITY so all
    # skill scripts spawned by _run() inherit the same level without re-parsing flags.
    os.environ["JOBSEARCH_VERBOSITY"] = str(min(2, args.verbose or 0))
    store.init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
