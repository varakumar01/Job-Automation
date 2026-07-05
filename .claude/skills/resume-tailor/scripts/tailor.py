"""resume-tailor — build a per-job tailored résumé copy from the master. PLAN §5 #4.

LLM-driven but **fabrication-safe**: the model only returns *directives* (a rephrased
Professional Summary using the SAME facts, a reordered tagline drawn from the
candidate's real roles, and a reordering of the EXISTING skill rows). Deterministic
Python splices those directives into a COPY of the master `varakumar_resume.tex` and
compiles it with tectonic. The master `.tex` and its PDF are NEVER modified.

Variant store + reuse (PLAN §9): each distinct (role_profile + keyword) signature
becomes a variant under `tailored/<id>/` (resume.tex + resume.pdf + meta.json). A new
job that is SIMILAR to an existing variant (same role_profile, high keyword overlap)
REUSES it instead of regenerating — so similar postings share one tailored résumé.

Pipeline: reads `matched` jobs that have a `jd_brief` and no `tailored_resume_path`,
writes `tailored_resume_path` (the per-job PDF) and advances `matched → tailored`.

Runs in all three LLM modes (PLAN §9 · execution/llm.py):

- ``session`` (default): ``prepare`` resolves reuse jobs immediately and prints the
  directive prompts for jobs needing a NEW variant (→ .tmp/resume-tailor/prompts.json);
  the orchestrator writes directives to answers.json; ``save`` applies + builds.
- ``api`` / ``grok``: ``run`` loops, calling ``llm.complete`` for each new variant.

Usage::

    python3 .../tailor.py prepare [--limit N]      # session: reuse + emit new-variant prompts
    python3 .../tailor.py save                      # session: apply directives + build PDFs
    python3 .../tailor.py run [--limit N]           # api/grok: one-shot
    python3 .../tailor.py                           # auto: prepare (session) | run (api/grok)
    python3 .../tailor.py show                       # tailored jobs + variant reuse map
    # flags: --master <tex>  --no-build (skip tectonic, link .tex)  --reuse-threshold 0.6
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
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
from execution import llm  # noqa: E402
from execution.log import add_verbose_arg, apply_verbosity, vprint  # noqa: E402

DEFAULT_MASTER = ROOT / "varakumar_resume.tex"
TAILORED_DIR = ROOT / "tailored"
TMP_DIR = ROOT / ".tmp" / "resume-tailor"
PROMPTS_PATH = TMP_DIR / "prompts.json"
ANSWERS_PATH = TMP_DIR / "answers.json"
REUSE_THRESHOLD = 0.6  # Jaccard on keyword signature for "similar enough to reuse"

DIRECTIVE_KEYS = ("tagline_roles", "summary", "skill_order")

SYSTEM_PROMPT = (
    "You tailor a cybersecurity résumé to ONE job by REORDERING and REPHRASING the "
    "candidate's EXISTING material — you must NOT invent skills, tools, employers, or "
    "experience the candidate does not already have.\n"
    "The candidate has ~2 years of experience: present that depth and impact CONFIDENTLY, "
    "but NEVER imply seniority, lead/architect/manager scope, or more years than they have. "
    "If the job wants more years, foreground concrete impact (metrics, ownership), not tenure.\n"
    "Make it ATS-FRIENDLY and PRECISE: where the candidate GENUINELY has the experience, "
    "mirror the exact wording of the job's jd_brief.keywords / must_have in the summary, and "
    "lead with the single real achievement most relevant to THIS job (foreground matching "
    "tools/domains, push the rest back). Tight and concrete — no filler, no buzzword padding, "
    "no seniority inflation. Two hard rules: (1) use ONLY tools/terms that literally appear in "
    "current_summary or skill_labels — do NOT introduce a specific product/platform (e.g. a "
    "named CI/CD tool) that isn't there, even if the JD wants it and it seems implied; "
    "(2) PRESERVE the candidate's strongest quantified achievements (specific numbers/metrics "
    "in current_summary, e.g. '3,200+' plugins, '700%' coverage) — never drop them when "
    "tightening.\n"
    "Return STRICT JSON (no prose, no fences):\n"
    "{\n"
    '  "tagline_roles": ["3-5 role labels for the header, chosen ONLY from '
    'allowed_tagline_roles, ordered to match this job"],\n'
    '  "summary": "the Professional Summary rewritten for this job — SAME facts, numbers, '
    'tools and employers as current_summary, only re-emphasized/reordered and re-worded to '
    'mirror the job\'s real keywords. Plain text, ~3-4 TIGHT sentences, end with a \\"Seeking '
    'opportunities in …\\" sentence ordered for this role.",\n'
    '  "skill_order": ["the skill_labels reordered most-relevant-first; use the EXACT '
    'label strings given; omit none — any you don\'t list are appended in their '
    'original order"]\n'
    "}\n"
    "Only facts present in current_summary/skill_labels may appear. "
    "Return ONLY the JSON — no <think> tags, no markdown fences, no preamble."
)


# ── master parsing / splicing ──────────────────────────────────────────────

_TAGLINE_RE = re.compile(r"(\{\\small\\itshape\s)(.*?)(\}\\\\\[3pt\])")
_SUMMARY_RE = re.compile(
    r"(\\resumeSection\{Professional Summary\}\n)(.*?)(?=\n%\s*──|\n\\resumeSection)",
    re.DOTALL,
)
_TECHROW_RE = re.compile(r"^\\techrow\{([^}]*)\}")
_TAGLINE_SEP = r" \ \textbar\ \ "


def _read_master(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"master résumé not found: {path}")
    return path.read_text(encoding="utf-8")


def _tagline_roles(master: str) -> list[str]:
    m = _TAGLINE_RE.search(master)
    if not m:
        return []
    parts = re.split(r"\\textbar", m.group(2))
    return [re.sub(r"\\.|[{}]", "", p).strip() for p in parts if p.strip()]


def _summary_text(master: str) -> str:
    m = _SUMMARY_RE.search(master)
    if not m:
        return ""
    body = m.group(2).strip()
    body = re.sub(r"\\%", "%", body)          # unescape for a clean plain-text view
    body = re.sub(r"[{}]|\\textbf|\\&", lambda x: "&" if x.group() == r"\&" else "", body)
    return re.sub(r"\s+", " ", body).strip()


def _seeking_roles(summary_text: str) -> list[str]:
    m = re.search(r"Seeking opportunities in (.+?)\.", summary_text)
    if not m:
        return []
    chunk = re.sub(r"\band\b", ",", m.group(1))
    return [r.strip() for r in chunk.split(",") if r.strip()]


def _skill_labels(master: str) -> list[str]:
    return [_TECHROW_RE.match(ln).group(1)
            for ln in master.splitlines() if _TECHROW_RE.match(ln)]


def master_parts(master: str) -> dict:
    summary = _summary_text(master)
    roles = _tagline_roles(master)
    allowed = list(dict.fromkeys(roles + _seeking_roles(summary)))  # ordered-unique
    return {
        "summary": summary,
        "tagline_roles": roles,
        "allowed_tagline_roles": allowed,
        "skill_labels": _skill_labels(master),
    }


_ESCAPE = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
           "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
           "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def _tex_escape(text: str) -> str:
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


def _norm_label(s: str) -> str:
    """Normalize a skill label for tolerant matching (LaTeX `\\&`, case, whitespace)."""
    return re.sub(r"\s+", " ", s.replace("\\&", "&")).strip().lower()


def apply_directives(master: str, directives: dict, parts: dict) -> str:
    """Splice validated directives into a copy of the master. Never fabricates."""
    tex = master

    # 1) Tagline — only roles the candidate actually claims (allowlist), else master's.
    allow = {r.lower(): r for r in parts["allowed_tagline_roles"]}
    roles = [allow[r.lower()] for r in directives.get("tagline_roles", [])
             if isinstance(r, str) and r.lower() in allow]
    roles = list(dict.fromkeys(roles)) or parts["tagline_roles"]
    if roles:
        # Escape each role (the allowlist is plain text) so a future role containing
        # a LaTeX special (e.g. "R&D Security") can't break the build.
        joined = _TAGLINE_SEP.join(_tex_escape(r) for r in roles)
        tex = _TAGLINE_RE.sub(lambda m: m.group(1) + joined + m.group(3), tex, count=1)

    # 2) Summary — rephrased prose, LaTeX-escaped; fall back to master's if blank.
    summary = (directives.get("summary") or "").strip()
    if summary:
        esc = _tex_escape(summary)
        tex = _SUMMARY_RE.sub(lambda m: m.group(1) + "\n" + esc + "\n", tex, count=1)

    # 3) Skill rows — permute existing \techrow lines among their slots (order-safe:
    #    unknown labels ignored, unlisted labels kept in original order, none dropped).
    #    Match tolerantly: the model may return "Detection & Scripting:" for the
    #    LaTeX label "Detection \& Scripting:", or differ in case/whitespace.
    want = [s for s in directives.get("skill_order", []) if isinstance(s, str)]
    lines = tex.split("\n")
    idxs = [i for i, ln in enumerate(lines) if _TECHROW_RE.match(ln)]
    if idxs:
        norm = [_norm_label(_TECHROW_RE.match(lines[i]).group(1)) for i in idxs]
        line_for = {norm[k]: lines[idxs[k]] for k in range(len(idxs))}
        want_norm = list(dict.fromkeys(_norm_label(s) for s in want))
        ordered = [w for w in want_norm if w in line_for] + [n for n in norm if n not in want_norm]
        ordered = list(dict.fromkeys(ordered))
        if len(ordered) == len(idxs):  # only permute if it's a clean 1:1 (else leave as-is)
            for pos, n in zip(idxs, ordered):
                lines[pos] = line_for[n]
            tex = "\n".join(lines)
    return tex


def _normalize_directives(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("directives is not a JSON object")
    out = {}
    out["tagline_roles"] = [str(r).strip() for r in (raw.get("tagline_roles") or [])
                            if str(r).strip()]
    # Collapse internal newlines → spaces: a blank line in the summary would create
    # an unwanted paragraph break inside the Professional Summary block.
    out["summary"] = re.sub(r"\s*\n\s*", " ", str(raw.get("summary") or "")).strip()
    out["skill_order"] = [str(s).strip() for s in (raw.get("skill_order") or [])
                          if str(s).strip()]
    if len(out["summary"]) > 2000:  # sanity bound; a real summary is a short paragraph
        out["summary"] = out["summary"][:2000]
    return out


def _extract_json(text: str) -> dict:
    """First complete JSON object from a model reply; tolerates ```json fences and
    trailing prose (even with braces) via a string-aware brace scan.
    Strips <think>…</think> reasoning traces emitted by some models (e.g. Nemotron)."""
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


# ── variant store + reuse ──────────────────────────────────────────────────

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "general").lower()).strip("-") or "general"


def _signature(brief: dict) -> set[str]:
    sig: set[str] = set()
    for key in ("keywords", "must_have"):
        for v in brief.get(key) or []:
            t = str(v).strip().lower()
            if t:
                sig.add(t)
    return sig


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _load_variants() -> list[dict]:
    variants = []
    if not TAILORED_DIR.exists():
        return variants
    for meta_path in sorted(TAILORED_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["_dir"] = meta_path.parent
            meta["_sig"] = set(meta.get("signature") or [])
            variants.append(meta)
        except (json.JSONDecodeError, OSError):
            continue
    return variants


def _find_reusable(role_profile: str, sig: set[str], variants: list[dict],
                   threshold: float) -> dict | None:
    # best_score starts at `threshold` = the minimum overlap that qualifies for reuse;
    # a candidate must meet it AND beat any better candidate seen so far.
    best, best_score = None, threshold
    for v in variants:
        if (v.get("role_profile") or "") != (role_profile or ""):
            continue
        score = _jaccard(sig, v["_sig"])
        if score >= best_score:
            best, best_score = v, score
    return best


def _build_pdf(variant_dir: Path) -> Path:
    """Compile resume.tex → resume.pdf with tectonic. Raises on failure."""
    tex = variant_dir / "resume.tex"
    proc = subprocess.run(
        ["tectonic", tex.name],
        cwd=str(variant_dir), capture_output=True, text=True, timeout=180,
    )
    pdf = variant_dir / "resume.pdf"
    if proc.returncode != 0 or not pdf.exists():
        tail = (proc.stderr or proc.stdout or "")[-400:]
        raise RuntimeError(f"tectonic build failed for {tex.name}: {tail}")
    return pdf


def _create_variant(job: dict, brief: dict, sig: set[str], directives: dict,
                    master: str, parts: dict, build: bool) -> dict:
    role = job.get("role_profile") or "General"
    sig8 = hashlib.sha256("|".join(sorted(sig)).encode()).hexdigest()[:8]
    variant_id = f"{_slug(role)}-{sig8}"
    vdir = TAILORED_DIR / variant_id
    vdir.mkdir(parents=True, exist_ok=True)

    tailored = apply_directives(master, directives, parts)
    (vdir / "resume.tex").write_text(tailored, encoding="utf-8")

    rel_pdf = f"tailored/{variant_id}/resume.pdf"
    rel_tex = f"tailored/{variant_id}/resume.tex"
    artifact = rel_tex
    if build:
        _build_pdf(vdir)
        artifact = rel_pdf

    meta = {
        "variant_id": variant_id,
        "role_profile": role,
        "signature": sorted(sig),
        "summary": directives.get("summary", ""),
        "tagline_roles": directives.get("tagline_roles", []),
        "jobs": [job["id"]],
        "artifact": artifact,
        "built": build,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (vdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    meta["_dir"], meta["_sig"] = vdir, sig
    return meta


def _attach_job(variant: dict, job_id: int) -> None:
    """Record that job_id reuses this variant (idempotent)."""
    meta_path = variant["_dir"] / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if job_id not in meta["jobs"]:
        meta["jobs"].append(job_id)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ── pipeline ───────────────────────────────────────────────────────────────

def _pending_jobs(limit: int | None, ids: list[int] | None = None) -> list[dict]:
    """Matched jobs with a jd_brief and no tailored résumé yet (the work queue).
    `ids` (from `--jobs`) restricts to those specific job ids.
    Sorted best-score first so a limited run processes the highest-rated jobs."""
    keep = set(ids) if ids else None
    out = []
    for j in store.get_jobs(status="matched"):
        if (j.get("jd_brief") or "").strip() and not (j.get("tailored_resume_path") or "").strip():
            if keep is None or j["id"] in keep:
                out.append(j)
    out.sort(key=lambda j: -(j["llm_score"] if j.get("llm_score") is not None else (j.get("match_score") or 0)))
    return out[:limit] if limit is not None else out


def _brief_of(job: dict) -> dict:
    try:
        b = json.loads(job.get("jd_brief") or "{}")
        return b if isinstance(b, dict) else {}
    except json.JSONDecodeError:
        return {}


def _job_prompt(job: dict, brief: dict, parts: dict) -> str:
    return json.dumps({
        "job_title": job.get("title"),
        "company": job.get("company"),
        "role_profile": job.get("role_profile"),
        "jd_brief": {k: brief.get(k) for k in
                     ("role_summary", "must_have", "nice_to_have", "keywords",
                      "seniority", "fit_notes")},
        "current_summary": parts["summary"],
        "allowed_tagline_roles": parts["allowed_tagline_roles"],
        "skill_labels": parts["skill_labels"],
    }, ensure_ascii=False, indent=2)


def _resolve_reuse(job: dict, variant: dict, build: bool) -> str:
    _attach_job(variant, job["id"])
    artifact = variant.get("artifact") or f"tailored/{variant['variant_id']}/resume.tex"
    store.update_job(job["id"], tailored_resume_path=artifact, status="tailored")
    return artifact


def prepare(limit: int | None, master_path: Path, build: bool, threshold: float,
            ids: list[int] | None = None) -> int:
    master = _read_master(master_path)
    parts = master_parts(master)
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no matched jobs with a brief awaiting tailoring.")
        return 0

    variants = _load_variants()
    reused, new_prompts = 0, []
    for job in jobs:
        brief = _brief_of(job)
        sig = _signature(brief)
        match = _find_reusable(job.get("role_profile") or "", sig, variants, threshold)
        if match:
            art = _resolve_reuse(job, match, build)
            reused += 1
            print(f"  ↻ job {job['id']} reuses variant {match['variant_id']} → {art}")
        else:
            new_prompts.append({"job_id": job["id"], "title": job.get("title") or "",
                                "prompt": _job_prompt(job, brief, parts)})

    if reused:
        store.export_json()
    if not new_prompts:
        print(f"\nall {reused} job(s) reused existing variants — nothing to generate. {store.stats()}")
        return 0

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_PATH.write_text(json.dumps(new_prompts, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(f"\nLLM_PROVIDER=session — {reused} reused, {len(new_prompts)} need a new variant.")
    print(f"Prompts → {PROMPTS_PATH}\n")
    print("SYSTEM INSTRUCTION (apply to every job):")
    print("-" * 72)
    print(SYSTEM_PROMPT)
    print("-" * 72)
    print(f"\nWrite a JSON object mapping job_id (string) → directives to:\n  {ANSWERS_PATH}\n"
          f"Then run:  python3 {Path(__file__).relative_to(ROOT)} save\n")
    for item in new_prompts:
        print(f"\n===== job_id {item['job_id']} — {item['title']} =====")
        print(item["prompt"])
    return 0


def save(path: Path | None, master_path: Path, build: bool, threshold: float) -> int:
    src = path or ANSWERS_PATH
    master = _read_master(master_path)
    parts = master_parts(master)
    answers: dict = {}
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
        answers = {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}

    variants = _load_variants()
    saved = reused = failed = 0
    for job in _pending_jobs(None):  # already filters matched + jd_brief + no tailored path
        brief = _brief_of(job)
        sig = _signature(brief)
        match = _find_reusable(job.get("role_profile") or "", sig, variants, threshold)
        if match:
            art = _resolve_reuse(job, match, build)
            reused += 1
            print(f"  ↻ job {job['id']} reused {match['variant_id']} → {art}")
            continue
        raw = answers.get(str(job["id"]))
        if raw is None:
            print(f"  · job {job['id']}: no directives in answers — skipped", file=sys.stderr)
            continue
        try:
            directives = _normalize_directives(raw)
            variant = _create_variant(job, brief, sig, directives, master, parts, build)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            # OSError covers tectonic missing (FileNotFoundError); keep the batch going.
            print(f"  ✗ job {job['id']}: {exc}", file=sys.stderr)
            failed += 1
            continue
        _resolve_reuse(job, variant, build)
        variants.append(variant)  # later similar jobs in this batch can reuse it
        saved += 1
        print(f"  ✓ job {job['id']}: variant {variant['variant_id']} → {variant['artifact']}")

    store.export_json()
    print(f"\ntailored {saved} new, {reused} reused, {failed} failed. {store.stats()}")
    return 0 if failed == 0 else 1


def run(limit: int | None, master_path: Path, build: bool, threshold: float,
        ids: list[int] | None = None) -> int:
    if llm.is_session_mode():
        print("LLM_PROVIDER=session — `run` needs an API backend. Use `prepare`/`save`,\n"
              "or set LLM_PROVIDER=api or grok in .env.", file=sys.stderr)
        return 1
    master = _read_master(master_path)
    parts = master_parts(master)
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no matched jobs with a brief awaiting tailoring.")
        return 0
    print(f"LLM_PROVIDER={llm.provider()} model={llm.model()} — tailoring {len(jobs)} job(s).\n")
    variants = _load_variants()
    done = reused = failed = 0
    for job in jobs:
        brief = _brief_of(job)
        sig = _signature(brief)
        match = _find_reusable(job.get("role_profile") or "", sig, variants, threshold)
        if match:
            art = _resolve_reuse(job, match, build)
            reused += 1
            print(f"  ↻ job {job['id']} reused {match['variant_id']} → {art}")
            continue
        # LLM call: a network/API error aborts the batch (every later job would hit it).
        prompt = _job_prompt(job, brief, parts)
        vprint(2, f"\n  [vv] tailor prompt ({len(prompt)} chars):\n{prompt[:600]}…")
        try:
            reply = llm.complete(prompt, system=SYSTEM_PROMPT, max_tokens=1500)
            vprint(2, f"  [vv] reply: {reply[:400]}…")
        except Exception as exc:  # network/API error: stop cleanly, keep saved work
            print(f"  ✗ job {job['id']}: API error — {exc}", file=sys.stderr)
            failed += 1
            break
        # Parse + build: a bad reply or a build failure skips just THIS job.
        try:
            directives = _normalize_directives(_extract_json(reply))
            vprint(1, f"    directives: tagline={directives.get('tagline_roles')}  skill_order[0:3]={directives.get('skill_order', [])[:3]}")
            variant = _create_variant(job, brief, sig, directives, master, parts, build)
        except (json.JSONDecodeError, ValueError, RuntimeError, OSError,
                subprocess.TimeoutExpired) as exc:
            print(f"  ✗ job {job['id']}: {exc}", file=sys.stderr)
            failed += 1
            continue
        _resolve_reuse(job, variant, build)
        variants.append(variant)
        done += 1
        print(f"  ✓ job {job['id']}: variant {variant['variant_id']} → {variant['artifact']}")
    store.export_json()
    print(f"\ntailored {done} new, {reused} reused, {failed} failed. {store.stats()}")
    return 0 if failed == 0 else 1


def passthrough(limit: int | None, ids: list[int] | None = None) -> int:
    """Use the master résumé AS-IS (no LLM modification) for 'eligible' jobs: point
    `tailored_resume_path` at the master PDF and advance `matched → tailored`."""
    master_pdf = ROOT / "varakumar_resume.pdf"
    if not master_pdf.exists():
        print("master PDF varakumar_resume.pdf not found — run `make` to build it.",
              file=sys.stderr)
        return 1
    jobs = _pending_jobs(limit, ids)
    if not jobs:
        print("no matched jobs awaiting a résumé.")
        return 0
    for job in jobs:
        store.update_job(job["id"], tailored_resume_path="varakumar_resume.pdf",
                         status="tailored")
        print(f"  = job {job['id']}: master résumé as-is (no modification) → tailored")
    store.export_json()
    print(f"\n{len(jobs)} job(s) set to master résumé. {store.stats()}")
    return 0


def show() -> int:
    variants = _load_variants()
    tailored = store.get_jobs(status="tailored")
    print(f"{len(variants)} variant(s), {len(tailored)} tailored job(s):\n")
    for v in variants:
        print(f"  • {v['variant_id']:<28} [{v.get('role_profile','?'):<22}] "
              f"jobs={v.get('jobs')} built={v.get('built')}")
    for j in tailored:
        print(f"    job {j['id']:>3} {(j.get('title') or '')[:40]:<40} → {j.get('tailored_resume_path')}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tailor the master résumé per matched job")
    ap.add_argument("cmd", nargs="?", default="auto",
                    choices=["auto", "prepare", "save", "run", "show"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--jobs", default=None, help="comma-separated job ids to limit to")
    ap.add_argument("--master", default=str(DEFAULT_MASTER))
    ap.add_argument("--from", dest="from_path", default=None, help="answers file for `save`")
    ap.add_argument("--no-build", action="store_true", help="skip tectonic; link the .tex")
    ap.add_argument("--no-modify", action="store_true",
                    help="use the master résumé as-is (no LLM tailoring) → tailored")
    ap.add_argument("--reuse-threshold", type=float, default=REUSE_THRESHOLD,
                    help="keyword-overlap (Jaccard) to reuse a variant (default 0.6)")
    add_verbose_arg(ap)
    args = ap.parse_args(argv)
    apply_verbosity(args)

    store.init_db()
    build = not args.no_build
    master_path = Path(args.master)
    thr = args.reuse_threshold
    ids = store.parse_ids(args.jobs)

    if args.no_modify:
        return passthrough(args.limit, ids)

    cmd = args.cmd
    if cmd == "auto":
        cmd = "prepare" if llm.is_session_mode() else "run"
    if cmd == "prepare":
        return prepare(args.limit, master_path, build, thr, ids)
    if cmd == "save":
        return save(Path(args.from_path) if args.from_path else None, master_path, build, thr)
    if cmd == "run":
        return run(args.limit, master_path, build, thr, ids)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
