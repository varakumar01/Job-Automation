"""apply-agent — the human-in-the-loop apply spine. PLAN §5 #6 + §6 (safety).

THIS SCRIPT NEVER TOUCHES A BROWSER AND NEVER SUBMITS ANYTHING. It is the
deterministic half of the apply step: it builds an **apply packet** for each
`ready` job (URL + tailored résumé path + the drafted answers + the screening
fields the human must supply), and — only AFTER a human has reviewed and clicked
submit — records the outcome and advances the job's status.

The browser half (open the posting, click Easy Apply, fill the fields, upload the
résumé, answer screening questions, screenshot, then STOP at the review gate) is
driven by the ORCHESTRATOR via the chrome-devtools MCP, following the procedure in
SKILL.md. Nothing here clicks Submit; the human does that.

Pipeline: reads `ready` jobs, and on `log` advances `ready → applied | skipped |
failed` with `outcome` + `applied_at`. Persists per job → resumable.

Usage::

    python3 .../apply.py packet [--limit 3] [--job N] [--source linkedin]  # apply packet(s), best-matched first
    python3 .../apply.py show                            # ready queue + apply log
    # AFTER the human reviews + submits (or decides to skip):
    python3 .../apply.py log --job N --outcome applied|skipped|failed [--note "..."]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ROOT = SKILL_DIR.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import store  # noqa: E402
from execution import candidate  # noqa: E402

# Conservative default batch size (PLAN §6: small batches, human-paced). The apply
# step is deliberately slow and supervised — do not raise this without a §9 decision.
DEFAULT_BATCH = 3
OUTCOMES = {"applied", "skipped", "failed"}

# Common LinkedIn Easy-Apply screening fields → which drafted answer to use.
# NOTE: read by the ORCHESTRATOR (not by Python code in this file) to map live form
# fields to drafted answers; anything not covered is a human input.
ANSWER_FIELD_HINTS = {
    "why_role": ("why do you want", "why are you interested", "why this role"),
    "why_company": ("why do you want to work", "why us", "why this company"),
    "relevant_experience": ("relevant experience", "experience with", "describe your experience"),
    "strengths": ("strengths", "why should we", "what makes you"),
    "cover_letter": ("cover letter", "additional information", "anything else"),
}


def _answers_of(job: dict) -> dict:
    try:
        a = json.loads(job.get("answers_json") or "{}")
        return a if isinstance(a, dict) else {}
    except json.JSONDecodeError:
        return {}


def _ready_jobs(limit: int | None, job_id: int | None,
                source: str | None = None, query: str | None = None,
                job_ids: list[int] | None = None) -> list[dict]:
    jobs = store.get_jobs(status="ready")
    if source:
        jobs = [j for j in jobs if (j.get("source") or "").lower() == source.lower()]
    if job_ids:
        keep = set(job_ids)
        jobs = [j for j in jobs if j["id"] in keep]
    if query:
        q = query.lower()
        jobs = [j for j in jobs if q in (j.get("title") or "").lower()
                or q in (j.get("company") or "").lower()]
    if job_id is not None:
        jobs = [j for j in jobs if j["id"] == job_id]

    # Best-first: prefer the Grok/LLM rerank score when a job has one (the tuned ranking),
    # else fall back to the deterministic keyword match_score. Newest breaks ties.
    def _best(j):
        return j["llm_score"] if j.get("llm_score") is not None else (j.get("match_score") or 0.0)
    jobs.sort(key=lambda j: (-_best(j), -(j.get("id") or 0)))
    return jobs[:limit] if limit is not None else jobs


def _packet(job: dict, facts: dict | None = None) -> dict:
    ans = _answers_of(job)
    facts = facts if facts is not None else candidate.known_facts(candidate.load_details())
    resume = job.get("tailored_resume_path") or ""
    resume_abs = str((ROOT / resume)) if resume and not Path(resume).is_absolute() else resume
    # Drop any screening item the candidate has since answered in candidate.json — its
    # value is in `candidate_facts` below, so it isn't a "human must fill" anymore. The
    # stored screening_todo was computed earlier (maybe before the user filled it in).
    covered = candidate.covered_labels(facts)
    must_fill = [t for t in (ans.get("screening_todo") or [])
                 if not any(c.lower() in t.lower() or t.lower() in c.lower() for c in covered)]
    return {
        "job_id": job["id"],
        "title": job.get("title"),
        "company": job.get("company"),
        "source": job.get("source"),
        "url": job.get("url"),
        "resume_path": resume,
        "resume_abspath": resume_abs,
        "resume_exists": bool(resume) and Path(resume_abs).exists(),
        "cover_letter": ans.get("cover_letter", ""),
        "answers": ans.get("answers", {}),
        # Known personal facts the orchestrator can type into screening questions
        # directly (notice period, CTC, relocation, …) — no human stop needed for these.
        "candidate_facts": facts,
        # What's STILL missing (unknown facts) — the human supplies these at the gate.
        "human_must_fill": must_fill,
    }


def packet(limit: int | None, job_id: int | None, source: str | None = None,
           query: str | None = None, job_ids: list[int] | None = None) -> int:
    jobs = _ready_jobs(limit if limit is not None else DEFAULT_BATCH, job_id,
                       source, query, job_ids)
    if not jobs:
        bits = []
        if source:
            bits.append(f"source {source!r}")
        if query:
            bits.append(f"matching {query!r}")
        if job_ids:
            bits.append(f"ids {job_ids}")
        where = f" ({', '.join(bits)})" if bits else ""
        print(f"no jobs at status 'ready'{where}. (run humanise-responder first?)")
        return 0
    facts = candidate.known_facts(candidate.load_details())
    print(f"=== APPLY PACKET — {len(jobs)} ready job(s)"
          f"{f' (best-matched first, source={source})' if source else ' (best-matched first)'} ===")
    print("SAFETY: the orchestrator fills the form via the chrome-devtools MCP and STOPS")
    print("at the review gate. A HUMAN reviews the screenshot and clicks Submit. Then run")
    print("`apply.py log --job <id> --outcome applied|skipped|failed`.\n")
    for j in jobs:
        p = _packet(j, facts)
        print(json.dumps(p, ensure_ascii=False, indent=2))
        if not p["resume_exists"]:
            print(f"  ⚠ tailored résumé missing at {p['resume_abspath']} — re-run resume-tailor.",
                  file=sys.stderr)
        if p["human_must_fill"]:
            print(f"  ⚠ HUMAN must supply before submit: {', '.join(p['human_must_fill'])}",
                  file=sys.stderr)
        print("-" * 60)
    print("\nReminder: human-paced, small batches. Nothing is submitted by this tool.")
    return 0


def log(job_id: int, outcome: str, note: str | None) -> int:
    if outcome not in OUTCOMES:
        print(f"outcome must be one of {sorted(OUTCOMES)}", file=sys.stderr)
        return 2
    job = store.get_job(job_id)
    if job is None:
        print(f"no job with id {job_id}", file=sys.stderr)
        return 1
    if job["status"] != "ready":
        # Guard: only a reviewed `ready` job should be logged. Re-logging or logging a
        # job that never reached the review gate is almost always a mistake.
        print(f"job {job_id} is at status {job['status']!r}, not 'ready' — refusing to "
              f"log. (only log a job after the human reviewed it at the apply gate.)",
              file=sys.stderr)
        return 1
    fields: dict = {"status": outcome, "outcome": outcome}
    # applied_at = when it was actually submitted; only meaningful for 'applied'.
    if outcome == "applied":
        fields["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if note:
        fields["notes"] = note
    store.update_job(job_id, **fields)
    store.export_json()
    verb = {"applied": "✓ applied", "skipped": "↷ skipped", "failed": "✗ failed"}[outcome]
    print(f"{verb}: job {job_id} ({job.get('title')}) → status {outcome}. {store.stats()}")
    return 0


def show() -> int:
    ready = _ready_jobs(None, None)
    facts = candidate.known_facts(candidate.load_details())
    print(f"{len(ready)} job(s) READY to apply (best-matched first):\n")
    for j in ready:
        p = _packet(j, facts)
        flag = "" if p["resume_exists"] else "  ⚠ résumé missing"
        todo = f"  (human fills: {', '.join(p['human_must_fill'])})" if p["human_must_fill"] else ""
        print(f"  [{j['id']:>3}] {(j.get('title') or '')[:40]:<40} @ {(j.get('company') or '')[:20]} ({j['source']}){flag}{todo}")
    for status in ("applied", "skipped", "failed"):
        rows = store.get_jobs(status=status)
        if rows:
            print(f"\n{status.upper()} ({len(rows)}):")
            for j in rows:
                note = f"  — {j['notes']}" if (j.get("notes") or "").strip() else ""
                print(f"  [{j['id']:>3}] {(j.get('title') or '')[:40]:<40} @ "
                      f"{(j.get('company') or '')[:20]}  {j.get('applied_at') or ''}{note}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Human-in-the-loop apply spine (never submits)")
    sub = ap.add_subparsers(dest="cmd")

    p_pk = sub.add_parser("packet", help="build apply packet(s) for ready jobs")
    p_pk.add_argument("--limit", type=int, default=None, help=f"max jobs (default {DEFAULT_BATCH})")
    p_pk.add_argument("--job", type=int, default=None, help="a single job id")
    p_pk.add_argument("--source", default=None, help="only this portal (e.g. linkedin)")
    p_pk.add_argument("--query", default=None, help="only jobs whose title/company contains this text")
    p_pk.add_argument("--jobs", default=None, help="comma-separated job ids to include")

    sub.add_parser("show", help="ready queue + apply log")

    p_log = sub.add_parser("log", help="record outcome AFTER the human reviewed + submitted")
    p_log.add_argument("--job", type=int, required=True)
    p_log.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
    p_log.add_argument("--note", default=None)

    args = ap.parse_args(argv)
    store.init_db()
    if args.cmd == "packet":
        return packet(args.limit, args.job, args.source,
                      args.query, store.parse_ids(args.jobs))
    if args.cmd == "log":
        return log(args.job, args.outcome, args.note)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
