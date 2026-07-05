"""jd-understander — turn each matched job's JD into a structured brief. PLAN §5 #3.

Reads jobs at status ``matched`` that still lack a ``jd_brief`` (the work queue) and
produces a compact JSON brief (company, role, must-haves, ATS keywords, fit angle)
that steers resume-tailor and humanise-responder. Writes ``jd_brief``; the row STAYS
at ``matched`` (resume-tailor advances it to ``tailored``). Persists per job, so a run
resumes after interruption.

Works in all three LLM modes (see ``execution/llm.py`` / PLAN §9):

- ``session`` (default, free): two steps — ``prepare`` writes the per-job prompts to
  ``.tmp/jd-understander/prompts.json`` (and prints them); the ORCHESTRATOR (this
  Claude Code session) reads them, writes its JSON briefs to
  ``.tmp/jd-understander/answers.json``, then ``save`` validates and stores them.
- ``api`` / ``grok``: ``run`` loops over the pending jobs calling ``llm.complete``.

Usage::

    # session mode (default):
    python3 .../understand.py prepare [--limit N]   # -> prompts.json (+ printout)
    python3 .../understand.py save                  # <- answers.json -> store
    # api / grok mode:
    python3 .../understand.py run [--limit N]       # complete() each pending job
    # any mode:
    python3 .../understand.py            # auto: 'prepare' (session) or 'run' (api/grok)
    python3 .../understand.py show       # list matched jobs + brief status
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
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / ".env")  # LLM_PROVIDER / keys before llm reads os.environ
except ImportError:  # python-dotenv optional; env may already be exported
    pass

from data import store  # noqa: E402
from execution import llm  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity, vprint  # noqa: E402
from execution.profile import candidate_profile  # noqa: E402

_PROFILE_CACHE: dict | None = None


def _profile() -> dict:
    """The candidate profile (résumé-derived), cached so the résumé is parsed once per run.
    Given to the model so fit_notes is CANDIDATE-SPECIFIC, not generic advice."""
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        _PROFILE_CACHE = candidate_profile()
    return _PROFILE_CACHE


TMP_DIR = ROOT / ".tmp" / "jd-understander"
PROMPTS_PATH = TMP_DIR / "prompts.json"
ANSWERS_PATH = TMP_DIR / "answers.json"

# Brief schema — keys the orchestrator/model must return. resume-tailor reads
# `keywords`/`must_have`/`nice_to_have`; humanise-responder reads `fit_notes`/`role_summary`.
BRIEF_KEYS = [
    "company_summary", "role_summary", "key_tools", "must_have",
    "nice_to_have", "keywords", "seniority", "red_flags", "fit_notes",
]
REQUIRED_KEYS = ("company_summary", "role_summary")  # a brief is useless without these

SYSTEM_PROMPT = (
    "You analyze a single job posting for a cybersecurity candidate (~2 years' "
    "experience) and return a STRICT JSON object — no prose, no markdown fences. "
    "Extract only what the posting states; do not invent requirements. Schema:\n"
    '{\n'
    '  "company_summary": "1-2 sentences: what the company does",\n'
    '  "role_summary": "1-2 sentences: the day-to-day of this role",\n'
    '  "key_tools": ["concrete tools/tech named in the JD"],\n'
    '  "must_have": ["hard requirements the JD lists as required"],\n'
    '  "nice_to_have": ["preferred / bonus skills"],\n'
    '  "keywords": ["ATS keywords to mirror in the resume"],\n'
    '  "seniority": "junior | mid | senior | unclear (as the JD frames it)",\n'
    '  "red_flags": ["vague scope, mismatched seniority, or other cautions; [] if none"],\n'
    '  "fit_notes": "1-2 sentences using the CANDIDATE PROFILE provided: how THIS specific '
    'candidate should angle the resume/answers — name their concrete relevant strengths '
    '(their real tools/automation/cloud/detection work) and any genuine gap. Do NOT give '
    'generic advice."\n'
    '}\n'
    "A CANDIDATE PROFILE is provided with the posting — ground fit_notes in it. "
    "Return ONLY the JSON object — no <think> tags, no markdown fences, no preamble."
)


def _build_user_prompt(job: dict) -> str:
    jd = (job.get("jd_text") or "").strip()
    if len(jd) > 12000:  # keep token cost bounded; the tail is rarely substantive
        jd = jd[:12000] + "\n…[truncated]"
    prof = _profile()
    prof_slim = {k: prof.get(k) for k in
                 ("current_title", "total_experience", "target_roles", "skills", "summary")}
    parts = [
        "CANDIDATE PROFILE (ground fit_notes in this — the résumé being angled):",
        json.dumps(prof_slim, ensure_ascii=False),
        "",
        f"Title: {job.get('title') or '(none)'}",
        f"Company: {job.get('company') or '(none)'}",
        f"Location: {job.get('location') or '(none)'}",
        f"Source: {job.get('source') or '(none)'}",
        "",
        "Job description:",
        jd or "(no description text was scraped — infer only from the title/company above)",
    ]
    return "\n".join(parts)


def _pending_jobs(limit: int | None, ids: list[int] | None = None) -> list[dict]:
    """Matched jobs that still lack a jd_brief — the resumable work queue.
    `ids` (from `--jobs`) restricts to those specific job ids.
    Sorted best-score first so a limited run processes the highest-rated jobs."""
    jobs = [j for j in store.get_jobs(status="matched") if not (j.get("jd_brief") or "").strip()]
    if ids:
        keep = set(ids)
        jobs = [j for j in jobs if j["id"] in keep]
    jobs.sort(key=lambda j: -(j["llm_score"] if j.get("llm_score") is not None else (j.get("match_score") or 0)))
    return jobs[:limit] if limit is not None else jobs


def _extract_json(text: str) -> dict:
    """Parse the first complete JSON object from a model reply, tolerating ```json
    fences and trailing prose (even prose with braces) via a string-aware brace scan.
    Strips <think>…</think> reasoning traces emitted by some models (e.g. Nemotron)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "<think>" in text:  # unclosed tag: drop everything from it onward
        text = text[:text.index("<think>")].strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    a = text.find("{")
    if a == -1:
        return json.loads(text)  # no object → raise a clear JSONDecodeError
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
    return json.loads(text[a:])  # unbalanced → raise


def _normalize_brief(raw: dict) -> dict:
    """Coerce a brief to the schema: required keys present, lists are lists."""
    if not isinstance(raw, dict):
        raise ValueError("brief is not a JSON object")
    missing = [k for k in REQUIRED_KEYS if not str(raw.get(k) or "").strip()]
    if missing:
        raise ValueError(f"brief missing required field(s): {missing}")
    brief: dict = {}
    for k in BRIEF_KEYS:
        v = raw.get(k)
        if k in ("key_tools", "must_have", "nice_to_have", "keywords", "red_flags"):
            if v is None:
                v = []
            elif isinstance(v, str):
                v = [s.strip() for s in v.split(",") if s.strip()]
            elif isinstance(v, list):
                v = [str(s).strip() for s in v if str(s).strip()]
            else:
                v = [str(v)]
        else:
            v = "" if v is None else str(v).strip()
        brief[k] = v
    return brief


def _store_brief(job_id: int, brief: dict) -> None:
    store.update_job(job_id, jd_brief=json.dumps(brief, ensure_ascii=False))


# ── session mode: prepare → [orchestrator answers] → save ──────────────────

def prepare(limit: int | None, ids: list[int] | None = None) -> int:
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no matched jobs awaiting a brief. (run profile-matcher first?)")
        return 0
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "job_id": j["id"],
            "title": j.get("title") or "",
            "company": j.get("company") or "",
            "prompt": _build_user_prompt(j),
        }
        for j in jobs
    ]
    PROMPTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"LLM_PROVIDER=session — orchestrator-in-the-loop.\n")
    print(f"Prepared {len(payload)} job prompt(s) → {PROMPTS_PATH}\n")
    print("SYSTEM INSTRUCTION (apply to every job):")
    print("-" * 72)
    print(SYSTEM_PROMPT)
    print("-" * 72)
    print(
        f"\nFor EACH job below, produce the strict-JSON brief. Write a single JSON\n"
        f"object mapping job_id (string) → brief object to:\n  {ANSWERS_PATH}\n"
        f"Then run:  python3 {Path(__file__).relative_to(ROOT)} save\n"
    )
    for item in payload:
        print(f"\n===== job_id {item['job_id']} — {item['title']} @ {item['company']} =====")
        print(item["prompt"])
    return 0


def save(path: Path | None) -> int:
    src = path or ANSWERS_PATH
    if not src.exists():
        print(f"answers file not found: {src}\n"
              f"Run `prepare` first, write the briefs there, then `save`.", file=sys.stderr)
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))

    # Accept {"<job_id>": brief, …} or [{"job_id": .., "brief": ..}, …].
    items: list[tuple[int, dict]] = []
    if isinstance(data, dict):
        try:
            items = [(int(jid), brief) for jid, brief in data.items()]
        except (ValueError, TypeError) as exc:
            print(f"answers object has a non-integer job_id key: {exc}", file=sys.stderr)
            return 1
    elif isinstance(data, list):
        for row in data:
            try:
                # flat format: brief fields may live directly on the row (no "brief" wrapper)
                items.append((int(row["job_id"]), row.get("brief", row)))
            except (KeyError, ValueError, TypeError) as exc:
                print(f"  ✗ skipping malformed list entry ({exc}): {row}", file=sys.stderr)
    else:
        print("answers must be a JSON object {job_id: brief} or a list.", file=sys.stderr)
        return 1

    pending_ids = {j["id"] for j in store.get_jobs(status="matched")}
    saved = skipped = 0
    for job_id, raw in items:
        if job_id not in pending_ids:
            print(f"  · job {job_id} not at status 'matched' — skipped", file=sys.stderr)
            skipped += 1
            continue
        try:
            brief = _normalize_brief(raw)
        except ValueError as exc:
            print(f"  ✗ job {job_id}: {exc} — skipped", file=sys.stderr)
            skipped += 1
            continue
        _store_brief(job_id, brief)  # persist per job → resumable
        saved += 1
        print(f"  ✓ job {job_id}: brief stored ({len(brief['keywords'])} keywords)")
    print(f"\nsaved {saved} brief(s), skipped {skipped}. {store.stats()}")
    return 0 if saved or not items else 1


# ── api / grok mode: one-shot run loop ─────────────────────────────────────

def run(limit: int | None, ids: list[int] | None = None) -> int:
    if llm.is_session_mode():
        print("LLM_PROVIDER=session — `run` needs an API backend. Use `prepare`/`save`,\n"
              "or set LLM_PROVIDER=api (Anthropic) or grok (xAI) in .env.", file=sys.stderr)
        return 1
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no matched jobs awaiting a brief.")
        return 0
    print(f"LLM_PROVIDER={llm.provider()} model={llm.model()} — briefing {len(jobs)} job(s).\n")
    done = failed = 0
    for j in jobs:
        prompt = _build_user_prompt(j)
        vprint(2, f"\n  [vv] understand prompt ({len(prompt)} chars):\n{prompt[:600]}…")
        try:
            reply = llm.complete(prompt, system=SYSTEM_PROMPT, max_tokens=1500)
            vprint(2, f"  [vv] reply: {reply[:400]}…")
            brief = _normalize_brief(_extract_json(reply))
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  ✗ job {j['id']} ({j.get('title')}): bad model output — {exc}", file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # network/API error: stop cleanly, keep what's saved
            print(f"  ✗ job {j['id']}: API error — {exc}", file=sys.stderr)
            failed += 1
            break
        _store_brief(j["id"], brief)  # persist per job → resumable
        done += 1
        print(f"  ✓ job {j['id']}: {(j.get('title') or '')[:40]} → brief stored")
        vprint(1, f"    tools={brief.get('key_tools', [])[:4]}  keywords={len(brief.get('keywords', []))}  seniority={brief.get('seniority')}")
    print(f"\nbriefed {done} job(s), {failed} failed. {store.stats()}")
    return 0 if failed == 0 else 1


def show() -> int:
    jobs = store.get_jobs(status="matched")
    if not jobs:
        print("no matched jobs yet — run profile-matcher first.")
        return 0
    with_brief = [j for j in jobs if (j.get("jd_brief") or "").strip()]
    print(f"{len(jobs)} matched job(s); {len(with_brief)} have a brief:\n")
    for j in jobs:
        mark = "✓" if (j.get("jd_brief") or "").strip() else " "
        print(f"  [{mark}] {(j.get('title') or '')[:42]:<42} @ {(j.get('company') or '')[:22]} ({j['source']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build structured briefs for matched jobs")
    ap.add_argument("cmd", nargs="?", default="auto",
                    choices=["auto", "prepare", "save", "run", "show"],
                    help="auto = prepare (session) or run (api/grok)")
    ap.add_argument("--limit", type=int, default=None, help="max jobs to process")
    ap.add_argument("--jobs", default=None, help="comma-separated job ids to limit to")
    ap.add_argument("--from", dest="from_path", default=None, help="answers file for `save`")
    add_verbose_arg(ap)
    args = ap.parse_args(argv)
    apply_verbosity(args)

    store.init_db()
    cmd = args.cmd
    ids = store.parse_ids(args.jobs)
    if cmd == "auto":
        cmd = "prepare" if llm.is_session_mode() else "run"

    if cmd == "prepare":
        return prepare(args.limit, ids)
    if cmd == "save":
        return save(Path(args.from_path) if args.from_path else None)
    if cmd == "run":
        return run(args.limit, ids)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
