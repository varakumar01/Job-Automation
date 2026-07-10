"""llm_rank — rank jobs by fit for THIS résumé using an LLM (Grok/DeepSeek/API).

The Python matcher (match.py) is a fast, free, deterministic coarse ranker. This adds an
LLM *reranker*: it takes a shortlist (the top Python candidates, or a chosen set) and
orders them by how well the candidate actually fits — judging from the JD DUTIES (not the
title), weighing skill overlap, role alignment, and required years vs the candidate's ~2.
The ranking prompt was tuned to closely match a human/Claude ranking of the same jobs.

Needs an API backend (LLM_PROVIDER=grok|api). Driven by main.py `rank --llm grok`.

    python3 .../llm_rank.py --limit 20            # rerank top-20 Python matches
    python3 .../llm_rank.py --eligible            # only eligible best-match jobs
    python3 .../llm_rank.py --jobs "101,128,175"  # only these ids
    python3 .../llm_rank.py --save                # persist llm_score/llm_reason to the store
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ROOT = SKILL_DIR.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from data import store  # noqa: E402
from execution import eligibility, llm  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity, vprint  # noqa: E402
from execution.profile import candidate_profile  # noqa: E402
from execution.prompts import load_prompt  # noqa: E402

JD_SNIPPET_CHARS = 700  # per-job JD context sent to the model (keeps the batch token-bound)

_DEFAULT_SYSTEM_PROMPT = (
    "You are an expert technical recruiter ranking cybersecurity job postings by how well "
    "ONE specific candidate fits each — fewest résumé changes needed to be a credible "
    "applicant.\n"
    "JUDGE FROM THE 'jd' CONTENT (responsibilities/requirements), NOT THE TITLE. A title "
    "may say 'Offensive', 'Pentester', 'Architect', or a level number, yet the actual work "
    "described is security AUTOMATION, tooling, detection/plugin development, CI/CD, or "
    "cloud security — which is exactly THIS candidate's core strength; weight that work "
    "HIGH regardless of the title. Conversely a friendly title with the wrong actual work "
    "(e.g. Wintel/Linux PATCHING, ITIL ops, release management) is a POOR fit.\n"
    "Weigh: (1) overlap between the candidate's real skills (security automation in Python/"
    "Bash, vulnerability-detection development, cloud-security/compliance, scanning tools) "
    "and the jd's actual duties; (2) seniority INFERRED FROM THE DUTIES, not title words — "
    "an 'Architect/II/Engineer III' that is an individual-contributor testing/automation "
    "role is fine; HEAVILY penalize only jds whose duties truly demand many years or "
    "lead/principal/management scope. BUT an EXPLICIT senior marker — 'Senior', 'Lead', "
    "'Staff', 'Principal', 'Vice President'/'VP', 'Manager', or a level number 3 or higher "
    "(e.g. 'Engineer 3') — signals a seniority gap for a ~2-yr candidate: cap such a role "
    "below the on-level matches even when its duties fit well (strong content does NOT "
    "erase the level gap); (3) the candidate is a detection/automation/cloud-security "
    "DEVELOPER, NOT a dedicated penetration tester — rank pure manual/network/red-team "
    "PENTEST roles (especially ones demanding 4+ years of hands-on pentesting) BELOW "
    "automation/cloud/appsec/detection roles; (4) LOCATION is a modest tiebreaker: among "
    "otherwise-similar jobs, rank one in the candidate's preferred_locations (or Remote) "
    "ABOVE one elsewhere, and order two identical postings that differ only by city "
    "consistently (best-preferred-location first); (5) if a jd is mostly company fluff "
    "with NO concrete requirements, do NOT inflate the score — rank it lower (unverifiable). "
    "Do not invent requirements. Return STRICT JSON (no prose, no fences):\n"
    '{"ranking": [{"id": <job id int>, "score": <0-100 int>, "reason": "<concrete, <=10 words>"}]}\n'
    "CRITICAL: include EVERY input job EXACTLY ONCE — do not drop any id — sorted best-fit "
    "first. Each 'reason' must be SPECIFIC and cite the jd DUTIES (e.g. 'builds Python "
    "automation + CI/CD', 'Azure cloud security controls', 'Wintel patching, off-profile', "
    "'duties are senior/lead scope', 'fluff jd, fit unverifiable'). No generic phrases like "
    "'good match', 'relevant skills', or ones that just restate the title. "
    "Return ONLY the JSON object — no <think> tags, no markdown fences, no preamble."
)
# Editable at prompts/profile-matcher.txt (see execution/prompts.py) — falls
# back to the constant above until a user actually edits it via the frontend.
SYSTEM_PROMPT = load_prompt("profile-matcher", _DEFAULT_SYSTEM_PROMPT)


# ── job selection ───────────────────────────────────────────────────────────

def _shortlist(status: str, limit: int | None, ids: list[int] | None,
               eligible: bool) -> list[dict]:
    jobs = store.get_jobs(status=status)
    if ids:
        keep = set(ids)
        jobs = [j for j in jobs if j["id"] in keep]
    elif eligible:
        # Shared classifier (execution/eligibility) — single source of truth with main.py.
        jobs = [j for j in jobs if eligibility.classify(j) == "eligible"]
    # Coarse order by the Python score, then take the top `limit` as the rerank shortlist.
    jobs.sort(key=lambda j: -(j.get("match_score") or 0.0))
    return jobs[:limit] if limit is not None else jobs


# Anchor the JD snippet at the requirements/responsibilities, skipping "About us" fluff.
_REQ_ANCHOR = re.compile(
    r"(?i)(responsibilit|requirement|qualificat|what you|you will|you'll|you ll|"
    r"must have|key skills|technical skills|looking for|experience (with|in|:))")


def _snippet(job: dict) -> str:
    jd = re.sub(r"\s+", " ", (job.get("jd_text") or "").strip())
    m = _REQ_ANCHOR.search(jd)
    # Only jump forward if the anchor isn't already near the end.
    start = m.start() if (m and m.start() < max(0, len(jd) - 200)) else 0
    return jd[start:start + JD_SNIPPET_CHARS] or "(no job description available)"


def _extract_json(text: str) -> dict:
    """Strip <think>…</think> traces (e.g. Nemotron), then extract first JSON object."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "<think>" in text:  # unclosed tag: drop everything from it onward
        text = text[:text.index("<think>")].strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    a = text.find("{")
    if a == -1:
        return json.loads(text)
    depth = 0
    in_str = esc = False
    for i in range(a, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[a:i + 1])
    return json.loads(text[a:])


