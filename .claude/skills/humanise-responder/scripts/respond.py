"""humanise-responder — draft honest, human-sounding application answers. PLAN §5 #5.

For each job at status ``tailored`` that lacks ``answers_json``, produce a cover
letter plus answers to the common open-ended application questions, GROUNDED in the
candidate's real résumé + the job's ``jd_brief`` — never fabricating experience.
Factual screening fields only the candidate can supply (notice period, expected CTC,
relocation) are listed in ``screening_todo`` for the human to fill at the apply gate,
not invented. Writes ``answers_json`` and advances ``tailored → ready``. Per-job
persistence = resumable.

Runs in all three LLM modes (PLAN §9 · execution/llm.py), same as jd-understander:

- ``session`` (default): ``prepare`` writes per-job prompts to
  ``.tmp/humanise-responder/prompts.json``; the orchestrator writes answers to
  ``answers.json``; ``save`` validates + stores.
- ``api`` / ``grok``: ``run`` loops calling ``llm.complete``.

Usage::

    python3 .../respond.py prepare [--limit N]
    python3 .../respond.py save
    python3 .../respond.py run [--limit N]
    python3 .../respond.py            # auto: prepare (session) | run (api/grok)
    python3 .../respond.py show       # tailored/ready jobs + answer status
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

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from data import store  # noqa: E402
from execution import candidate  # noqa: E402
from execution import llm  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity, vprint  # noqa: E402
from execution.prompts import load_prompt  # noqa: E402

DEFAULT_MASTER = ROOT / "varakumar_resume.tex"
TMP_DIR = ROOT / ".tmp" / "humanise-responder"
PROMPTS_PATH = TMP_DIR / "prompts.json"
ANSWERS_PATH = TMP_DIR / "answers.json"

ANSWER_KEYS = ("why_role", "why_company", "relevant_experience", "strengths",
               "availability_note")
REQUIRED_TOP = ("cover_letter",)  # an answers_json is useless without a cover letter

# Candidate-only facts a model must NOT invent into an answer — flag for human review.
_SENSITIVE_RE = re.compile(
    r"(?i)\b(ctc|lpa|lakh|salary|stipend|notice period|per annum|expected pay)\b"
    r"|\b\d+\s*(days?|weeks?|months?)\s+notice\b|[₹$]\s?\d")

_DEFAULT_SYSTEM_PROMPT = (
    "You draft HONEST, human-sounding job-application answers for a cybersecurity "
    "candidate (~2 years' experience). Ground every claim in the candidate_profile "
    "provided — do NOT invent employers, tools, certifications, or experience. Write "
    "natural first-person prose: specific, concise, no buzzword padding, no em-dash "
    "clichés, no 'I am writing to express'. You may use the candidate_facts block "
    "(notice period, relocation, etc.) for honest availability/relocation lines — but "
    "use ONLY values present there; never invent a number, date, or salary that isn't "
    "given. ALWAYS weigh the role's required years/seniority (in jd_brief.seniority / "
    "must_have) against the candidate's ~2 years: if the posting asks for MORE, name the "
    "gap honestly and lead with transferable depth — NEVER claim more years or a higher "
    "seniority than the profile shows; if it's on-level, say so plainly. Return STRICT "
    "JSON (no prose, no fences):\n"
    "{\n"
    '  "cover_letter": "3 short paragraphs tailored to this company/role, grounded in '
    'real achievements",\n'
    '  "answers": {\n'
    '    "why_role": "2-4 sentences",\n'
    '    "why_company": "2-4 sentences specific to this company",\n'
    '    "relevant_experience": "how the candidate\'s real experience maps to the '
    'must_have list",\n'
    '    "strengths": "2-3 honest strengths backed by the profile",\n'
    '    "availability_note": "a brief, neutral availability line (NO invented dates/'
    'salary)"\n'
    "  },\n"
    '  "screening_todo": ["facts only the candidate can supply — e.g. notice period, '
    'expected CTC, current CTC, relocation/visa — list them; do NOT fabricate values"]\n'
    "}\n"
    "Return ONLY the JSON object — no <think> tags, no markdown fences, no preamble."
)
# Editable at prompts/humanise-responder.txt (see execution/prompts.py) — falls
# back to the constant above until a user actually edits it via the frontend.
SYSTEM_PROMPT = load_prompt("humanise-responder", _DEFAULT_SYSTEM_PROMPT)


# ── candidate profile (read-only) from the master résumé ───────────────────

def _candidate_profile(master_path: Path) -> dict:
    raw = master_path.read_text(encoding="utf-8") if master_path.exists() else ""
    # Drop LaTeX comment lines so commented-out \item/\techrow blocks don't leak
    # into the profile the LLM sees.
    raw = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("%"))

    def _clean(s: str) -> str:
        s = s.replace("\\%", "%").replace("\\&", "&")
        s = s.replace("{,}", ",")                 # LaTeX thousands sep: 3{,}200 → 3,200
        s = re.sub(r"\\textemdash\b", " - ", s)
        s = re.sub(r"\\[,;:!> ]", " ", s)        # LaTeX spacing commands (\, \; …)
        s = re.sub(r"\$[^$]*\$", "", s)           # drop inline math
        s = re.sub(r"\\textbf\b", "", s)          # keep the bolded text, drop the command
        s = re.sub(r"\\[a-zA-Z]+\b\*?", " ", s)   # drop remaining \commands
        s = re.sub(r"[{}\\]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    summary = ""
    m = re.search(r"\\resumeSection\{Professional Summary\}\n(.*?)(?=\n%\s*──|\n\\resumeSection)",
                  raw, re.DOTALL)
    if m:
        summary = _clean(m.group(1))

    achievements: list[str] = []
    m = re.search(r"\\resumeSection\{Key Achievements\}.*?\\begin\{itemize\}(.*?)\\end\{itemize\}",
                  raw, re.DOTALL)
    if m:
        for item in re.findall(r"\\item\s+(.*?)(?=\n\s*\\item|\n\s*\\end\{itemize\}|$)",
                               m.group(1), re.DOTALL):
            text = _clean(item)
            if text:
                achievements.append(text)

    skills = []
    for ln in raw.splitlines():
        m = re.match(r"\\techrow\{([^}]*)\}\{([^}]*)\}", ln)
        if m:  # tolerate a malformed/partial \techrow line instead of crashing
            skills.append(m.group(2))
    return {
        "summary": summary,
        "key_achievements": achievements[:5],
        "skills": [_clean(s) for s in skills],
    }


def _brief_of(job: dict) -> dict:
    try:
        b = json.loads(job.get("jd_brief") or "{}")
        return b if isinstance(b, dict) else {}
    except json.JSONDecodeError:
        return {}


def _job_prompt(job: dict, brief: dict, profile: dict, facts: dict) -> str:
    return json.dumps({
        "job_title": job.get("title"),
        "company": job.get("company"),
        "jd_brief": {k: brief.get(k) for k in
                     ("company_summary", "role_summary", "must_have", "keywords",
                      "seniority", "fit_notes")},
        "candidate_profile": profile,
        "candidate_facts": facts,  # known personal facts the model MAY cite (never invent)
    }, ensure_ascii=False, indent=2)


def _normalize_answers(raw: dict, gaps: list[str] | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("answers is not a JSON object")
    cover = str(raw.get("cover_letter") or "").strip()
    if not cover:
        raise ValueError("missing required field: cover_letter")
    ans_in = raw.get("answers") or {}
    if not isinstance(ans_in, dict):
        ans_in = {}
    answers = {k: str(ans_in.get(k) or "").strip() for k in ANSWER_KEYS}
    # screening_todo = the DETERMINISTIC gaps from candidate.json (facts still unknown),
    # plus any extra job-specific question the model surfaced that we don't already cover.
    todo = list(gaps or [])
    extra = raw.get("screening_todo") or []
    if isinstance(extra, str):
        extra = [t.strip() for t in extra.split(",") if t.strip()]
    elif not isinstance(extra, list):
        extra = []
    for item in extra:
        item = str(item).strip()
        if item and not any(item.lower() in g.lower() or g.lower() in item.lower()
                            for g in todo):
            todo.append(item)
    # Programmatic anti-fabrication guardrail (defense-in-depth on top of the prompt):
    # if any drafted answer mentions pay/notice/dates — facts the candidate must supply
    # — flag it for the human review gate rather than trusting the model didn't invent it.
    blob = " ".join([cover, *answers.values()])
    if _SENSITIVE_RE.search(blob):
        note = "REVIEW: an answer mentions pay/notice/dates — verify it's accurate, not invented"
        if note not in todo:
            todo.append(note)
    return {"cover_letter": cover, "answers": answers, "screening_todo": todo}


def _extract_json(text: str) -> dict:
    """Parse the first complete JSON object from a model reply, tolerating ``` fences
    and trailing prose (even prose containing braces) via a string-aware brace scan.
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


def _pending_jobs(limit: int | None, ids: list[int] | None = None) -> list[dict]:
    """Tailored jobs without answers yet (the resumable work queue).
    `ids` (from `--jobs`) restricts to those specific job ids.
    Sorted best-score first so a limited run processes the highest-rated jobs."""
    out = [j for j in store.get_jobs(status="tailored")
           if not (j.get("answers_json") or "").strip()]
    if ids:
        keep = set(ids)
        out = [j for j in out if j["id"] in keep]
    out.sort(key=lambda j: -(j["llm_score"] if j.get("llm_score") is not None else (j.get("match_score") or 0)))
    return out[:limit] if limit is not None else out


def _store_answers(job_id: int, answers: dict) -> None:
    store.update_job(job_id, answers_json=json.dumps(answers, ensure_ascii=False),
                     status="ready")


# ── session mode ───────────────────────────────────────────────────────────

def prepare(limit: int | None, master_path: Path, ids: list[int] | None = None) -> int:
    profile = _candidate_profile(master_path)
    details = candidate.load_details()
    facts = candidate.known_facts(details)
    gaps = candidate.screening_gaps(details)
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no tailored jobs awaiting answers. (run resume-tailor first?)")
        return 0
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    payload = [{"job_id": j["id"], "title": j.get("title") or "",
                "company": j.get("company") or "",
                "prompt": _job_prompt(j, _brief_of(j), profile, facts)} for j in jobs]
    PROMPTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"LLM_PROVIDER=session — {len(payload)} job(s) → {PROMPTS_PATH}\n")
    print("SYSTEM INSTRUCTION (apply to every job):")
    print("-" * 72)
    print(SYSTEM_PROMPT)
    print("-" * 72)
    if facts:
        print(f"\nKNOWN candidate_facts (use these, don't invent): "
              f"{', '.join(sorted(facts))}")
    print(f"UNKNOWN → these auto-fill screening_todo for the human gate: "
          f"{', '.join(gaps) or 'none'}")
    print(f"\nWrite a JSON object mapping job_id (string) → answers to:\n  {ANSWERS_PATH}\n"
          f"Then run:  python3 {Path(__file__).relative_to(ROOT)} save\n")
    for item in payload:
        print(f"\n===== job_id {item['job_id']} — {item['title']} @ {item['company']} =====")
        print(item["prompt"])
    return 0


def save(path: Path | None) -> int:
    src = path or ANSWERS_PATH
    if not src.exists():
        print(f"answers file not found: {src}\nRun `prepare`, write answers, then `save`.",
              file=sys.stderr)
        return 1
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"answers file contains invalid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("answers must be a JSON object {job_id: answers}.", file=sys.stderr)
        return 1
    gaps = candidate.screening_gaps(candidate.load_details())
    pending_ids = {j["id"] for j in store.get_jobs(status="tailored")}
    saved = skipped = 0
    for jid, raw in data.items():
        try:
            job_id = int(jid)
        except (ValueError, TypeError):
            print(f"  ✗ bad job_id key {jid!r} — skipped", file=sys.stderr)
            skipped += 1
            continue
        if job_id not in pending_ids:
            print(f"  · job {job_id} not at status 'tailored' — skipped", file=sys.stderr)
            skipped += 1
            continue
        try:
            answers = _normalize_answers(raw, gaps)
        except ValueError as exc:
            print(f"  ✗ job {job_id}: {exc} — skipped", file=sys.stderr)
            skipped += 1
            continue
        _store_answers(job_id, answers)
        saved += 1
        todo = ", ".join(answers["screening_todo"]) or "none"
        print(f"  ✓ job {job_id}: answers stored → ready  (screening to fill: {todo})")
    store.export_json()
    print(f"\nsaved {saved}, skipped {skipped}. {store.stats()}")
    return 0 if saved or not data else 1


# ── api / grok mode ────────────────────────────────────────────────────────

def run(limit: int | None, master_path: Path, ids: list[int] | None = None) -> int:
    if llm.is_session_mode():
        print("LLM_PROVIDER=session — `run` needs an API backend. Use `prepare`/`save`,\n"
              "or set LLM_PROVIDER=api or grok in .env.", file=sys.stderr)
        return 1
    profile = _candidate_profile(master_path)
    details = candidate.load_details()
    facts = candidate.known_facts(details)
    gaps = candidate.screening_gaps(details)
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no tailored jobs awaiting answers.")
        return 0
    print(f"LLM_PROVIDER={llm.provider()} model={llm.model()} — answering {len(jobs)} job(s).\n")
    done = failed = 0
    for j in jobs:
        prompt = _job_prompt(j, _brief_of(j), profile, facts)
        vprint(2, f"\n  [vv] respond prompt ({len(prompt)} chars):\n{prompt[:600]}…")
        try:
            reply = llm.complete(prompt, system=SYSTEM_PROMPT, max_tokens=1800)
            vprint(2, f"  [vv] reply: {reply[:400]}…")
            answers = _normalize_answers(_extract_json(reply), gaps)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"  ✗ job {j['id']}: bad model output — {exc}", file=sys.stderr)
            failed += 1
            continue
        except Exception as exc:  # network/API error: stop cleanly, keep saved work
            print(f"  ✗ job {j['id']}: API error — {exc}", file=sys.stderr)
            failed += 1
            break
        _store_answers(j["id"], answers)
        done += 1
        print(f"  ✓ job {j['id']}: {(j.get('title') or '')[:40]} → ready")
        vprint(1, f"    screening_todo: {answers.get('screening_todo', [])}")
    store.export_json()
    print(f"\nanswered {done}, {failed} failed. {store.stats()}")
    return 0 if failed == 0 else 1


def show() -> int:
    tailored = store.get_jobs(status="tailored")
    ready = store.get_jobs(status="ready")
    print(f"{len(tailored)} tailored (awaiting answers), {len(ready)} ready:\n")
    for j in tailored:
        print(f"  [ ] {(j.get('title') or '')[:42]:<42} @ {(j.get('company') or '')[:22]} ({j['source']})")
    for j in ready:
        todo = ""
        try:
            todo = ", ".join(json.loads(j.get("answers_json") or "{}").get("screening_todo") or [])
        except json.JSONDecodeError:
            pass
        print(f"  [✓] {(j.get('title') or '')[:42]:<42} @ {(j.get('company') or '')[:22]} "
              f"→ ready{f'  (fill: {todo})' if todo else ''}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Draft application answers for tailored jobs")
    ap.add_argument("cmd", nargs="?", default="auto",
                    choices=["auto", "prepare", "save", "run", "show"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--jobs", default=None, help="comma-separated job ids to limit to")
    ap.add_argument("--master", default=str(DEFAULT_MASTER))
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
        return prepare(args.limit, Path(args.master), ids)
    if cmd == "save":
        return save(Path(args.from_path) if args.from_path else None)
    if cmd == "run":
        return run(args.limit, Path(args.master), ids)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
