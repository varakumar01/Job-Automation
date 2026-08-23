#!/usr/bin/env python3
"""main.py — one terminal entrypoint for the whole job-search pipeline.

Wires the per-stage skill scripts into repeatable commands so the pipeline runs
from the terminal, optionally automated with Grok (or the Anthropic API), and
stores every artifact itself. See PLAN.md §11.

Commands can be written bare (`apply`) or as a flag (`--apply`); options always
take `--`. The full reference (every command + its flags + examples) lives in the
argparse help — run `python3 main.py -h` (it is the single source of truth, so it
is not duplicated here). Pipeline order:

    search → lists → [arrange] → prep → apply → log → report

`search` only scrapes — new jobs land at 'scraped' ("Unarranged"), unscored.
`arrange` (alias: `rank`) is what sorts Unarranged into eligible/needs_mod/stretch:
it runs the deterministic keyword matcher first (scraped → matched, cheap, scores
everything), auto-rejects off-profile hard-nos, then LLM-reranks a shortlist by
résumé fit. 2026-08-23: `search` used to also match/rerank/auto-reject in one shot;
split out so there's a visible "just found, not yet judged" state (PLAN §9).

Job selection (so you can target just the best matches): `prep --eligible` /
`prep --jobs "1,2"`, and `apply --query <text>` / `apply --jobs "1,2"` /
`apply --source <portal>`.

LLM choice (`prep --llm`, `arrange --llm`): auto (default) tries nvidia → grok →
deepseek → api and uses whichever actually answers right now; `prep` additionally
accepts claude = this Claude Code session (free, manual, no automation). Full
provider/default detail lives in `-h`, not here.
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
# llama-3.3-70b-versatile (the prior default here) was deprecated by Groq on 2026-06-17
# (email notice; free/developer tier only) — probes started failing with a 404
# model_not_found. openai/gpt-oss-120b is Groq's own suggested replacement for it.
GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"

# DeepSeek — OpenAI-compatible, no free-tier TPM wall (concurrency-based), very cheap.
# Reuses the `grok` provider path with a different base/key/model. Needs DEEPSEEK_API_KEY.
# `deepseek-chat` is the general model (maps to deepseek-v4-flash); override via LLM_MODEL.
DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# NVIDIA NIM — OpenAI-compatible, 100+ open-weight models, free-tier ~40 req/min.
# Reuses the `grok` provider path with NVIDIA's base/key/model. Needs NVIDIA_API_KEY (nvapi-…).
# Model selection (re-verified live 2026-08-22 — both prior picks had gone dead):
#   Primary   moonshotai/kimi-k2.6 — 404 "Function ... Not found for account" (in the NIM
#             catalog but not entitled on this account). REPLACED.
#   Backup    mistralai/mistral-large-3-675b-instruct-2512 — 410 Gone, end-of-life. REPLACED.
#   Current primary/backup below were both confirmed reachable with a live "Say OK." probe
#   against this account's actual key on 2026-08-22 — re-verify with
#   `execution/llm_health.py` (or scrape.py --keys --llm) before trusting this comment again,
#   NVIDIA's catalog and entitlements change without notice.
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "nvidia/nemotron-3-super-120b-a12b"
NVIDIA_BACKUP_MODEL = "mistralai/mistral-nemotron"
# Sent as the FIRST line of the system prompt on the NVIDIA path — a legacy nudge,
# kept because it's harmless, but NOT the real fix (see NVIDIA_EXTRA_BODY below).
NVIDIA_SYSTEM_PREFIX = "detailed thinking off"
# The ACTUAL documented control for `nemotron-3-super-*`: reasoning defaults ON
# and is toggled via this request-body field, not a system-prompt string (that's
# NVIDIA's mechanism for a DIFFERENT model family — see PLAN §9 2026-08-23 for the
# full investigation: sources, the 6/6-jobs-failed repro, and why NVIDIA_SYSTEM_
# PREFIX above doesn't fix it). Merged into the request via LLM_EXTRA_BODY
# (execution/llm.py); a finish_reason=="length" retry there is the safety net.
NVIDIA_EXTRA_BODY = json.dumps({"chat_template_kwargs": {"enable_thinking": False}})


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
    # scrape.py itself persists every plugin's rows the moment that plugin
    # finishes (not after the whole run), so a Ctrl-C here only loses whatever
    # plugin(s) were still in flight.
    scrape_args = ["--source", args.source, "--queries", args.queries,
                   "--locations", args.locations, "--limit", str(args.limit),
                   "--workers", str(args.workers)]
    if getattr(args, "recheck", False):
        scrape_args.append("--recheck")
    rc = _run(SCRAPE, *scrape_args, env=env)
    # 2026-08-23 (PLAN §9): search is scrape-ONLY now — new/re-seen rows land at
    # 'scraped' ("Unarranged") unscored. Matching, auto-reject, and LLM rerank all
    # moved to `arrange` (alias `rank`), which is the single action that sorts
    # Unarranged into eligible/needs_mod/stretch. Run `main.py arrange` next.
    unarranged = len(store.get_jobs(status="scraped"))
    print(f"\n{unarranged} job(s) awaiting arrangement — run `main.py arrange` to sort "
          f"them into eligible/needs_mod/stretch.")
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
    reject the jobs the Grok reranker scored below the best-cutoff (needs `arrange --save`)."""
    if args.by_llm:
        n = _reject_by_llm()
        counts = store.stats()
        print(f"Grok-filtered: rejected {n} low-scored job(s) → 'rejected'. "
              f"({counts.get('matched', 0)} remain at 'matched', "
              f"{counts.get('rejected', 0)} rejected total.)")
        if n == 0:
            print("  (nothing rejected — jobs may lack an llm_score; run "
                  "`main.py arrange --llm grok --eligible --save` first.)")
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
        print("No rejected jobs yet. Run `main.py reject` (or `arrange`) to park off-profile jobs.")
        return 0
    print(f"🚫 REJECTED — non-relevant jobs ({len(rows)}); excluded from ranking/prep:\n")
    for j in _ordered(rows):
        print(f"  {(j.get('match_score') or 0):>5.1f}  {(j.get('title') or '')[:42]:<42} @ "
              f"{(j.get('company') or '')[:20]:<20} | {(j.get('location') or '')[:22]}")
    return 0