# ── LLM ranking ─────────────────────────────────────────────────────────────

def llm_rank(jobs: list[dict], profile: dict) -> list[dict]:
    """Return [{id, score, reason}] best-first from the model for the given jobs."""
    payload = {
        "candidate": profile,
        "jobs": [{"id": j["id"], "title": j.get("title"), "company": j.get("company"),
                  "location": j.get("location"), "jd": _snippet(j)} for j in jobs],
    }
    payload_str = json.dumps(payload, ensure_ascii=False)
    vprint(2, f"\n  [vv] rank payload ({len(payload_str)} chars):\n{payload_str[:600]}…")
    # temperature=0 → stable, repeatable ranking (the whole point of a reranker).
    reply = llm.complete(payload_str, system=SYSTEM_PROMPT, max_tokens=2000, temperature=0)
    vprint(2, f"  [vv] reply: {reply[:400]}…")
    data = _extract_json(reply)
    ranking = data.get("ranking") if isinstance(data, dict) else None
    if not isinstance(ranking, list):
        raise ValueError("model did not return a 'ranking' list")
    known = {j["id"] for j in jobs}
    out: list[dict] = []
    seen: set[int] = set()
    for r in ranking:
        # Parse id AND score together: a malformed entry is skipped (then recovered by the
        # completeness guard below), never aborting the whole batch.
        try:
            jid = int(r["id"])
            score = float(r.get("score") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if jid in known and jid not in seen:
            seen.add(jid)
            out.append({"id": jid, "score": score,
                        "reason": str(r.get("reason") or "").strip()})
    # Completeness safety: never lose a job the model dropped or returned malformed —
    # append it so the ranking always covers the full shortlist exactly once.
    for j in jobs:
        if j["id"] not in seen:
            out.append({"id": j["id"], "score": 0.0, "reason": "⚠ not ranked by model"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LLM rerank of jobs by résumé fit")
    ap.add_argument("--limit", type=int, default=20, help="shortlist size to rerank (default 20)")
    ap.add_argument("--status", default="matched")
    ap.add_argument("--jobs", default=None, help="comma-separated job ids")
    ap.add_argument("--eligible", action="store_true", help="only eligible best-match jobs")
    ap.add_argument("--save", action="store_true", help="persist llm_score/llm_reason to store")
    add_verbose_arg(ap)
    args = ap.parse_args(argv)
    apply_verbosity(args)

    store.init_db()
    jobs = _shortlist(args.status, args.limit, store.parse_ids(args.jobs), args.eligible)
    if not jobs:
        print(f"no '{args.status}' jobs to rank.")
        return 0
    profile = candidate_profile()

    if llm.is_session_mode():
        print("LLM_PROVIDER=session — llm_rank needs an API backend (grok/api). "
              "Set it in .env or run via `main.py rank --llm grok`.", file=sys.stderr)
        return 1

    print(f"LLM rerank via {llm.provider()}/{llm.model()} — {len(jobs)} job(s) "
          f"(candidate: {profile['current_title']}, {profile['total_experience']}).")
    try:
        ranking = llm_rank(jobs, profile)
    except Exception as exc:
        print(f"✗ LLM ranking failed: {exc}", file=sys.stderr)
        return 1

    by_id = {j["id"]: j for j in jobs}
    print(f"\n{'#':>3} {'score':>5}  Job")
    print("-" * 70)
    for i, r in enumerate(ranking, 1):
        j = by_id[r["id"]]
        print(f"{i:>3} {r['score']:>5.0f}  {(j.get('title') or '')[:38]:<38} @ "
              f"{(j.get('company') or '')[:18]}")
        print(f"          ↳ {r['reason'][:72]}")

    if args.save:
        for r in ranking:
            store.update_job(r["id"], llm_score=r["score"],
                             llm_reason=r["reason"])
        store.export_json()
        print(f"\nsaved llm_score/llm_reason for {len(ranking)} job(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