def _print_lists(raw: bool = False) -> None:
    # Unarranged = scraped, not yet matched/scored — `main.py arrange` sorts these
    # into the tiers below (2026-08-23, PLAN §9: search no longer matches inline).
    unarranged = store.get_jobs(status="scraped")
    print(f"\n🗂  UNARRANGED — scraped, not yet sorted ({len(unarranged)})")
    if unarranged:
        print("   Run `main.py arrange` to sort these into eligible/needs_mod/stretch.")

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

    # LIST 1 — the arranged pool (matched & sorted, still unactioned). Summarized by
    # portal so 300+ rows don't flood the terminal; `--raw` dumps the full list.
    # Named "MATCHED" (not "scraped") to avoid clashing with the 'scraped' status,
    # which is now the separate Unarranged bucket printed above.
    by_src: dict[str, int] = {}
    for j in jobs:
        by_src[j.get("source") or "?"] = by_src.get(j.get("source") or "?", 0) + 1
    src_summary = ", ".join(f"{k} {v}" for k, v in sorted(by_src.items()))
    print(f"\n📊 MATCHED — arranged & sorted ({len(jobs)}) [{src_summary or 'none'}]")
    if raw:
        for j in _ordered(jobs):
            print(_row(j) + f"  <{classify(j)}>")
    else:
        print("   (full list: `main.py lists --raw`  or  `main.py arrange`)")

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
    if provider == "auto":
        # Callers must resolve "auto" to a concrete provider via _pick_llm_provider()
        # BEFORE calling _llm_env — the catch-all below would otherwise silently hand
        # back session mode for an unresolved "auto" instead of erroring (see
        # cmd_arrange/cmd_prep for the resolve-then-call pattern).
        raise ValueError("_llm_env('auto') — resolve via _pick_llm_provider() first")
    if provider == "grok":
        return {"LLM_PROVIDER": "grok",
                "XAI_BASE_URL": os.environ.get("XAI_BASE_URL") or GROQ_BASE,
                "LLM_MODEL": os.environ.get("LLM_MODEL") or GROQ_MODEL,
                # ALWAYS clear (code-reviewer MAJOR, 2026-08-23): a stray LLM_EXTRA_BODY
                # left in the shell/.env from a previous nvidia run would otherwise leak
                # NVIDIA-only fields (chat_template_kwargs) into xAI's request body.
                "LLM_EXTRA_BODY": ""}
    if provider == "deepseek":
        # OpenAI-compatible → reuse the grok backend with DeepSeek's base/model/key.
        # Feed DeepSeek's key into the slot the grok backend reads. ALWAYS set it (to ""
        # if missing) so a stray XAI_API_KEY (Groq) in the environment can't leak to
        # DeepSeek's server via the env merge — an empty key yields a clear missing-key error.
        env = {"LLM_PROVIDER": "grok",
               "XAI_BASE_URL": os.environ.get("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE,
               "LLM_MODEL": os.environ.get("LLM_MODEL") or DEEPSEEK_MODEL,
               "XAI_API_KEY": os.environ.get("DEEPSEEK_API_KEY") or "",
               "GROK_API_KEY": "",  # clear alias so _grok_pool can't discover a stale Groq key
               # ALWAYS clear (code-reviewer MAJOR, 2026-08-23) — same cross-provider
               # leakage risk as GROK_API_KEY above, but for NVIDIA's request-body knob.
               "LLM_EXTRA_BODY": ""}
        return env
    if provider == "nvidia":
        # OpenAI-compatible NIM endpoint → reuse the grok backend with NVIDIA's base/key/model.
        # ALWAYS set both XAI_API_KEY and GROK_API_KEY explicitly (to "" if missing) so neither
        # Groq key alias can leak to NVIDIA's server via the subprocess env merge.
        # LLM_BACKUP_MODEL is read by execution/llm.py complete() for a one-shot fallback.
        # LLM_SYSTEM_PREFIX is a harmless legacy nudge (see NVIDIA_SYSTEM_PREFIX above).
        # LLM_EXTRA_BODY carries the ACTUAL reasoning-mode toggle for nemotron-3-super
        # (see NVIDIA_EXTRA_BODY above) so <think> doesn't eat the whole token budget.
        return {"LLM_PROVIDER": "grok",
                "XAI_BASE_URL": os.environ.get("NVIDIA_BASE_URL") or NVIDIA_BASE,
                "LLM_MODEL": os.environ.get("LLM_MODEL") or NVIDIA_MODEL,
                "LLM_BACKUP_MODEL": os.environ.get("LLM_BACKUP_MODEL") or NVIDIA_BACKUP_MODEL,
                "XAI_API_KEY": os.environ.get("NVIDIA_API_KEY") or "",
                "GROK_API_KEY": "",  # clear alias so _grok_pool can't discover a stale Groq key
                "LLM_SYSTEM_PREFIX": os.environ.get("LLM_SYSTEM_PREFIX", NVIDIA_SYSTEM_PREFIX),
                "LLM_EXTRA_BODY": os.environ.get("LLM_EXTRA_BODY") or NVIDIA_EXTRA_BODY}
    if provider == "api":
        # ALWAYS clear (code-reviewer MINOR, 2026-08-23): a stray LLM_MODEL/
        # LLM_EXTRA_BODY left over from a previous nvidia/deepseek run would
        # otherwise leak into the Anthropic call — model() reads LLM_MODEL at
        # call time, so a nemotron/deepseek model id here would produce an
        # opaque Anthropic 404 instead of falling back to the api default.
        return {"LLM_PROVIDER": "api", "LLM_MODEL": "", "LLM_EXTRA_BODY": ""}
    return {"LLM_PROVIDER": "session"}


def _pick_llm_provider(force_recheck: bool = False) -> tuple[str | None, dict]:
    """Auto-select the first LLM provider that actually answers right now
    (nvidia -> grok -> deepseek -> api). See execution/llm_health.py — the
    manual `--llm nvidia` etc. choice still exists for when you want to force
    one, but nothing here should assume yesterday's working provider is
    today's: accounts, free-tier grants, and model entitlements change
    without notice (main.py's NVIDIA_MODEL comment has the receipts)."""
    from execution import llm_health
    return llm_health.pick_provider(_llm_env, force_recheck=force_recheck)


def cmd_prep(args) -> int:
    if args.llm == "claude":
        print("--llm=claude → session mode: the writing is done by the Claude Code "
              "session.\nJust ask Claude: \"run jd-understander and resume-tailor for "
              "the matched jobs\" — it will prepare/save each stage. "
              "(No automation here by design.)")
        return 0
    provider = args.llm
    if provider == "auto":
        provider, probe_results = _pick_llm_provider(
            force_recheck=getattr(args, "recheck_providers", False))
        if provider is None:
            # Unlike rank, prep has no keyword-matcher fallback — writing a JD brief and
            # tailoring a résumé genuinely requires a model. Report why and stop rather
            # than silently degrading to session mode (that would be the same
            # _llm_env('auto') trap this function exists to avoid).
            reasons = "; ".join(f"{p}: {r['detail']}" for p, r in probe_results.items())
            print(f"⚠ no working LLM provider ({reasons}) — nothing to prep with. "
                  f"Run `main.py keys --llm` for details, or pass --llm claude for "
                  f"session mode.", file=sys.stderr)
            return 1
        print(f"(auto-picked provider: {provider})")
    env = _llm_env(provider)
    lim = ["--limit", str(args.limit)] if args.limit is not None else []
    store.init_db()

    # Resolve which jobs to prep: explicit --jobs wins; else --eligible (best-match) and/
    # or --needs-mod (non-best, need tailoring) select by classification; else everything
    # pending. `ids` (or None for all) threads through all stages.
    ids = store.parse_ids(args.jobs)
    if ids is None and args.llm_best:
        # Grok/LLM reranker decides the best (not the keyword score) — needs `arrange
        # --llm grok --eligible --save` first so jobs carry an llm_score.
        ids = [j["id"] for j in store.get_jobs(status="matched") if eligibility.llm_best(j)]
        if not ids:
            print("no Grok-scored best jobs at 'matched'. Run "
                  "`main.py arrange --llm grok --eligible --save` first, then retry --llm-best.")
            return 0
    wanted = {c for c, on in (("eligible", args.eligible),
                              ("needs_mod", args.needs_mod),
                              ("stretch", args.stretch)) if on}
    if ids is None and wanted:
        ids = [j["id"] for j in store.get_jobs(status="matched") if classify(j) in wanted]
        if not ids:
            print(f"no {' or '.join(sorted(wanted))} jobs at 'matched' to prep "
                  "(they may already be tailored). Nothing to do.")
            return 0
    jobs_arg = ["--jobs", ",".join(map(str, ids))] if ids else []
    id_set = set(ids) if ids else None
    scope = f"{len(ids)} selected job(s)" if ids else "all pending jobs"
    print(f"Prep via --llm={provider} (model {env.get('LLM_MODEL', 'default')}) — {scope}.")

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
    # Prep ends here — a "tailored" job is apply-gate-ready. humanise-responder
    # (pre-drafting screening-question answers) is retired from this flow: no
    # direct-apply automation is ever planned, so there was nothing to feed
    # pre-drafted answers into. The skill script still exists if that changes.

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
        print("No applications logged yet. Apply to a tailored job, then "
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
    if args.force:
        extra.append("--force")
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
                if j["status"] in ("tailored", "applied", "skipped", "failed")]
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
    ready = [r for r in index if r["status"] == "tailored"]
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
    """Key health. Default = Apify tokens; `--llm` = Grok/Groq keys + LLM provider health.
    `--reset` clears flags."""
    if args.llm:
        from execution import llm as _llm
        from execution import llm_health
        pool = _llm._grok_pool()
        if args.reset:
            print(f"reset {pool.reset(args.reset)} Grok key(s) → unknown.")
            return 0
        rows = pool.status_table()
        if not rows:
            print("No Grok keys configured. Set XAI_API_KEY (one or many) and/or "
                  "XAI_API_KEY_1 / _2 / … in .env.")
        else:
            icons = {"healthy": "✓", "unknown": "?", "throttled": "⏳", "invalid": "✗"}
            print(f"{len(rows)} Grok key(s):\n")
            for r in rows:
                err = f"  — {r['last_error']}" if r["last_error"] else ""
                print(f"  {icons.get(r['status'], '?')} [{r['n']}] {r['hint']}   "
                      f"{r['status']:<9} checked={r['checked_at']}{err}")
            print("\nlegend: ✓ healthy · ? unused · ⏳ throttled (TPM, auto-recovers ~90s) · "
                  "✗ invalid.  Multiple keys rotate automatically on a 429.")

        # LLM provider health (nvidia/grok/deepseek/api) — last-known state from
        # the cache written by `main.py arrange --llm auto`; does NOT probe live
        # (use `arrange --llm auto --recheck-providers` for that).
        print("\nLLM provider health (last known — `arrange --llm auto` probe live):\n")
        for row in llm_health.status_table():
            glyph = "✓" if row["ok"] else ("✗" if row["ok"] is False else "?")
            # Some provider error strings embed a raw newline (seen from the
            # xAI/DeepSeek error bodies) — collapse it so the row stays one line.
            detail = " ".join(row["detail"].split())
            print(f"  {glyph} {row['provider']:<9} {detail:<50} checked={row['checked_at']}")
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
    order = ["scraped", "matched", "tailored", "applied", "skipped", "failed",
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


def cmd_arrange(args) -> int:
    """Sorts Unarranged (scraped) jobs into eligible/needs_mod/stretch, then LLM-reranks
    a shortlist by résumé fit (judging the JD duties, not the title). Alias: `rank`.

    Three steps, always in this order: (1) the deterministic keyword matcher scores
    every scraped job and advances it scraped→matched — cheap, always runs, handles
    the whole backlog; (2) off-profile hard-nos are auto-rejected so the LLM never
    spends tokens scoring them; (3) `--llm auto` (default) tries nvidia -> grok ->
    deepseek -> api and uses whichever actually answers right now, or `--llm
    nvidia|grok|deepseek|api` forces one, to refine a shortlist's ordering within its
    tier. If every provider is currently dead, steps 1-2 (the sort) already happened —
    only the LLM refinement is skipped, falling back to the keyword matcher's own
    ordering (`match.py --show`)."""
    match_rc = _run(MATCH)
    if match_rc != 0:
        print(f"⚠ profile-matcher exited {match_rc} — scores/coverage above may be "
              f"stale or incomplete (see its output above for why).", file=sys.stderr)
    # Park off-profile jobs BEFORE the LLM step so it never scores jobs about to be
    # rejected (moved here from `search` 2026-08-23 — PLAN §9).
    rejected = _auto_reject()
    if rejected:
        print(f"🚫 auto-rejected {rejected} off-profile job(s) → rejected list "
              f"(see `main.py rejected`).")
    provider = args.llm
    if provider == "auto":
        provider, probe_results = _pick_llm_provider(force_recheck=args.recheck_providers)
        if provider is None:
            reasons = "; ".join(f"{p}: {r['detail']}" for p, r in probe_results.items())
            print(f"⚠ no working LLM provider ({reasons}) — jobs are already sorted by "
                  f"keyword match_score; showing that ranking instead. Run `main.py keys "
                  f"--llm` for details.", file=sys.stderr)
            return _run(MATCH, "--show")
        print(f"(auto-picked provider: {provider})")
    env = _llm_env(provider)
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

    search → lists → [arrange] → prep → apply → log → report

━━━ SCRAPE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  search        Scrape portals only. New/re-seen jobs land at 'scraped'
                (Unarranged), unscored — run `arrange` afterwards to sort them.
                Ends with a SOURCE REPORT: every discovered plugin, whether it
                ran, what it found, and why an unavailable one was skipped.
    --queries   Comma-separated role keywords  (e.g. "detection eng,appsec")
    --locations Comma-separated cities         (e.g. "Bangalore,Remote")
    --source    Portal plugin name, or 'all' to run every available one
                  (default: all)
    --days N    Recency window (LinkedIn's date filter only; other portals
                  ignore it)                    (default: 2; 0 = no filter)
    --limit N   Max results per plugin per query×location  (default 30)
    --workers N Plugins fetched in parallel (a plugin's own query×location
                  combos still run one at a time)  (default 0 = auto, one
                  worker per available plugin)
    --recheck   Re-seen jobs currently 'rejected' go back to 'scraped'
                  (Unarranged) for re-evaluation instead of staying invisible
                  forever (off by default)
                Note: plugins that ignore location (most of them) run each query
                  ONCE, not once per location; jobs already saved persist per-plugin
                  as the run goes, so Ctrl-C only loses whatever plugin was still
                  in flight, not the whole run.

━━━ TRIAGE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  lists         Show Unarranged + the four classified tiers, best-score-first
                in each.
    --raw       Also dump every matched job row with its classification tag

  stats         Pipeline funnel — count of jobs at each status.

  arrange       Sorts Unarranged (scraped) jobs into eligible/needs_mod/stretch,
                then LLM-reranks a shortlist by résumé fit. Alias: `rank`.
                Always runs the keyword matcher + auto-reject first (cheap,
                covers the whole backlog); the LLM step only refines ordering
                within a tier and is skipped if every provider is dead.
    --llm       auto (default) tries nvidia→grok→deepseek→api and uses whichever
                  actually answers right now; or force one: nvidia | grok | deepseek | api.
                  Falls back to the keyword matcher's own ordering if every provider is dead.
    --recheck-providers  ignore the 10-min provider-health cache, re-probe now
    --limit N   Shortlist size for the LLM refinement step  (default 20)
    --eligible  Refine only eligible best-match jobs
    --jobs "…"  Refine only these comma-separated job ids
    --save      Persist llm_score + llm_reason to the store

  reject        Park hard-nos in 'rejected' so they never re-appear in lists/prep.
                (arrange does this automatically; re-run after score tweaks.)
    --by-llm    Reject jobs the LLM reranker scored below the cutoff instead
                (needs `arrange --save` first)

  rejected      Show the rejected list — excluded from all ranking and prep.

━━━ PREP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  prep          Write JD briefs → tailor résumé. Advances to 'tailored' (the apply gate).
    --llm       Who writes: auto (default) | nvidia | grok | deepseek | api | claude
                  auto     — tries nvidia→grok→deepseek→api, uses whichever actually
                             answers right now; stops (no keyword fallback — writing
                             needs a model) if every provider is currently dead
                  nvidia   — NVIDIA NIM (model ids in main.py's NVIDIA_MODEL/_BACKUP —
                             re-verified 2026-08-22; NIM entitlements change without notice)
                  grok     — Groq free tier (rate-limited)
                  deepseek — DeepSeek API (paid, cheap, no TPM wall)
                  api      — Anthropic API
                  claude   — this Claude Code session (free, manual; still available,
                             just no longer the default)
    --recheck-providers  with --llm auto, ignore the 10-min provider-health cache,
                  re-probe now

    JOB SELECTION (pick one, or omit for all pending):
    --eligible      ✅ Best-match jobs only (apply with master résumé as-is)
    --needs-mod     ✏️  Jobs that need résumé tailoring (modified copy, master untouched)
    --stretch       🧗 Stretch jobs — low-fit security roles needing a heavy rewrite;
                       you opt in and review each PDF before applying
    --llm-best      LLM reranker's top picks (needs `arrange --save` first)
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

  # Arrange (sort + LLM rank), then prep by tier
  main.py arrange --llm nvidia --eligible --save
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
            "reject", "rejected", "keys", "sources", "stats", "arrange", "rank",
            "reset", "serve")


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
    p.add_argument("--llm", action="store_true",
                   help="show Grok/Groq keys + LLM provider health instead of Apify")
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

    p = sub.add_parser("arrange", aliases=["rank"])
    p.add_argument("--llm", choices=["auto", "grok", "deepseek", "nvidia", "api"], default="auto",
                   help="auto (default) = try nvidia→grok→deepseek→api, use whichever "
                        "actually answers right now; or force one provider. Falls back to "
                        "the deterministic keyword matcher if every provider is dead.")
    p.add_argument("--recheck-providers", action="store_true",
                   help="with --llm auto, ignore the cached provider health and re-probe "
                        "every candidate now (default: reuse a probe result up to 10 min old)")
    p.add_argument("--limit", type=int, default=20, help="shortlist size to rerank")
    p.add_argument("--eligible", action="store_true", help="only eligible best-match jobs")
    p.add_argument("--jobs", default=None, help="comma-separated job ids")
    p.add_argument("--save", action="store_true", help="persist llm_score/llm_reason")
    p.set_defaults(func=cmd_arrange)

    p = sub.add_parser(
        "search", help="Scrape portals into Unarranged. Run `arrange` next to sort them.",
        description="Scrape portals — new/re-seen jobs land at 'scraped' (Unarranged), "
                    "unscored. Prints a SOURCE REPORT covering every discovered plugin. "
                    "Run `main.py arrange` afterwards to sort into tiers.")
    p.add_argument("--locations", default="Hyderabad,Bengaluru,India",
                   help="comma-separated cities, e.g. 'Bangalore,Remote' (default: %(default)s)")
    p.add_argument("--queries", default="security engineer,detection engineer,vulnerability",
                   help="comma-separated role keywords (default: %(default)s)")
    p.add_argument("--days", type=int, default=2,
                   help="recency window in days, applied to the LinkedIn plugin's date filter "
                        "(default: %(default)s; other portals ignore this)")
    p.add_argument("--limit", type=int, default=30,
                   help="max results per plugin per query×location combo (default: %(default)s)")
    p.add_argument("--source", default="all",
                   help="portal plugin name (linkedin/wellfound/remoteok/...) or 'all' "
                        "to run every available plugin (default: %(default)s)")
    p.add_argument("--workers", type=int, default=0,
                   help="max plugins fetched in parallel; 0 (default) = auto, one worker per "
                        "available plugin. A single plugin's own combos still run sequentially")
    p.add_argument("--recheck", action="store_true",
                   help="re-seen jobs currently at 'rejected' are moved back to 'scraped' "
                        "(Unarranged) so `arrange` re-evaluates them (e.g. after an "
                        "eligibility rule change). Off by default.")
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
    p.add_argument("--llm", choices=["auto", "claude", "grok", "deepseek", "nvidia", "api"],
                   default="auto",
                   help="auto (default) tries nvidia→grok→deepseek→api and uses whichever "
                        "actually answers right now; claude = session mode (manual, no "
                        "automation); or force one provider.")
    p.add_argument("--recheck-providers", action="store_true",
                   help="with --llm auto, ignore the cached provider health and re-probe "
                        "every candidate now (default: reuse a probe result up to 10 min old)")
    p.add_argument("--eligible", action="store_true",
                   help="only eligible best-match jobs (keyword score)")
    p.add_argument("--llm-best", action="store_true",
                   help="only jobs the Grok reranker scored best (needs arrange --save first)")
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
    p.add_argument("--query", default=None, help="filter tailored jobs by title/company text")
    p.add_argument("--jobs", default=None, help="comma-separated job ids")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("log")
    p.add_argument("--job", type=int, required=True)
    p.add_argument("--outcome", required=True, choices=["applied", "skipped", "failed"])
    p.add_argument("--note", default=None)
    p.add_argument("--screenshot", default=None)
    p.add_argument("--force", action="store_true",
                    help="log even if the job isn't at status 'tailored'")
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
